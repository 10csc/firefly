# -*- coding: utf-8 -*-
"""应用配置兼容层（阶段 2.1 拆分后）— 实现已移入 app/core/

本文件保留**同一个模块名**，因此既有几百处 `from modules import app_config as cfg`
零改动。实现按职责拆到：
- `core.paths`      路径公式与平台常量（BASE_DIR/ROOT/USER_DIR/CONFIG_FILE、mode_* 目录、resolve_asset）
- `core.userctx`    每请求用户上下文（contextvars）与配额钩子注册
- `core.config`     config.json 读写、供应商 CRUD、eff_cfg、get_client
- `core.presets`    预设包注册表（PRESETS/MODES/reload_presets/char_name/user_name）
- `core.migrations` 老数据迁移、默认文件拷贝、首启初始化

**为什么需要 `_ShimModule` 这层转发**：`USER_DIR` / `CONFIG_FILE` / `MODES` 等是
"会被重新赋值的全局量"（测试用 `cfg.USER_DIR = tmp` 注入沙箱数据根、reload_presets
会重绑 MODES）。若在兼容层按值 re-export，`cfg.USER_DIR = tmp` 只会在这个模块上新增
一个属性，真正干活的 core.paths.USER_DIR 不变 —— 测试就会写进**真实 user_data**
（不是理论风险：约 20 个测试文件都这样注入沙箱）。转发读写即可保持与拆分前逐字等价。
"""

import logging
import sys
import types

from core.paths import (
    AUTH_SERVER_DEFAULT, BASE_DIR, PORT, ROOT,
    STATIC_DIR, bundled_character_dir, ensure_mode_root, mode_character_dir,
    mode_data_dir, mode_journal_dir, mode_root, resolve_asset,
)
from core.userctx import (
    _user_ctx, _user_ctx_dir, _user_overlay, get_user_overlay,
    reset_user_context, set_proxy_quota_checker, set_proxy_quota_counter, set_proxy_quota_failer,
    set_user_context, set_user_overlay, user_dir_id, user_scope_key,
)
from core.config import (
    API_BASE, APP_VERSION, GO_BASE, MODEL,
    SUGGESTED_MODELS, SUGGESTED_PROVIDERS, VALID_EFFORTS, _BASE_MAX_LEN,
    _CONFIG_BAK_KEEP, _MODEL_MAX_LEN, _PACK_CFG_NAME, _active_provider,
    _clean_model, _clean_provider, _deepseek_preset, _load_config,
    _on_caps_probe, _sync_derived, _valid_http_base, active_provider,
    config, eff_cfg, get_api_key, get_client,
    normalize_providers, pack_cfg, provider_by_id, relay_needs_key,
    remove_provider, save_config, set_active_provider, set_pack_cfg,
    upsert_provider, user_has_key,
)
from core.presets import (
    DEFAULT_MODE, PRESETS, PRESET_SCHEMA, _PRESET_ID_RE,
    _clean_knowledge_dirs, _discover_presets, _parse_preset, char_name,
    reload_presets, user_name,
)
from core.migrations import (
    _DEFAULTS, _LEGACY_PRE_MOVE_ZIP, _backup_legacy_sources, _cleanup_stale_defaults,
    _defaults_map, _legacy_targets, _migrate_legacy_providers, run_legacy_migration,
    run_startup_init,
)

logger = logging.getLogger(__name__)

from core import paths as _paths
from core import userctx as _userctx
from core import config as _config
from core import presets as _presets
from core import migrations as _migrations

_SOURCES = (_paths, _userctx, _config, _presets, _migrations)

# 会被重新赋值/替换的全局量：读与写都转发到真正持有它的模块
_FORWARD = frozenset({
    "APP_DIR", "CONFIG_FILE", "MODES", "USER_DIR",
    "_proxy_quota_checker", "_proxy_quota_counter", "_proxy_quota_failer",
})

# 说明：不定义 __all__ —— 转发的名字不在本模块命名空间里（pyflakes 会报 F822），
# 且全仓没有 `from modules.app_config import *` 用法；显式 import 走 __getattr__ 正常。


class _ShimModule(types.ModuleType):
    """兼容层模块类：未在本地定义的名字转发到 core.*；可变全局量的赋值同样转发。"""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        for _m in _SOURCES:
            if hasattr(_m, name):
                return getattr(_m, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        # 写：落到**所有**持有该名字的 core 模块 —— 拆分前 cfg 与被调函数是同一个命名空间，
        # 一次赋值就能影响全部调用点（`cfg.get_client = stub` 会连带影响模块内部对 get_client
        # 的调用；`cfg.time = 假时钟` 同理）。只写兼容层或只写第一个模块都会让语义变味。
        _hit = False
        for _m in _SOURCES:
            if hasattr(_m, name):
                setattr(_m, name, value)
                _hit = True
        if _hit and name not in _FORWARD:
            # 显式 re-export 的名字：兼容层本地也要更新（读走本地属性）
            super().__setattr__(name, value)
        elif not _hit:
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ShimModule
