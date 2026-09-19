# -*- coding: utf-8 -*-
"""预设包注册表 — 扫描 assets/character/*/preset.json 得到 PRESETS/MODES（任务 2.1 自 app_config 拆出）

`MODES` 会被 reload_presets() **重新赋值**（reload 后消费方必须重新读），
其他模块请经 `presets.MODES` 或 `cfg.MODES` 属性访问，不要按值 import。

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
# 模式元组（兼容既有几百处 `mode in cfg.MODES` 校验）：默认模式在前，其余按 id 排序
MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))


def reload_presets() -> None:
    """重新扫描预设包（新建/删除自定义包后调用）。
    PRESETS 原地清空重建（dict 引用共享，各模块运行时再取）；MODES 重新赋值——
    消费方须用 cfg.MODES 运行时访问（from-import 的值拷贝会陈旧）。"""
    global PRESETS, MODES
    fresh = _discover_presets()
    PRESETS.clear()
    PRESETS.update(fresh)
    MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))


def char_name(mode: str = DEFAULT_MODE) -> str:
    """当前模式角色名（**只从预设包取**；非法 mode 回退默认包）。

    2026-09-18：兜底从写死的「流萤」改成包声明的 mode name / 中性词「角色」——
    框架里写死某个角色名，自建包（没填 char_name）会顶着别人的名字说话。
    """
    p = PRESETS.get(mode) or PRESETS.get(DEFAULT_MODE) or {}
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
    p = PRESETS.get(mode) or PRESETS.get(DEFAULT_MODE) or {}
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
