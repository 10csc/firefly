# -*- coding: utf-8 -*-
"""聊天主链（阶段 2.3 自 routes.py 拆出）— 合并窗口 + 流水线入口

- 合并窗口：消息发送即达后端（即时写盘，不丢），后端按 (session, mode, 用户) 5 秒滑动窗口合并；
  主请求挂起等窗口结束再跑流水线，副请求立即返回 {"queued": true}（回复由主请求带回）。
- 端点：/chat、/chat/hint、/chat/flush、/history、/chat-stage、/save-journal。
  （/rest、/undo、/clear-history、/open-mode、/proactive-status、/wake-status、/time
  在阶段 2.8 移至 api/chat_ops.py，经本模块 re-export。）

chat() 本身只是编排壳：_ingest_user_messages（写盘）→ 主动性复位 → 空消息分流 →
_merge_window（窗口）→ _run_pipeline（流水线）。
"""

import logging
import threading
import time
from urllib.parse import urlparse, parse_qs

from modules import app_config as cfg
from modules.app_config import DEFAULT_MODE
from orchestrator import handle_chat
from routes_common import (
    _CONTENT_MAX, _SESSIONS_LOCK, _body_mode, _body_mode_ex,
    _load_image_data_url, _notify_reply_if_background, _query_mode,
    _read_json, _session_key, _write_replies, get_session,
    sessions,
)

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
    # B5（审计 2026-09-15）：手账唯一副本改原子写（memory_manager.update_journal
    # 同款修复）——裸写崩溃即手账全损，用户手改内容无法恢复
    from modules.storage import atomic_write_text
    if not atomic_write_text(fp, content):
        h._json({"ok": False, "error": "写盘失败，手账未保存（请重试）"})
        return
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


def _ingest_user_messages(h, body: dict, mode: str) -> tuple:
    """把本请求携带的用户消息**即时写盘**，返回 (LLM 输入文本, 首轮识图 data URL 列表)。

    支持 text / sticker / image 三种消息对象与旧版纯字符串；表情包缺 path 时按 label 反查，
    图片消息只把 img_id 与 desc 写进 jsonl（图片字节本地持有，不进历史/日志）。
    （阶段 2.3 自 chat() 原样搬出，未改逻辑。）"""
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
                        from domain.stickers.picker import pick_sticker_by_label
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
    return user_input, vision_urls


def _merge_window(session_id: str, mode: str, user_input: str, vision_urls: list) -> tuple:
    """合并窗口：入队本批消息并等窗口结束，返回 (是否主请求, 合并输入, 合并后的图片)。

    非主请求返回 (False, "", []) —— 消息已入队，回复由主请求带回，本请求立即返回。
    （阶段 2.3 自 chat() 原样搬出，未改逻辑。）"""
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
        return False, "", []

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
    return True, user_input, vision_images


def _refresh_memory_head(mode: str, head: str) -> None:
    """自动整理完成后的回调：刷新本进程内所有同 mode 会话的 memory_head。

    为什么需要：`session["memory_head"]` 在会话创建时快照一次，之后只有手动 rest
    才更新。自动整理跑在后台线程里，不改的话当前会话到进程结束前都还在用旧摘要
    （表现：用户觉得"整理完了但她好像没记住"）。
    放这里而不是 auto_rest 里，是因为 modules/ 不应反向依赖 routes/api 层。
    """
    try:
        from routes_common import sessions
        for s in sessions.values():
            if isinstance(s, dict) and s.get("mode") == mode:
                s["memory_head"] = head
    except Exception as e:
        logger.debug("刷新 memory_head 失败（下次进聊天自然生效）: %s", e)


