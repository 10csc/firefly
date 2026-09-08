# -*- coding: utf-8 -*-
"""API 路由 — 每个端点一个函数，POST_ROUTES / GET_ROUTES 分发

server 拆分产物：server.py 只留 HTTP 骨架（分发/响应工具/启动）。
业务路由按职能拆分：routes_common（共享状态/工具）、routes_auth（A7 认证代理）、
routes_config（配置/供应商）、routes_assets（表情包/角色文件/图片/资产/收藏）、
routes_fix（设定纠错）、routes_data（导出/备份/同步）、routes_update（自动更新）、
routes_relay（relay 中转）。本文件保留聊天主链路（合并窗口/chat/rest/历史/记忆/
主动性等）并在末尾聚合各子模块函数为统一分发表——消费方面不变：
routes.POST_ROUTES.get(path) / routes.GET_ROUTES.get(path)。
路由函数签名统一为 fn(h)，h 为 handler 实例，通过 h._json(...) / h._serve_file(...) 回写响应。
"""

import json
import logging
import threading
import time
from urllib.parse import urlparse, parse_qs

from modules import app_config as cfg
from modules.context_manager import ContextManager
from modules.multipart import parse_multipart  # 兼容面：测试直接改写 routes.parse_multipart（routes_assets 内经 routes 局部绑定取用）
from orchestrator import handle_chat
from modules.app_config import DEFAULT_MODE

# 子模块必须顶部静态 import：PyInstaller 靠静态分析收集依赖（勿改函数内 lazy import）
from routes_common import (
    sessions, _SESSIONS_LOCK, _session_key, get_session,
    _read_json, _body_mode, _query_mode, _is_server, _CONTENT_MAX,
    _write_replies, _notify_reply_if_background, _IMG_ID_RE, _load_image_data_url,
)
from routes_auth import (_AUTH_PROXY_MAP, _auth_server_base, _mk_auth_proxy,
                         auth_proxy, auth_state)
from routes_config import (set_key, set_config, check_key, get_config,
                           get_models, get_balance, get_modes)
from routes_assets import (add_sticker_route, sticker_update, sticker_delete,
                           get_stickers, get_character_files,
                           upload_image, get_image, assets_index, assets_raw,
                           add_favorite_route, get_favorites, delete_favorite_route)
from routes_pack import (character_file_update, delete_character_file,
                         get_pack_files, upload_pack_asset, delete_pack_asset,
                         create_pack, delete_pack)
from routes_fix import (setting_fix_status, setting_fix_message, setting_fix_start,
                        setting_fix_apply, setting_fix_dismiss, setting_fix_rollback,
                        setting_fix_reset)
from routes_data import (export_data, import_data, backup_create, backups_list,
                         backup_restore, backup_delete, sync_manifest, sync_import,
                         sync_export, sync_now)
from routes_update import (check_update, update_download, get_latest_release,
                           _get_asset_url, _fetch_json, _match_asset)
from routes_relay import relay_pending, relay_result, relay_proxy

logger = logging.getLogger(__name__)


def save_journal(h):
    body = _read_json(h)
    mode = _body_mode(body)
    content = body.get("content", "")
    # 审查约束：类型 + 大小上限（防磁盘滥用/非 str 写盘崩 500）
    if not isinstance(content, str):
        h._json({"ok": False, "error": "内容必须为文本"}); return
    if len(content) > _CONTENT_MAX:
        h._json({"ok": False, "error": f"内容过长（上限 {_CONTENT_MAX} 字符）"}); return
    # 路径与 load_journal 同源（llm_base 内按模式公式），避免两处各写一遍公式再次分裂
    from modules.llm_base import reload_journal
    from modules.app_config import mode_journal_dir
    fp = mode_journal_dir(mode) / "手账.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    reload_journal(mode)
    h._json({"ok": True})


