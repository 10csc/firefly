# -*- coding: utf-8 -*-
"""角色卡生命周期路由（阶段 2.8 自 routes_pack 拆出，纯移动无行为变化）

职责：建包（表单/AI 向导端点）、归档/取消归档、两级删除（含孤儿目录清理 R-09）。
卡文件编辑/资产上传等"包内容管理"仍在 routes_pack.py；
本模块经 routes_pack re-export，router 的 import 路径不变。
"""

import json
import logging
from pathlib import Path

from modules import app_config as cfg
from routes_common import _read_json, _is_server
from core.preset_parse import _PRESET_ID_RE as _PACK_ID_RE

logger = logging.getLogger(__name__)


def _templates():
    """建包模板（**调用时**从 routes_pack 读）：测试经 `rp._PACK_*_TPL = None` 打桩
    验证建包回滚（test_pack_lifecycle B4/B5）——模板的所有权留在 routes_pack，
    本模块只做引用，保证打桩语义在拆分后不变。"""
    from routes_pack import _PACK_CORE_TPL, _PACK_IDENTITY_TPL, _PACK_SAMPLES_TPL
    return _PACK_CORE_TPL, _PACK_IDENTITY_TPL, _PACK_SAMPLES_TPL


def _mode_label(presentation: str) -> str:
    """模式中文名（模式仅两类：故事/剧情、剧本——剧本带旁白与环境描写）。"""
    return "剧本" if presentation == "narration" else "剧情"


def _default_pack_name(cname: str, presentation: str) -> str:
    """默认展示名：角色名_模式名_创建时间（月日_时分），如「流萤_剧情_0918_0041」。"""
    import time as _t
    return f"{cname}_{_mode_label(presentation)}_{_t.strftime('%m%d_%H%M')}"


def _default_pack_id(presentation: str) -> str:
    """默认包 id（=user_data 目录名）。

    受 `_PRESET_ID_RE = ^[a-z0-9_-]{1,32}$` 约束**不能含中文**（id 会出现在 URL
    参数 ?mode= 与同步/快照清单里），所以角色名只进 name 不进 id；同角色多包靠
    name + 时间戳区分。冲突时追加 _2/_3…。"""
    import time as _t
    base = f"{presentation}_{_t.strftime('%m%d_%H%M')}"
    pid, i = base, 1
    while (cfg.USER_DIR / pid).exists() or pid in cfg.PRESETS:
        i += 1
        pid = f"{base}_{i}"
    return pid


def create_pack(h):
    """POST /pack-create：新建自定义角色卡（仅本地版；建好时尚无聊天数据，随使用成为角色包）。
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
    if not cname or not uname:
        h._json({"ok": False, "error": "角色名、对方称呼都必填"}); return
    # 命名统一（2026-09-18）：角色名_模式名_角色卡创建时间（月日时分），如「流萤_剧情_0918_0041」。
    # 未填包名称时按此生成默认名——同角色多包靠它区分（建包时 char_name 允许重复）。
    if not name:
        # 同角色多包：同一分钟内建两个同角色同模式的包会得到同名（默认名只到时分），
        # 副标题就无法区分了——与 id 同款去重，重名追加 _2/_3…
        base = _default_pack_name(cname, presentation)[:30]
        name, _dup = base, 1
        while any((p.get("name") or "") == name for p in cfg.PRESETS.values()):
            _dup += 1
            name = f"{base}_{_dup}"[:30]
    pid = str(body.get("id") or "").strip().lower()
    if not pid:
        pid = _default_pack_id(presentation)
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
        _core_tpl, _identity_tpl, _samples_tpl = _templates()
        (cdir / "prompts").mkdir(exist_ok=True)
        (cdir / "prompts" / "polisher.md").write_text(_EMERGENCY_PERSONA, encoding="utf-8")
        (cdir / "core.md").write_text(_core_tpl.format(char_name=cname, user_name=uname), encoding="utf-8")
        (cdir / "identity.md").write_text(_identity_tpl.format(char_name=cname, user_name=uname), encoding="utf-8")
        (cdir / "sms_samples.md").write_text(_samples_tpl.format(char_name=cname), encoding="utf-8")
        # 槽位全集对齐 PACK_SLOT_FILES：用户设定自建包也要有（空文件起步，用户在角色卡里填）
        (cdir / "用户设定.md").write_text("", encoding="utf-8")
        # 阶段 D（2026-09-15）：自建包同样带知识库/记忆库骨架（五域结构 + 出厂记忆）——
        # 「所有角色都要有知识库和记忆库」。空骨架不挂检索（has_knowledge 对空目录为否）。
        (cdir / "knowledge").mkdir(exist_ok=True)
        (cdir / "knowledge" / "index.md").write_text(
            f"# 知识库索引 — {name}\n\n> 结构：world/世界观 · factions/势力 · story/主线剧情 · character/角色个人 · dialogues/对话压缩。\n",
            encoding="utf-8")
        (cdir / "memory").mkdir(exist_ok=True)
        (cdir / "memory" / "default.md").write_text(
            f"# 出厂记忆（{cname}）\n\n> 范围：{cname}开局就记得的事。\n\n## 核心记忆\n\n（在这里写：{cname}的来历、当前状态、与{uname}的初始关系）\n\n## 既定事实\n\n",
            encoding="utf-8")
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
