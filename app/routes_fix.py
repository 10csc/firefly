# -*- coding: utf-8 -*-
"""设定纠错助手路由（routes.py 拆分产物，纯重构无行为变化）：
对齐 → 提案 → 批准应用 → 回滚（AI 只提案，用户点「应用」才生效）。"""

import logging
import time

from modules import app_config as cfg

from routes_common import _read_json, _body_mode, _query_mode, get_session

logger = logging.getLogger(__name__)


# ── 设定纠错助手（对齐 → 提案 → 批准应用 → 回滚）────────────
# 权限模型：AI 只提案（pending），用户点「应用」才生效。
# 运行位置：本地版=本地 Python；服务器版=服务器 Python（按用户上下文隔离）。

def _fix_need_key(h) -> bool:
    if not cfg.get_client():
        h._json({"ok": False, "error": "请先设置 API Key", "need_key": True})
        return True
    if cfg.relay_needs_key():
        h._json({"ok": False, "error": "请先设置 API Key", "need_key": True})
        return True
    return False


def _fix_err_code(e: Exception) -> str:
    from modules.api_client import ApiError
    return e.code if isinstance(e, ApiError) else "unknown"


def setting_fix_status(h):
    from modules.setting_fix_store import get_status
    mode = _query_mode(h)
    try:
        h._json(get_status(mode))
    except Exception as e:
        h._json({"ok": False, "error": f"读取状态失败: {e}"})


def setting_fix_message(h):
    if _fix_need_key(h):
        return
    body = _read_json(h)
    mode = _body_mode(body)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        h._json({"ok": False, "error": "请描述角色哪里说得不对"})
        return
    text = text.strip()

    from modules.setting_fix import run_alignment, run_proposal
    from modules.setting_fix_store import (locked, load_conversation,
                                           append_conversation, load_pending,
                                           save_pending)
    client = cfg.get_client()
    model = cfg.eff_cfg("polisher_model", "deepseek-flash")
    effort = cfg.eff_cfg("polisher_effort", "high")
    with locked(mode):
        conversation = load_conversation(mode)
        pending = load_pending(mode)
        try:
            if pending is not None:
                # proposal 阶段用户继续补充 → 先记下用户的话，再重新生成方案（仍不生效）
                append_conversation(mode, "user", text, stage="proposal")
                conversation = load_conversation(mode)
                result = run_proposal(client, mode, conversation, model, effort)
            else:
                result = run_alignment(client, mode, conversation, text, model, effort)
        except Exception as e:
            h._json({"ok": False, "error": f"分析失败，请稍后再试: {e}",
                     "error_code": _fix_err_code(e)})
            return

        if "error" in result and not result.get("ok", True):
            h._json(result)
            return

        if pending is not None:
            if result.get("kind") in ("proposal", "no_fix"):
                proposal = {
                    "kind": result.get("kind"),
                    "diagnosis": result.get("diagnosis", ""),
                    "changes": result.get("changes", []),
                    "model": model,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_pending(mode, proposal)
            h._json({"ok": True, "stage": "proposal",
                     "diagnosis": result.get("diagnosis", ""),
                     "changes": result.get("changes", [])})
            return

        append_conversation(mode, "user", text, stage=result.get("stage", "aligning"))
        assistant = append_conversation(
            mode, "assistant", result.get("text", ""),
            options=result.get("options", []), stage=result.get("stage", "aligning"))
        h._json({"ok": True, "stage": result.get("stage", "aligning"),
                 "text": result.get("text", ""),
                 "options": result.get("options", []),
                 "message": assistant})


def setting_fix_start(h):
    if _fix_need_key(h):
        return
    body = _read_json(h)
    mode = _body_mode(body)

    from modules.setting_fix import run_proposal
    from modules.setting_fix_store import locked, load_conversation, save_pending
    client = cfg.get_client()
    model = cfg.eff_cfg("polisher_model", "deepseek-flash")
    effort = cfg.eff_cfg("polisher_effort", "high")
    with locked(mode):
        conversation = load_conversation(mode)
        if not conversation:
            h._json({"ok": False, "error": "请先描述角色哪里说得不对"})
            return
        try:
            result = run_proposal(client, mode, conversation, model, effort)
        except Exception as e:
            h._json({"ok": False, "error": f"生成修改方案失败，请稍后再试: {e}",
                     "error_code": _fix_err_code(e)})
            return
        if not result.get("ok"):
            h._json(result)
            return
        proposal = {
            "kind": result.get("kind"),
            "diagnosis": result.get("diagnosis", ""),
            "changes": result.get("changes", []),
            "model": model,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_pending(mode, proposal)
        h._json({"ok": True, "stage": "proposal",
                 "diagnosis": proposal["diagnosis"],
                 "changes": proposal["changes"]})


def setting_fix_apply(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session_id = body.get("session_id", "default")

    from modules.setting_fix_store import locked, apply_pending
    with locked(mode):
        ok, err, applied, version = apply_pending(mode)
    if not ok:
        h._json({"ok": False, "error": err})
        return

    # 若本次改了 memory.md：让当前会话立即用新记忆头部（不等待重启）
    try:
        if "memory.md" in applied:
            session = get_session(session_id, mode)
            from modules.memory_manager import wake as memory_wake
            session["memory_head"] = memory_wake(None, cfg.MODEL, mode)
    except Exception as e:
        logger.warning("应用后重载会话记忆失败: %s", e)

    h._json({"ok": True, "version": version, "applied": applied,
             "message": "修改已生效，可在「最近修正记录」中撤销"})


def setting_fix_dismiss(h):
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.setting_fix_store import locked, dismiss
    with locked(mode):
        ok, err = dismiss(mode)
    h._json({"ok": ok, "error": err})


def setting_fix_rollback(h):
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.setting_fix_store import locked, rollback
    with locked(mode):
        ok, err, version = rollback(mode)
    h._json({"ok": ok, "error": err, "version": version})


def setting_fix_reset(h):
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.setting_fix_store import locked, reset
    with locked(mode):
        ok, err = reset(mode)
    h._json({"ok": ok, "error": err})
