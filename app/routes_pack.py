# -*- coding: utf-8 -*-
"""角色包管理路由（routes.py 拆分）：
包文件编辑（设定放权）、包资产上传/恢复、包文件清单、自建包创建/删除。

边界（2026-09-08 用户拍板）：
- 角色卡 = 基础设定：core/identity/sms_samples/prompts/封面/头像/知识库/开场/表情包
- 用户数据 = 互动产生：对话/记忆/手账/收藏——不在本文件职责内
- 核心三件（core/identity/sms_samples）2026-09-08 起放权可编辑（保存即生效：
  clear_cache 全清角色设定缓存，缓存按内容哈希自然重建）"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from modules import app_config as cfg
from modules.app_config import DEFAULT_MODE

from routes_common import _read_json, _body_mode, _query_mode, _is_server

logger = logging.getLogger(__name__)

# 可经 API 编辑的包文件（用户设定 + 核心三件 + 六个提示词段）
_PACK_PROMPT_FILES = ("prompts/polisher.md", "prompts/analyzer_extra.md",
                      "prompts/organizer_sticker.md", "prompts/organizer_narration.md",
                      "prompts/proactive_context.md", "prompts/env_suffix.md")
_PACK_CORE_FILES = ("core.md", "identity.md", "sms_samples.md")
_EDITABLE_FILES = frozenset({"用户设定.md"} | set(_PACK_CORE_FILES) | set(_PACK_PROMPT_FILES))

_PACK_ASSET_SLOTS = ("avatar", "cover")   # 头像 / 封面
_PACK_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def character_file_update(h):
    body = _read_json(h)
    mode = _body_mode(body)
    filename = (body.get("filename") or "").strip()
    content = (body.get("content") or "")
    if filename not in _EDITABLE_FILES:
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


def _no_bundled_fallback(mode: str) -> bool:
    """该包是否存在 bundled 版本可回退（F-2，2026-09-10）。

    自建包（{user}/custom_x/character/）没有 bundled 对应物 → 它自己的文件就是**唯一副本**。
    此时"恢复默认"实际等于**永久删除该文件**（删完 load_slot 读空，角色设定变空），
    而按钮文案却是"恢复默认"，属误导性不可逆操作。此函数用于在删除前拦下来。"""
    try:
        return not (cfg.bundled_character_dir(mode) / "preset.json").exists()
    except Exception:
        return False


def delete_character_file(h):
    """POST /character-file/delete：删除文案用户副本（恢复 bundled 默认）。白名单同 update。

    2026-09-10（F-2）：自建包无可回退的默认内容 → 拒绝删除（文件即唯一副本）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    filename = (body.get("filename") or "").strip()
    if filename not in _EDITABLE_FILES:
        h._json({"ok": False, "error": f"不允许的文件: {filename}"}); return
    if _no_bundled_fallback(mode):
        h._json({"ok": False, "error": "自建角色包没有可恢复的默认内容——"
                                       "删除即永久清空该文件。如确需清空，请直接编辑内容。"}); return
    fp = cfg.mode_character_dir(mode) / filename
    existed = fp.exists()
    try:
        fp.unlink(missing_ok=True)
    except OSError as e:
        h._json({"ok": False, "error": f"删除失败: {e}"}); return
    from modules.llm_base import clear_cache
    clear_cache()
    h._json({"ok": True, "filename": filename, "existed": existed})


