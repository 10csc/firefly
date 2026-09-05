# -*- coding: utf-8 -*-
"""资产与媒体路由（routes.py 拆分产物，纯重构无行为变化）：
表情包增删改查、角色设定文件、图片上传/读取、资产清单/下载、收藏夹。"""

import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from modules import app_config as cfg
from modules.app_config import DEFAULT_MODE

from routes_common import (_read_json, _body_mode, _query_mode, _is_server,
                           _image_dir, _image_quota_bytes, _image_used_bytes,
                           _IMG_ID_RE)

logger = logging.getLogger(__name__)


def upload_image(h):
    """POST /upload-image（A9）：接收压缩图字节 → 落盘 {mode}/images/。
    2026-08-29 变更：不再生成/要求文字描述，图片理解统一由模型原生识图（发图当轮
    注入原图给回复器）；模型真不支持识图时由上游拒绝 → polisher 降级说明，前端不弹窗。
    铁律修订：原图不出设备，服务器只存压缩图——服务器版同样落盘
    （cfg.mode_root 经 _user_ctx 按用户目录隔离）。配额：每用户 FIREFLY_IMAGE_QUOTA_MB
    （默认 200MB），统计该用户所有模式 images/ 总字节，超限拒绝。"""
    import uuid as _uuid
    # 局部绑定：call-time 从 routes 取，保持测试改写 routes.parse_multipart 的拦截面不变
    from routes import parse_multipart
    fields, files = parse_multipart(h, max_bytes=11 * 1024 * 1024)
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少图片文件"}); return
    data = file_info["data"]
    ext = Path(str(file_info.get("filename") or "")).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp/gif 图片格式"}); return
    if not isinstance(data, bytes) or len(data) > 10 * 1024 * 1024:
        h._json({"ok": False, "error": "图片过大（上限 10MB）"}); return
    if _image_used_bytes() + len(data) > _image_quota_bytes():
        h._json({"ok": False, "error": "图片空间已满，请在数据面板清理"}); return
    img_id = "img_" + _uuid.uuid4().hex[:12]
    fp = _image_dir(mode) / (img_id + ext)
    from modules.storage import atomic_write_bytes
    if not atomic_write_bytes(fp, data):
        h._json({"ok": False, "error": "图片保存失败"}); return
    # 图片理解统一由模型原生识图；desc 恒空（旧数据手填 desc 兼容读法不受影响）
    h._json({"ok": True, "img_id": img_id, "file": fp.name, "desc": "",
             "server_side": _is_server()})


def add_sticker_route(h):
    # multipart/form-data 解析：本地版保存图片到 user_data/stickers/；
    # 服务器版（A2 媒体策略）只传输不保存：校验+哈希 → 仅注册文字元数据（file=local:<sha256>.<ext>，
    # 图片本体由前端存 WebView IndexedDB；其它设备无图 → 占位提示「（表情包已失效）」）
    from tools.sticker_picker import add_sticker, StickerAddError
    # 局部绑定：call-time 从 routes 取，保持测试改写 routes.parse_multipart 的拦截面不变
    from routes import parse_multipart
    try:
        fields, files = parse_multipart(h)
        category = fields.get("category", "")
        label = fields.get("label", "")
        # 归属包（角色预设化阶段6）：前端上传带当前模式；空 = 全局共享
        pack = (fields.get("mode") or "").strip()
        file_info = files.get("file")
        if not file_info:
            h._json({"ok": False, "error": "缺少图片文件"}); return
        if category not in ("可爱", "帅气"):
            h._json({"ok": False, "error": "分类必须为 可爱/帅气"}); return
        if not label:
            h._json({"ok": False, "error": "缺少含义描述"}); return

        if _is_server():
            import hashlib
            original = file_info["filename"] or "sticker"
            ext = Path(original).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp/gif 图片格式"}); return
            data = file_info["data"]
            if not isinstance(data, bytes) or len(data) > 10 * 1024 * 1024:
                h._json({"ok": False, "error": "图片过大（上限 10MB）"}); return
            # 服务器对图片字节只传输不保存：仅登记元数据（内容哈希供客户端索引
            # IndexedDB；label 即图片文字描述，已入 LLM 上下文）
            digest = hashlib.sha256(data).hexdigest()
            entry = add_sticker(f"local:{digest}{ext}", category, label, pack=pack)
            h._json({"ok": True, "id": entry.id, "label": entry.label,
                     "file": entry.file, "local": True,
                     "note": "图片已存于本机（服务器不保存图片）"})
            return

        # 保存图片：扩展名白名单（防 .html/.svg 落盘后被静态服务按 MIME 回吐成存储型 XSS），
        # 随机后缀防同秒同名碰撞覆盖
        import secrets
        original = file_info["filename"]
        ext = Path(original).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp/gif 图片格式"}); return
        safe_name = f"user_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        # 图片文件与注册表同作用域：服务器版写入 user_data/{uid}/stickers/（账号隔离），
        # 本地版退回 USER_DIR/stickers（行为不变）
        from tools.sticker_picker import _user_registry_file
        save_dir = _user_registry_file().parent
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / safe_name).write_bytes(file_info["data"])

        # 写入注册表
        entry = add_sticker(f"stickers/{safe_name}", category, label, pack=pack)
        h._json({"ok": True, "sticker_id": entry.id, "label": entry.label})
    except StickerAddError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"上传失败: {e}"})