def _run_pipeline(h, session_id: str, mode: str, client, user_input: str, hint: str,
                  vision_images: list | None = None, mode_fell_back: bool = False,
                  notify: bool = True) -> None:
    """跑一轮完整流水线并回包：回复通道锁 → 会话锁 → handle_chat → 逐条写盘 → 响应。

    notify：后台回复完成通知（安卓状态栏）。空消息+hint 的快速路径原本不通知，
    为保持拆分前行为逐字一致，该路径传 notify=False（差异是历史遗留，清理另开卡）。
    vision_images=None 表示**不传该参数**（保持 handle_chat 的默认值语义）。
    （阶段 2.3 把 chat() 里两处几乎重复的调用合并到这里。）"""
    from modules.proactive import reply_lock, reply_unlock
    reply_lock(mode)
    try:
        session = get_session(session_id, mode)
        # 会话级锁：同会话操作串行（chat 耗时长，防 undo/rest 并发读写 ctx）
        with session["lock"]:
            _kw = dict(
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
            if vision_images is not None:
                _kw["vision_images"] = vision_images
            result = handle_chat(user_input, session, client, **_kw)
        # 即时写盘：流萤回复每条立刻记，并把 time 回传给前端
        enriched = _write_replies(result, mode)
        # 自动记忆整理（P1，2026-09-18）：够阈值就在**后台线程**整理，不占用户等待。
        # 放在写盘之后 —— 用户已经拿到回复，整理晚一点完成无所谓。
        # 成本：每达到阈值（默认 10 轮）2 次 LLM 调用（记忆 + 手账），与手动"休息"同价。
        try:
            from modules import auto_rest
            # client 必须在**本请求线程**里取好传进去：服务器版 cfg.get_client() 依赖
            # 线程本地的用户上下文，后台线程里是空的（会取错客户端）。
            auto_rest.maybe_schedule(mode, on_done=_refresh_memory_head, client=client)
        except Exception as e:
            logger.warning("自动整理排程异常（忽略，不影响聊天）: %s", e)
        if notify:
            # 后台回复完成 → 状态栏通知（安卓；PC/服务器版静默跳过）
            _notify_reply_if_background(enriched, mode)
        resp = {"messages": enriched}
        if result.error_code:
            resp["error_code"] = result.error_code   # 错误分类（前端人话提示）
        # R-07（2026-09-10）：客户端请求的 mode 在本端未注册（如 PC 本地版自建角色包 →
        # 服务器版无此包）→ 显式标记，避免用户把"角色变了"当成 bug 而不知原因。
        if mode_fell_back:
            resp["mode_fallback"] = True
            resp["mode_used"] = mode
        h._json(resp)
    finally:
        reply_unlock(mode)


def chat(h):
    """聊天主链（阶段 2.3 拆分后只剩编排）：
    取客户端 → 解析请求 → 用户消息即时写盘（_ingest_user_messages）→ 主动性复位 →
    空消息分流 → 合并窗口（_merge_window）→ 跑流水线（_run_pipeline）。"""
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
    mode, mode_fell_back = _body_mode_ex(body)

    user_input, vision_urls = _ingest_user_messages(h, body, mode)

    # 用户回应 → 主动性信号量复位（用户发送即解锁主动通道，接上响应式回复）
    from modules.proactive import _active_reset
    _active_reset(mode)

    if not user_input:
        # 空消息且无 hint：直接降级话术返回，不走 LLM 流水线（防无输入刷完整推理链）；
        # 有 hint（打字中提示）继续走下方流程——typing 场景的产品功能
        if not hint:
            from orchestrator import _handle_direct
            h._json({"messages": [{"type": "text", "content": m}
                                  for m in _handle_direct("input:empty")]})
            return
        # 有 hint 的空消息：不进窗口，直接降级快速返回（历史行为：不触发后台通知）
        _run_pipeline(h, session_id, mode, client, "", hint, notify=False)
        return

    is_primary, user_input, vision_images = _merge_window(session_id, mode, user_input, vision_urls)
    if not is_primary:
        # 副请求：消息已入队，回复由主请求带回；立即返回，不挂起
        h._json({"queued": True})
        return
    _run_pipeline(h, session_id, mode, client, user_input, hint, vision_images, mode_fell_back)


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


# ── 操作端点（阶段 2.8 抽至 api/chat_ops.py；此处 re-export，router/routes 兼容层不变）──
from api.chat_ops import (   # noqa: F401,E402
    clear_history, get_wake_status, open_mode, proactive_status, rest, undo,
    get_time, _resolve_today,
)

# re-export 面（pyflakes 已使用标记；router/routes 兼容层经此处取全部端点）
__all__ = [
    "save_journal", "chat", "chat_hint", "chat_flush", "get_chat_stage", "get_history",
    "clear_history", "get_wake_status", "open_mode", "proactive_status", "rest", "undo",
    "get_time", "_resolve_today",
]
