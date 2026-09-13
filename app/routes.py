# -*- coding: utf-8 -*-
"""HTTP 路由 —— 兼容层（阶段 2.3 拆分后）

实现已按职责移出：
- `api.chat`    聊天主链与合并窗口（chat/hint/flush/rest/undo/clear-history/history/open-mode/
                proactive-status/chat-stage/wake-status/save-journal）
- `api.debug`   调试与只读面板（metrics/requests/pipeline/user-memory/journal）
- `api.router`  两张分发表（POST_ROUTES / GET_ROUTES）

本文件保留**同一个模块名**：`app/server.py`、`server/server_app.py` 用 `routes.POST_ROUTES`
分发；各处 handler 运行时 `from routes import parse_multipart / _read_json`；
tests 大量 `routes.xxx(h)` 直接调端点。以上全部零改动。

**`_ShimModule` 的写转发是必需的**：tests 会打桩 `routes.parse_multipart`（多份测试模拟
multipart 请求）——拆分前它们是同一个命名空间，一次赋值影响所有调用点；
转发到**所有**持有该名字的新模块才能保持这个语义。
"""

import logging
import sys
import types

from api.chat import (
    _CHAT_WINDOWS, _CHAT_WINDOW_IDLE, _CHAT_WINDOW_LOCK, _CHAT_WINDOW_MAX,
    _CHAT_WINDOW_MAX_MSGS, _CHAT_WINDOW_SEC, _chat_window_cleanup, _chat_window_key,
    _ingest_user_messages, _merge_window, _resolve_today, _run_pipeline,
    chat, chat_flush, chat_hint, clear_history,
    get_chat_stage, get_history, get_time, get_wake_status,
    open_mode, proactive_status, rest, save_journal,
    undo,
)
from api.debug import (
    get_journal, get_metrics, get_pipeline, get_requests,
    get_user_memory, save_user_memory,
)
from api.router import (
    GET_ROUTES, POST_ROUTES,
)

# 原模块「顺带 import 进来」的名字也要 re-export —— 它们不是死导入，而是**对外可见的
# 命名空间**：tests 直接经 routes.* 取用（routes.auth_proxy / routes._IMG_ID_RE /
# routes._fetch_json / routes._match_asset / routes.get_latest_release /
# routes._get_asset_url / routes.get_session / routes.handle_chat / routes.parse_multipart
# / routes._read_json …）。少一个就是测试里 AttributeError。
from modules import app_config as cfg
from modules.app_config import DEFAULT_MODE
from modules.context_manager import ContextManager
from modules.multipart import parse_multipart
from orchestrator import handle_chat
from routes_assets import (
    add_favorite_route, add_sticker_route, assets_index, assets_raw,
    delete_favorite_route, get_character_files, get_favorites, get_image,
    get_stickers, sticker_delete, sticker_update, upload_image,
)
from routes_auth import (
    _AUTH_PROXY_MAP, _auth_server_base, _mk_auth_proxy, auth_proxy,
    auth_state,
)
from routes_common import (
    _CONTENT_MAX, _IMG_ID_RE, _SESSIONS_LOCK, _body_mode,
    _body_mode_ex, _is_server, _load_image_data_url, _notify_reply_if_background,
    _query_mode, _query_mode_ex, _read_json, _session_key,
    _write_replies, get_session, sessions,
)
from routes_config import (
    check_key, get_balance, get_config, get_models,
    get_modes, set_config, set_key,
)
from routes_data import (
    backup_create, backup_delete, backup_restore, backups_list,
    export_data, import_data, sync_export, sync_import,
    sync_manifest, sync_now,
)
from routes_fix import (
    setting_fix_apply, setting_fix_dismiss, setting_fix_message, setting_fix_reset,
    setting_fix_rollback, setting_fix_start, setting_fix_status,
)
from routes_pack import (
    character_file_update, create_pack, delete_character_file, delete_pack,
    delete_pack_asset, get_pack_files, pack_forge_finish, pack_forge_next,
    pack_forge_start, set_pack_config, upload_pack_asset,
)
from routes_relay import relay_pending, relay_proxy, relay_result
from routes_snapshot import (
    snapshot_create, snapshot_delete, snapshot_download, snapshot_list,
    snapshot_restore,
)
from routes_update import (
    _fetch_json, _get_asset_url, _match_asset, check_update,
    get_latest_release, update_download,
)

logger = logging.getLogger(__name__)

from api import chat as _chat
from api import debug as _debug
from api import router as _router

_SOURCES = (_chat, _debug, _router)

class _ShimModule(types.ModuleType):
    """兼容层模块类：读写都转发到持有实现的新模块。"""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        for _m in _SOURCES:
            if hasattr(_m, name):
                return getattr(_m, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        # 写转发给所有持有该名字的模块（等价于拆分前同一命名空间里的一次赋值）
        for _m in _SOURCES:
            if hasattr(_m, name):
                setattr(_m, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ShimModule
