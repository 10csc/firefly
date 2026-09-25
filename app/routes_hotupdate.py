# -*- coding: utf-8 -*-
"""热更新路由（见 docs/热更新规范.md 与 热更新/02_实现契约.md §五）。

五个端点都很轻：status 只读状态；action 是唯一会改盘/联网的入口；boot-ok 与 activity
是前端的心跳（分别用于"启动成功判据"和"空闲判定"）；restart 是壳/用户问
"现在该不该重启进程"（v2：含 py 层的补丁只能靠重启生效）。

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
    from hotupdate import (apply_available, check, clear_note, rollback,
                           set_enabled, set_url_roots)
    if act == "check":
        h._json(check(manual=True))
    elif act == "apply":
        h._json(apply_available())
    elif act == "rollback":
        h._json(rollback("用户手动回滚"))
    elif act == "clear_note":
        # 清掉"上次修复已回退/上次检查失败"的提示（提示只写不清会让用户永远看着它）
        h._json(clear_note())
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


def hotupdate_restart(h):
    """壳问：现在该重启进程吗？（v2）

    ★ 与 `reload_pending` 的分工：reload 只换前端，**换不掉已 import 的 Python 模块**。
    含 py 层的补丁必须走这里的"重启进程"分支（契约 §七）。
    只看不行动 —— 真正的重启动作在 Kotlin 壳（`MainActivity.maybeRestartForHotUpdate`），
    前端没有重启自己的能力，所以它只把这件事显示给用户。
    """
    if _deny(h):
        return
    from hotupdate import idle, restart_ready, status as hu_status
    h._json({"ok": True, "restart": restart_ready(), "idle": idle(),
             "status": hu_status()})
