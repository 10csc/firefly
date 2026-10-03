# -*- coding: utf-8 -*-
"""预设包注册表 — 扫描 assets/character/*/preset.json 得到 PRESETS/MODES（任务 2.1 自 app_config 拆出）

`MODES` 会被 reload_presets() **重新赋值**（reload 后消费方必须重新读），
其他模块请经 `presets.MODES` 或 `cfg.MODES` 属性访问，不要按值 import。

**共创平台 M1.5 起（2026-10-01）**：请求路径请改用按用户解析的访问器
（`valid_mode` / `all_modes` / `pack_meta` / `all_packs`）——服务器模式下用户经广场
安装的角色卡不在全局 `MODES`/`PRESETS` 里，直接判它们会漏包或误判成非法。
`MODES`/`PRESETS` 退化为"全局内置包视图"，仍由本地版与启动期逻辑使用。

阶段 2.8 再拆：preset.json 解析 → `core/preset_parse.py`；
packs.json 清单读写与自愈 → `core/pack_registry.py`。
本文件只留"注册表扫描 + 运行时访问器 + 备份白名单"，并 re-export 全部被移名字
（core.presets / modules.app_config 兼容层的消费方零改动）。
"""

import logging
import os
from pathlib import Path

from core import paths as _paths
from core.paths import BASE_DIR
# re-export（纯移动兼容面；勿删，app_config 兼容层与多处测试经此处取名字）
from core.preset_parse import (   # noqa: F401
    PACK_SLOT_FILES, PACK_STATES, PRESET_SCHEMA, SLOT_STATES, _PRESET_ID_RE,
    _clean_knowledge_dirs, _now, _parse_preset, _read_text, _scan_custom_dirs,
    slot_state_from_disk,
)
from core.pack_registry import _REGISTRIES, PackRegistry, pack_registry   # noqa: F401

logger = logging.getLogger(__name__)

# re-export 面（纯移动兼容）：这些名字被 app_config 兼容层与多处测试经 core.presets 取用。
# __all__ 既是文档也是 pyflakes 的"已使用"标记（与 app_config.py:71 的转发注释同款处置）。
__all__ = [
    # 本模块自有
    "DEFAULT_MODE", "PRESETS", "MODES", "reload_presets", "char_name", "user_name",
    "_discover_presets", "backup_pack_ids",
    # 按用户解析的包视图（共创平台 M1.5）：消费方请用这四个访问器，别直接判 PRESETS/MODES
    "user_packs", "all_packs", "pack_meta", "valid_mode", "all_modes",
    # preset_parse 解析层
    "PRESET_SCHEMA", "_PRESET_ID_RE", "_parse_preset", "_clean_knowledge_dirs",
    "PACK_STATES", "SLOT_STATES", "PACK_SLOT_FILES", "slot_state_from_disk",
    "_read_text", "_now", "_scan_custom_dirs",
    # pack_registry 清单层
    "PackRegistry", "pack_registry", "_REGISTRIES",
]


