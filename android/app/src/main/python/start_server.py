# -*- coding: utf-8 -*-
"""安卓内嵌入口（Chaquopy）：启动流萤 HTTP 服务

数据布局（构建时由 Gradle syncBackend 从仓库根同步）：
  src/main/python/backend/
    ├── app/          ← 后端代码（server.py 等）
    ├── memory/       ← 设定资料（只读）
    ├── knowledge/    ← 知识库（只读）
    └── database/     ← 原始资料（只读）
运行时全部解压到 app 私有目录，只读；用户数据写 os.environ["HOME"]/firefly_data。
"""

import os
import sys
import threading
from os.path import dirname, join

_BACKEND = join(dirname(__file__), "backend")


def _setup_env():
    home = os.environ.get("HOME", "")
    data_root = join(home, "firefly_data")
    os.environ.setdefault("FIREFLY_ANDROID", "1")
    os.environ.setdefault("FIREFLY_DATA_DIR", data_root)
    os.environ["FIREFLY_NO_BROWSER"] = "1"


def start():
    """在调用线程中启动服务（阻塞至服务退出）。"""
    _setup_env()
    sys.path.insert(0, _BACKEND)
    sys.path.insert(0, join(_BACKEND, "app"))
    import server
    server.main()


def start_in_thread():
    """后台线程启动服务（MainActivity 调用入口）。"""
    threading.Thread(target=start, daemon=True).start()
