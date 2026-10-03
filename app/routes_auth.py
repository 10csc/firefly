# -*- coding: utf-8 -*-
"""A7 本地版认证代理路由（routes.py 拆分产物，纯重构无行为变化）。
登录前置化；账号体系仍在服务器，本地后端转发 /auth/* 并持有 token。"""

import json
import logging
import urllib.request

from modules import app_config as cfg

from routes_common import _is_server

logger = logging.getLogger(__name__)


# ══ A7：本地版认证代理（登录前置化；账号体系仍在服务器）══
# 本地后端把 /auth/* 转发到认证服务器（无 CORS 问题；安卓本地模式同一引擎同享）。
# token 由本地后端持有（user_data/auth.json，与 config.json 分离，服务器不落盘）。
_AUTH_PROXY_TIMEOUT = 15


_AUTH_PROXY_MAP = {
    "/auth/mail-send": "mail-send",
    "/auth/register": "register",
    "/auth/login": "login",
    "/auth/reset-send": "reset-send",
    "/auth/reset-password": "reset-password",
    "/auth/logout": "logout",
    "/auth/verify": "verify",
}


def _auth_server_base() -> str:
    base = str(cfg.config.get("auth_server_url", "") or "").strip().rstrip("/")
    return base if base.startswith(("http://", "https://")) else cfg.AUTH_SERVER_DEFAULT


def _auth_forward(e: str, body: dict, token: str):
    """转发认证请求到服务器。返回 (status, data)；网络不可达返回 (None, error_text)。"""
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        _auth_server_base() + "/auth/" + e,
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_AUTH_PROXY_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        try:
            return ex.code, json.loads(ex.read().decode("utf-8"))
        except Exception:
            logger.warning("认证服务返回非 JSON（HTTP %s）", ex.code)
            return ex.code, {"error": "认证服务暂时异常，请稍后重试"}
    except Exception as ex:
        logger.warning("无法连接认证服务器（%s）：%s", _auth_server_base(), ex)
        return None, "无法连接登录服务，请检查网络后重试"


def auth_proxy(h, e: str):
    """POST /auth/<e> 代理（本地版）。登录成功 → 落 auth.json（token 由后端持有）。"""
    if _is_server():
        h._json({"error": "服务器版认证请直接访问本站"}, 403)
        return
    # 局部绑定：call-time 从 routes 取，保持 patch("routes._read_json") 拦截面不变
    from routes import _read_json
    body = _read_json(h)
    token = ""
    auth_hdr = h.headers.get("Authorization", "") or ""
    if auth_hdr.startswith("Bearer "):
        token = auth_hdr[7:].strip()
    status, data = _auth_forward(e, body, token)
    if status is None:
        h._json({"error": data}, 502)
        return
    # 登录成功：本地落盘凭证（不回传 token 给前端——本地后端持有）
    if e == "login" and isinstance(data, dict) and data.get("ok") and data.get("token"):
        from modules.auth_store import save_login
        email = body.get("email", "") or ""
        role = str(data.get("role") or "user")
        save_login(str(data.get("token", "")), email,
                   str(data.get("expires_at", "") or ""), role=role)   # role 一起存 ⇒ /auth/state 离线也能回
        data = {"ok": True, "email": email, "role": role,
                "server": _auth_server_base()}
    # 登出：本地清除凭证
    elif e == "logout" and isinstance(data, dict) and data.get("ok"):
        from modules.auth_store import clear_login
        clear_login()
    h._json(data, status if status and status < 1000 else 400)


def auth_state(h):
    """GET /auth/state（本地版）：登录态；联网时隐式 verify（服务器滚动续期）。

    `role`（2026-10-01 收口）：登录时服务器回带的角色**随状态一起回给前端**（`admin`/`user`），
    否则前端只能隐藏"官方卡"勾选框（clientpack 实测）。来源优先级：
    ① 本次 verify 响应里的 role（服务器现算的，最新）→ ② 本地 auth.json 里登录时存下的 role。
    未登录时口径不变（只是多一个空 `role` 字段，前端不必分支）；**不改任何判定**。
    """
    if _is_server():
        h._json({"error": "服务器版请用 /auth/me"}, 403)
        return
    from modules.auth_store import get_state, get_token, clear_login, refresh_expires
    st = get_state()
    if st.get("logged_in"):
        status, data = _auth_forward("verify", {}, get_token())
        if status == 200 and isinstance(data, dict):
            if data.get("expires_at"):
                refresh_expires(str(data["expires_at"]))
                st["expires_at"] = str(data["expires_at"])
            if data.get("role"):
                st["role"] = str(data["role"])        # 服务器现算的角色优先（离线时用本地那份）
            st["verified"] = True
        elif status == 401:
            clear_login()      # 服务器判定失效：清凭证（前端引导重新登录）
            st = get_state()
            st["verified"] = False
        else:
            st["verified"] = False   # 网络不可达：保持离线宽限判定
    h._json(st)


def _mk_auth_proxy(e: str):
    return lambda h: auth_proxy(h, e)
