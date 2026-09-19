# -*- coding: utf-8 -*-
"""调试/只读面板数据（阶段 2.3 自 routes.py 拆出）

/metrics（进程级计数器，仅本地版）、/requests（最近请求）、/pipeline（流水线日志，按用户隔离）、
/user-memory 与 /journal 读写。这些端点不参与聊天主链，出问题也不该拖垮对话。
"""


from routes_common import _CONTENT_MAX, _body_mode, _is_server, _query_mode, _read_json


def get_metrics(h):
    # 2026-09-10 修复：metrics 是进程级全局计数器（token/费用/各模块调用数），
    # 服务器版下任何登录账号都能看到**全站**用量与运营成本 → 仅本地版可用。
    if _is_server():
        h._json({"error": "服务器版不提供全站指标（多用户共享计数器）"}, 403); return
    from modules.metrics import collect
    h._json(collect())


def get_requests(h):
    # 2026-09-10 修复：_REQUEST_LOG 是进程级全局环形缓冲（module/model/token/费用），
    # 服务器版下任一账号可见**全站**请求元数据 → 仅本地版可用。
    if _is_server():
        h._json({"error": "服务器版不提供全站请求日志（多用户共享缓冲）"}, 403); return
    from modules.llm_base import get_request_log
    log = get_request_log(200)
    h._json({"requests": log, "count": len(log)})


def get_pipeline(h):
    # 每轮对话各阶段的输入/输出/思考过程（调试答非所问用）
    from orchestrator import get_pipeline_log
    log = get_pipeline_log(20, mode=_query_mode(h))
    h._json({"pipeline": log, "count": len(log)})


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


# ── 记忆窗口状态 + 历史对话存档（2026-09-18）────────────────
# 活跃窗口 = "自上次整理以来"的对话原文；除刚开场外常驻 30–100 轮。
# 整理把"最近 keep_turns 轮以外"的部分搬进历史对话存档（只进检索器）。
# 顶部状态条轮询 /memory-status（很轻）；/archive 只在设定文件页打开时拉一次。
def get_memory_status(h):
    """GET /memory-status?mode= → 活跃窗口状态（聊天页顶部状态条数据源）。永不 500。"""
    mode = _query_mode(h)
    try:
        from modules.auto_rest import status
        h._json(status(mode))
    except Exception as e:
        h._json({"level": "unknown", "active_turns": 0, "total_turns": 0,
                 "window_max": 0, "keep_turns": 0, "warn_turns": 0, "auto": False,
                 "error": f"{type(e).__name__}: {e}"}, 200)


def get_archive(h):
    """GET /archive?mode=&month= → 存档月份列表 + 指定月原文（省略 month 取最新月）。"""
    from urllib.parse import urlparse, parse_qs
    from modules.memory_archive import archive_months, read_archive
    mode = _query_mode(h)
    qs = parse_qs(urlparse(h.path).query)
    month = (qs.get("month", [""])[0] or "").strip()
    try:
        months = archive_months(mode)
        if month and month not in months:
            h._json({"ok": False, "error": f"没有 {month} 的存档", "months": months}, 404)
            return
        use = month or (months[0] if months else "")
        h._json({"ok": True, "months": months, "month": use,
                 "content": read_archive(mode, use) if use else ""})
    except Exception as e:
        h._json({"ok": False, "error": f"{type(e).__name__}: {e}", "months": []}, 200)


# 白名单式动作派发（与 /voice/plugin 同哲学：端点越少，路由 oracle 维护面越小）
_MEMORY_ACTIONS = {"save_archive", "compress"}


def memory_action(h):
    """POST /memory-action {action, mode, month, content?}
    - save_archive：保存用户编辑过的存档原文（用户私有数据，改完检索器就该看到改后的）
    - compress    ：把指定月存档交给 AI 压缩进「用户记忆」（memory.md 头部）
    """
    from modules.memory_archive import read_archive, write_archive
    body = _read_json(h)
    mode = _body_mode(body)
    action = (body.get("action") or "").strip()
    if action not in _MEMORY_ACTIONS:
        h._json({"ok": False, "error": f"未知操作: {action!r}",
                 "allowed": sorted(_MEMORY_ACTIONS)}, 400)
        return
    month = (body.get("month") or "").strip()
    if action == "save_archive":
        content = body.get("content")
        if not isinstance(content, str):
            h._json({"ok": False, "error": "内容必须为文本"}, 400)
            return
        if len(content) > _ARCHIVE_MAX:
            h._json({"ok": False, "error": f"内容过长（上限 {_ARCHIVE_MAX} 字符）"}, 400)
            return
        ok = write_archive(mode, month, content)
        h._json({"ok": ok, "error": "" if ok else "保存失败（月份非法或写盘失败）"})
        return
    # action == "compress"
    from modules import app_config as cfg
    client = cfg.get_client()
    if not client:
        h._json({"ok": False, "error": "未设置 API Key"})
        return
    text = read_archive(mode, month)
    if not text.strip():
        h._json({"ok": False, "error": "这个月没有存档内容"})
        return
    from modules.memory_manager import MemoryManager
    from api.chat_ops import _resolve_today
    mm = MemoryManager(client, cfg.MODEL, mode=mode)
    try:
        res = mm.compress_text(text, today=_resolve_today())
    except Exception as e:
        h._json({"ok": False, "error": f"压缩失败: {e}"})
        return
    h._json({"ok": res.success, "month": month,
             "added": len(res.added_entries), "resolved": len(res.resolved_entries),
             "head_chars": len(res.new_head or ""),
             "error": res.error})


# 存档单文件上限（原文，比 memory.md 宽松得多；与 memory_archive.MAX_CHARS 同量级）
_ARCHIVE_MAX = 400000