# ── 聊天合并窗口（发送即达后端 + 后端 5 秒滑动窗口合并）────────
# 前端 send() 消息实时 POST 后端；后端按 session 合并窗口：
#   主请求（该 session 首个到达）挂起等待窗口结束 → 合并全部消息 → 流水线 → 返回回复；
#   副请求（窗口内到达）消息已入队 → 立即返回 {"queued": True}（回复由主请求带回）。
#   打字中（/chat/hint）重置窗口 deadline 继续等；提交窗口到期（/chat/flush）立即结束。
#   前端切后台冻结不发 flush → 窗口 5 秒自然到期兜底处理（消息已实时在后端，不丢）。
# key 含用户作用域：服务器版多用户各自独立窗口。
_CHAT_WINDOW_SEC = 5.0


_CHAT_WINDOW_MAX_MSGS = 10  # 0.8.1：单批连续消息合并上限（达到即提交，不无限合并）


_CHAT_WINDOW_IDLE = 600.0   # 窗口无活动超时（秒）：防止 session 废弃后窗口残留撑内存


_CHAT_WINDOW_MAX = 100      # 窗口字典硬上限：异常 session_id 可在无后续请求时残留，超限删最空闲的


_CHAT_WINDOW_LOCK = threading.Lock()


_CHAT_WINDOWS: dict[tuple, dict] = {}   # key -> {"msgs": [...], "deadline": float, "cond": Condition}


def _chat_window_key(session_id: str, mode: str) -> tuple:
    from modules.app_config import user_scope_key
    return (session_id, mode, user_scope_key())


def _chat_window_cleanup(now: float):
    """清理长时间无活动的窗口（session 刷新/用户离开后防内存泄漏）。
    ponytail: 超限时 sorted O(n log n)（n≤100+，可接受）；正常路径只依赖 deadline 过期。"""
    stale = [k for k, w in _CHAT_WINDOWS.items() if now - w["deadline"] > _CHAT_WINDOW_IDLE]
    for k in stale:
        _CHAT_WINDOWS.pop(k, None)
    if len(_CHAT_WINDOWS) > _CHAT_WINDOW_MAX:
        oldest = sorted(_CHAT_WINDOWS,
                        key=lambda k: _CHAT_WINDOWS[k]["deadline"])[:len(_CHAT_WINDOWS) - _CHAT_WINDOW_MAX]
        for k in oldest:
            _CHAT_WINDOWS.pop(k, None)