def sticker_update(h):
    from tools.sticker_picker import update_sticker, StickerUpdateError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    new_label = (body.get("label") or "").strip() or None
    new_category = (body.get("category") or "").strip() or None
    new_enabled = body.get("enabled")
    if new_enabled is not None and not isinstance(new_enabled, bool):
        h._json({"ok": False, "error": "enabled 必须是 true/false"}); return
    try:
        entry = update_sticker(sid, new_label=new_label, new_category=new_category,
                               new_enabled=new_enabled)
        h._json({"ok": True, "id": entry.id, "label": entry.label,
                 "category": entry.category, "enabled": entry.enabled})
    except StickerUpdateError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"修改失败: {e}"})


def sticker_delete(h):
    from tools.sticker_picker import delete_sticker, StickerDeleteError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    try:
        delete_sticker(sid)
        h._json({"ok": True, "id": sid})
    except StickerDeleteError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"删除失败: {e}"})


def character_file_update(h):
    body = _read_json(h)
    mode = _body_mode(body)
    filename = (body.get("filename") or "").strip()
    content = (body.get("content") or "")
    # 白名单：用户设定 + 包提示词段（角色预设化：软件内可编辑包文案）。
    # 核心设定 core/identity/sms_samples 不可经 API 修改（走设定纠错链路）。
    allowed = {"用户设定.md",
               "prompts/polisher.md", "prompts/analyzer_extra.md",
               "prompts/organizer_sticker.md", "prompts/organizer_narration.md",
               "prompts/proactive_context.md", "prompts/env_suffix.md"}
    if filename not in allowed:
        h._json({"ok": False, "error": f"不允许的文件: {filename}"}); return
    if not content:
        h._json({"ok": False, "error": "内容不能为空"}); return
    try:
        filepath = cfg.mode_character_dir(mode) / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        # 清除各模块的角色设定缓存（含包提示词段缓存——render_pack_prompt 走同一缓存）
        from modules.llm_base import clear_cache
        clear_cache()
        from modules.polisher import clear_samples_cache
        clear_samples_cache()
        h._json({"ok": True, "filename": filename})
    except Exception as e:
        h._json({"ok": False, "error": f"保存失败: {e}"})


def get_image(h):
    """GET /image?id=<img_id>（A9）：图片字节服务（用户目录 {mode}/images/，按 ext 给 MIME）。
    服务器版同样服务（铁律修订：服务器只存压缩图；cfg.mode_root 经 _user_ctx 按用户隔离）。"""
    qs = parse_qs(urlparse(h.path).query)
    img_id = (qs.get("id", [""])[0] or "").strip()[:200]
    if not _IMG_ID_RE.fullmatch(img_id):   # 与 _load_image_data_url 同一白名单（安全审查 2026-08-25）
        h._json({"error": "非法图片 id"}, 400)
        return
    mode = (qs.get("mode", [DEFAULT_MODE])[0] or DEFAULT_MODE)
    mode = mode if mode in cfg.MODES else DEFAULT_MODE
    from modules.vision import EXT_MIME
    d = _image_dir(mode)
    for fp in sorted(d.glob(img_id + ".*")):
        mime = EXT_MIME.get(fp.suffix.lower())
        if not mime:
            continue
        try:
            data = fp.read_bytes()
            h.send_response(200)
            h.send_header("Content-Type", mime)
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return
        except OSError:
            continue
    h._json({"error": "图片不存在"}, 404)


def get_stickers(h):
    # 返回表情包列表。?enabled=1 只返回启用项（聊天页选择面板）；
    # 不带参数返回全量（管理页，含停用项）。editable：当前用户可改/删的条目。
    from tools.sticker_picker import list_all_stickers, _STICKERS_DEFAULT, editable_ids
    qs = parse_qs(urlparse(h.path).query)
    enabled_only = qs.get("enabled", [""])[0] == "1"
    ids = editable_ids()
    editable_all = not cfg.user_scope_key()   # 本地版（无用户上下文）全部可编辑
    items = list_all_stickers()
    if enabled_only:
        items = [s for s in items if s.enabled]
    h._json({
        "stickers": [
            {"id": s.id, "file": s.file, "category": s.category,
             "label": s.label, "enabled": bool(s.enabled), "pack": s.pack,
             "is_default": s.id in _STICKERS_DEFAULT,
             "editable": editable_all or s.id in ids}
            for s in items
        ],
    })