def get_pack_files(h):
    """GET /pack-files?mode=：包详情页数据——可编辑文案（用户副本优先，含是否已覆盖标记）
    + 视觉资产当前 URL + 知识库文件清单（只读）。"""
    from modules.llm_base import resolve_character_file
    mode = _query_mode(h)
    files = []
    for fname in _PACK_CORE_FILES + ("用户设定.md",) + _PACK_PROMPT_FILES:
        user_fp = cfg.mode_character_dir(mode) / fname
        fp = resolve_character_file(fname, mode)
        content = fp.read_text(encoding="utf-8") if fp.exists() else ""
        # customized = 用户副本与 bundled 内容不同（首启拷贝不算用户修改）
        customized = False
        if user_fp.exists():
            try:
                user_text = user_fp.read_text(encoding="utf-8")
                bundled_fp = cfg.bundled_character_dir(mode) / fname
                bundled_text = bundled_fp.read_text(encoding="utf-8") if bundled_fp.exists() else ""
                customized = user_text.strip() != bundled_text.strip()
            except OSError:
                customized = True
        files.append({"name": fname, "content": content,
                      "customized": customized})
    p = cfg.PRESETS.get(mode) or {}

    def _slot_url(slot: str) -> str:
        for base in (cfg.mode_character_dir(mode) / "assets",
                     cfg.bundled_character_dir(mode) / "assets"):
            if base.is_dir():
                for fp in sorted(base.glob(f"{slot}.*")):
                    if fp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                        return f"/assets/character/{mode}/assets/{fp.name}"
        return ""

    # 知识库清单（内置包只读浏览；自建包可编辑由 character-file 系列另行开放）
    knowledge = []
    from modules.llm_retriever import _source_dirs
    for d in _source_dirs(mode):
        if not d.is_dir():
            continue
        for fp in sorted(d.rglob("*.md")):
            knowledge.append(str(fp.relative_to(d)))

    h._json({
        "mode": mode, "name": p.get("name") or mode, "presentation": p.get("presentation", ""),
        "custom": bool(p.get("custom")),
        "files": files,
        "assets": {"avatar": _slot_url("avatar"), "cover": _slot_url("cover")},
        "knowledge": knowledge,
        "proactive": _pack_proactive_view(mode),
    })


# 主动消息包级配置（主动消息随角色走；详情页编辑）
def _pack_proactive_view(mode: str) -> dict:
    return {
        "enabled": bool(cfg.pack_cfg(mode, "proactive_enabled", True)),
        "hard": int(cfg.pack_cfg(mode, "proactive_hard", 6)),
        "soft": float(cfg.pack_cfg(mode, "proactive_soft", 0.35)),
        "prob_enabled": bool(cfg.pack_cfg(mode, "prob_reply_enabled", True)),
        "prob_value": float(cfg.pack_cfg(mode, "prob_reply_value", 0.10)),
        "hidden_enabled": bool(cfg.pack_cfg(mode, "hidden_reply_enabled", True)),
    }


def set_pack_config(h):
    """POST /pack-config：写包级主动消息配置（仅白名单字段，逐个校验）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    updates = {}
    if "enabled" in body:
        updates["proactive_enabled"] = bool(body["enabled"])
    if "hard" in body:
        try:
            updates["proactive_hard"] = max(1, min(10, int(body["hard"])))
        except (TypeError, ValueError):
            h._json({"ok": False, "error": "hard 必须为 1-10 整数"}); return
    if "soft" in body:
        try:
            updates["proactive_soft"] = max(0.0, min(1.0, float(body["soft"])))
        except (TypeError, ValueError):
            h._json({"ok": False, "error": "soft 必须为 0-1 数值"}); return
    if "prob_enabled" in body:
        updates["prob_reply_enabled"] = bool(body["prob_enabled"])
    if "prob_value" in body:
        try:
            updates["prob_reply_value"] = max(0.0, min(1.0, float(body["prob_value"])))
        except (TypeError, ValueError):
            h._json({"ok": False, "error": "prob_value 必须为 0-1 数值"}); return
    if "hidden_enabled" in body:
        updates["hidden_reply_enabled"] = bool(body["hidden_enabled"])
    if not updates:
        h._json({"ok": False, "error": "没有可更新的字段"}); return
    cfg.set_pack_cfg(mode, updates)
    h._json({"ok": True, "proactive": _pack_proactive_view(mode)})


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
    """POST /pack-asset/delete：删除包资产用户副本（恢复 bundled 默认）。

    2026-09-10（F-2）：自建包无可回退的默认资产 → 拒绝删除（该图即唯一副本）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    slot = (body.get("slot") or "").strip()
    if slot not in _PACK_ASSET_SLOTS:
        h._json({"ok": False, "error": "slot 必须为 avatar/cover"}); return
    if _no_bundled_fallback(mode):
        h._json({"ok": False, "error": "自建角色包没有可恢复的默认图片——"
                                       "删除后该位置将无头像/封面。如确需更换请直接上传新图。"}); return
    d = cfg.mode_character_dir(mode) / "assets"
    removed = 0
    if d.is_dir():
        for fp in d.glob(f"{slot}.*"):
            try:
                fp.unlink(); removed += 1
            except OSError:
                pass
    h._json({"ok": True, "slot": slot, "removed": removed})