def chat(h):
    client = cfg.get_client()
    if not client:
        h._json({"reply": None, "error": "请先设置 API Key", "need_key": True})
        return
    # 服务器版 relay 模式：用户未带 Key → 立即返回 need_key（前端弹设置引导），
    # 否则流水线每个 LLM 阶段 relay 等待 120s 超时（story 模式约 4 分钟）才报错
    if cfg.relay_needs_key():
        h._json({"reply": None, "error": "请先设置 API Key", "need_key": True})
        return

    body = _read_json(h)
    session_id = body.get("session_id", "default")
    hint = (body.get("hint") or "").strip()
    mode = _body_mode(body)

    # 即时写盘：用户消息一发就记。
    # 前端分条发送（messages 数组）→ 分条写盘（刷新后显示多条），
    # LLM 侧用合并文本（\n 连接，保持一轮处理）。
    # 表情包消息：{"type":"sticker","label":...} → 写盘 sticker 类型 + LLM 提示。
    # 引用消息：{"type":"text","content":...,"quote":{...}} → 引用快照随消息写盘，
    # LLM 侧以 [引用流萤：「…」] 前缀体现（长按消息 → 引用功能）。
    from modules.conversation_store import append_message as _append_msg
    from modules.conversation_store import sanitize_quote as _sanitize_quote
    from modules.conversation_store import compose_user_text as _compose_user_text
    msgs = body.get("messages")
    llm_parts = []
    vision_urls = []   # A9：本批图片的 data URL（本地版 direct 首轮识图用；空=不生效）
    if isinstance(msgs, list):
        for m in msgs:
            # 统一消息对象类型：{"type":"text","content":...} / {"type":"sticker","label":...}
            if isinstance(m, dict) and m.get("type") == "text" and m.get("content"):
                text = str(m["content"]).strip()
                if text:
                    q = _sanitize_quote(m.get("quote"))
                    rec = {"type": "text", "content": text}
                    if q:
                        rec["quote"] = q
                    _append_msg("user", rec, mode=mode)
                    llm_parts.append(_compose_user_text(text, q, mode))
            elif isinstance(m, dict) and m.get("type") == "sticker" and m.get("label"):
                label = m["label"]
                path = m.get("path") or m.get("file") or ""
                q = None
                if not path:
                    try:
                        from tools.sticker_picker import pick_sticker_by_label
                        entry = pick_sticker_by_label(label, mode)
                        path = entry.file if entry else ""
                    except Exception:
                        path = ""
                if path:
                    q = _sanitize_quote(m.get("quote"))
                    rec = {"type": "sticker", "label": label, "path": path}
                    if q:
                        rec["quote"] = q
                    _append_msg("user", rec, mode=mode)
                llm_parts.append(_compose_user_text(f"[表情包：{label}]", q, mode))
            elif isinstance(m, dict) and m.get("type") == "image" and m.get("img_id"):
                # A9：jsonl 只存 img_id + desc（图片字节本地持有，不进任何历史/日志）
                img_id = str(m["img_id"]).strip()[:_CONTENT_MAX]
                desc = str(m.get("desc") or "").strip()[:_CONTENT_MAX]
                q = _sanitize_quote(m.get("quote"))
                rec = {"type": "image", "img_id": img_id}
                if desc:
                    rec["desc"] = desc
                if q:
                    rec["quote"] = q
                _append_msg("user", rec, mode=mode)
                if desc:
                    # 有描述（旧图兼容）按描述理解；无描述默认识图，原图当轮注入
                    llm_parts.append(_compose_user_text(f"[图片：{desc}]", q, mode))
                else:
                    llm_parts.append(_compose_user_text("[图片]", q, mode))
                # 首轮识图：图片已落盘，读字节转 data URL 进 vision_urls（orchestrator 注入回复器）
                if len(vision_urls) < 4:
                    url = _load_image_data_url(mode, img_id)
                    if url:
                        vision_urls.append(url)
            elif isinstance(m, str) and m.strip():
                # 兼容旧格式（纯字符串）
                _append_msg("user", {"type": "text", "content": m.strip()}, mode=mode)
                llm_parts.append(m.strip())
    if llm_parts:
        user_input = "\n".join(llm_parts)
    else:
        user_input = (body.get("message") or "").strip()
        if user_input:
            _append_msg("user", {"type": "text", "content": user_input}, mode=mode)

    # 用户回应 → 主动性信号量复位（用户发送即解锁主动通道，接上响应式回复）
    from modules.proactive import _active_reset
    _active_reset(mode)

    # 合并窗口入队：消息实时到达后端即安全；窗口按 (session, mode, 用户) 隔离
    if not user_input:
        # 空消息且无 hint：直接降级话术返回，不走 LLM 流水线（防无输入刷完整推理链）；
        # 有 hint（打字中提示）继续走下方流程——typing 场景的产品功能
        if not hint:
            from orchestrator import _handle_direct
            h._json({"messages": [{"type": "text", "content": m}
                                  for m in _handle_direct("input:empty")]})
            return
        # 有 hint 的空消息：不进窗口，直接降级快速返回
        from modules.proactive import reply_lock, reply_unlock
        reply_lock(mode)
        try:
            session = get_session(session_id, mode)
            with session["lock"]:
                result = handle_chat("", session, client,
                                     analyzer_model=cfg.eff_cfg("analyzer_model"),
                                     organizer_model=cfg.eff_cfg("organizer_model"),
                                     polisher_model=cfg.eff_cfg("polisher_model"),
                                     retriever_model=cfg.eff_cfg("retriever_model"),
                                     retriever_effort=cfg.eff_cfg("retriever_effort"),
                                     analyzer_effort=cfg.eff_cfg("analyzer_effort"),
                                     polisher_effort=cfg.eff_cfg("polisher_effort"),
                                     organizer_effort=cfg.eff_cfg("organizer_effort"),
                                     retriever_temperature=cfg.eff_cfg("retriever_temperature"),
                                     polisher_temperature=cfg.eff_cfg("polisher_temperature"),
                                     memory_head=session.get("memory_head", ""),
                                     hint=hint,
                                     mode=mode,
                                     )
            enriched = _write_replies(result, mode)
            resp = {"messages": enriched}
            if result.error_code:
                resp["error_code"] = result.error_code
            h._json(resp)
        finally:
            reply_unlock(mode)
        return

    key = _chat_window_key(session_id, mode)
    with _CHAT_WINDOW_LOCK:
        _chat_window_cleanup(time.time())
        win = _CHAT_WINDOWS.get(key)
        if win is None:
            win = {"msgs": [], "deadline": 0.0, "active": False,
                   "cond": threading.Condition()}
            _CHAT_WINDOWS[key] = win
        with win["cond"]:
            if not win["active"]:
                win["active"] = True          # 本请求成为主请求（窗口首个/上一批已结束）
                is_primary = True
            else:
                is_primary = False
            win["msgs"].append(user_input)          # 入队（消息已写盘，不会丢）
            win["deadline"] = time.time() + _CHAT_WINDOW_SEC   # 新消息重置 5 秒窗口
            if vision_urls:
                win["vision"] = list(win.get("vision") or [])
                win["vision"].extend(vision_urls[:4 - len(win.get("vision") or [])])
            win["cond"].notify_all()

    if not is_primary:
        # 副请求：消息已入队，回复由主请求带回；立即返回，不挂起
        h._json({"queued": True})
        return

    # 主请求：等待窗口结束（滑动 deadline；/chat/hint 重置延长，/chat/flush 立即结束；
    # /chat 自身达到 _CHAT_WINDOW_MAX_MSGS 上限也立即结束——0.8.1 连续消息最多合并 10 条）
    with win["cond"]:
        while True:
            remaining = win["deadline"] - time.time()
            if remaining <= 0 or len(win["msgs"]) >= _CHAT_WINDOW_MAX_MSGS:
                merged_msgs = list(win["msgs"])
                win["msgs"] = []
                vision_merge = list(win.get("vision") or [])
                win["vision"] = []
                win["active"] = False   # 释放主请求权：后续消息开新一批
                break
            win["cond"].wait(timeout=min(remaining, 1.0))
    user_input = "\n".join(merged_msgs)
    vision_images = vision_merge  # A9：本批首轮识图 base64 列表（orchestrator 按 caps.vision 决定）

    # 回复通道锁（阻塞）：用户消息不可丢，等待本模式任何主动生成完成后再处理
    # （按模式分锁：不阻塞其他模式的回复通道）
    from modules.proactive import reply_lock, reply_unlock
    reply_lock(mode)
    try:
        session = get_session(session_id, mode)
        # 会话级锁：同会话操作串行（chat 耗时长，防 undo/rest 并发读写 ctx）
        with session["lock"]:
            result = handle_chat(
                user_input, session, client,
                analyzer_model=cfg.eff_cfg("analyzer_model"),
                organizer_model=cfg.eff_cfg("organizer_model"),
                polisher_model=cfg.eff_cfg("polisher_model"),
                retriever_model=cfg.eff_cfg("retriever_model"),
                retriever_effort=cfg.eff_cfg("retriever_effort"),
                analyzer_effort=cfg.eff_cfg("analyzer_effort"),
                polisher_effort=cfg.eff_cfg("polisher_effort"),
                organizer_effort=cfg.eff_cfg("organizer_effort"),
                retriever_temperature=cfg.eff_cfg("retriever_temperature"),
                polisher_temperature=cfg.eff_cfg("polisher_temperature"),
                memory_head=session.get("memory_head", ""),
                hint=hint,
                mode=mode,
                vision_images=vision_images,
            )
        # 即时写盘：流萤回复每条立刻记，并把 time 回传给前端
        enriched = _write_replies(result, mode)
        # 后台回复完成 → 状态栏通知（安卓；PC/服务器版静默跳过）
        _notify_reply_if_background(enriched)
        resp = {"messages": enriched}
        if result.error_code:
            resp["error_code"] = result.error_code   # 错误分类（前端人话提示）
        h._json(resp)
    finally:
        reply_unlock(mode)