def get_character_files(h):
    from modules.llm_base import resolve_character_file
    mode = _query_mode(h)
    files = []
    # 核心设定已隐藏，仅暴露用户可维护的补充设定文件
    for fname in ("用户设定.md",):
        fp = resolve_character_file(fname, mode)
        if fp.exists():
            files.append({"name": fname, "content": fp.read_text(encoding="utf-8")})
    h._json({"files": files})


# ═══ 角色包管理（阶段6：软件内修改包资产）═══
# 可编辑的包提示词段（与 character_file_update 白名单同一份语义）
_PACK_PROMPT_FILES = ("prompts/polisher.md", "prompts/analyzer_extra.md",
                      "prompts/organizer_sticker.md", "prompts/organizer_narration.md",
                      "prompts/proactive_context.md", "prompts/env_suffix.md")
_PACK_ASSET_SLOTS = ("avatar", "cover")   # 头像 / 封面


def get_pack_files(h):
    """GET /pack-files?mode=：包管理页数据——可编辑文案（读用户副本优先，含是否已覆盖标记）
    + 视觉资产当前 URL。"""
    from modules.llm_base import resolve_character_file
    mode = _query_mode(h)
    files = []
    for fname in ("用户设定.md",) + _PACK_PROMPT_FILES:
        user_fp = cfg.mode_character_dir(mode) / fname
        fp = resolve_character_file(fname, mode)
        content = fp.read_text(encoding="utf-8") if fp.exists() else ""
        files.append({"name": fname, "content": content,
                      "customized": user_fp.exists()})
    p = cfg.PRESETS.get(mode) or {}

    def _slot_url(slot: str) -> str:
        for base in (cfg.mode_character_dir(mode) / "assets",
                     cfg.bundled_character_dir(mode) / "assets"):
            if base.is_dir():
                for fp in sorted(base.glob(f"{slot}.*")):
                    if fp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                        return f"/assets/character/{mode}/assets/{fp.name}"
        return ""

    h._json({
        "mode": mode, "name": p.get("name") or mode, "presentation": p.get("presentation", ""),
        "files": files,
        "assets": {"avatar": _slot_url("avatar"), "cover": _slot_url("cover")},
    })


def upload_pack_asset(h):
    """POST /pack-asset（multipart: mode + slot + file）：上传包头像/封面。
    落 user_data/{mode}/character/assets/（用户副本，resolve_asset 优先于 bundled；
    恢复默认 = POST /pack-asset/delete 删副本）。"""
    from routes import parse_multipart
    fields, files = parse_multipart(h, max_bytes=11 * 1024 * 1024)
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    slot = (fields.get("slot") or "").strip()
    if slot not in _PACK_ASSET_SLOTS:
        h._json({"ok": False, "error": "slot 必须为 avatar/cover"}); return
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少图片文件"}); return
    data = file_info["data"]
    ext = Path(str(file_info.get("filename") or "")).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp 图片格式"}); return
    if not isinstance(data, bytes) or len(data) > 5 * 1024 * 1024:
        h._json({"ok": False, "error": "图片过大（上限 5MB）"}); return
    # 按原扩展名落盘（避免扩展名与内容不符导致 MIME 错误）；
    # 同名槽位只留一份（清掉旧的其他扩展名副本）
    d = cfg.mode_character_dir(mode) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    from modules.storage import atomic_write_bytes
    for old in d.glob(f"{slot}.*"):
        if old.suffix.lower() != ext:
            try: old.unlink()
            except OSError: pass
    fp = d / f"{slot}{ext}"
    if not atomic_write_bytes(fp, data):
        h._json({"ok": False, "error": "图片保存失败"}); return
    h._json({"ok": True, "slot": slot,
             "url": f"/assets/character/{mode}/assets/{slot}{ext}"})


