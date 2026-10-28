# -*- coding: utf-8 -*-
"""广场 HTTP 传输层：响应、查询、本地代理与当前用户解析。

本模块不承载路由。`plaza.api` 会 re-export 这里的名字，兼容既有 patch
路径与测试导入，后续再逐步把端点迁到按业务划分的模块。
"""

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_LOCAL_PROXY_TIMEOUT = 30


def _is_server() -> bool:
    from routes_common import _is_server as f

    return bool(f())


def _auth_base() -> str:
    from routes_auth import _auth_server_base

    return _auth_server_base()


def _local_token() -> str:
    try:
        from modules.auth_store import get_token

        return str(get_token() or "")
    except Exception:
        return ""


def _send_bytes(
    h,
    blob: bytes,
    mime: str,
    *,
    cache: str = "no-store",
    extra: dict | None = None,
    status: int = 200,
) -> None:
    h.send_response(status)
    h.send_header("Content-Type", mime)
    h.send_header("Content-Length", len(blob))
    h.send_header("Cache-Control", cache)
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h._cors_headers()
    h.end_headers()
    h.wfile.write(blob)


def _err(h, status: int, msg: str, **extra) -> None:
    """错误响应：`{ok:false, error}`；extra 可附带 error_kind/code 等机读字段。"""
    payload = {"ok": False, "error": msg}
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    h._json(payload, status)


def _q(h) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items() if v}


def _proxy(h, sub: str, timeout: int = _LOCAL_PROXY_TIMEOUT) -> None:
    """把 GET /plaza/api/<sub>?<query> 原样转发到认证服务器并回传。"""
    base = _auth_base().rstrip("/")
    target = f"{base}/plaza/api/{sub}"
    if "?" in h.path:
        target += "?" + h.path.split("?", 1)[1]
    headers = {}
    tok = _local_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    inm = (h.headers.get("If-None-Match") or "").strip()
    if inm:
        headers["If-None-Match"] = inm
    req = urllib.request.Request(target, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            blob = r.read()
            ctype = r.headers.get("Content-Type", "application/json")
            cache = r.headers.get("Cache-Control", "no-store")
            extra = {}
            for key in ("Content-Disposition", "ETag"):
                val = r.headers.get(key)
                if val:
                    extra[key] = val
            _send_bytes(h, blob, ctype, cache=cache, extra=extra, status=r.status)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            h.send_response(304)
            etag = e.headers.get("ETag") if e.headers else None
            if etag:
                h.send_header("ETag", etag)
            h.send_header("Cache-Control", "public, max-age=86400")
            h.end_headers()
            return
        try:
            h._json(json.loads(e.read().decode("utf-8")), e.code)
        except Exception:
            _err(h, e.code, f"广场服务返回 {e.code}")
    except Exception as e:
        logger.warning("广场代理失败 %s: %s", target, e)
        _err(h, 502, f"无法连接广场服务（{base}）：{e}")


def _proxy_post(h, sub: str, timeout: int = _LOCAL_PROXY_TIMEOUT) -> None:
    """POST 版代理：把请求体原样转发到认证服务器，不记录请求体。"""
    from routes import _read_json

    body = _read_json(h) or {}
    base = _auth_base().rstrip("/")
    headers = {"Content-Type": "application/json"}
    tok = _local_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(
        f"{base}/plaza/api/{sub}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h._json(json.loads(r.read().decode("utf-8")), r.status)
    except urllib.error.HTTPError as e:
        try:
            h._json(json.loads(e.read().decode("utf-8")), e.code)
        except Exception:
            _err(h, e.code, f"广场服务返回 {e.code}")
    except Exception as e:
        logger.warning("广场代理失败（POST %s）", sub)
        _err(h, 502, f"无法连接广场服务（{base}）：{e}")


def _plaza_side(h, sub: str, timeout: int = _LOCAL_PROXY_TIMEOUT) -> bool:
    """广场侧 GET：本地模式代理到认证服务器，服务器模式由本端执行。"""
    if _is_server():
        return False
    _proxy(h, sub, timeout=timeout)
    return True


def _plaza_side_post(h, sub: str) -> bool:
    """广场侧 POST：本地模式代理到认证服务器，服务器模式由本端执行。"""
    if _is_server():
        return False
    _proxy_post(h, sub)
    return True


def _current_user(h) -> dict | None:
    """服务器模式下取当前登录用户；本地代理模式拿不到也不影响。"""
    try:
        return h._bearer_user()
    except Exception:
        return None


def _is_admin_user(h) -> bool:
    """当前用户是否管理员（只判断、不发送响应）。"""
    return str((_current_user(h) or {}).get("role") or "") == "admin"