def chat_hint(h):
    """用户打字中：重置该 session 合并窗口 deadline（窗口不存在则无操作）。
    语义：输入框有内容（用户在打下一条）→ 流萤继续等，不提前提交。"""
    body = _read_json(h)
    session_id = body.get("session_id", "default")
    mode = _body_mode(body)
    key = _chat_window_key(session_id, mode)
    with _CHAT_WINDOW_LOCK:
        _chat_window_cleanup(time.time())
        win = _CHAT_WINDOWS.get(key)
        if win is not None:
            with win["cond"]:
                win["deadline"] = time.time() + _CHAT_WINDOW_SEC
                win["cond"].notify_all()
    h._json({"ok": True})


def chat_flush(h):
    """前端提交窗口到期：立即结束该 session 合并窗口（主请求从等待中醒来处理）。
    切后台前端冻结不发 flush → 窗口 5 秒自然到期兜底，消息不丢。"""
    body = _read_json(h)
    session_id = body.get("session_id", "default")
    mode = _body_mode(body)
    key = _chat_window_key(session_id, mode)
    with _CHAT_WINDOW_LOCK:
        _chat_window_cleanup(time.time())
        win = _CHAT_WINDOWS.get(key)
        if win is not None:
            with win["cond"]:
                win["deadline"] = time.time()   # 立即到期
                win["cond"].notify_all()
    h._json({"ok": True})