# ═══ 自建角色包（仅本地版——服务器版不做自定义整包）═══
_PACK_CORE_TPL = "# {char_name} · 核心设定\n\n（在这里写：她是谁——身份、经历、价值观。每条一行，以 [事实] 开头）\n"
_PACK_IDENTITY_TPL = "# {char_name} · 人际关系与认知边界\n\n（在这里写：她与{user_name}的关系、习惯与喜好、她知道什么不知道什么）\n"
_PACK_SAMPLES_TPL = "# {char_name} · 短信风格示例\n\n（在这里贴几条她说的话的示例，模型会模仿这个语气。越真实越好）\n"


def create_pack(h):
    """POST /pack-create：新建自定义角色包（仅本地版）。
    包目录 = user_data/{id}/（preset.json 在 character/ 下，与读取语义对齐——
    自建包的全部内容即用户副本，天然可编辑）。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持自建角色包"}); return
    body = _read_json(h)
    name = str(body.get("name") or "").strip()[:30]
    cname = str(body.get("char_name") or "").strip()[:20]
    uname = str(body.get("user_name") or "").strip()[:20]
    presentation = str(body.get("presentation") or "sticker").strip()
    if presentation not in ("sticker", "narration", "none"):
        presentation = "sticker"
    if not name or not cname or not uname:
        h._json({"ok": False, "error": "包名称、角色名、对方称呼都必填"}); return
    pid = str(body.get("id") or "").strip().lower()
    if not pid:
        import uuid as _uuid
        pid = "custom_" + _uuid.uuid4().hex[:8]
    if not _PACK_ID_RE.fullmatch(pid):
        h._json({"ok": False, "error": "包 id 只能是小写字母/数字/下划线/短横线"}); return
    if pid in cfg.PRESETS or (cfg.USER_DIR / pid).exists():
        h._json({"ok": False, "error": "包 id 已存在"}); return
    cdir = cfg.USER_DIR / pid / "character"
    pack_dir = cfg.USER_DIR / pid
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "preset.json").write_text(json.dumps({
            "id": pid, "name": name, "char_name": cname, "user_name": uname,
            "presentation": presentation,
            "desc": str(body.get("desc") or "").strip()[:60],
            "tagline": str(body.get("tagline") or "").strip()[:60],
            "schema": cfg.PRESET_SCHEMA,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        from modules.polisher import _EMERGENCY_PERSONA
        (cdir / "prompts").mkdir(exist_ok=True)
        (cdir / "prompts" / "polisher.md").write_text(_EMERGENCY_PERSONA, encoding="utf-8")
        (cdir / "core.md").write_text(_PACK_CORE_TPL.format(char_name=cname, user_name=uname), encoding="utf-8")
        (cdir / "identity.md").write_text(_PACK_IDENTITY_TPL.format(char_name=cname, user_name=uname), encoding="utf-8")
        (cdir / "sms_samples.md").write_text(_PACK_SAMPLES_TPL.format(char_name=cname), encoding="utf-8")
        # 阶段 3.2：包存在性以 packs.json 为权威 —— 先登记再重扫（顺序铁律：reload 是从清单读的）
        cfg.pack_registry().register(pid, source="custom")
        cfg.reload_presets()
    except Exception as e:
        # R-09（2026-09-13）：建包中途失败必须把已建目录删掉。原来失败就撒手，
        # 留下一个**孤儿包目录**：没注册进 PRESETS（或只注册了一半），列表里看不见、
        # 没有任何管理入口，却占着这个 id 让用户无法用同名重建（真实案例：
        # user_data/custom_f447527f/ 只剩一个空 character/）。
        import shutil
        shutil.rmtree(pack_dir, ignore_errors=True)
        try:
            cfg.pack_registry().unregister(pid)   # 3.2：清单里也不留半注册条目
            cfg.reload_presets()      # 目录已删，重扫一遍清掉可能的半注册状态
        except Exception:
            pass
        logger.warning("自建角色包创建失败，已回滚目录 %s: %s", pid, e)
        h._json({"ok": False, "error": f"创建失败（已回滚，未留下残留目录）: {e}"}); return
    logger.info("自建角色包创建: %s（%s）", pid, name)
    h._json({"ok": True, "id": pid, "name": name})


# ═══ AI 建卡向导（搜索 + 分步生成；仅本地版）═══

def pack_forge_start(h):
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持"}); return
    client = cfg.get_client()
    if not client:
        h._json({"ok": False, "error": "请先设置 API Key"}); return
    from modules.pack_forge import forge_start
    h._json(forge_start(client, _read_json(h)))


def pack_forge_next(h):
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持"}); return
    from modules.pack_forge import forge_next
    h._json(forge_next(_read_json(h)))


def pack_forge_finish(h):
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持"}); return
    from modules.pack_forge import forge_finish
    h._json(forge_finish(_read_json(h)))


def _is_orphan_pack_dir(d: Path) -> bool:
    """孤儿包目录判定（R-09，2026-09-13）：目录存在、不含任何文件，且除空的 character/ 外
    没有别的子目录。

    为什么要求这么严：这是"删除"分支的准入条件，只允许清掉**确定啥也没有**的残留
    （建包中途失败、早期版本残留）。用户手工塞过东西的目录一律拒绝——
    宁可留着让用户自己处理，也不能替用户删数据。"""
    try:
        if not d.is_dir():
            return False
        for p in d.rglob("*"):
            if p.is_file():
                return False
            if p.is_dir() and p.relative_to(d).as_posix() != "character":
                return False
        return True
    except OSError:
        return False


def delete_pack(h):
    """POST /pack-delete：删除自定义角色包（仅本地版；内置包 story/haruno 拒绝）。
    连数据一起删（user_data/{id}/ 整个目录）——前端已二次确认。

    R-09（2026-09-13）：不再要求"必须已注册"。孤儿包目录（建包中途失败/早期版本残留）
    不在注册表里，前端列表看不见、没有任何管理入口，却占着 id 让用户无法同名重建——
    允许在"确认啥也没有"（空目录或只剩空 character/）时清掉。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持自建角色包"}); return
    body = _read_json(h)
    pid = str(body.get("id") or "").strip()
    if not _PACK_ID_RE.fullmatch(pid):
        h._json({"ok": False, "error": "非法包 id"}); return
    target = cfg.USER_DIR / pid
    p = cfg.PRESETS.get(pid) or {}
    if pid in cfg.PRESETS:
        if not p.get("custom"):
            h._json({"ok": False, "error": "内置包不能删除"}); return
    else:
        # 未注册：只允许删"孤儿残留"，不碰可能有内容的目录
        if not _is_orphan_pack_dir(target):
            h._json({"ok": False, "error": "该包未注册且目录非空，为免误删数据已拒绝"}); return
        logger.info("清理孤儿包目录: %s", pid)
    import shutil
    shutil.rmtree(target, ignore_errors=True)
    cfg.pack_registry().unregister(pid)   # 3.2：目录删除与清单注销成对出现
    cfg.reload_presets()
    logger.info("自建角色包删除: %s", pid)
    h._json({"ok": True, "id": pid})
