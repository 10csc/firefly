# -*- coding: utf-8 -*-
"""热更新路由（见 docs/热更新规范.md 与 热更新/02_实现契约.md §五）。

四个端点都很轻：status 只读状态；action 是唯一会改盘/联网的入口；boot-ok 与 activity
是前端的心跳（分别用于"启动成功判据"和"空闲判定"）。

**仅本地版可用**：服务器版跑的是同一份代码，但热更是端侧覆盖层的事，服务器上既无意义
又是个"任意登录用户可触发联网下载"的口子（与 /metrics 同理）。
"""
from __future__ import annotations

from routes_common import _is_server, _read_json


def _deny(h) -> bool:
    if _is_server():
        h._json({"ok": False, "error": "服务器版不提供热更新"}, 403)
        return True
    return False


def hotupdate_status(h):
    if _deny(h):
        return
    from hotupdate import status
    h._json(status())


def hotupdate_action(h):
    if _deny(h):
        return
    body = _read_json(h) or {}
    act = str(body.get("action") or "").strip()
    from hotupdate import apply_available, check, rollback, set_enabled, set_url_roots
    if act == "check":
        h._json(check(manual=True))
    elif act == "apply":
        h._json(apply_available())
    elif act == "rollback":
        h._json(rollback("用户手动回滚"))
    elif act == "set_enabled":
        h._json(set_enabled(bool(body.get("enabled"))))
    elif act == "set_urls":
        roots = body.get("url_roots")
        if not isinstance(roots, list):
            h._json({"ok": False, "error": "url_roots 必须是数组"})
            return
        h._json(set_url_roots(roots))
    else:
        h._json({"ok": False, "error": f"未知操作：{act!r}"})


def hotupdate_boot_ok(h):
    if _deny(h):
        return
    from hotupdate import boot_ok
    h._json(boot_ok())


def hotupdate_activity(h):
    if _deny(h):
        return
    body = _read_json(h) or {}
    from hotupdate import note_activity
    h._json(note_activity(bool(body.get("busy")), str(body.get("why") or "")))
