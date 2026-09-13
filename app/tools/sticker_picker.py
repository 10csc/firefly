# -*- coding: utf-8 -*-
"""表情包领域 —— 兼容层（阶段 2.7 归位后）

实现已移到 `domain.stickers.picker`（领域层）。本文件保留原模块名：
`from tools.sticker_picker import ...`（app 内历史延迟导入）与 tests 的
`import tools.sticker_picker as sp` 全部零改动。

为什么归位：`app/tools/` 与仓库根 `tools/`（开发脚本）同名不同物，
`import tools.sticker_picker` 的归属取决于 sys.path 顺序 —— 真实歧义，不是洁癖。
另外该模块原本用 `Path(__file__).parent.parent` 定位 `assets/stickers/registry.json`，
归位后深度变化会指错目录（已改为 `cfg.BASE_DIR`）。

**写转发是必需的**：tests 直接改模块级全局量（`sp._REGISTRY_FILE = <临时文件>`）并期望
领域函数读到新值——拆分前它们同处一个命名空间；只按值 re-export 的话补丁不会生效
（test_sticker_picker / test_sticker_upload 立刻红）。
"""
import sys
import types

from domain.stickers.picker import (  # noqa: F401
    StickerAddError, StickerDeleteError, StickerEntry, StickerUpdateError,
    VALID_CATEGORIES, _BASE_DIR, _PICK_COUNT, _REGISTRY_FILE, _REGISTRY_LEGACY,
    _STICKERS_DEFAULT, _USER_DIR, _char_overlap, _load_registry, _lock,
    _migrate_enabled_defaults, _migrate_legacy_registry, _read_registry_items,
    _save_user_entry, _user_registry_file, _write_registry_all, add_sticker,
    delete_sticker, editable_ids, get_all_stickers, get_counters,
    get_enabled_stickers, list_all_stickers, logger, pick_sticker,
    pick_sticker_by_label, pick_sticker_by_meaning, update_sticker,
)
from domain.stickers import picker as _picker

_SOURCES = (_picker,)


class _ShimModule(types.ModuleType):
    """兼容层模块类：未在本层定义的名字转发到领域模块；写同时落到领域模块。"""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        for _m in _SOURCES:
            if hasattr(_m, name):
                return getattr(_m, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        # 写转发：tests 的 `sp._REGISTRY_FILE = tmp` 必须被领域函数读到（同一命名空间语义）
        for _m in _SOURCES:
            if hasattr(_m, name):
                setattr(_m, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ShimModule
