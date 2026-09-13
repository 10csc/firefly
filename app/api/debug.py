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
