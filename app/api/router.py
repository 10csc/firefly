# -*- coding: utf-8 -*-
"""路由分发表（阶段 2.3 自 routes.py 拆出）

两张表是**对外端点的唯一清单**：`tests/test_routes_oracle.py` 逐字比对它们的键集，
阶段 2 的拆分前后必须一字不差。这里只做 import + 登记，不放任何处理逻辑。

注意（PyInstaller 约束）：分发表依赖的 handler 必须**顶部静态导入**——动态 import
会让打包分析漏模块，打出来是"运行到某个端点才 500"。
"""


from routes_assets import (
    add_favorite_route, add_sticker_route, assets_index, assets_raw,
    delete_favorite_route, get_character_files, get_favorites, get_image,
    get_stickers, sticker_delete, sticker_update, upload_image,
)
from routes_auth import _AUTH_PROXY_MAP, _mk_auth_proxy, auth_state
from routes_hotupdate import (
    hotupdate_action, hotupdate_activity, hotupdate_boot_ok, hotupdate_status,
)
from routes_notice import notice_action, notice_get, notice_image
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
from api.diag import export_diagnostics
from api.pack_assist import pack_assist, pack_assist_apply
from routes_pack import (
    archive_pack, character_file_update, create_pack, delete_character_file, delete_pack,
    delete_pack_asset, get_pack_files,
    restore_pack, set_pack_config, upload_pack_asset,
    pack_knowledge_create, pack_knowledge_delete, pack_knowledge_list,
    pack_knowledge_read, pack_knowledge_update, pack_memory_read, pack_memory_update,
    pack_file_delete, pack_file_read, pack_file_update, pack_structure,
)
from routes_relay import relay_pending, relay_proxy, relay_result
from routes_snapshot import (
    snapshot_create, snapshot_delete, snapshot_download, snapshot_list,
    snapshot_restore,
)
from routes_update import check_update, update_download
from voice.routes import (voice_file, voice_plugin, voice_plugin_action,
                          voice_status, voice_tts)

from api.chat import (
    chat, chat_flush, chat_hint, clear_history,
    get_chat_stage, get_history, get_time, get_wake_status,
    open_mode, proactive_status, rest, save_journal,
    undo,
)
from api.debug import (
    get_archive, get_journal, get_memory_status, get_metrics, get_pipeline, get_requests,
    get_user_memory, memory_action, save_user_memory,
)


# ── 分发表 ───────────────────────────────────────
POST_ROUTES = {
    "/set-key": set_key,
    "/set-config": set_config,
    "/save-journal": save_journal,
    "/save-user-memory": save_user_memory,
    "/memory-action": memory_action,
    "/check-key": check_key,
    "/chat": chat,
    "/chat/hint": chat_hint,
    "/chat/flush": chat_flush,
    "/open-mode": open_mode,
    "/proactive-status": proactive_status,
    "/rest": rest,
    "/add-sticker": add_sticker_route,
    "/sticker-update": sticker_update,
    "/sticker-delete": sticker_delete,
    "/character-file-update": character_file_update,
    "/check-update": check_update,
    "/update-download": update_download,
    "/hotupdate/action": hotupdate_action,
    "/hotupdate/boot-ok": hotupdate_boot_ok,
    "/hotupdate/activity": hotupdate_activity,
    # 公告 / 更新说明通道（服务端下发 + 端侧缓存；本地版与服务器版都开）
    "/notice/action": notice_action,
    "/relay/pending": relay_pending,
    "/relay/result": relay_result,
    "/relay/proxy": relay_proxy,
    "/import-data": import_data,
    "/backup/create": backup_create,
    "/backup/restore": backup_restore,
    "/backup/delete": backup_delete,
    "/sync/import": sync_import,
    "/sync/export": sync_export,
    "/sync/now": sync_now,
    "/undo": undo,
    "/clear-history": clear_history,
    "/setting-fix/message": setting_fix_message,
    "/setting-fix/start": setting_fix_start,
    "/setting-fix/apply": setting_fix_apply,
    "/setting-fix/dismiss": setting_fix_dismiss,
    "/setting-fix/rollback": setting_fix_rollback,
    "/setting-fix/reset": setting_fix_reset,
    "/favorite": add_favorite_route,
    "/favorites/delete": delete_favorite_route,
    "/upload-image": upload_image,
    "/pack-asset": upload_pack_asset,
    "/pack-asset/delete": delete_pack_asset,
    "/character-file/delete": delete_character_file,
    "/pack-create": create_pack,
    "/pack-delete": delete_pack,
    "/pack-archive": archive_pack,
    "/pack-restore": restore_pack,
    "/pack-config": set_pack_config,
    "/pack-assist": pack_assist,
    "/pack-assist/apply": pack_assist_apply,
    "/pack-knowledge/update": pack_knowledge_update,
    "/pack-knowledge/delete": pack_knowledge_delete,
    "/pack-knowledge/create": pack_knowledge_create,
    "/pack-memory/update": pack_memory_update,
    "/pack-file/update": pack_file_update,
    "/pack-file/delete": pack_file_delete,
    "/snapshot/create": snapshot_create,
    "/snapshot/delete": snapshot_delete,
    "/snapshot/restore": snapshot_restore,
    # 语音插件（独立子系统 app/voice/，只在这里接线；见 docs/工具/tts.md）
    "/voice/tts": voice_tts,
    "/voice/plugin": voice_plugin_action,
}


for _a_path, _a_ep in _AUTH_PROXY_MAP.items():
    POST_ROUTES[_a_path] = _mk_auth_proxy(_a_ep)


GET_ROUTES = {
    "/check-key": check_key,
    "/config": get_config,
    "/models": get_models,
    "/modes": get_modes,
    "/sync/manifest": sync_manifest,
    "/image": get_image,
    "/time": get_time,
    "/auth/state": auth_state,
    "/chat-stage": get_chat_stage,
    "/metrics": get_metrics,
    "/balance": get_balance,
    "/requests": get_requests,
    "/pipeline": get_pipeline,
    "/history": get_history,
    "/wake-status": get_wake_status,
    "/stickers": get_stickers,
    "/character-files": get_character_files,
    "/pack-files": get_pack_files,
    "/pack-structure": pack_structure,
    "/pack-knowledge": pack_knowledge_list,
    "/pack-knowledge/file": pack_knowledge_read,
    "/pack-memory": pack_memory_read,
    "/pack-file": pack_file_read,
    "/user-memory": get_user_memory,
    "/memory-status": get_memory_status,
    "/hotupdate/status": hotupdate_status,
    "/notice": notice_get,
    "/notice-image": notice_image,
    "/archive": get_archive,
    "/journal": get_journal,
    "/export-data": export_data,
    # 诊断包导出（本地导出，不经服务器；见 app/api/diag.py）
    "/export-diagnostics": export_diagnostics,
    "/backups": backups_list,
    "/snapshot/list": snapshot_list,
    "/snapshot/download": snapshot_download,
    "/setting-fix/status": setting_fix_status,
    "/assets/index": assets_index,
    "/assets/raw": assets_raw,
    "/favorites": get_favorites,
    # 语音插件（独立子系统 app/voice/）
    "/voice/status": voice_status,
    "/voice-file": voice_file,
    "/voice/plugin": voice_plugin,
}