def _discover_presets(base: Path | None = None) -> dict:
    """扫描发现预设包：bundled（assets/character/{id}/preset.json）+ 本地版追加用户自建区
    （USER_DIR/{id}/character/preset.json；服务器版跳过——用户区属各账号，自定义整包不入全局注册表）。
    内置包优先（用户区同 id 跳过）；一个都没发现回退两内置包硬编码兜底。base 参数供测试注入。"""
    presets = {}
    roots = [base or (BASE_DIR / "assets" / "character")]
    for root in roots:
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            fp = d / "preset.json"
            if not fp.exists():
                continue
            p = _parse_preset(fp, d.name)
            if p:
                presets[p["id"]] = p
    # 用户自建区（仅本地版；测试注入 base 时跳过）——**从 packs.json 读**（阶段 3.2）
    if base is None and not os.environ.get("FIREFLY_SERVER"):
        reg = pack_registry()
        # 只取 active 的（archived 包不进 MODES：3.5 的归档语义）；目录多出未注册的包
        # 由 PackRegistry.heal() 补齐（自愈），不会因为"忘了登记"就消失
        for pid in reg.active_ids():
            fp = _paths.USER_DIR / pid / "character" / "preset.json"
            if not fp.exists():
                continue
            p = _parse_preset(fp, pid)
            if not p:
                continue
            if p["id"] in presets:
                logger.warning("用户自建包 %s 与内置包同 id，跳过（内置优先）", p["id"])
                continue
            p["custom"] = True
            presets[p["id"]] = p
    if not presets:
        logger.error("预设包发现为空，回退内置 story/haruno 兜底")
        presets["story"] = {"id": "story", "name": "剧情模式", "char_name": "流萤",
                            "user_name": "开拓者", "presentation": "sticker",
                            "desc": "共同推进的故事（角色主线）",
                            "tagline": "会找到的，属于我的梦...",
                            "knowledge_dirs": None}
        presets["haruno"] = {"id": "haruno", "name": "剧本模式", "char_name": "流萤",
                             "user_name": "开拓者", "presentation": "narration",
                             "desc": "春日手信 · 流萤想象的普通学生生活",
                             "tagline": "会找到的，属于我的梦...",
                             "knowledge_dirs": None}
    return presets


PRESETS = _discover_presets()
DEFAULT_MODE = "story" if "story" in PRESETS else next(iter(PRESETS), "story")
# 模式元组（**全局内置包视图**；请求路径请用 all_modes() 取当前用户视角）：默认包在前，其余按 id 排序
MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))


def reload_presets() -> None:
    """重新扫描预设包（新建/删除自定义包后调用）。
    PRESETS 原地清空重建（dict 引用共享，各模块运行时再取）；MODES 重新赋值——
    消费方须在调用期访问（from-import 的值拷贝会陈旧）；请求路径优先用
    `valid_mode()/all_modes()/pack_meta()` 这三个按用户解析的访问器。
    共创平台 M1.5 起同时清空按用户包缓存（否则刚安装的广场卡要等重启才可见）。"""
    global PRESETS, MODES
    fresh = _discover_presets()
    PRESETS.clear()
    PRESETS.update(fresh)
    MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))
    _USER_PACKS_CACHE.clear()


# ── 按用户解析的包视图（共创平台 M1.5，2026-10-01）──────────────
# 为什么需要这一层：`PRESETS`/`MODES` 是**进程级全局**（import 期扫内置包；本地版另扫本机自建包），
# 而服务器模式下每个账号有独立 `user_data/{uid}/`，广场安装的角色卡落在**该账号自己**的目录里。
# 于是"当前用户有哪些包"必须按请求上下文解析。
#
# 已否决的反面做法：把所有用户装过的卡并进全局 `MODES`。那会让 A 的卡出现在 B 的列表里，
# 并让备份/主动性/诊断等循环遍历到不存在的包——正是 `docs/错误总结.md` #10
# 「两个口径不一致却当成一个」的同类事故。因此这里只做**只读叠加**：
# 全局 `PRESETS` 仍是内置包的真源，用户包只在 `all_packs()` 这一层合并。
_USER_PACKS_CACHE: dict = {}
_USER_PACKS_CACHE_MAX = 256          # 多用户部署下防止缓存无限增长


def _ctx_user_root() -> Path:
    """当前用户上下文的数据根（服务器版 = user_data/{uid}；本地版 = USER_DIR）。"""
    from core.userctx import _user_ctx_dir
    return Path(_user_ctx_dir() or _paths.USER_DIR)


