# -*- coding: utf-8 -*-
"""热更新器运行参数；只依赖标准库。

热更新器必须能在业务代码无法导入时独立完成 preflight、回滚和状态读取，因此这里
不接受 `core` / `modules` 的任何对象。Android 壳在 Python 启动前注入版本号；PC 与
测试通过环境变量或 `configure()` 提供同一组事实。
"""
from __future__ import annotations

import os
from pathlib import Path


_base_version = ""
_data_root: Path | None = None


def configure(*, base_version: str | None = None, data_root: str | Path | None = None) -> None:
    global _base_version, _data_root
    if base_version is not None and str(base_version).strip():
        _base_version = str(base_version).strip()
    if data_root is not None and str(data_root).strip():
        _data_root = Path(data_root).resolve()


def reset_for_test() -> None:
    global _base_version, _data_root
    _base_version = ""
    _data_root = None


def base_version() -> str:
    return (
        _base_version
        or str(os.environ.get("FIREFLY_BASE_VERSION") or "").strip()
        or "unknown"
    )


def data_root() -> Path:
    if _data_root is not None:
        return _data_root
    raw = str(os.environ.get("FIREFLY_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).resolve()
    # 开发布局：app/hotupdate/runtime.py -> 仓库根/hotupdate
    return Path(__file__).resolve().parents[2]


def hotupdate_root() -> Path:
    if _data_root is not None:
        return _data_root / "hotupdate"
    raw = str(os.environ.get("FIREFLY_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).resolve() / "hotupdate"
    return Path(__file__).resolve().parents[2] / "hotupdate"
