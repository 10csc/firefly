# -*- coding: utf-8 -*-
"""表情包注册表 + 选择器

纯代码层，无 LLM 调用。
- pick_sticker_by_meaning(meaning): 按"想表达的意思"匹配 label 最接近的图 → StickerEntry | None（主用）
- pick_sticker(category): 按分类随机选图 → StickerEntry | None（保留作 fallback / 测试）
- add_sticker(file, category, label): 用户添加表情包，持久化到 registry.json
- get_all_stickers(): 返回合并后的全量注册表（默认 + 用户添加）
"""

import json
import logging
import random
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────
VALID_CATEGORIES = ("可爱", "帅气")

# ── 注册表 ────────────────────────────────────────
@dataclass
class StickerEntry:
    id: str
    file: str        # 相对于 assets/ 的路径
    category: str    # 可爱 | 帅气
    label: str       # 含义描述

# 代码内默认项（流萤常用可爱系 + 萨姆帅气系）
_STICKERS_DEFAULT: dict[str, StickerEntry] = {
    "strong_01":  StickerEntry("strong_01",  "stickers/流萤_出击.webp",             "帅气", "出击"),
    "neutral_02": StickerEntry("neutral_02", "stickers/流萤_也挺好(表示无奈).webp", "可爱", "无奈接受"),
    "weak_02":    StickerEntry("weak_02",    "stickers/流萤_没钱了.webp",           "可爱", "没钱了"),
    "neutral_01": StickerEntry("neutral_01", "stickers/流萤_比心.webp",             "可爱", "比心"),
    "like_01":    StickerEntry("like_01",    "stickers/呜呜伯_期待.webp",           "可爱", "呜呜伯期待"),
}

# 用户添加项持久化路径：user_data/（打包 exe 后 app/assets/ 不可写）
from modules.app_config import USER_DIR as _USER_DIR
_REGISTRY_FILE = _USER_DIR / "stickers" / "registry.json"
_REGISTRY_LEGACY = Path(__file__).resolve().parent.parent / "assets" / "stickers" / "registry.json"


