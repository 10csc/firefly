# -*- coding: utf-8 -*-
"""角色卡管理路由（routes.py 拆分）：
卡文件编辑（设定放权）、卡资产上传/恢复、卡文件清单、自建卡创建/删除。

命名统一（2026-09-18）：入口统称「角色卡管理」——聊天产生的私有数据在菜单页
（设定文件/收藏）管理，本文件职责是卡本身。唯一的例外是「彻底删除」，它连
user_data/{id}/ 整个目录（含私有聊天数据）一起删，见 api/pack_lifecycle.delete_pack。

边界（2026-09-08 用户拍板）：
- 角色卡 = 基础设定：core/identity/sms_samples/prompts/封面/头像/知识库/开场/表情包
- 用户数据 = 互动产生：对话/记忆/手账/收藏——不在本文件职责内
- 核心三件（core/identity/sms_samples）2026-09-08 起放权可编辑（保存即生效：
  clear_cache 全清角色设定缓存，缓存按内容哈希自然重建）"""

import logging
import re
from pathlib import Path

from modules import app_config as cfg
from modules.app_config import DEFAULT_MODE
from routes_common import _read_json, _body_mode, _query_mode

logger = logging.getLogger(__name__)

# 可经 API 编辑的包文件（用户设定 + 核心三件 + 六个提示词段）
# 3.3：白名单与"槽位三态"的记录口径必须**同一份**（否则 API 能写的文件与能被标记的文件会漂移），
# 故取自 `core.presets.PACK_SLOT_FILES`；下面两个元组只用于 /pack-files 的展示顺序。
_PACK_PROMPT_FILES = ("prompts/polisher.md", "prompts/analyzer_extra.md",
                      "prompts/organizer_sticker.md", "prompts/organizer_narration.md",
                      "prompts/proactive_context.md", "prompts/env_suffix.md")
_PACK_CORE_FILES = ("core.md", "identity.md", "sms_samples.md")
_EDITABLE_FILES = frozenset(cfg.PACK_SLOT_FILES)

_PACK_ASSET_SLOTS = ("avatar", "cover", "user_avatar")   # 角色头像 / 封面 / 用户形象（05）
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
        # 3.3：用户真的写了内容 → 该槽位标记 customized（判定不再靠事后逐字比对，
        # 否则 bundled 后续更新会把"用户改过"和"只是旧内容"混为一谈）
        try:
            cfg.pack_registry().mark_slot(mode, filename, "customized")
        except Exception as e:
            logger.warning("槽位三态标记失败（不影响保存）: %s", e)
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
    # 3.3：副本没了 → 读取回落到包自带内容，"已修改"随之消失（记 inherited 而不是 baseline：
    # 该槽位确实被动过——用户副本存在过，这与"从未有过副本"的出厂状态不同）
    try:
        cfg.pack_registry().mark_slot(mode, filename, "inherited")
    except Exception as e:
        logger.warning("槽位三态标记失败（不影响删除）: %s", e)
    h._json({"ok": True, "filename": filename, "existed": existed})


