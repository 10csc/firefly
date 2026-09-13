# -*- coding: utf-8 -*-
"""数据导出/导入/备份/同步 —— 兼容层（阶段 2.2 拆分后）

实现已按职责移出：
- `api.data_export`        导出/导入/本地备份端点（export_data/import_data/backup_*）
- `infra.sync.restore`     恢复引擎（原子换目录、zip 安全审查、自动备份、快照分发）
- `api.data_sync`          增量同步（manifest/import/export/now/_sync_one_mode）

本文件保留**同一个模块名**，因此 `from routes_data import ...`（routes.py 顶部分发表、
routes_snapshot 的延迟导入、tests）零改动。

`_ShimModule` 的写转发是必需的，不是装饰：tests/test_snapshot.py 会把
`routes_data._restore_full_snapshot` 换成记录桩，而 routes_snapshot 是**运行时
`from routes_data import _restore_full_snapshot`** 取值——补丁必须能被读到。
"""

import logging
import sys
import types

from api.data_export import (
    _BACKUP_NAME_RE, _backup_mode_of, _backup_name_ok, _build_backup_zip,
    backup_create, backup_delete, backup_restore, backups_list,
    export_data, import_data,
)
from api.data_sync import (
    _sync_mode_root, _sync_one_mode, sync_export, sync_import,
    sync_manifest, sync_now,
)
from infra.sync.restore import (
    _AUTO_KEEP, _BACKUP_KEEP, _EXPORT_EXCLUDE_DIRS, _IMPORT_MAX_BYTES,
    _IMPORT_MAX_FILE_BYTES, _IMPORT_MAX_TOTAL_BYTES, _backup_current_mode, _backup_dir,
    _import_zip_to_mode, _is_snapshot_zip, _pack_restorable, _prune_auto_backups,
    _restore_full_snapshot, _swap_dir_into_place, _zip_safe_entries,
)

logger = logging.getLogger(__name__)

from api import data_export as _data_export
from api import data_sync as _data_sync
from infra.sync import restore as _restore

_SOURCES = (_data_export, _restore, _data_sync)

class _ShimModule(types.ModuleType):
    """兼容层模块类：读写都转发到真正持有实现的新模块。"""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        for _m in _SOURCES:
            if hasattr(_m, name):
                return getattr(_m, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        # 写要落到**所有**持有该名字的新模块，不能只落第一个：
        # tests/test_backup.py 会把 routes_data.time 换成假时钟（拆分前 cfg 只有一个命名
        # 空间，一次赋值就能影响全部调用点）；只写第一个模块会让 _backup_current_mode
        # 仍用真 time → 15 次备份落在同一秒、裁剪断言失效。函数类补丁（owner 唯一）不受影响。
        for _m in _SOURCES:
            if hasattr(_m, name):
                setattr(_m, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ShimModule
