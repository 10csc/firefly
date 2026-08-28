# -*- coding: utf-8 -*-
"""后端代理（relay 中转）路由（routes.py 拆分产物，纯重构无行为变化）。"""

import logging

from modules import app_config as cfg

from routes_common import _read_json

logger = logging.getLogger(__name__)


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
    # SSRF 纵深防御（安全审查 2026-08-25）：入队侧拦截（server_app X-API-Base 校验）
    # 之外，代发前再验一次公网——防其它入队路径绕过或队列残留的脏 base。
    # 服务器以自身身份向该地址发请求，内网地址一律不发。
    from modules.api_client import is_public_endpoint
    if not is_public_endpoint(api_base):
        logger.warning("[SSRF-GUARD] relay_proxy 拒绝非公网 api_base: %s", api_base[:120])
        h._json({"ok": False, "error": "接口地址不被允许"}, 403)
        return
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
