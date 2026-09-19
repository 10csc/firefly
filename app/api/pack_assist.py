# -*- coding: utf-8 -*-
"""AI 辅助端点（阶段 E）：/pack-assist（对话→提案） + /pack-assist/apply（校验→备份→写入）"""

import logging

from modules import app_config as cfg
from routes_common import _read_json, _body_mode

logger = logging.getLogger(__name__)


def pack_assist(h):
    """POST /pack-assist {mode, file, message, history} → {ok, reply, changes?}
    AI 只产提案，不写盘。无 Key / 不可辅助的文件直接拒绝。"""
    client = cfg.get_client()
    if not client or cfg.relay_needs_key():
        h._json({"ok": False, "error": "请先设置 API Key", "need_key": True}); return
    body = _read_json(h)
    mode = _body_mode(body)
    file = str(body.get("file") or "").strip()
    from modules.pack_assist import assist_turn, assistable
    if not assistable(file):
        h._json({"ok": False, "error": "该文件不支持 AI 辅助"}); return
    try:
        out = assist_turn(client, mode, file, str(body.get("message") or ""),
                          body.get("history") or [],
                          model=cfg.eff_cfg("analyzer_model"), effort="high")
        h._json({"ok": True, **out})
    except Exception as e:
        logger.warning("pack-assist 失败: %s", e)
        h._json({"ok": False, "error": f"AI 调用失败: {e}"})


def pack_assist_apply(h):
    """POST /pack-assist/apply {mode, file, changes} → 校验+备份+写入。返回 {ok, error}。"""
    body = _read_json(h)
    mode = _body_mode(body)
    file = str(body.get("file") or "").strip()
    changes = body.get("changes")
    from modules.pack_assist import apply_proposal
    ok, err = apply_proposal(mode, file, changes if isinstance(changes, list) else [])
    h._json({"ok": ok, "error": err})