def rest(h):
    client = cfg.get_client()
    if not client:
        h._json({"ok": False, "error": "未设置 API Key"})
        return
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        from modules.memory_manager import MemoryManager
        mm = MemoryManager(client, cfg.MODEL, mode=mode)
        full_history = session["context"].get_full()
        result = mm.rest(full_history, session["context"].turn_count,
                         today=_resolve_today())
        # 休息成功后也更新手账
        if result.success:
            mm.update_journal(full_history[-100:])
            from modules.llm_base import reload_journal
            reload_journal(mode)
            # 立即刷新当前会话的 memory_head：新头部随下一条消息生效，
            # 不再等进程重启 / 30 会话淘汰（真 bug 修复）
            session["memory_head"] = mm.load_head()
    # skipped：无新对话可整理（LLM 未运行，added/resolved 均为 0——前端显示"没有新内容"而非"记忆已更新"）
    skipped = bool(result.error) and result.success
    h._json({"ok": result.success, "added": len(result.added_entries),
             "resolved": len(result.resolved_entries), "error": result.error,
             "skipped": skipped,
             # 0.8.1：文件级结果（弹窗文案用真实变化，LLM 的 added/resolved 数组可能为空
             # 但头部/手账仍更新了——只报"新增0条"会误导用户）
             "head_changed": bool(result.new_head.strip()),
             "journal_updated": True})


def get_time(h):
    """GET /time：服务器时钟（YYYY-MM-DD + 时间戳）。
    记忆整理等需要"今天"的场合用它做权威时钟（本地版经后端转发到认证服务器；
    _resolve_today：服务器优先、不可达时本地时钟兜底）。"""
    h._json({"ok": True, "date": time.strftime("%Y-%m-%d"),
             "ts": int(time.time())})


def _resolve_today() -> str:
    """rest 等场景的"今天"：服务器时间优先，本地设备时钟兜底。
    - 服务器版：进程就在业务服务器上，取本机时钟即服务器时间（无网络往返）；
    - 本地版：请求认证服务器 /time（0.8s 超时），成功用服务器日期，失败用设备时钟
      （离线宽限内 rest 仍可用）。"""
    if _is_server():
        return time.strftime("%Y-%m-%d")
    import urllib.request
    try:
        req = urllib.request.Request(_auth_server_base() + "/time")
        with urllib.request.urlopen(req, timeout=0.8) as r:
            data = json.loads(r.read().decode("utf-8"))
        d = str(data.get("date", "") or "").strip()
        import re as _re
        if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            return d
    except Exception:
        pass
    return time.strftime("%Y-%m-%d")


