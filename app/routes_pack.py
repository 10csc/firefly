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
# 3.3：白名单与"槽位三态"的记录口径必须**同一份**（否则 API 能写的文件与能被标记的文件会漂移），
# 故取自 `core.presets.PACK_SLOT_FILES`；下面两个元组只用于 /pack-files 的展示顺序。
_PACK_PROMPT_FILES = ("prompts/polisher.md", "prompts/analyzer_extra.md",
                      "prompts/organizer_sticker.md", "prompts/organizer_narration.md",
                      "prompts/proactive_context.md", "prompts/env_suffix.md")
_PACK_CORE_FILES = ("core.md", "identity.md", "sms_samples.md")
_EDITABLE_FILES = frozenset(cfg.PACK_SLOT_FILES)

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


def _pack_target(pid: str) -> Path:
    """包数据目录（3.3 起用 `pack_root`：归档包不在 MODES，`mode_root` 会静默回退默认包）。"""
    return cfg.pack_root(pid)


def _detach_pack_stickers(pid: str) -> int:
    """把归属该包的表情包条目降级为全局共享（3.5）。失败不致命（返回 0 并告警）：
    贴纸降级失败不该让归档/抹除整个操作失败——包的状态已经改了，日志留痕即可。"""
    try:
        from domain.stickers.picker import detach_pack
        n = detach_pack(pid)
        if n:
            logger.info("表情包降级为全局共享: pack=%s 条目数=%d", pid, n)
        return n
    except Exception as e:
        logger.warning("表情包降级失败（pack=%s）: %s", pid, e)
        return 0


def _force_pack_backup(pid: str) -> Path:
    """抹除前的**强制保险**（3.5）：先打一份全量快照（3.8 的白名单含 archived，所以
    "即将被抹除的这个包"一定在里面）落 `backups/`。

    为什么是全量而不是"只备这个包"：单包 zip 的恢复入口要求该包当时存在（mode 必须合法），
    包被抹除后恰恰不满足——而全量快照走的是"按包分发恢复"，新机器/空环境也能直接恢复，
    是真正可用的保险。失败**抛异常**，由调用方中止删除（没备份成功就不许抹除）。
    文件名前缀 `pack-erase-` 是刻意的：不进备份管理列表（`/backups` 只列 `{mode}-*.zip`），
    也不被 auto-/pre-restore 的滚动裁剪碰到 —— 这份保险不会被自动淘汰。"""
    import time as _time
    from routes_snapshot import build_full_snapshot_zip
    from infra.sync.restore import _backup_dir
    data = build_full_snapshot_zip()
    if not data.startswith(b"PK"):
        raise OSError("快照打包结果不是 zip")
    bdir = _backup_dir(cfg.DEFAULT_MODE)                  # {用户根}/backups（与模式目录平级）
    bdir.mkdir(parents=True, exist_ok=True)
    fp = bdir / f"pack-erase-{pid}-{_time.strftime('%Y%m%d-%H%M%S')}.zip"
    fp.write_bytes(data)
    if not fp.is_file() or fp.stat().st_size != len(data):
        raise OSError("备份写入校验失败")
    logger.info("抹除前强制备份: %s（%d 字节）", fp.name, len(data))
    return fp