def user_packs() -> dict:
    """当前用户上下文里、**不在全局 PRESETS 中**的包（= 该账号经广场安装的角色卡）。

    无用户上下文（本地版 / 启动期线程 / import 期）→ 返回 `{}`，
    调用方行为与本改动前**完全一致**（本地版自建包早已在 PRESETS 里，不会重复）。
    """
    from core.userctx import user_scope_key
    scope = user_scope_key() or ""
    if not scope:
        return {}
    hit = _USER_PACKS_CACHE.get(scope)
    if hit is not None:
        return hit
    out: dict = {}
    root = _ctx_user_root()
    try:
        ids = pack_registry().active_ids()
    except Exception as e:            # 清单损坏不能让请求 500：退化为"没有用户包"
        logger.warning("读取用户包清单失败（%s）：%s", scope, e)
        ids = []
    for pid in ids:
        if pid in PRESETS:            # 内置同名：内置优先（与 _discover_presets 同口径）
            continue
        fp = root / pid / "character" / "preset.json"
        if not fp.exists():
            continue
        p = _parse_preset(fp, pid)
        if not p:
            continue
        p["custom"] = True
        out[p["id"]] = p
    if len(_USER_PACKS_CACHE) >= _USER_PACKS_CACHE_MAX:
        _USER_PACKS_CACHE.clear()
    _USER_PACKS_CACHE[scope] = out
    return out


def all_packs() -> dict:
    """**当前用户视角**的完整包表（内置包 + 自己装的广场卡）。"""
    up = user_packs()
    if not up:
        return PRESETS
    merged = dict(PRESETS)
    merged.update(up)
    return merged


def pack_meta(mode) -> dict:
    """取单个包的元数据（当前用户视角）；找不到返回 `{}`（调用方无需判 None）。"""
    return all_packs().get(mode) or {}


def valid_mode(mode) -> bool:
    """当前用户能否使用该 mode（内置包，或自己安装的广场卡）。

    **替代 `mode in cfg.MODES`**：广场卡不在全局 MODES 里，直接判 MODES 会把
    用户的卡判成非法 → 静默回退默认包 → 数据写进别人的目录（历史踩过的坑）。
    """
    return bool(mode) and mode in all_packs()


def all_modes() -> tuple:
    """当前用户的模式元组（默认包在前，其余按 id 排序）。**替代 `for m in cfg.MODES`**。"""
    packs = all_packs()
    head = DEFAULT_MODE if DEFAULT_MODE in packs else next(iter(packs), DEFAULT_MODE)
    return tuple([head] + sorted(k for k in packs if k != head))


def char_name(mode: str = DEFAULT_MODE) -> str:
    """当前模式角色名（**只从预设包取**；非法 mode 回退默认包）。

    2026-09-18：兜底从写死的「流萤」改成包声明的 mode name / 中性词「角色」——
    框架里写死某个角色名，自建包（没填 char_name）会顶着别人的名字说话。
    """
    p = pack_meta(mode) or pack_meta(DEFAULT_MODE) or {}
    return p.get("char_name") or p.get("name") or "角色"


def user_name(mode: str = DEFAULT_MODE) -> str:
    """当前模式用户称呼（同 char_name）。

    05 用户形象入包（2026-09-15）：读取链 = 包级配置覆盖（用户在角色卡编辑页改的称呼，
    存 {pack}/data/proactive.json）→ preset.json 声明 → 兜底"开拓者"。lazy import 防环。"""
    try:
        from core.config import pack_cfg
        over = pack_cfg(mode, "user_name")
        if over:
            return str(over)
    except Exception:
        pass
    p = pack_meta(mode) or pack_meta(DEFAULT_MODE) or {}
    return p.get("user_name") or "开拓者"


def backup_pack_ids() -> list:
    """**备份/快照白名单**（架构重构 3.8）：存在数据的**全部**包 id。

    = 内置/当前可用包（`MODES`，由发行版扫描） ∪ 清单全部条目（`packs.json`，**含 archived**）。

    为什么要有这个函数：`MODES` 是**运行期**语义（只有 active 的包能聊天），
    而快照/同步是**备份期**语义 —— 归档 ≠ 丢保险，用户按了归档的包，数据仍必须进快照，
    否则"归档"就成了静默的数据删除。旧实现三处白名单都拿 `MODES` 当全集，正是这个漏。
    默认模式排最前（顺序稳定，便于快照内容比对）。"""
    ids = set(MODES) | set(pack_registry().all_ids())
    return [DEFAULT_MODE] + sorted(i for i in ids if i != DEFAULT_MODE)