def _migrate_legacy_registry():
    """旧位置（app/assets/stickers/）一次性迁移到 user_data/stickers/。"""
    if not _REGISTRY_FILE.exists() and _REGISTRY_LEGACY.exists():
        try:
            _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _REGISTRY_FILE.write_text(
                _REGISTRY_LEGACY.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("registry.json 已迁移到 user_data/stickers/")
        except Exception as e:
            logger.warning("registry.json 迁移失败: %s", e)


# 模块加载时迁移一次；不放在 _load_registry 里——测试替换 _REGISTRY_FILE 后
# 每次加载都迁移会把真实数据拷进测试目录
_migrate_legacy_registry()

import threading
_lock = threading.Lock()
_PICK_COUNT = 0


def get_counters() -> dict:
    with _lock:
        return {"pick_count": _PICK_COUNT}


# ── 注册表加载（合并默认 + 用户添加）─────────────
def _load_registry() -> dict[str, StickerEntry]:
    """合并代码内默认项 + registry.json 用户添加项。"""
    base = deepcopy(_STICKERS_DEFAULT)
    if _REGISTRY_FILE.exists():
        try:
            user_data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
            for item in user_data.get("stickers", []):
                if not isinstance(item, dict):
                    continue
                sid = item.get("id", "")
                category = item.get("category", "")
                if category not in VALID_CATEGORIES:
                    logger.warning("registry.json 条目分类非法，跳过: %s", item)
                    continue
                entry = StickerEntry(
                    id=sid,
                    file=item.get("file", ""),
                    category=category,
                    label=item.get("label", ""),
                )
                base[entry.id] = entry
        except Exception as e:
            logger.warning("注册表加载失败: %s", e)
    return base


def _save_user_entry(sid: str, file: str, category: str, label: str) -> None:
    """把用户添加项追加到 registry.json。"""
    existing = {"stickers": []}
    if _REGISTRY_FILE.exists():
        try:
            existing = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or "stickers" not in existing:
                existing = {"stickers": []}
        except Exception:
            existing = {"stickers": []}
    existing.setdefault("stickers", []).append({
        "id": sid, "file": file, "category": category, "label": label,
    })
    _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY_FILE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 用户添加 ──────────────────────────────────────
class StickerAddError(Exception):
    """表情包添加异常"""
    pass


class StickerUpdateError(Exception):
    """表情包更新异常"""
    pass


class StickerDeleteError(Exception):
    """表情包删除异常"""
    pass


def add_sticker(file: str, category: str, label: str) -> StickerEntry:
    """用户添加表情包——生成 id + 写盘到 registry.json。

    Args:
        file: 相对于 assets/ 的路径（如 stickers/用户上传的xxx.png）
        category: 可爱 | 帅气
        label: 含义描述

    Returns:
        新建的 StickerEntry
    """
    if category not in VALID_CATEGORIES:
        raise StickerAddError(f"分类必须为 {VALID_CATEGORIES}，实际: {category}")
    if not file or not isinstance(file, str):
        raise StickerAddError("file 不能为空")
    if not label or not isinstance(label, str):
        raise StickerAddError("label 不能为空")

    with _lock:
        sid = "user_" + uuid.uuid4().hex[:8]
        entry = StickerEntry(sid, file, category, label)
        _save_user_entry(sid, file, category, label)
    logger.info("用户添加表情包: id=%s file=%s category=%s", sid, file, category)
    return entry


# ── 列表 / 修改 / 删除（供前端管理表使用）──────────────
def list_all_stickers() -> list[StickerEntry]:
    """返回全量表情包列表（默认 + 用户添加），按 id 排序，供前端管理表展示。"""
    stickers = _load_registry()
    return sorted(stickers.values(), key=lambda s: s.id)


def _write_registry_all(stickers: dict[str, StickerEntry]) -> None:
    """把 registry.json 重写为当前全量状态。

    策略：默认项（_STICKERS_DEFAULT）若 label 未改则不入文件（保持代码内默认为准），
    若 label 被改则写一条覆盖记录；用户添加项全部入文件。
    """
    user_items = []
    for sid, s in stickers.items():
        if sid in _STICKERS_DEFAULT:
            default = _STICKERS_DEFAULT[sid]
            if s.label == default.label:
                continue  # 默认项 label 未改，不写文件
        user_items.append({"id": s.id, "file": s.file, "category": s.category, "label": s.label})
    _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY_FILE.write_text(
        json.dumps({"stickers": user_items}, ensure_ascii=False, indent=2), encoding="utf-8")


def update_sticker(sid: str, new_label: str = None, new_category: str = None) -> StickerEntry:
    """修改表情包的 label / category。传 None 的字段不修改。

    Returns:
        更新后的 StickerEntry
    Raises:
        StickerUpdateError: sid 不存在 / 没有可更新的内容
    """
    if not sid or not isinstance(sid, str):
        raise StickerUpdateError("sid 不能为空")
    has_label = new_label and isinstance(new_label, str) and new_label.strip()
    has_category = new_category in ("可爱", "帅气")
    if not has_label and not has_category:
        raise StickerUpdateError("没有可更新的内容")

    # 读→改→写整体加锁，防并发丢更新
    with _lock:
        stickers = _load_registry()
        if sid not in stickers:
            raise StickerUpdateError(f"表情包不存在: {sid}")

        s = stickers[sid]
        if has_label:
            s.label = new_label.strip()
        if has_category:
            s.category = new_category
        _write_registry_all(stickers)
    logger.info("用户修改表情包: id=%s label=%s category=%s", sid, s.label, s.category)
    return s


def delete_sticker(sid: str) -> None:
    """删除表情包条目。

    默认项（_STICKERS_DEFAULT 里的 5 个）不允许删除——它们是代码内置的兜底。
    用户添加项可删：从 registry.json 移除条目；图片文件保留（不删盘，避免误删）。
    Raises:
        StickerDeleteError: sid 不存在 / 试图删默认项
    """
    if not sid or not isinstance(sid, str):
        raise StickerDeleteError("sid 不能为空")

    with _lock:
        stickers = _load_registry()
        if sid not in stickers:
            raise StickerDeleteError(f"表情包不存在: {sid}")
        if sid in _STICKERS_DEFAULT:
            raise StickerDeleteError("默认表情包不允许删除")

        del stickers[sid]
        _write_registry_all(stickers)
    logger.info("用户删除表情包: id=%s", sid)


# ── 选图 ──────────────────────────────────────────
def pick_sticker(category: str = "可爱") -> StickerEntry | None:
    """从指定分类中随机选一张。无图则降级到可爱，仍无返回 None。"""
    global _PICK_COUNT
    if category not in VALID_CATEGORIES:
        category = "可爱"

    stickers = _load_registry()
    matches = [s for s in stickers.values() if s.category == category]
    if not matches and category != "可爱":
        matches = [s for s in stickers.values() if s.category == "可爱"]
    if not matches:
        return None

    with _lock: _PICK_COUNT += 1
    return random.choice(matches)


# ── 按语义选图（主入口）─────────────────────────────
def _char_overlap(a: str, b: str) -> int:
    """两个字符串的字符级重叠数（按字符集交集计，不关心顺序与重复）。"""
    return len(set(a) & set(b))


def pick_sticker_by_meaning(meaning: str) -> StickerEntry | None:
    """按"想表达的意思"匹配 label 最接近的一张。

    meaning: 规划器给的自然语言，如"害羞""安慰他""无奈""比心""撒娇""道歉"。
    匹配策略：meaning 与每个 sticker.label 做字符重叠打分，取最高分的一组，
    同分时随机挑一张（避免每次都选同一张）。
    无任何重叠或 meaning 为空 → 返回 None（没有合适的就不发，不乱发无关图）。
    """
    global _PICK_COUNT
    if not isinstance(meaning, str) or not meaning.strip():
        return None

    stickers = _load_registry()
    if not stickers:
        return None

    entries = list(stickers.values())
    scores = [(_char_overlap(meaning, s.label), s) for s in entries]
    max_score = max(s for s, _ in scores)

    if max_score == 0:
        # 无任何重叠 → 没有合适的图，不发
        return None

    # 取最高分一组，随机挑一张
    top = [s for sc, s in scores if sc == max_score]
    with _lock: _PICK_COUNT += 1
    return random.choice(top)


def pick_sticker_by_label(label: str) -> StickerEntry | None:
    """按 label 精确匹配（工具调度器直接输出 label 原文时用），
    未命中时降级字符重叠模糊匹配。"""
    global _PICK_COUNT
    if not isinstance(label, str) or not label.strip():
        return None
    label = label.strip()
    stickers = _load_registry()
    exact = [s for s in stickers.values() if s.label == label]
    if exact:
        with _lock: _PICK_COUNT += 1
        return random.choice(exact)
    return pick_sticker_by_meaning(label)


def get_all_stickers() -> dict[str, StickerEntry]:
    return _load_registry()