def archive_pack(h):
    """POST /pack-archive {id}：归档自建包（3.5 的"一级删除"）。

    归档 ≠ 删除：清单条目与数据目录都留着，只是 `state=archived` → 不进 MODES
    （模式列表/切换里消失）。这是本卡的核心行为变化：删包不再一步灭失数据。
    副作用：该包专属的表情包条目降级为**全局共享**（贴纸是用户资产，包不在了也该继续可用）。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持自建角色包"}); return
    body = _read_json(h)
    pid = str(body.get("id") or "").strip()
    if not _PACK_ID_RE.fullmatch(pid):
        h._json({"ok": False, "error": "非法包 id"}); return
    reg = cfg.pack_registry()
    meta = reg.get(pid)
    if not meta:
        if pid in cfg.PRESETS:
            h._json({"ok": False, "error": "内置包不能归档"}); return
        h._json({"ok": False, "error": "包不存在（清单里没有）"}); return
    if not _pack_target(pid).is_dir():
        h._json({"ok": False, "error": "包目录不存在，无法归档"}); return
    changed = reg.set_state(pid, "archived")
    detached = _detach_pack_stickers(pid)
    cfg.reload_presets()          # 重扫：让 archived 立刻从 MODES 消失
    if pid in cfg.MODES:
        # 输出验证：归档后该包必须已不在可用列表里，否则状态与投影不一致（宁可报错也别假装成功）
        h._json({"ok": False, "error": "归档后该包仍在可用列表（状态未生效）"}); return
    logger.info("自建角色包归档: %s（贴纸降级 %d 条）", pid, detached)
    h._json({"ok": True, "id": pid, "state": "archived",
             "changed": changed, "stickers_detached": detached})


def restore_pack(h):
    """POST /pack-restore {id}：取消归档（archived → active）。数据本来就在，只是重新可见。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持自建角色包"}); return
    body = _read_json(h)
    pid = str(body.get("id") or "").strip()
    if not _PACK_ID_RE.fullmatch(pid):
        h._json({"ok": False, "error": "非法包 id"}); return
    reg = cfg.pack_registry()
    if reg.get(pid) is None:
        h._json({"ok": False, "error": "包不存在（清单里没有）"}); return
    if not (_pack_target(pid) / "character" / "preset.json").is_file():
        h._json({"ok": False, "error": "包定义缺失（character/preset.json）"}); return
    changed = reg.set_state(pid, "active")
    cfg.reload_presets()
    if pid not in cfg.MODES:
        h._json({"ok": False, "error": "取消归档后仍未进入可用列表（包定义非法？）"}); return
    logger.info("自建角色包取消归档: %s", pid)
    h._json({"ok": True, "id": pid, "state": "active", "changed": changed})


def delete_pack(h):
    """POST /pack-delete：**彻底删除**自定义角色包（仅本地版；内置包 story/haruno 拒绝）。

    3.5 起改为**两级删除**的第二级：
    - 只有 `state=archived` 的包才允许抹除（活跃包必须先归档 —— 归档不删数据，可反悔）；
    - 抹除前**强制**打一份全量快照落 `backups/`（失败即中止，数据一个字节都不动）；
    - 该包专属贴纸条目降级为全局共享。
    连数据一起删（user_data/{id}/ 整个目录）——前端要求输入包名二次确认。

    R-09（2026-09-13，保留）：不要求"必须已注册"的另一条分支仍在——孤儿包目录
    （建包中途失败/早期版本残留）在"确认啥也没有"时可直接清掉。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版暂不支持自建角色包"}); return
    body = _read_json(h)
    pid = str(body.get("id") or "").strip()
    if not _PACK_ID_RE.fullmatch(pid):
        h._json({"ok": False, "error": "非法包 id"}); return
    target = _pack_target(pid)
    p = cfg.PRESETS.get(pid) or {}
    meta = cfg.pack_registry().get(pid)
    if pid in cfg.PRESETS and not p.get("custom"):
        h._json({"ok": False, "error": "内置包不能删除"}); return
    if meta is None:
        # 未注册：只允许删"孤儿残留"，不碰可能有内容的目录
        if not _is_orphan_pack_dir(target):
            h._json({"ok": False, "error": "该包未注册且目录非空，为免误删数据已拒绝"}); return
        logger.info("清理孤儿包目录: %s", pid)
    elif (meta.get("state") or "active") != "archived":
        # 已注册的自建包：必须已归档（归档不删数据、可随时恢复；抹除不可恢复）
        h._json({"ok": False,
                 "error": "请先「归档」再彻底删除——归档不删数据、可随时恢复；"
                          "彻底删除不可恢复"}); return
    backup_name = ""
    if meta is not None:      # 只有"真删一个已归档的包"才需要保险（孤儿目录无数据可保）
        try:
            backup_name = _force_pack_backup(pid).name
        except Exception as e:
            logger.warning("抹除前强制备份失败，已中止删除 %s: %s", pid, e)
            h._json({"ok": False, "error": f"备份失败，已中止删除（数据未动）: {e}"}); return
    detached = _detach_pack_stickers(pid)
    import shutil
    shutil.rmtree(target, ignore_errors=True)
    if _pack_target(pid).exists():
        # 输出验证：目录必须真的没了，否则清单先注销会让"删不掉的包"彻底失联
        h._json({"ok": False, "error": "目录删除失败（清单未改动，可重试）"}); return
    cfg.pack_registry().unregister(pid)   # 3.2：目录删除与清单注销成对出现
    cfg.reload_presets()
    logger.info("自建角色包彻底删除: %s（备份 %s，贴纸降级 %d 条）", pid, backup_name or "-", detached)
    h._json({"ok": True, "id": pid, "backup": backup_name, "stickers_detached": detached})
