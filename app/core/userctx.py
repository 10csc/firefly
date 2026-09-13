# -*- coding: utf-8 -*-
"""用户上下文与配额钩子 — 每请求的用户作用域（任务 2.1 自 app_config 拆出）

contextvars 承载 user_dir / api_key / api_base / proxy 标记；服务器版多用户隔离靠它。
`_proxy_quota_*` 是运行期由 server_app 注册的回调（会被重新赋值），消费方请按属性访问。
"""

import contextvars
from pathlib import Path


# ── 用户上下文（服务器版多用户隔离；本地版不设置，行为与之前完全一致）──
# 每个请求一个上下文：user_dir（该用户数据根）、api_key/api_base（用户自己的 Key）。
# 用 contextvars（Flask/Werkzeug 同款标准模式）：set 返回 Token，请求结束 reset 恢复。
# ThreadingHTTPServer 每请求新线程 + reset 双保险，无跨请求泄漏。
_user_ctx: contextvars.ContextVar = contextvars.ContextVar("firefly_user_ctx", default=None)


def set_user_context(user_dir=None, api_key=None, api_base=None, proxy=False) -> contextvars.Token:
    """设置当前请求的用户上下文，返回 Token（请求结束 reset_user_context 恢复）。
    proxy=True：服务器托管 API 模式（用户不提供 Key，服务器用运营者 Key 直发）。"""
    data = dict(_user_ctx.get() or {})
    if user_dir is not None:
        data["user_dir"] = Path(user_dir)
    if api_key is not None:
        data["api_key"] = api_key
    if api_base is not None:
        data["api_base"] = api_base
    data["proxy"] = bool(proxy)
    return _user_ctx.set(data)


def reset_user_context(token) -> None:
    """请求结束恢复上下文（try/finally 中调用，LLM 异常也会正确恢复）。"""
    _user_ctx.reset(token)


def _user_ctx_dir():
    ctx = _user_ctx.get()
    return ctx.get("user_dir") if ctx else None


def user_scope_key() -> str:
    """当前用户作用域标识（缓存 key 用）：服务器版 = 用户目录路径，本地版 = 空串。

    各模块的按 mode 缓存（角色设定/手账/短信样本）必须带上此维度，
    否则多用户并发下 A 的设定/手账会被 B 读到（缓存串扰）。
    """
    d = _user_ctx_dir()
    return str(d) if d else ""


def user_dir_id() -> int:
    """当前用户 id（= 数据目录名，服务器版；本地版无上下文返回 0）。配额记账用。"""
    d = _user_ctx_dir()
    if d is None:
        return 0
    try:
        return int(d.name)
    except ValueError:
        return 0


# ── 按用户配置覆盖（A2：/set-config 服务器版只写覆盖，不污染全站默认）──
_user_overlay: contextvars.ContextVar = contextvars.ContextVar("firefly_user_overlay", default=None)


def set_user_overlay(d: dict | None) -> None:
    """注入当前请求的用户配置覆盖（server_app 每请求从 user_data/{uid}/settings.json 读）。"""
    _user_overlay.set(d if isinstance(d, dict) else {})


def get_user_overlay() -> dict:
    return _user_overlay.get() or {}




# ── 托管模式配额钩子（服务器注册；本地版不注册 = 不限制）──
_proxy_quota_checker = None
_proxy_quota_counter = None
_proxy_quota_failer = None


def set_proxy_quota_checker(fn) -> None:
    """注册托管模式配额检查函数：fn() 返回错误文案（""=放行）。
    A3（默认拍板）：检查与记账分离——检查放行后才调用，成功再记数（失败单独记失败表）。"""
    global _proxy_quota_checker
    _proxy_quota_checker = fn


def set_proxy_quota_counter(fn) -> None:
    """注册成功记账函数：每次调用成功（无异常）后调用。"""
    global _proxy_quota_counter
    _proxy_quota_counter = fn


def set_proxy_quota_failer(fn) -> None:
    """注册失败记账函数：调用抛异常时调用（失败不占额度，单独记录）。"""
    global _proxy_quota_failer
    _proxy_quota_failer = fn
