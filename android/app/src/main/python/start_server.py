# -*- coding: utf-8 -*-
"""安卓内嵌入口（Chaquopy）：启动流萤 HTTP 服务

数据布局（构建时由 Gradle syncBackend 从仓库根同步）：
  src/main/python/backend/
    ├── app/          ← 后端代码（server.py 等，含 modules/ 流水线与记忆管理器）
    ├── knowledge/    ← 知识库（只读，检索器扫描区，含 story/ 个人经历）
    └── database/     ← 原始资料（只读，仅查证）
运行时全部解压到 app 私有目录，只读；用户数据写 os.environ["HOME"]/firefly_data。
"""

import os
import sys
import threading
from os.path import dirname, join

_BACKEND = join(dirname(__file__), "backend")

# 同进程只启动一次（安卓二次打开闪退的根因修复）：
# MainActivity.onCreate 每次进入都调 start_in_thread()，而退出 App 后前台服务
# （KeepAliveService）保活进程，Python 服务仍在监听 8765——重复启动会触发
# server.py 的多开检测：探测 /health 成功 → 调 /shutdown → 旧实例 os._exit(0)
# 直接终止整个 JVM 进程（含新实例）→ 二次打开闪退，三次冷启动正常，循环。
_START_LOCK = threading.Lock()
_STARTED = False


def _setup_env():
    home = os.environ.get("HOME", "")
    data_root = join(home, "firefly_data")
    os.environ.setdefault("FIREFLY_ANDROID", "1")
    os.environ.setdefault("FIREFLY_DATA_DIR", data_root)
    os.environ["FIREFLY_NO_BROWSER"] = "1"


def start():
    """在调用线程中启动服务（阻塞至服务退出）。"""
    _setup_env()
    # ★ 热更新 py 层注入（契约 §七 / 规范 §5.3）—— 本函数是**安卓侧最早**能插 sys.path 的地方：
    #   · Chaquopy 的 `Python.start()` 已经在 Kotlin 侧调过（MainActivity.startEmbeddedServer），
    #     本函数必然在它**之后**执行 —— 满足"Chaquopy 重写 sys.path 之后再插"的时序要求；
    #   · 又必须赶在 `import server` 之前 —— 覆盖层只对**尚未 import** 的模块生效。
    #   所以只有这一个点是对的：往前往后都会静默失效（补丁装上了、代码还是旧的，且不报错）。
    _overlay_py = join(os.environ.get("FIREFLY_DATA_DIR", ""), "hotupdate", "py")
    sys.path.insert(0, _BACKEND)
    sys.path.insert(0, join(_BACKEND, "app"))
    # ★ 覆盖层必须插在**这两条之后**（insert(0) ⇒ 最后插的在最前）。
    #   若先插覆盖层再插 `_BACKEND/app`，`import modules.x` 会先命中 APK 里的底座代码，
    #   补丁形同没装 —— 这是一个不报错的静默失效，故写死顺序。
    if os.path.isdir(_overlay_py) and _overlay_py not in sys.path:
        sys.path.insert(0, _overlay_py)
    import server
    server.main()


def start_in_thread():
    """后台线程启动服务（MainActivity 调用入口）。同进程幂等：服务已在跑则跳过。"""
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=start, daemon=True).start()