def get_pack_files(h):
    """GET /pack-files?mode=：包详情页数据——可编辑文案（用户副本优先，含是否已覆盖标记）
    + 视觉资产当前 URL + 知识库文件清单（只读）。

    3.3：`customized` 不再每次现算 diff，而是读 packs.json 的 `slots` 三态
    （`customized` ⇔ state==""customized""）。现算 diff 的毛病：bundled 内容一更新，
    用户从没编辑过的旧副本就被判成"已修改"（且与 `_cleanup_stale_defaults` 的清理口径各写一份）。
    清单里没有记录时（服务器版无清单 / 老盘数据）就地推导并回写，保证首屏即正确。"""
    from modules.llm_base import resolve_character_file
    mode = _query_mode(h)
    reg = None
    try:
        reg = cfg.pack_registry()
    except Exception as e:
        logger.warning("pack_registry 不可用，槽位三态回退就地推导: %s", e)
    files = []
    for fname in _PACK_CORE_FILES + ("用户设定.md",) + _PACK_PROMPT_FILES:
        user_fp = cfg.mode_character_dir(mode) / fname
        fp = resolve_character_file(fname, mode)
        content = fp.read_text(encoding="utf-8") if fp.exists() else ""
        state = (reg.get_slot(mode, fname) if reg else None)
        if state is None:
            # 无记录（服务器版无清单 / 启动链还没跑）：就地推导，首屏即正确。
            # persist=False —— 这是**读路径**，不写盘（2.6 的口径：读操作不改盘），
            # 落盘交给启动链 `run_startup_init() → refresh_slots()` 与两个写路径。
            from core.presets import slot_state_from_disk
            state = slot_state_from_disk(mode, fname, None)
            if reg is not None:
                reg.mark_slot(mode, fname, state, persist=False)
        files.append({"name": fname, "content": content,
                      "customized": state == "customized", "slot_state": state})
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

    # 包状态（3.5）：详情页据此决定给「归档」还是「恢复 / 彻底删除」。
    # 未注册（服务器版 / 目录自愈还没登记）→ 按 active 呈现，不假装是归档包。
    _meta = {}
    try:
        _meta = cfg.pack_registry().get(mode) or {}
    except Exception:
        _meta = {}
    h._json({
        "mode": mode, "name": p.get("name") or mode, "presentation": p.get("presentation", ""),
        "custom": bool(p.get("custom")),
        "state": _meta.get("state") or ("active" if (p.get("custom") or mode in cfg.MODES) else ""),
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
    # 05 用户形象入包：用户称呼（包级覆盖，最长 20 字）
    if "user_name" in body:
        un = str(body.get("user_name") or "").strip()[:20]
        updates["user_name"] = un   # 空串 = 清除覆盖（读链回落 preset 声明）
    if not updates:
        h._json({"ok": False, "error": "没有可更新的字段"}); return
    cfg.set_pack_cfg(mode, updates)
    resp = {"ok": True, "proactive": _pack_proactive_view(mode)}
    if "user_name" in updates:
        resp["user_name"] = cfg.user_name(mode)   # 回读生效值（覆盖被清时回落 preset）
    h._json(resp)


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
        h._json({"ok": False, "error": "slot 必须为 avatar/cover/user_avatar"}); return
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
        h._json({"ok": False, "error": "slot 必须为 avatar/cover/user_avatar"}); return
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
# 阶段 2.8：建包/AI 向导/归档/两级删除的实现已移至 app/api/pack_lifecycle.py。
# 此处 re-export，api/router 与 routes.py 兼容层、测试（`import routes_pack as rp`）的取用路径不变。
# 模板常量保留在本模块：测试经 `rp._PACK_*_TPL = None` 打桩验证建包回滚（test_pack_lifecycle B4/B5），
# pack_lifecycle.create_pack 在**调用时**从本模块读（保持打桩语义）。
_PACK_CORE_TPL = "# {char_name} · 核心设定\n\n（在这里写：{char_name}是谁——身份、经历、价值观。每条一行，以 [事实] 开头）\n"
_PACK_IDENTITY_TPL = "# {char_name} · 人际关系与认知边界\n\n（在这里写：{char_name}与{user_name}的关系、习惯与喜好、{char_name}知道什么不知道什么）\n"
_PACK_SAMPLES_TPL = "# {char_name} · 短信风格示例\n\n（在这里贴几条{char_name}说的话的示例，模型会模仿这个语气。越真实越好）\n"

from api.pack_lifecycle import (   # noqa: F401,E402
    archive_pack, create_pack, delete_pack, restore_pack,
    _force_pack_backup, _is_orphan_pack_dir, _detach_pack_stickers, _pack_target,
)

# re-export 面（pyflakes 的"已使用"标记；见 core/presets.py 同款处置）
__all__ = [
    "character_file_update", "delete_character_file", "get_pack_files", "set_pack_config",
    "upload_pack_asset", "delete_pack_asset",
    "archive_pack", "create_pack", "delete_pack", "restore_pack",
    "_force_pack_backup", "_is_orphan_pack_dir", "_detach_pack_stickers", "_pack_target",
    "_PACK_CORE_TPL", "_PACK_IDENTITY_TPL", "_PACK_SAMPLES_TPL",
    "_EDITABLE_FILES", "_PACK_ASSET_SLOTS", "_no_bundled_fallback",
]


# ═══ 知识库 / 记忆库编辑（阶段 C，2026-09-15）═══
# 读取链与既有槽位同哲学：用户副本（{pack}/character/knowledge/…）优先 → bundled 包内回落。
# 写入一律原子写 + 白名单路径 + 写后清知识缓存（R-06 的失效入口在此落地）。

_KB_ALLOWED_TOP = ("world", "factions", "story", "character", "dialogues")   # 五域（02 规范结构）
_KB_CONTENT_MAX = 500_000        # 单文件写入上限（防巨型文本打爆磁盘）


def _kb_user_root(mode: str) -> Path:
    """知识库用户副本根（写路径唯一落点）。"""
    return cfg.mode_character_dir(mode) / "knowledge"


def _kb_bundled_root(mode: str) -> Path:
    """知识库 bundled 根（包内；不存在则空——haruno 等包目前没有知识库）。"""
    return cfg.bundled_character_dir(mode) / "knowledge"


def _safe_kb_rel(rel: str) -> str | None:
    """知识库相对路径白名单审查：五域子目录下的 .md；拒绝 ../绝对/盘符/控制字符。
    文件名允许中文/空格/括号/句点（"2.0任务对话.md"、"3.8任务对话（超长）.md" 都是合法的）。
    返回归一化相对路径（posix）或 None。"""
    if not isinstance(rel, str) or not rel.strip():
        return None
    r = rel.strip().replace("\\", "/").lstrip("/")
    parts = r.split("/")
    if len(parts) < 2 or parts[0] not in _KB_ALLOWED_TOP:
        return None
    if any(p in ("", ".", "..") for p in parts):
        return None
    name = parts[-1]
    if not name.endswith(".md") or ".." in name or re.search(r"[\x00-\x1f]", name):
        return None
    return r


def _kb_resolve(mode: str, rel: str):
    """读路径解析：用户副本优先 → bundled。返回 (路径, 是否用户副本) 或 (None, False)。"""
    u = _kb_user_root(mode) / rel
    if u.is_file():
        return u, True
    b = _kb_bundled_root(mode) / rel
    if b.is_file():
        return b, False
    return None, False


def pack_knowledge_list(h):
    """GET /pack-knowledge?mode= → {ok, files: [{path, size, customized, user_copy}]}"""
    mode = _query_mode(h)
    out = {}
    for root, is_user in ((_kb_bundled_root(mode), False), (_kb_user_root(mode), True)):
        if not root.is_dir():
            continue
        for fp in sorted(root.rglob("*.md")):
            rel = fp.relative_to(root).as_posix()
            if _safe_kb_rel(rel) is None:
                continue   # 检索器同口径：五域之外/非法名不进列表（与 _load_knowledge 的排除分离：它不管顶层）
            try:
                st = fp.stat()
            except OSError:
                continue
            cur = out.get(rel)
            # 用户副本覆盖 bundled 同名条目（读取链同序）
            if cur is None or is_user:
                out[rel] = {"path": rel, "size": st.st_size, "user_copy": is_user}
    h._json({"ok": True, "mode": mode, "files": sorted(out.values(), key=lambda x: x["path"])})


def pack_knowledge_read(h):
    """GET /pack-knowledge/file?mode=&path= → {ok, path, content, user_copy}"""
    mode = _query_mode(h)
    from urllib.parse import urlparse, parse_qs
    rel = _safe_kb_rel(parse_qs(urlparse(h.path).query).get("path", [""])[0])
    if rel is None:
        h._json({"ok": False, "error": "非法知识库路径"}); return
    fp, is_user = _kb_resolve(mode, rel)
    if fp is None:
        h._json({"ok": False, "error": "文件不存在"}, 404); return
    try:
        h._json({"ok": True, "path": rel, "content": fp.read_text(encoding="utf-8"),
                 "user_copy": is_user})
    except OSError as e:
        h._json({"ok": False, "error": f"读取失败: {e}"}, 500)


def pack_knowledge_update(h):
    """POST /pack-knowledge/update {mode, path, content}：写用户副本（原子写）+ 清知识缓存。"""
    body = _read_json(h)
    mode = _body_mode(body)
    rel = _safe_kb_rel(str(body.get("path") or ""))
    content = body.get("content")
    if rel is None:
        h._json({"ok": False, "error": "非法知识库路径（只允许五域子目录下的 .md）"}); return
    if not isinstance(content, str) or not content.strip():
        h._json({"ok": False, "error": "内容不能为空"}); return
    if len(content) > _KB_CONTENT_MAX:
        h._json({"ok": False, "error": f"内容过长（上限 {_KB_CONTENT_MAX} 字符）"}); return
    fp = _kb_user_root(mode) / rel
    from modules.storage import atomic_write_text
    if not atomic_write_text(fp, content):
        h._json({"ok": False, "error": "写入失败"}); return
    from modules.llm_retriever import clear_knowledge_cache
    clear_knowledge_cache(mode)
    logger.info("知识库更新: %s/%s（%d 字符）", mode, rel, len(content))
    h._json({"ok": True, "path": rel})


def pack_knowledge_delete(h):
    """POST /pack-knowledge/delete {mode, path}：删用户副本（回落 bundled）；
    自建包无 bundled，删除的是本体——前端已二次确认。"""
    body = _read_json(h)
    mode = _body_mode(body)
    rel = _safe_kb_rel(str(body.get("path") or ""))
    if rel is None:
        h._json({"ok": False, "error": "非法知识库路径"}); return
    fp = _kb_user_root(mode) / rel
    if not fp.is_file():
        h._json({"ok": False, "error": "没有可删除的用户副本（该文件当前用的是包自带内容）"}); return
    try:
        fp.unlink()
    except OSError as e:
        h._json({"ok": False, "error": f"删除失败: {e}"}); return
    from modules.llm_retriever import clear_knowledge_cache
    clear_knowledge_cache(mode)
    h._json({"ok": True, "path": rel})


def pack_knowledge_create(h):
    """POST /pack-knowledge/create {mode, path, title}：在五域子目录下新建知识文件（骨架落用户副本）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    rel = _safe_kb_rel(str(body.get("path") or ""))
    title = str(body.get("title") or "").strip()[:60]
    if rel is None:
        h._json({"ok": False, "error": "非法知识库路径（如 world/新地区.md）"}); return
    if not title:
        h._json({"ok": False, "error": "请填写文件标题"}); return
    fp = _kb_user_root(mode) / rel
    if fp.exists() or _kb_resolve(mode, rel)[0] is not None:
        h._json({"ok": False, "error": "同名文件已存在"}); return
    skeleton = f"# {title}\n\n> 范围：本文件覆盖……（一句话说明，供检索器与 AI 辅助理解边界）\n\n"
    from modules.storage import atomic_write_text
    if not atomic_write_text(fp, skeleton):
        h._json({"ok": False, "error": "写入失败"}); return
    from modules.llm_retriever import clear_knowledge_cache
    clear_knowledge_cache(mode)
    logger.info("知识库新建: %s/%s", mode, rel)
    h._json({"ok": True, "path": rel})


# ── 记忆库（出厂记忆 default.md）──
def _mem_resolve(mode: str):
    """出厂记忆读取链：用户副本 → bundled 包内 memory/default.md。返回 (路径|None, 是否用户副本)。"""
    u = cfg.mode_character_dir(mode) / "memory" / "default.md"
    if u.is_file():
        return u, True
    b = cfg.bundled_character_dir(mode) / "memory" / "default.md"
    if b.is_file():
        return b, False
    return None, False


def pack_memory_read(h):
    """GET /pack-memory?mode= → {ok, content, user_copy, exists}"""
    mode = _query_mode(h)
    fp, is_user = _mem_resolve(mode)
    if fp is None:
        h._json({"ok": True, "mode": mode, "content": "", "user_copy": False, "exists": False})
        return
    try:
        h._json({"ok": True, "mode": mode, "content": fp.read_text(encoding="utf-8"),
                 "user_copy": is_user, "exists": True})
    except OSError as e:
        h._json({"ok": False, "error": f"读取失败: {e}"}, 500)


def pack_memory_update(h):
    """POST /pack-memory/update {mode, content}：写出厂记忆用户副本（原子写）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        h._json({"ok": False, "error": "内容不能为空"}); return
    if len(content) > 200_000:
        h._json({"ok": False, "error": "内容过长"}); return
    fp = cfg.mode_character_dir(mode) / "memory" / "default.md"
    from modules.storage import atomic_write_text
    if not atomic_write_text(fp, content):
        h._json({"ok": False, "error": "写入失败"}); return
    logger.info("出厂记忆更新: %s（%d 字符）", mode, len(content))
    h._json({"ok": True})


# ═══ 通用包文件（/pack-file 族）═══
# 白名单 = PACK_STRUCTURE 里 type=="file" 声明的 path（规范驱动：规范加一条 file 来源，
# 端点自动放行，无需改这里）。覆盖：memory/default.md、opening.json、_时态标记规范.md……
def _declared_file_paths() -> frozenset:
    from core.pack_structure import PACK_STRUCTURE
    return frozenset(s["path"] for d in PACK_STRUCTURE for s in d["sources"]
                     if s.get("type") == "file" and s.get("path"))


def _check_file_path(h, path: str) -> bool:
    if path not in _declared_file_paths():
        h._json({"ok": False, "error": f"不允许的包文件: {path}"})
        return False
    return True


def pack_file_read(h):
    """GET /pack-file?mode=&path= → {ok, content, user_copy, exists}（用户副本 → bundled 回落）。"""
    mode = _query_mode(h)
    from urllib.parse import urlparse, parse_qs
    path = (parse_qs(urlparse(h.path).query).get("path") or [""])[0].strip()
    if not _check_file_path(h, path):
        return
    from api.pack_paths import read_pack_text
    text = read_pack_text(mode, path)
    h._json({"ok": True, "mode": mode, "path": path, "content": text,
             "user_copy": (cfg.mode_character_dir(mode) / path).is_file(),
             "exists": bool(text)})


def pack_file_update(h):
    """POST /pack-file/update {mode, path, content}：写用户副本（备份 → 原子写 → 清缓存）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    path = str(body.get("path") or "").strip()
    if not _check_file_path(h, path):
        return
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        h._json({"ok": False, "error": "内容不能为空"}); return
    if len(content) > 200_000:
        h._json({"ok": False, "error": "内容过长"}); return
    if path.endswith(".json"):
        try:
            json.loads(content)
        except ValueError as e:
            h._json({"ok": False, "error": f"JSON 格式错误: {e}"}); return
    from api.pack_paths import write_pack_text
    err = write_pack_text(mode, path, content, backup_tag="edit")
    if err:
        h._json({"ok": False, "error": err}); return
    from modules.llm_base import clear_cache
    clear_cache()
    logger.info("包文件更新: %s/%s（%d 字符）", mode, path, len(content))
    h._json({"ok": True})


def pack_file_delete(h):
    """POST /pack-file/delete {mode, path}：删用户副本（回落 bundled = 恢复默认）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    path = str(body.get("path") or "").strip()
    if not _check_file_path(h, path):
        return
    if _no_bundled_fallback(mode):
        h._json({"ok": False, "error": "自建角色包没有可恢复的默认内容——这个文件就是唯一副本，"
                                       "只能修改不能删除"}); return
    fp = cfg.mode_character_dir(mode) / path
    if not fp.is_file():
        h._json({"ok": False, "error": "没有可删除的用户副本（该文件当前用的是包自带内容）"}); return
    try:
        fp.unlink()
    except OSError as e:
        h._json({"ok": False, "error": f"删除失败: {e}"}); return
    from modules.llm_base import clear_cache
    clear_cache()
    logger.info("包文件恢复默认: %s/%s", mode, path)
    h._json({"ok": True})


def pack_structure(h):
    """GET /pack-structure?mode=：角色卡结构规范树 + 实态（前端管理树的唯一数据源，2026-09-15）。"""
    mode = _query_mode(h)
    from core.pack_structure import build_tree
    h._json({"ok": True, **build_tree(mode)})