def delete_pack_asset(h):
    """POST /pack-asset/delete：删除包资产用户副本（恢复 bundled 默认）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    slot = (body.get("slot") or "").strip()
    if slot not in _PACK_ASSET_SLOTS:
        h._json({"ok": False, "error": "slot 必须为 avatar/cover"}); return
    d = cfg.mode_character_dir(mode) / "assets"
    removed = 0
    if d.is_dir():
        for fp in d.glob(f"{slot}.*"):
            try:
                fp.unlink(); removed += 1
            except OSError:
                pass
    h._json({"ok": True, "slot": slot, "removed": removed})


def delete_character_file(h):
    """POST /character-file/delete：删除文案用户副本（恢复 bundled 默认）。
    白名单与 character_file_update 相同。"""
    body = _read_json(h)
    mode = _body_mode(body)
    filename = (body.get("filename") or "").strip()
    allowed = {"用户设定.md"} | set(_PACK_PROMPT_FILES)
    if filename not in allowed:
        h._json({"ok": False, "error": f"不允许的文件: {filename}"}); return
    fp = cfg.mode_character_dir(mode) / filename
    existed = fp.exists()
    try:
        fp.unlink(missing_ok=True)
    except OSError as e:
        h._json({"ok": False, "error": f"删除失败: {e}"}); return
    from modules.llm_base import clear_cache
    clear_cache()
    h._json({"ok": True, "filename": filename, "existed": existed})


# ═══ 收藏夹（长按消息 → 收藏）═══
def add_favorite_route(h):
    from modules.favorite_store import add_favorite, FavoriteError
    body = _read_json(h)
    mode = _body_mode(body)
    try:
        rec = add_favorite(mode, body.get("message"))
    except FavoriteError as e:
        h._json({"ok": False, "error": str(e)})
        return
    except Exception as e:
        logger.warning("收藏失败: %s", e)
        h._json({"ok": False, "error": "收藏失败，请重试"})
        return
    h._json({"ok": True, "id": rec["id"]})


def get_favorites(h):
    from modules.favorite_store import list_favorites
    h._json({"items": list_favorites(_query_mode(h))})


def delete_favorite_route(h):
    from modules.favorite_store import delete_favorite
    body = _read_json(h)
    mode = _body_mode(body)
    fav_id = (body.get("id") or "").strip()
    if not fav_id:
        h._json({"ok": False, "error": "缺少收藏 id"})
        return
    ok = delete_favorite(mode, fav_id)
    h._json({"ok": ok, "error": "" if ok else "收藏不存在或已删除"})


# ══ 资产清单（服务器告诉 APP 用哪些资产）═══════════
_ASSET_MD5_CACHE: dict[str, str] = {}


_ASSET_MD5_LOCK = threading.Lock()


def _asset_md5(text: str) -> str:
    """资产版本指纹（内容 hash 前 8 位，内容变化即版本变化）。"""
    import hashlib
    with _ASSET_MD5_LOCK:
        key = text[:64]
        if key in _ASSET_MD5_CACHE:
            return _ASSET_MD5_CACHE[key]
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
        _ASSET_MD5_CACHE[key] = digest
        return digest


def assets_index(h):
    """资产清单：服务器"告诉 APP 要用哪些资产"（版本指纹+大小）。
    APP 比对本地版本，缺失/过期则从 /assets/raw 下载。?mode=story|haruno（默认 story）。"""
    from modules.llm_retriever import _load_knowledge, get_knowledge_stats
    from modules.llm_base import resolve_character_file

    mode = _query_mode(h)
    kb = _load_knowledge(mode)
    stats = get_knowledge_stats(mode)

    def _char_asset(name):
        # resolve_character_file 不拼后缀（load_slot 才拼），这里显式拼 .md
        fp = resolve_character_file(name + ".md", mode)
        if fp.exists():
            content = fp.read_text(encoding="utf-8")
            return {"version": _asset_md5(content), "size": len(content)}
        return {"version": "0", "size": 0}

    h._json({
        "mode": mode,
        "knowledge": {"version": _asset_md5(kb), "size": len(kb),
                      "chars": stats.get("chars", 0)},
        "character": {
            "core": _char_asset("core"),
            "identity": _char_asset("identity"),
            "sms_samples": _char_asset("sms_samples"),
        },
    })


def assets_raw(h):
    """资产下载（认证后可用）：APP 首次本地化 / 更新时拉取。
    ?name=knowledge|core|identity|sms_samples&mode=story|haruno"""
    from modules.llm_retriever import _load_knowledge
    from modules.llm_base import resolve_character_file
    qs = parse_qs(urlparse(h.path).query)
    name = qs.get("name", [""])[0]
    mode = (qs.get("mode", [DEFAULT_MODE])[0] or DEFAULT_MODE)
    mode = mode if mode in cfg.MODES else DEFAULT_MODE
    if name == "knowledge":
        h._json({"name": name, "content": _load_knowledge(mode)})
        return
    if name in ("core", "identity", "sms_samples"):
        fp = resolve_character_file(name + ".md", mode)
        if fp.exists():
            h._json({"name": name, "content": fp.read_text(encoding="utf-8")})
            return
        h._json({"error": "资产不存在"}, 404)
        return
    h._json({"error": "未知资产"}, 404)
