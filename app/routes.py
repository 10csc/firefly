# -*- coding: utf-8 -*-
"""API 路由 — 每个端点一个函数，POST_ROUTES / GET_ROUTES 分发

server 拆分产物：server.py 只留 HTTP 骨架（分发/响应工具/启动），
业务路由全部在这里。路由函数签名统一为 fn(h)，h 为 handler 实例，
通过 h._json(...) / h._serve_file(...) 回写响应。
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from modules import app_config as cfg
from modules.context_manager import ContextManager
from modules.multipart import parse_multipart
from orchestrator import handle_chat
from modules.app_config import DEFAULT_MODE

logger = logging.getLogger(__name__)


# ── 会话状态（按 (sid, mode) 隔离）─────────────────
sessions: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_MAX = 30   # 会话上限：防任意 session_id 无限撑内存（超过删除最早创建的）


def _session_key(sid: str, mode: str) -> str:
    return f"{sid}::{mode}"


def get_session(sid: str, mode: str = DEFAULT_MODE) -> dict:
    # ThreadingHTTPServer 下并发首访同一 sid 会重复创建并互相覆盖 context，必须加锁
    key = _session_key(sid, mode)
    with _SESSIONS_LOCK:
        if key not in sessions:
            # 首次创建会话：加载记忆头部（无记忆/中断/异常都降级为空串，不阻塞会话）
            from modules.memory_manager import wake as memory_wake
            from modules.conversation_store import hydrate_context
            from modules.proactive import _restore_active_semaphore
            client = cfg.get_client()
            memory_head = memory_wake(client, cfg.MODEL, mode) if client else ""
            ctx = ContextManager()
            try:
                n = hydrate_context(ctx, mode=mode)
                if n:
                    logger.info("会话 %s[%s] 回灌 %d 轮历史", sid, mode, n)
            except Exception as e:
                logger.warning("历史回灌失败（空上下文启动）: %s", e)
            # 重启兜底：ACTIVE 信号量从 proactive_log 重建（防退出重进刷主动）
            _restore_active_semaphore(mode)
            sessions[key] = {
                "context": ctx,
                "memory_head": memory_head,
                "mode": mode,
                # 会话级锁：chat/rest/undo/clear-history 串行化，防并发读写竞态
                "lock": threading.Lock(),
            }
            # 超限清理（dict 保持插入序 = 创建序，删最早的一个）
            while len(sessions) > _SESSION_MAX:
                oldest = next(k for k in sessions if k != key)
                del sessions[oldest]
        return sessions[key]


# JSON 请求体上限：防异常大 body 吃内存（本地单用户，1MB 足够）
_MAX_BODY = 1_048_576
# 写盘内容上限（手账/用户记忆）：防超大文本占满磁盘（服务器版多用户放大面）
_CONTENT_MAX = 200_000


def _read_json(h) -> dict:
    length = int(h.headers.get("Content-Length", 0))
    if length <= 0:
        return {}
    if length > _MAX_BODY:
        return {}
    try:
        body = json.loads(h.rfile.read(length))
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _body_mode(body: dict) -> str:
    """从请求体取模式，非法回退默认（审查约束）。"""
    m = body.get("mode", DEFAULT_MODE)
    return m if m in cfg.MODES else DEFAULT_MODE


def _query_mode(h) -> str:
    """从 query string 取模式（GET 接口用）。"""
    qs = parse_qs(urlparse(h.path).query)
    m = qs.get("mode", [DEFAULT_MODE])[0]
    return m if m in cfg.MODES else DEFAULT_MODE


# ══ POST 路由 ═══════════════════════════════════

def _is_server() -> bool:
    """服务器平台标记（FIREFLY_SERVER=1，server_app.py 启动时设置）。
    服务器版禁用本地版专属端点（Key 落盘/安装包下载）用。"""
    return bool(os.environ.get("FIREFLY_SERVER"))


def set_key(h):
    # 服务器版：Key 只存用户浏览器（X-API-Key 头），禁写服务器全局配置（防落盘 + 全局串 Key）
    if _is_server():
        h._json({"ok": False, "error": "服务器版请在设置面板填写 Key（仅存本机浏览器）"}, 403)
        return
    body = _read_json(h)
    cfg.config["api_key"] = (body.get("api_key") or "").strip()
    cfg.save_config()
    h._json({"ok": bool(cfg.config["api_key"])})


def set_config(h):
    body = _read_json(h)
    # 服务器版：剥离 api_key 字段（Key 不落服务器全局配置；模型/主动性等全局参数照常）
    new_key = "" if _is_server() else (body.get("api_key") or "").strip()
    for key in ("analyzer_model", "organizer_model", "polisher_model", "retriever_model"):
        val = body.get(key, cfg.config[key])
        if val in cfg.VALID_MODELS:
            cfg.config[key] = val
    for key in ("retriever_effort", "analyzer_effort", "polisher_effort", "organizer_effort"):
        val = body.get(key, cfg.config[key])
        if val in cfg.VALID_EFFORTS:
            cfg.config[key] = val
    try:
        t = float(body.get("retriever_temperature", cfg.config["retriever_temperature"]))
        cfg.config["retriever_temperature"] = max(0.0, min(2.0, t))
    except (TypeError, ValueError):
        pass
    try:
        t = float(body.get("polisher_temperature", cfg.config["polisher_temperature"]))
        cfg.config["polisher_temperature"] = max(0.0, min(2.0, t))
    except (TypeError, ValueError):
        pass
    eff = body.get("polisher_effort", cfg.config["polisher_effort"])
    if eff in cfg.VALID_EFFORTS:
        cfg.config["polisher_effort"] = eff
    # 主动性插件配置（v2：轮次硬约束 + 概率软约束，替代 v1 时间制）
    if "proactive_enabled" in body:
        cfg.config["proactive_enabled"] = bool(body.get("proactive_enabled"))
    if "proactive_hard" in body:
        try:
            ph = int(body.get("proactive_hard", 4))
            cfg.config["proactive_hard"] = max(1, min(10, ph))
        except (TypeError, ValueError):
            pass
    if "proactive_soft" in body:
        try:
            ps = float(body.get("proactive_soft", 0.5))
            cfg.config["proactive_soft"] = max(0.0, min(1.0, ps))
        except (TypeError, ValueError):
            pass
    # 概率式回复配置
    if "prob_reply_enabled" in body:
        cfg.config["prob_reply_enabled"] = bool(body.get("prob_reply_enabled"))
    if "prob_reply_value" in body:
        try:
            pv = float(body.get("prob_reply_value", 0.3))
            cfg.config["prob_reply_value"] = max(0.0, min(1.0, pv))
        except (TypeError, ValueError):
            pass
    # 隐藏式回复配置（独立开关，关前台概率式不影响隐藏式）
    if "hidden_reply_enabled" in body:
        cfg.config["hidden_reply_enabled"] = bool(body.get("hidden_reply_enabled"))
    # 接口地址：仅允许官方 / OpenCode Go 两个已知端点
    if "api_base" in body:
        _base = str(body.get("api_base") or "").strip()
        if _base in (cfg.API_BASE, cfg.GO_BASE):
            cfg.config["api_base"] = _base
    if new_key:
        cfg.config["api_key"] = new_key
    cfg.save_config()
    h._json({
        "ok": bool(cfg.config["api_key"]),
        "api_base": cfg.config.get("api_base", cfg.API_BASE),
        "analyzer_model": cfg.config["analyzer_model"],
        "organizer_model": cfg.config["organizer_model"],
        "polisher_model": cfg.config["polisher_model"],
        "retriever_model": cfg.config["retriever_model"],
        "retriever_effort": cfg.config["retriever_effort"],
        "analyzer_effort": cfg.config["analyzer_effort"],
        "polisher_effort": cfg.config["polisher_effort"],
        "organizer_effort": cfg.config["organizer_effort"],
        "retriever_temperature": cfg.config["retriever_temperature"],
        "polisher_temperature": cfg.config["polisher_temperature"],
        "proactive_enabled": bool(cfg.config.get("proactive_enabled", True)),
        "proactive_hard": cfg.config.get("proactive_hard", 4),
        "proactive_soft": cfg.config.get("proactive_soft", 0.5),
        "prob_reply_enabled": bool(cfg.config.get("prob_reply_enabled", True)),
        "prob_reply_value": cfg.config.get("prob_reply_value", 0.3),
        "hidden_reply_enabled": bool(cfg.config.get("hidden_reply_enabled", True)),
    })


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


def check_key(h):
    # 服务器版：has_key 反映当前用户请求头的 Key（relay 模式 get_client 恒非 None，
    # 不能用它判断）；本地版：等价于原 bool(get_client())
    h._json({"has_key": cfg.user_has_key()})


# ── 聊天合并窗口（发送即达后端 + 后端 5 秒滑动窗口合并）────────
# 前端 send() 消息实时 POST 后端；后端按 session 合并窗口：
#   主请求（该 session 首个到达）挂起等待窗口结束 → 合并全部消息 → 流水线 → 返回回复；
#   副请求（窗口内到达）消息已入队 → 立即返回 {"queued": True}（回复由主请求带回）。
#   打字中（/chat/hint）重置窗口 deadline 继续等；提交窗口到期（/chat/flush）立即结束。
#   前端切后台冻结不发 flush → 窗口 5 秒自然到期兜底处理（消息已实时在后端，不丢）。
# key 含用户作用域：服务器版多用户各自独立窗口。
_CHAT_WINDOW_SEC = 5.0
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


def _write_replies(result, mode: str) -> list:
    """流萤回复写盘并返回 enriched（带 time 回传前端）。"""
    from modules.conversation_store import append_message as _append_msg
    enriched = []
    for m in result.messages:
        record = {"type": m.get("type")}
        if m.get("type") == "text":
            record["content"] = m.get("content", "")
            seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]}, mode=mode)
        elif m.get("type") == "sticker":
            record["path"] = m.get("path", "")
            record["label"] = m.get("label", "")
            seq, t = _append_msg("firefly", {"type": "sticker", "path": record["path"], "label": record["label"]}, mode=mode)
        elif m.get("type") == "narration":
            # 视觉小说式旁白：scene=居中小字（环境/事件），action=居中括号（动作）
            record["text"] = m.get("text", "")
            record["style"] = m.get("style", "action")
            seq, t = _append_msg("firefly", {"type": "narration", "text": record["text"], "style": record["style"]}, mode=mode)
        else:
            record["content"] = str(m)
            seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]}, mode=mode)
        record["time"] = t
        enriched.append(record)
    return enriched


def _notify_reply_if_background(enriched: list):
    """后台回复完成通知（安卓）：App 不在前台则状态栏提醒（复用隐藏式通知通道）。
    PC/服务器版无 com.firefly.android 模块，try/except 静默跳过。"""
    try:
        from com.firefly.android import KeepAliveService
        if not KeepAliveService.isAppForeground():
            texts = [r.get("content", "") for r in enriched if r.get("type") == "text"]
            if texts:
                # 通知标题带 AI 标识（防"半夜收到真人消息"误解；角色扮演合规）
                KeepAliveService.notify("流萤 · AI", "\n".join(texts)[:200])
    except Exception:
        pass


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
    from modules.conversation_store import append_message as _append_msg
    msgs = body.get("messages")
    llm_parts = []
    if isinstance(msgs, list):
        for m in msgs:
            # 统一消息对象类型：{"type":"text","content":...} / {"type":"sticker","label":...}
            if isinstance(m, dict) and m.get("type") == "text" and m.get("content"):
                text = str(m["content"]).strip()
                if text:
                    _append_msg("user", {"type": "text", "content": text}, mode=mode)
                    llm_parts.append(text)
            elif isinstance(m, dict) and m.get("type") == "sticker" and m.get("label"):
                label = m["label"]
                path = m.get("path") or m.get("file") or ""
                if not path:
                    try:
                        from tools.sticker_picker import pick_sticker_by_label
                        entry = pick_sticker_by_label(label)
                        path = entry.file if entry else ""
                    except Exception:
                        path = ""
                if path:
                    _append_msg("user", {"type": "sticker", "label": label, "path": path}, mode=mode)
                llm_parts.append(f"[表情包：{label}]")
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
                                     analyzer_model=cfg.config["analyzer_model"],
                                     organizer_model=cfg.config["organizer_model"],
                                     polisher_model=cfg.config["polisher_model"],
                                     retriever_model=cfg.config["retriever_model"],
                                     retriever_effort=cfg.config["retriever_effort"],
                                     analyzer_effort=cfg.config["analyzer_effort"],
                                     polisher_effort=cfg.config["polisher_effort"],
                                     organizer_effort=cfg.config["organizer_effort"],
                                     retriever_temperature=cfg.config["retriever_temperature"],
                                     polisher_temperature=cfg.config["polisher_temperature"],
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
            win["cond"].notify_all()

    if not is_primary:
        # 副请求：消息已入队，回复由主请求带回；立即返回，不挂起
        h._json({"queued": True})
        return

    # 主请求：等待窗口结束（滑动 deadline；/chat/hint 重置延长，/chat/flush 立即结束）
    with win["cond"]:
        while True:
            remaining = win["deadline"] - time.time()
            if remaining <= 0:
                merged_msgs = list(win["msgs"])
                win["msgs"] = []
                win["active"] = False   # 释放主请求权：后续消息开新一批
                break
            win["cond"].wait(timeout=min(remaining, 1.0))
    user_input = "\n".join(merged_msgs)

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
                analyzer_model=cfg.config["analyzer_model"],
                organizer_model=cfg.config["organizer_model"],
                polisher_model=cfg.config["polisher_model"],
                retriever_model=cfg.config["retriever_model"],
                retriever_effort=cfg.config["retriever_effort"],
                analyzer_effort=cfg.config["analyzer_effort"],
                polisher_effort=cfg.config["polisher_effort"],
                organizer_effort=cfg.config["organizer_effort"],
                retriever_temperature=cfg.config["retriever_temperature"],
                polisher_temperature=cfg.config["polisher_temperature"],
                memory_head=session.get("memory_head", ""),
                hint=hint,
                mode=mode,
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
        result = mm.rest(full_history, session["context"].turn_count)
        # 休息成功后也更新手账
        if result.success:
            mm.update_journal(full_history[-100:])
            from modules.llm_base import reload_journal
            reload_journal(mode)
    h._json({"ok": result.success, "added": len(result.added_entries),
             "resolved": len(result.resolved_entries), "error": result.error})


def add_sticker_route(h):
    # multipart/form-data 解析：保存图片到 user_data/stickers/，写入 registry.json
    from tools.sticker_picker import add_sticker, StickerAddError
    try:
        fields, files = parse_multipart(h)
        category = fields.get("category", "")
        label = fields.get("label", "")
        file_info = files.get("file")
        if not file_info:
            h._json({"ok": False, "error": "缺少图片文件"}); return
        if category not in ("可爱", "帅气"):
            h._json({"ok": False, "error": "分类必须为 可爱/帅气"}); return
        if not label:
            h._json({"ok": False, "error": "缺少含义描述"}); return

        # 保存图片：扩展名白名单（防 .html/.svg 落盘后被静态服务按 MIME 回吐成存储型 XSS），
        # 随机后缀防同秒同名碰撞覆盖
        import secrets
        original = file_info["filename"]
        ext = Path(original).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp/gif 图片格式"}); return
        safe_name = f"user_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        save_dir = cfg.USER_DIR / "stickers"
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / safe_name).write_bytes(file_info["data"])

        # 写入注册表
        entry = add_sticker(f"stickers/{safe_name}", category, label)
        h._json({"ok": True, "sticker_id": entry.id, "label": entry.label})
    except StickerAddError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"上传失败: {e}"})


def sticker_update(h):
    from tools.sticker_picker import update_sticker, StickerUpdateError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    new_label = (body.get("label") or "").strip() or None
    new_category = (body.get("category") or "").strip() or None
    try:
        entry = update_sticker(sid, new_label=new_label, new_category=new_category)
        h._json({"ok": True, "id": entry.id, "label": entry.label, "category": entry.category})
    except StickerUpdateError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"修改失败: {e}"})


def sticker_delete(h):
    from tools.sticker_picker import delete_sticker, StickerDeleteError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    try:
        delete_sticker(sid)
        h._json({"ok": True, "id": sid})
    except StickerDeleteError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"删除失败: {e}"})


def character_file_update(h):
    body = _read_json(h)
    mode = _body_mode(body)
    filename = (body.get("filename") or "").strip()
    content = (body.get("content") or "")
    # 白名单：仅允许用户维护的补充设定（核心设定 core/identity/sms_samples 隐藏且不可经 API 修改）
    allowed = {"用户设定.md"}
    if filename not in allowed:
        h._json({"ok": False, "error": f"不允许的文件: {filename}"}); return
    if not content:
        h._json({"ok": False, "error": "内容不能为空"}); return
    try:
        filepath = cfg.mode_character_dir(mode) / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        # 清除各模块的角色设定缓存
        from modules.llm_base import clear_cache
        clear_cache()
        from modules.polisher import clear_samples_cache
        clear_samples_cache()
        h._json({"ok": True, "filename": filename})
    except Exception as e:
        h._json({"ok": False, "error": f"保存失败: {e}"})


# ── 行为改进（harness P2）：候选批准 / 忽略 / 回滚 ──
# 权限模型：候选由优化器离线生成（pending），这里的端点只做「人批准」与回滚。
# 服务器版按 user context 天然 per-user 隔离；apply 前 registry 会再跑一次静态校验。

def prompt_candidates(h):
    mode = _query_mode(h)
    try:
        from modules.prompt_registry import get_status
        st = get_status(mode)
        h._json({"ok": True, **st})
    except Exception as e:
        h._json({"ok": False, "error": f"读取候选失败: {e}"})


def prompt_apply(h):
    body = _read_json(h)
    mode = _body_mode(body)
    try:
        from modules.prompt_registry import apply
        r = apply(mode)
        h._json(r)
    except Exception as e:
        h._json({"ok": False, "error": f"应用失败: {e}"})


def prompt_dismiss(h):
    body = _read_json(h)
    mode = _body_mode(body)
    try:
        from modules.prompt_registry import dismiss
        r = dismiss(mode)
        h._json(r)
    except Exception as e:
        h._json({"ok": False, "error": f"忽略失败: {e}"})


def prompt_rollback(h):
    body = _read_json(h)
    mode = _body_mode(body)
    try:
        from modules.prompt_registry import rollback
        r = rollback(mode)
        h._json(r)
    except Exception as e:
        h._json({"ok": False, "error": f"回滚失败: {e}"})


def undo(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        result = session["context"].pop_last_turn()
        from modules.conversation_store import remove_last_turn
        removed = remove_last_turn(mode=mode)
    # 以文件为准：重启后内存 context 为空但文件仍有历史，文件删成功就算成功
    if removed > 0 or result is not None:
        h._json({"ok": True, "removed_turn": 1, "files_removed": removed})
    else:
        h._json({"ok": False, "error": "没有可撤回的轮次"})


# ── 反馈采集（harness P1）────────────────────────────
# 定位：失败案例采样器，不是投票计分器。计数不作质量统计，
# 👎 的价值在上下文快照（归因器原料），👍 是风格样本来源。
_FEEDBACK_LABELS = ("人设崩了", "记错了", "重复", "太冷淡", "太黏", "不像她", "其他")
_FEEDBACK_REASON_MAX = 200       # 理由长度上限（字符）
_FEEDBACK_SNAPSHOT_TURNS = 8     # 上下文快照轮数


def feedback(h):
    body = _read_json(h)
    mode = _body_mode(body)
    verdict = body.get("verdict")
    if verdict not in ("like", "dislike"):
        h._json({"ok": False, "error": "verdict 必须是 like 或 dislike"}); return
    label = (body.get("reason_label") or "").strip()
    if label and label not in _FEEDBACK_LABELS:
        h._json({"ok": False, "error": "未知的反馈标签"}); return
    text = (body.get("reason_text") or "").strip()[:_FEEDBACK_REASON_MAX]

    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        turn = int(getattr(session["context"], "turn_count", 0) or 0)
        snapshot = [
            {"role": str(m.get("role", "")), "content": str(m.get("content", ""))[:_FEEDBACK_SNAPSHOT_TURNS * 25]}
            for m in session["context"].get_recent(_FEEDBACK_SNAPSHOT_TURNS)
        ]

    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "turn": turn,
        "verdict": verdict,
        "reason_label": label,
        "reason_text": text,
        "context": snapshot,
    }
    try:
        fp = cfg.mode_data_dir(mode) / "feedback.jsonl"
        fp.parent.mkdir(parents=True, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        h._json({"ok": True})
    except Exception as e:
        h._json({"ok": False, "error": f"反馈保存失败: {e}"})


# ── 换一条（harness P1）────────────────────────────
# 复用 undo 的轮次移除 + 完整流水线重跑：实现简单、复用全部逻辑，
# 且能沉淀 (选中, 落选) 偏好对——比纯文本理由更可信的训练信号。
def reroll(h):
    client = cfg.get_client()
    if not client:
        h._json({"ok": False, "error": "请先设置 API Key", "need_key": True}); return
    if cfg.relay_needs_key():
        h._json({"ok": False, "error": "请先设置 API Key", "need_key": True}); return

    body = _read_json(h)
    session_id = body.get("session_id", "default")
    mode = _body_mode(body)

    from modules.proactive import reply_lock, reply_unlock
    reply_lock(mode)
    try:
        session = get_session(session_id, mode)
        with session["lock"]:
            popped = session["context"].pop_last_turn()
            if popped is None:
                h._json({"ok": False, "error": "没有可重来的回复"}); return
            user_msg, old_reply = popped
            if user_msg == "__proactive__":
                h._json({"ok": False, "error": "主动消息暂不支持换一条（可先撤回）"}); return
            from modules.conversation_store import remove_last_turn
            remove_last_turn(mode=mode)
            result = handle_chat(
                user_msg, session, client,
                analyzer_model=cfg.config["analyzer_model"],
                organizer_model=cfg.config["organizer_model"],
                polisher_model=cfg.config["polisher_model"],
                retriever_model=cfg.config["retriever_model"],
                retriever_effort=cfg.config["retriever_effort"],
                analyzer_effort=cfg.config["analyzer_effort"],
                polisher_effort=cfg.config["polisher_effort"],
                organizer_effort=cfg.config["organizer_effort"],
                retriever_temperature=cfg.config["retriever_temperature"],
                polisher_temperature=cfg.config["polisher_temperature"],
                memory_head=session.get("memory_head", ""),
                hint="",
                mode=mode,
            )
        enriched = _write_replies(result, mode)
        try:
            fp = cfg.mode_data_dir(mode) / "preference.jsonl"
            fp.parent.mkdir(parents=True, exist_ok=True)
            with open(fp, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": mode,
                    "turn": int(getattr(session["context"], "turn_count", 0) or 0),
                    "chosen": [m.get("content", "") for m in enriched if m.get("type") == "text"],
                    "rejected": old_reply,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("偏好对落盘失败: %s", e)
        resp = {"ok": True, "messages": enriched}
        if result.error_code:
            resp["error_code"] = result.error_code
        h._json(resp)
    finally:
        reply_unlock(mode)


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


# ══ GET 路由 ════════════════════════════════════

def _platform_tag() -> str:
    """当前运行平台：pc（本地版 Windows，可退出）/ android（本地版安卓）/ server（服务器版）。"""
    if os.environ.get("FIREFLY_ANDROID"):
        return "android"
    if os.environ.get("FIREFLY_SERVER"):
        return "server"
    return "pc"


def get_chat_stage(h):
    """流水线阶段进度（前端等待回复时轮询）：?sid=&mode= → {"stage": "retriever"|...|null}。
    只读不创建会话；查不到会话返回 null（前端回退默认"对方正在输入…"）。"""
    q = parse_qs(urlparse(h.path).query)
    sid = (q.get("sid") or [""])[0] or "default"
    mode = (q.get("mode") or [DEFAULT_MODE])[0]
    if mode not in cfg.MODES:
        mode = DEFAULT_MODE
    key = _session_key(sid, mode)
    with _SESSIONS_LOCK:
        session = sessions.get(key)
    if not session:
        h._json({"stage": None})
        return
    from orchestrator import get_chat_stage as _get_stage, stage_label
    stage = _get_stage(session)
    h._json({"stage": stage, "label": stage_label(stage) if stage else None})


def export_data(h):
    """导出当前模式数据为 zip 备份（对话/记忆/手账/设定/表情包，不含 API Key）。
    Content-Disposition: attachment 触发浏览器/WebView 下载。只读打包，不修改数据。"""
    q = parse_qs(urlparse(h.path).query)
    mode = (q.get("mode") or [DEFAULT_MODE])[0]
    if mode not in cfg.MODES:
        mode = DEFAULT_MODE
    root = cfg.mode_root(mode)
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(root.rglob("*")):
            if fp.is_file():
                zf.write(fp, fp.relative_to(root).as_posix())
    data = buf.getvalue()
    fname = f"firefly-backup-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f'attachment; filename="{fname}"')
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


# ══ 数据导入 / 账号备份（数据同步：本地导入 + 服务器云端备份）════
# 导出复用 GET /export-data（zip 下载）；导入=multipart zip 覆盖；服务器版另有
# /sync/upload（把导出的 zip 存账号）+ /sync/download（取回）——两端组合即"换机恢复"，
# 不新写解压逻辑（服务器恢复 = /sync/download 拿 zip → 前端再 POST /import-data）。
_IMPORT_MAX_BYTES = 60 * 1024 * 1024          # zip 上传上限（含 multipart 开销）
_IMPORT_MAX_FILE_BYTES = 20 * 1024 * 1024     # 包内单文件解压上限
_IMPORT_MAX_TOTAL_BYTES = 100 * 1024 * 1024   # 包内解压总量上限
_SYNC_KEEP = 3                                # 每模式保留最近备份份数


def _zip_safe_entries(zf) -> list[tuple[str, object]]:
    """zip slip 防御：拒绝绝对路径与 .. 穿越，只收普通文件；返回 [(name, info)]。
    超限抛 ValueError（调用方转人话文案）。"""
    out = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError("压缩包内含非法路径，已拒绝")
        if info.file_size > _IMPORT_MAX_FILE_BYTES:
            raise ValueError("压缩包内单个文件过大，已拒绝")
        total += info.file_size
        if total > _IMPORT_MAX_TOTAL_BYTES:
            raise ValueError("压缩包解压总量过大，已拒绝")
        out.append((name, info))
    return out


def _backup_dir(mode: str) -> Path:
    """账号/本机备份目录：{数据根}/backups/（与模式目录平级，不进导出循环）。"""
    return cfg.mode_root(mode).parent / "backups"


def _backup_current_mode(mode: str, prefix: str) -> None:
    """把当前模式数据打成 zip 存到 backups/（导入前自动备份，防误操作）。空目录跳过。"""
    root = cfg.mode_root(mode)
    if not any(root.rglob("*")):
        return
    import io as _io
    import zipfile as _zipfile
    _backup_dir(mode).mkdir(parents=True, exist_ok=True)
    fp = _backup_dir(mode) / f"{prefix}-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with _zipfile.ZipFile(fp, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(root).as_posix())


def _import_zip_to_mode(data: bytes, mode: str) -> tuple[bool, str, int]:
    """zip 数据覆盖导入到指定模式。返回 (ok, error, 文件数)。"""
    import io as _io
    import zipfile as _zipfile
    import shutil as _sh
    if not data.startswith(b"PK"):
        return False, "不是有效的 zip 备份文件", 0
    try:
        zf = _zipfile.ZipFile(_io.BytesIO(data))
    except Exception:
        return False, "zip 解析失败（文件损坏？）", 0
    try:
        entries = _zip_safe_entries(zf)
    except ValueError as e:
        return False, str(e), 0

    # 覆盖式导入：先自动备份现有数据，再清空目标目录解压
    try:
        _backup_current_mode(mode, "auto")
    except Exception:
        pass    # 备份失败不阻塞导入（导入包本身是用户拿来的数据源）
    root = cfg.mode_root(mode)
    try:
        if root.exists():
            _sh.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        for name, info in entries:
            dst = root / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dst, "wb") as out:
                _sh.copyfileobj(src, out)
    except Exception as e:
        return False, f"写入失败: {e}", 0
    # 清缓存：设定/手账/短信样本的内存缓存必须重载（旧内容会串进新数据）
    try:
        from modules.llm_base import clear_cache, reload_journal
        from modules.polisher import clear_samples_cache
        clear_cache()
        clear_samples_cache()
        reload_journal(mode)
    except Exception:
        pass
    return True, "", len(entries)


def import_data(h):
    """导入 zip 备份（覆盖当前模式数据）。multipart：mode + file。
    服务器版 = 「从账号恢复」的第二跳；本地版 = 文件导入（换机/恢复）。"""
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少 zip 文件"}); return
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    ok, err, n = _import_zip_to_mode(file_info["data"], mode)
    if not ok:
        h._json({"ok": False, "error": err}); return
    h._json({"ok": True, "files": n, "mode": mode})


def sync_upload(h):
    """账号云端备份：上传导出的 zip（服务器版专属；本地版无账号概念）。"""
    if not _is_server():
        h._json({"ok": False, "error": "仅服务器版支持账号备份"}, 403); return
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少 zip 文件"}); return
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    data = file_info["data"]
    if not data.startswith(b"PK"):
        h._json({"ok": False, "error": "不是有效的 zip 备份文件"}); return
    bdir = _backup_dir(mode)
    bdir.mkdir(parents=True, exist_ok=True)
    fp = bdir / f"sync-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    fp.write_bytes(data)
    # 每模式只留最近 _SYNC_KEEP 份
    olds = sorted(bdir.glob(f"sync-{mode}-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in olds[_SYNC_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    h._json({"ok": True, "mode": mode, "saved": fp.name})


def sync_download(h):
    """账号云端备份下载：最新一份 zip（服务器版专属）。"""
    if not _is_server():
        h._json({"ok": False, "error": "仅服务器版支持账号备份"}, 403); return
    mode = _query_mode(h)
    bdir = _backup_dir(mode)
    if not bdir.exists():
        h._json({"error": "账号还没有备份"}, 404); return
    zips = sorted(bdir.glob(f"sync-{mode}-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        h._json({"error": "账号还没有备份"}, 404); return
    data = zips[0].read_bytes()
    fname = zips[0].name
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f'attachment; filename="{fname}"')
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def get_config(h):
    key = cfg.config.get("api_key", "")
    h._json({
        "platform": _platform_tag(),
        "has_key": bool(cfg.get_api_key()),
        "key_prefix": key[:12] + "..." if key else "",
        "api_base": cfg.config.get("api_base", cfg.API_BASE),
        "api_bases": [cfg.API_BASE, cfg.GO_BASE],
        "analyzer_model": cfg.config["analyzer_model"],
        "organizer_model": cfg.config["organizer_model"],
        "polisher_model": cfg.config["polisher_model"],
        "retriever_model": cfg.config["retriever_model"],
        "retriever_effort": cfg.config["retriever_effort"],
        "analyzer_effort": cfg.config["analyzer_effort"],
        "polisher_effort": cfg.config["polisher_effort"],
        "organizer_effort": cfg.config["organizer_effort"],
        "retriever_temperature": cfg.config["retriever_temperature"],
        "polisher_temperature": cfg.config["polisher_temperature"],
        "proactive_enabled": bool(cfg.config.get("proactive_enabled", True)),
        "proactive_hard": cfg.config.get("proactive_hard", 4),
        "proactive_soft": cfg.config.get("proactive_soft", 0.5),
        "prob_reply_enabled": bool(cfg.config.get("prob_reply_enabled", True)),
        "prob_reply_value": cfg.config.get("prob_reply_value", 0.3),
        "hidden_reply_enabled": bool(cfg.config.get("hidden_reply_enabled", True)),
        "valid_models": list(cfg.VALID_MODELS),
        "valid_efforts": list(cfg.VALID_EFFORTS),
    })


def get_metrics(h):
    from modules.metrics import collect
    h._json(collect())


def get_balance(h):
    # 查询 DeepSeek 账户余额（key 来源与 get_client 一致，兼容环境变量）
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{cfg.API_BASE.replace('/v1','')}/user/balance",
            headers={"Authorization": f"Bearer {cfg.get_api_key()}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            h._json(json.loads(r.read()))
    except Exception as e:
        h._json({"error": str(e)})


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


def get_stickers(h):
    # 返回全量表情包列表（供前端管理表展示）。editable：当前用户可改/删的条目
    # （服务器版=自己上传的；本地版=全部；默认项与公共项不可删改）
    from tools.sticker_picker import list_all_stickers, _STICKERS_DEFAULT, editable_ids
    ids = editable_ids()
    editable_all = not cfg.user_scope_key()   # 本地版（无用户上下文）全部可编辑
    h._json({
        "stickers": [
            {"id": s.id, "file": s.file, "category": s.category,
             "label": s.label, "is_default": s.id in _STICKERS_DEFAULT,
             "editable": editable_all or s.id in ids}
            for s in list_all_stickers()
        ],
    })


def get_character_files(h):
    from modules.llm_base import resolve_character_file
    mode = _query_mode(h)
    files = []
    # 核心设定已隐藏，仅暴露用户可维护的补充设定文件
    for fname in ("用户设定.md",):
        fp = resolve_character_file(fname, mode)
        if fp.exists():
            files.append({"name": fname, "content": fp.read_text(encoding="utf-8")})
    h._json({"files": files})


def get_user_memory(h):
    # 用户记忆 = 跨会话记忆文件（休息时自动整理），展示为可编辑
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
    """模式开场演出：haruno 首次进入时返回自动首条消息（旁白+流萤的话）。

    幂等保护：会话已有历史时不再重复开场（重进不重演）。
    """
    body = _read_json(h)
    mode = _body_mode(body)
    if mode == "haruno":
        from modules.conversation_store import get_total_count
        if get_total_count(mode="haruno") == 0:
            from orchestrator import haruno_opening
            msgs = haruno_opening()
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
                enabled=bool(cfg.config.get("proactive_enabled", True)),
                hard=cfg.config.get("proactive_hard", 4),
                soft=cfg.config.get("proactive_soft", 0.5),
                prob_enabled=bool(cfg.config.get("prob_reply_enabled", True)),
                prob_value=cfg.config.get("prob_reply_value", 0.3),
                polisher_model=cfg.config["polisher_model"],
                polisher_effort=cfg.config["polisher_effort"],
                polisher_temperature=cfg.config["polisher_temperature"],
                organizer_model=cfg.config["organizer_model"],
                organizer_effort=cfg.config["organizer_effort"],
                memory_head=session.get("memory_head", ""),
            )
    finally:
        reply_unlock(mode)
    if not result.messages or result.discarded:
        h._json({"messages": [], "reason": result.reason_type})
        return
    h._json({"messages": result.messages, "proactive": True})


# ══ 自动更新 ════════════════════════════════════
# 规范（见 docs/版本更新规范.md）：
# - 检测源双源：GitHub 优先（语义严格），失败降级 Gitee
# - 下载 URL 固定 Gitee 优先（国内用户下载快），GitHub 降级——检测与下载解耦
# - 资产名固定 firefly-setup.exe / firefly.apk（按扩展名匹配，不依赖版本号，跳版本天然兼容）
# - 版本号只认 x.y.z 纯数字；前后端版本对比统一以 APP_VERSION 为权威
_UPDATE_SOURCES = (
    ("https://api.github.com/repos/10csc/firefly/releases/latest",
     "https://github.com/10csc/firefly/releases"),
    ("https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest",
     "https://gitee.com/cpt-asymmetry/firefly/releases"),
)
# 下载源顺序：Gitee 资产优先（国内直连快），GitHub 降级
_DOWNLOAD_SOURCES = (
    "https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest",
    "https://api.github.com/repos/10csc/firefly/releases/latest",
)

# ── 下载加固（轻量：无 sha256 链路，防 URL 投毒与无节制下载）──
_DOWNLOAD_KINDS = ("exe", "apk")
_DOWNLOAD_MAX_BYTES = {"exe": 200 * 1024 * 1024, "apk": 100 * 1024 * 1024}
_DOWNLOAD_MIN_BYTES = 100 * 1024          # 防错误页 HTML 冒充资产
# 域名白名单：Gitee/GitHub 资产域。校验初始 URL 的 host；
# urllib 自动跟随 GitHub 官方重定向（objects.githubusercontent.com），重定向链信任官方域。
_DOWNLOAD_HOSTS = ("gitee.com", "github.com")


def _validate_download_url(url: str, kind: str) -> str:
    """下载 URL 审查：https + 域名白名单 + 扩展名与 kind 一致。返回错误文案（""=通过）。"""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https":
        return "下载地址必须为 https"
    if not any(host == h or host.endswith("." + h) for h in _DOWNLOAD_HOSTS):
        return "下载地址域名不在白名单"
    suffix = ".apk" if kind == "apk" else ".exe"
    if not p.path.lower().endswith(suffix):
        return "下载地址与资产类型不符"
    return ""


def _fetch_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Firefly/" + cfg.APP_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _match_asset(assets, pattern):
    for a in assets or []:
        # 审查：非 dict 资产项直接跳过（防 API 脏数据导致 AttributeError 崩掉检测链）
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        # 匹配 name 或 URL 任一（防资产 name 不带扩展名但 URL 是 .exe/.apk 的漏检）
        if pattern.search(name) or pattern.search(url):
            return url or name
    return ""


def get_latest_release():
    """返回 (tag, html_url)。检测源：GitHub 优先，Gitee 降级。"""
    for api, html in _UPDATE_SOURCES:
        try:
            data = _fetch_json(api)
            tag = str(data.get("tag_name") or "").lstrip("v")
            if tag:
                return tag, html
        except Exception:
            continue
    return None


def _get_asset_url(kind: str) -> str:
    """按下载源顺序找资产 URL（Gitee 优先，GitHub 降级）。"""
    import re
    pat = re.compile(r"\.exe$", re.I) if kind == "exe" else re.compile(r"\.apk$", re.I)
    for api in _DOWNLOAD_SOURCES:
        try:
            data = _fetch_json(api)
            url = _match_asset(data.get("assets"), pat)
            if url:
                return url
        except Exception:
            continue
    return ""


def check_update(h):
    info = get_latest_release()
    if not info:
        h._json({"ok": False, "error": "检查失败（网络或仓库不可达）"})
        return
    tag, html = info
    h._json({
        "ok": True, "tag": tag, "current": cfg.APP_VERSION,
        "html_url": html,
    })


def update_download(h):
    """下载发行版资产到临时目录（轻量加固：kind 白名单 + https/域名校验 + 大小上限）。
    PC(exe)：下载后由后端静默启动安装器（/VERYSILENT 覆盖安装，保留 user_data），
             服务器随之关闭（安装器接管）；安卓(apk)：仅下载，前端引导系统安装器。
    服务器版禁用：检查更新走 version.json；此端点会把资产下载到服务器磁盘/带宽，
    任何登录用户可反复触发（防磁盘填满与 3Mbps 带宽耗尽）。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版请从下载页获取安装包"}, 403)
        return
    body = _read_json(h)
    kind = body.get("kind", "exe")
    if kind not in _DOWNLOAD_KINDS:
        h._json({"ok": False, "error": "不支持的资产类型"})
        return
    url = _get_asset_url(kind)
    if not url:
        h._json({"ok": False, "error": "发行版未附安装包资产或仓库不可达"})
        return
    err = _validate_download_url(url, kind)
    if err:
        h._json({"ok": False, "error": err})
        return
    try:
        import tempfile
        local = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Firefly/" + cfg.APP_VERSION})
            max_size = _DOWNLOAD_MAX_BYTES[kind]
            with urllib.request.urlopen(req, timeout=600) as resp, tempfile.NamedTemporaryFile(
                    suffix=".apk" if kind == "apk" else ".exe", delete=False, dir=tempfile.gettempdir()) as out:
                local = out.name
                # Content-Length 预检 + 流式累计兜底（防无/伪造 Content-Length）
                cl = (getattr(resp, "headers", None) or {}).get("Content-Length")
                if cl:
                    try:
                        if int(cl) > max_size:
                            raise ValueError("文件过大")
                    except (TypeError, ValueError):
                        raise
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError("文件过大")
                    out.write(chunk)
            if total < _DOWNLOAD_MIN_BYTES:
                raise ValueError("文件异常过小，疑似错误页面")
        except Exception:
            # 下载中途失败：清理残留临时文件（防垃圾堆积）
            if local:
                try:
                    Path(local).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        if kind == "exe" and getattr(sys, "frozen", False):
            # PC 发行版：静默启动安装器（覆盖安装保留 user_data），本服务随之退出
            import subprocess
            subprocess.Popen([local, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"])
            # 优雅关闭自身：请求 /shutdown（保存文件后退出），安装器接管
            import threading as _t
            def _close():
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://127.0.0.1:{cfg.PORT}/shutdown", timeout=2)
                except Exception:
                    pass
            _t.Timer(2.0, _close).start()
            h._json({"ok": True, "path": local, "installing": True})
            return
        h._json({"ok": True, "path": local})
    except Exception as e:
        h._json({"ok": False, "error": f"下载失败: {e}"})




# ══ 后端代理（relay 中转）═════════════════════════
# 用户 Key 模式：服务器构建请求体（含资产占位符）→ APP 代发 DeepSeek（用户 Key）→ 回传。
# 服务器不持有用户 Key；资产（知识库/设定）在 APP 本地，占位符由 APP 填充。
def relay_pending(h):
    """APP 轮询：取待代发的 LLM 请求体（可能含 __KNOWLEDGE__ 等资产占位符）。"""
    from modules.api_client import relay_pending as _pending
    item = _pending(cfg.user_scope_key() or "local")
    if item:
        h._json({"pending": True, "call_id": item["call_id"],
                 "payload": item["payload"], "api_base": item["api_base"]})
    else:
        h._json({"pending": False})


def relay_result(h):
    """APP 回传 DeepSeek 响应，唤醒等待中的流水线线程（带 HTTP 状态码做错误分类）。"""
    body = _read_json(h)
    from modules.api_client import relay_result as _result
    try:
        status = int(body.get("status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    ok = _result(cfg.user_scope_key() or "local",
                 body.get("call_id", ""), body.get("response") or {}, status=status)
    h._json({"ok": ok})


def relay_proxy(h):
    """中转降级：APP 直连 api_base 被 CORS 拦截（如 OpenCode Go 端点不支持浏览器
    跨域）时，服务器用本请求的 X-API-Key 代发并回传结果。Key 仅内存即弃不落盘。

    防滥用：call_id 必须匹配该用户队列中真实 pending 项（服务器自己入队的请求），
    否则 404——中转不是开放代理。payload 用前端已填充占位符的版本（资产在 APP 本地，
    服务器只有占位符版本）；payload 只影响用户自己的 Key 调用，无越权面。"""
    body = _read_json(h)
    call_id = str(body.get("call_id") or "")
    user_key = cfg.user_scope_key() or "local"
    from modules.api_client import relay_has, relay_result as _result
    if not relay_has(user_key, call_id):
        h._json({"ok": False, "error": "无此待发请求"}, 404)
        return
    payload = body.get("payload")
    if not isinstance(payload, dict):
        h._json({"ok": False, "error": "payload 缺失"}, 400)
        return
    key = (h.headers.get("X-API-Key", "") or "").strip()
    if not key:
        h._json({"ok": False, "error": "缺少 API Key（请先设置）"}, 400)
        return
    # api_base 从队列项取（服务器入队时校验过白名单），不信任前端传值
    from modules.api_client import _relay_queues, _relay_lock
    with _relay_lock:
        q = _relay_queues.get(user_key) or []
        item = next((i for i in q if i["call_id"] == call_id), None)
        api_base = (item or {}).get("api_base", cfg.API_BASE)
    try:
        import requests as _requests
        resp = _requests.post(api_base.rstrip("/") + "/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=payload, timeout=120)
        try:
            data = resp.json()
        except ValueError:
            data = {"error": {"message": f"中转响应解析失败（HTTP {resp.status_code}）"}}
        # 带真实状态码回传：错误响应（401/402/429/5xx）由服务器转成分类错误唤醒流水线
        _result(user_key, call_id, data, status=resp.status_code)
        h._json({"ok": True, "response": data})
    except Exception as e:
        h._json({"ok": False, "error": f"中转失败: {e}"}, 502)


# ══ 资产清单（服务器告诉 APP 用哪些资产）═══════════
_ASSET_MD5_CACHE: dict[str, str] = {}
_ASSET_MD5_LOCK = threading.Lock()


def _asset_md5(text: str) -> str:
    """资产版本指纹（内容 hash 前 8 位，内容变化即版本变化）。"""
    import hashlib
    with _ASSET_MD5_LOCK:
        key = text[:64]
        if key in _ASSET_MD5_CACHE:
            return _ASSET_MD5_CACHE[key]
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
        _ASSET_MD5_CACHE[key] = digest
        return digest


def assets_index(h):
    """资产清单：服务器"告诉 APP 要用哪些资产"（版本指纹+大小）。
    APP 比对本地版本，缺失/过期则从 /assets/raw 下载。?mode=story|haruno（默认 story）。"""
    from modules.llm_retriever import _load_knowledge, get_knowledge_stats
    from modules.llm_base import resolve_character_file

    mode = _query_mode(h)
    kb = _load_knowledge(mode)
    stats = get_knowledge_stats(mode)

    def _char_asset(name):
        # resolve_character_file 不拼后缀（load_slot 才拼），这里显式拼 .md
        fp = resolve_character_file(name + ".md", mode)
        if fp.exists():
            content = fp.read_text(encoding="utf-8")
            return {"version": _asset_md5(content), "size": len(content)}
        return {"version": "0", "size": 0}

    h._json({
        "mode": mode,
        "knowledge": {"version": _asset_md5(kb), "size": len(kb),
                      "chars": stats.get("chars", 0)},
        "character": {
            "core": _char_asset("core"),
            "identity": _char_asset("identity"),
            "sms_samples": _char_asset("sms_samples"),
        },
    })


def assets_raw(h):
    """资产下载（认证后可用）：APP 首次本地化 / 更新时拉取。
    ?name=knowledge|core|identity|sms_samples&mode=story|haruno"""
    from modules.llm_retriever import _load_knowledge
    from modules.llm_base import resolve_character_file
    qs = parse_qs(urlparse(h.path).query)
    name = qs.get("name", [""])[0]
    mode = (qs.get("mode", [DEFAULT_MODE])[0] or DEFAULT_MODE)
    mode = mode if mode in cfg.MODES else DEFAULT_MODE
    if name == "knowledge":
        h._json({"name": name, "content": _load_knowledge(mode)})
        return
    if name in ("core", "identity", "sms_samples"):
        fp = resolve_character_file(name + ".md", mode)
        if fp.exists():
            h._json({"name": name, "content": fp.read_text(encoding="utf-8")})
            return
        h._json({"error": "资产不存在"}, 404)
        return
    h._json({"error": "未知资产"}, 404)


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
    "/sync/upload": sync_upload,
    "/undo": undo,
    "/clear-history": clear_history,
    "/feedback": feedback,
    "/chat/reroll": reroll,
    "/prompt-apply": prompt_apply,
    "/prompt-dismiss": prompt_dismiss,
    "/prompt-rollback": prompt_rollback,
}

GET_ROUTES = {
    "/check-key": check_key,
    "/config": get_config,
    "/chat-stage": get_chat_stage,
    "/metrics": get_metrics,
    "/balance": get_balance,
    "/requests": get_requests,
    "/pipeline": get_pipeline,
    "/history": get_history,
    "/wake-status": get_wake_status,
    "/stickers": get_stickers,
    "/character-files": get_character_files,
    "/user-memory": get_user_memory,
    "/journal": get_journal,
    "/export-data": export_data,
    "/sync/download": sync_download,
    "/prompt-candidates": prompt_candidates,
    "/assets/index": assets_index,
    "/assets/raw": assets_raw,
}
