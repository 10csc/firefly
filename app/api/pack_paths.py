# -*- coding: utf-8 -*-
"""兼容导出：实现已下沉至 `core.pack_paths`，禁止在这里重新实现路径公式。"""

from core.pack_paths import (
    _BACKUP_KEEP,
    _backup_current,
    _paths_for,
    kb_bundled_root,
    kb_user_root,
    read_pack_text,
    read_pack_text_for_write,
    write_pack_text,
)

__all__ = [
    "_BACKUP_KEEP",
    "_backup_current",
    "_paths_for",
    "kb_bundled_root",
    "kb_user_root",
    "read_pack_text",
    "read_pack_text_for_write",
    "write_pack_text",
]
