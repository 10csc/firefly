# -*- coding: utf-8 -*-
"""Android 原生进程重启适配器。

正常路径调用 Kotlin `HotUpdateBridge.requestProcessRestart()`；测试可注入 handler，
验证只有含 py 层的 pending 才请求原生重启。这里不导入任何业务模块。
"""
from __future__ import annotations

import logging
import threading


logger = logging.getLogger(__name__)
_lock = threading.Lock()
_restart_handler = None


def set_restart_handler(handler) -> None:
    global _restart_handler
    with _lock:
        _restart_handler = handler


def reset_restart_handler() -> None:
    set_restart_handler(None)


def _android_restart(pid: int, reason: str) -> bool:
    from java import jclass

    bridge = jclass("com.firefly.android.HotUpdateBridge")
    return bool(bridge.requestProcessRestart())


def request_process_restart(*, pid: int, reason: str) -> dict:
    with _lock:
        handler = _restart_handler
    fn = handler or _android_restart
    try:
        called = bool(fn(int(pid), str(reason)))
    except Exception as exc:
        logger.warning("原生进程重启请求失败: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "called": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": called, "called": called}