def undo(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        result = session["context"].pop_last_turn()
        from modules.conversation_store import remove_last_turn
        removed = remove_last_turn(mode=mode)
        # 撤回后回退 .memory_index：若整合游标 > 当前轮数（说明被撤回轮次
        # 已被记过数），必须压低游标，否则后续 rest 按 turn 号切片会把新的
        # 对话全部误判为"已整理过"而永远跳过（真 bug 修复）
        try:
            from modules.memory_manager import _index_file
            from modules.conversation_store import count_user_turns
            fp = _index_file(mode)
            if fp.exists():
                idx = json.loads(fp.read_text(encoding="utf-8"))
                last_turn = int(idx.get("last_integrated_turn", 0))
                # 以文件为准：重启后内存 context 只回灌近期轮次，
                # session["context"].turn_count 小于真实进度会把游标误压低；
                # 注意必须用用户轮数（与游标同口径），不可用消息总行数
                cur_turn = count_user_turns(mode=mode)
                if last_turn > cur_turn:
                    idx["last_integrated_turn"] = cur_turn
                    fp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("撤回后记忆游标回退失败: %s", e)
    # 以文件为准：重启后内存 context 为空但文件仍有历史，文件删成功就算成功
    if removed > 0 or result is not None:
        h._json({"ok": True, "removed_turn": 1, "files_removed": removed})
    else:
        h._json({"ok": False, "error": "没有可撤回的轮次"})


def clear_history(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        session["context"] = ContextManager()
        # 清空持久化文件
        from modules.conversation_store import conv_file
        try:
            fp = conv_file(mode)
            if fp.exists():
                fp.write_text("", encoding="utf-8")
        except Exception:
            pass
        # 记忆整理进度必须同步归零：turn_count 已归零，旧 index 会让下次
        # 休息时把新对话全部误判为"已整理过"而跳过
        try:
            from modules.memory_manager import _index_file
            fp = _index_file(mode)
            if fp.exists():
                fp.write_text(
                    json.dumps({"last_integrated_turn": 0}, ensure_ascii=False),
                    encoding="utf-8")
        except Exception:
            pass
        # 会话聊天产生的数据全部随历史清理（除配置/已写进设定文件的）：
        # proactive_log（主动判断记录）、pipeline（流水线日志）、
        # 内存信号量（ACTIVE 复位）+ 忽视计数清零
        try:
            from modules.proactive import _log_file, _active_set, _IGNORED, _HIDDEN
            fp = _log_file(mode)
            if fp.exists():
                fp.unlink()
            _active_set(mode, 1)
            _IGNORED.pop(mode, None)
            _HIDDEN.pop(mode, None)   # 隐藏式冷却随历史清理重置
        except Exception:
            pass
        try:
            from modules.app_config import mode_data_dir
            fp = mode_data_dir(mode) / "pipeline.jsonl"
            if fp.exists():
                fp.unlink()
        except Exception:
            pass
    h._json({"ok": True})


def get_chat_stage(h):
    """流水线阶段进度（前端等待回复时轮询）：?sid=&mode= → {"stage": "retriever"|...|null, "waited": 秒}。
    只读不创建会话；查不到会话返回 null（前端回退默认"对方正在输入…"）。
    waited：当前阶段已等待秒数，供前端在上游卡住时显示超时预警。"""
    q = parse_qs(urlparse(h.path).query)
    sid = (q.get("sid") or [""])[0] or "default"
    mode = (q.get("mode") or [DEFAULT_MODE])[0]
    if mode not in cfg.MODES:
        mode = DEFAULT_MODE
    key = _session_key(sid, mode)
    with _SESSIONS_LOCK:
        session = sessions.get(key)
    if not session:
        h._json({"stage": None, "waited": 0})
        return
    from orchestrator import get_chat_stage as _get_stage, stage_label, get_stage_waited
    stage = _get_stage(session)
    h._json({"stage": stage, "label": stage_label(stage) if stage else None,
             "waited": round(get_stage_waited(session)) if stage else 0})


def get_metrics(h):
    from modules.metrics import collect
    h._json(collect())


def get_requests(h):
    from modules.llm_base import get_request_log
    log = get_request_log(200)
    h._json({"requests": log, "count": len(log)})


def get_pipeline(h):
    # 每轮对话各阶段的输入/输出/思考过程（调试答非所问用）
    from orchestrator import get_pipeline_log
    log = get_pipeline_log(20, mode=_query_mode(h))
    h._json({"pipeline": log, "count": len(log)})


def get_history(h):
    # 分页加载历史：?limit=150&before_seq=N&mode=story
    from modules.conversation_store import load_recent, get_total_count, get_min_seq
    mode = _query_mode(h)
    qs = parse_qs(urlparse(h.path).query)
    try:
        limit = int(qs.get("limit", ["150"])[0])
    except Exception:
        limit = 150
    # 审查约束：钳制到 [1, 500]（防 limit=10**9 全量读 jsonl 进内存放大）
    limit = max(1, min(limit, 500))
    before_seq_raw = qs.get("before_seq", [None])[0]
    # 审查约束：非法 before_seq 回退 None（防 /history?before_seq=abc 崩 500；同函数 limit 已有 try）
    try:
        before_seq = int(before_seq_raw) if before_seq_raw else None
    except (TypeError, ValueError):
        before_seq = None
    msgs = load_recent(limit=limit, before_seq=before_seq, mode=mode)
    total = get_total_count(mode=mode)
    # has_more：当前页最小 seq > 全局最小 seq 时还有更早历史
    cur_min = int(msgs[0]["seq"]) if msgs else 0
    has_more = cur_min > get_min_seq(mode=mode) if total > 0 else False
    h._json({"messages": msgs, "total": total, "has_more": has_more})


def get_wake_status(h):
    from modules.memory_manager import _index_file, _memory_file
    mode = _query_mode(h)
    interrupted = _index_file(mode).exists() and not _memory_file(mode).exists()
    h._json({"interrupted": interrupted, "has_memory": _memory_file(mode).exists()})


def get_user_memory(h):
    # 用户记忆 = memory.md（休息时自动整理的过往摘要），展示为可编辑
    from modules.memory_manager import _memory_file
    mode = _query_mode(h)
    fp = _memory_file(mode)
    content = fp.read_text(encoding="utf-8") if fp.exists() else ""
    h._json({"content": content})


def save_user_memory(h):
    from modules.memory_manager import _memory_file
    body = _read_json(h)
    mode = _body_mode(body)
    content = (body.get("content") or "")
    # 审查约束：类型 + 大小上限（与 save_journal 同规则）
    if not isinstance(content, str):
        h._json({"ok": False, "error": "内容必须为文本"}); return
    if len(content) > _CONTENT_MAX:
        h._json({"ok": False, "error": f"内容过长（上限 {_CONTENT_MAX} 字符）"}); return
    try:
        fp = _memory_file(mode)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        h._json({"ok": True})
    except Exception as e:
        h._json({"ok": False, "error": f"保存失败: {e}"})


def get_journal(h):
    from modules.llm_base import load_journal
    h._json({"content": load_journal(_query_mode(h))})


def open_mode(h):
    """模式开场演出：包内存在 opening.json 且首次进入时返回自动首条消息（旁白+角色的话）。

    幂等保护：会话已有历史时不再重复开场（重进不重演）。
    """
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.llm_base import resolve_character_file
    if resolve_character_file("opening.json", mode).exists():
        from modules.conversation_store import get_total_count
        if get_total_count(mode=mode) == 0:
            from orchestrator import preset_opening
            msgs = preset_opening(mode)
            h._json({"messages": msgs, "opened": True})
            return
    h._json({"messages": [], "opened": False})


def proactive_status(h):
    """主动性检查入口：REPLY 预占用 → 主动式/概率式串联判断 → 生成 → 写盘。

    前端轮询调用（空闲时）；每次调用都是独立判断，门控不通过则零成本返回
    {"messages": []}。生成的主动消息直接写盘，返回 messages 供前端即时渲染
    （与 /chat 返回格式一致）。

    信号量：REPLY 非阻塞预占用（忙碌则放弃）；ACTIVE 在 proactive 模块内
    管理（主动式/概率式互斥 + 用户回应复位 + 超时恢复）。
    """
    client = cfg.get_client()
    if not client or cfg.relay_needs_key():
        # 服务器版 relay 模式用户未带 Key：零成本返回，避免轮询线程空等 120s relay 超时
        h._json({"messages": []}); return
    body = _read_json(h)
    mode = _body_mode(body)
    session_id = body.get("session_id", "default")

    from modules.proactive import check_and_generate, reply_try_lock, reply_unlock
    if not reply_try_lock(mode):
        h._json({"messages": []}); return   # 回复通道忙（响应式生成中/其他主动生成中）
    try:
        session = get_session(session_id, mode)
        with session["lock"]:
            result = check_and_generate(
                session, client, mode=mode,
                enabled=bool(cfg.eff_cfg("proactive_enabled", True)),
                hard=cfg.eff_cfg("proactive_hard", 6),
                soft=cfg.eff_cfg("proactive_soft", 0.35),
                prob_enabled=bool(cfg.eff_cfg("prob_reply_enabled", True)),
                prob_value=cfg.eff_cfg("prob_reply_value", 0.10),
                polisher_model=cfg.eff_cfg("polisher_model"),
                polisher_effort=cfg.eff_cfg("polisher_effort"),
                polisher_temperature=cfg.eff_cfg("polisher_temperature"),
                organizer_model=cfg.eff_cfg("organizer_model"),
                organizer_effort=cfg.eff_cfg("organizer_effort"),
                memory_head=session.get("memory_head", ""),
            )
    finally:
        reply_unlock(mode)
    if not result.messages or result.discarded:
        h._json({"messages": [], "reason": result.reason_type})
        return
    h._json({"messages": result.messages, "proactive": True})


# ── 分发表 ───────────────────────────────────────
POST_ROUTES = {
    "/set-key": set_key,
    "/set-config": set_config,
    "/save-journal": save_journal,
    "/save-user-memory": save_user_memory,
    "/check-key": check_key,
    "/chat": chat,
    "/chat/hint": chat_hint,
    "/chat/flush": chat_flush,
    "/open-mode": open_mode,
    "/proactive-status": proactive_status,
    "/rest": rest,
    "/add-sticker": add_sticker_route,
    "/sticker-update": sticker_update,
    "/sticker-delete": sticker_delete,
    "/character-file-update": character_file_update,
    "/check-update": check_update,
    "/update-download": update_download,
    "/relay/pending": relay_pending,
    "/relay/result": relay_result,
    "/relay/proxy": relay_proxy,
    "/import-data": import_data,
    "/backup/create": backup_create,
    "/backup/restore": backup_restore,
    "/backup/delete": backup_delete,
    "/sync/import": sync_import,
    "/sync/export": sync_export,
    "/sync/now": sync_now,
    "/undo": undo,
    "/clear-history": clear_history,
    "/setting-fix/message": setting_fix_message,
    "/setting-fix/start": setting_fix_start,
    "/setting-fix/apply": setting_fix_apply,
    "/setting-fix/dismiss": setting_fix_dismiss,
    "/setting-fix/rollback": setting_fix_rollback,
    "/setting-fix/reset": setting_fix_reset,
    "/favorite": add_favorite_route,
    "/favorites/delete": delete_favorite_route,
    "/upload-image": upload_image,
    "/pack-asset": upload_pack_asset,
    "/pack-asset/delete": delete_pack_asset,
    "/character-file/delete": delete_character_file,
    "/pack-create": create_pack,
    "/pack-delete": delete_pack,
}


# A7：本地版认证代理端点（账号体系在服务器；本地模式前端同源调用）
for _a_path, _a_ep in _AUTH_PROXY_MAP.items():
    POST_ROUTES[_a_path] = _mk_auth_proxy(_a_ep)


GET_ROUTES = {
    "/check-key": check_key,
    "/config": get_config,
    "/models": get_models,
    "/modes": get_modes,
    "/sync/manifest": sync_manifest,
    "/image": get_image,
    "/time": get_time,
    "/auth/state": auth_state,
    "/chat-stage": get_chat_stage,
    "/metrics": get_metrics,
    "/balance": get_balance,
    "/requests": get_requests,
    "/pipeline": get_pipeline,
    "/history": get_history,
    "/wake-status": get_wake_status,
    "/stickers": get_stickers,
    "/character-files": get_character_files,
    "/pack-files": get_pack_files,
    "/user-memory": get_user_memory,
    "/journal": get_journal,
    "/export-data": export_data,
    "/backups": backups_list,
    "/setting-fix/status": setting_fix_status,
    "/assets/index": assets_index,
    "/assets/raw": assets_raw,
    "/favorites": get_favorites,
}
