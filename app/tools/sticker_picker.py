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
    enabled: bool = True   # 是否参与选图（管理页可启停）

# 代码内默认项（流萤常用可爱系 + 萨姆帅气系）
_STICKERS_DEFAULT: dict[str, StickerEntry] = {
    "strong_01":  StickerEntry("strong_01",  "stickers/流萤_出击.webp",             "帅气", "出击"),
    "neutral_02": StickerEntry("neutral_02", "stickers/流萤_也挺好(表示无奈).webp", "可爱", "无奈接受"),
    "weak_02":    StickerEntry("weak_02",    "stickers/流萤_没钱了.webp",           "可爱", "没钱了"),
    "neutral_01": StickerEntry("neutral_01", "stickers/流萤_比心.webp",             "可爱", "比心"),
    "like_01":    StickerEntry("like_01",    "stickers/呜呜伯_期待.webp",           "可爱", "呜呜伯期待"),
}

# 用户添加项持久化路径：user_data/（打包 exe 后 app/assets/ 不可写）
from modules.app_config import USER_DIR as _USER_DIR, user_scope_key
_REGISTRY_FILE = _USER_DIR / "stickers" / "registry.json"
_REGISTRY_LEGACY = Path(__file__).resolve().parent.parent / "assets" / "stickers" / "registry.json"


def _user_registry_file() -> Path:
    """当前作用域的 registry 路径：
    - 服务器版（有用户上下文）= 用户目录 user_data/{id}/stickers/registry.json（按账号隔离）
    - 本地版（无上下文）= 全局 _REGISTRY_FILE（行为与之前一致）
    旧全局文件保留为服务器版「公共贴纸池」（只读共享，历史数据不丢；新上传按用户隔离）。"""
    d = user_scope_key()
    return Path(d) / "stickers" / "registry.json" if d else _REGISTRY_FILE


def _read_registry_items(path: Path) -> list[dict]:
    """读 registry.json 的 stickers 列表（文件缺失/损坏返回空列表，不抛异常）。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("stickers", []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict)]
    except Exception as e:
        logger.warning("注册表读取失败（跳过）: %s", e)
        return []


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


def _migrate_enabled_defaults():
    """把新版 bundled 的 enabled/描述词合并进已有 user_data registry（幂等）。

    规则：用户文件中“没有 enabled 字段”的条目 = 启用开关上线后尚未手动改过，
    用 bundled 同 id 的 enabled 与 label 覆盖；用户自己上传的条目补 enabled=true；
    bundled 新增条目（含内置默认项的启用状态）补齐进用户文件，保证老安装与新装一致。
    """
    if not _REGISTRY_FILE.exists() or not _REGISTRY_LEGACY.exists():
        return
    try:
        data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("stickers"), list):
            return
    except Exception:
        return
    bundled = {i.get("id"): i for i in _read_registry_items(_REGISTRY_LEGACY) if i.get("id")}
    known = set()
    changed = False
    for item in data["stickers"]:
        if not isinstance(item, dict):
            continue
        sid = item.get("id", "")
        known.add(sid)
        if "enabled" not in item:
            default = bundled.get(sid, {})
            item["enabled"] = bool(default.get("enabled", True))
            if sid in bundled and default.get("label"):
                item["label"] = default.get("label")
            changed = True
    # 补齐 bundled 中新增的条目（例如首次加入的内置默认项/新增贴纸）
    for sid, item in bundled.items():
        if sid not in known:
            data["stickers"].append(dict(item))
            changed = True
    if changed:
        _REGISTRY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# 模块加载时迁移一次；不放在 _load_registry 里——测试替换 _REGISTRY_FILE 后
# 每次加载都迁移会把真实数据拷进测试目录
_migrate_legacy_registry()
_migrate_enabled_defaults()

import threading
_lock = threading.Lock()
_PICK_COUNT = 0


def get_counters() -> dict:
    with _lock:
        return {"pick_count": _PICK_COUNT}


# ── 注册表加载（合并默认 + 用户添加）─────────────
def _load_registry() -> dict[str, StickerEntry]:
    """合并注册表（优先级低→高）：代码内默认 → 旧全局公共池 → 当前用户条目。

    服务器版：默认项 + 历史公共贴纸所有人可见；用户自己的条目只自己可见（隔离）。
    本地版：用户文件与全局文件同路径，合并退化为原行为。"""
    base = deepcopy(_STICKERS_DEFAULT)

    def _merge(items: list[dict]):
        for item in items:
            sid = item.get("id", "")
            category = item.get("category", "")
            if category not in VALID_CATEGORIES:
                logger.warning("registry.json 条目分类非法，跳过: %s", item)
                continue
            file = item.get("file", "")
            # 路径校验：仅允许 stickers/ 相对前缀，防手改 registry.json 写 ../../ 越权读取
            if not isinstance(file, str) or not file.startswith("stickers/") \
                    or ".." in file or "\\" in file:
                logger.warning("registry.json 条目路径非法，跳过: %s", item)
                continue
            base[sid] = StickerEntry(
                id=sid, file=file, category=category,
                label=item.get("label", ""),
                enabled=bool(item.get("enabled", True)),
            )

    _merge(_read_registry_items(_REGISTRY_FILE))
    if _user_registry_file() != _REGISTRY_FILE:
        _merge(_read_registry_items(_user_registry_file()))
    return base


def editable_ids() -> set[str]:
    """当前用户可改/删的条目 id（自己 registry 文件里的）。
    本地版=用户文件即全局文件（含改名默认项）；服务器版=仅自己上传的。"""
    return {str(i.get("id", "")) for i in _read_registry_items(_user_registry_file())}


def _save_user_entry(sid: str, file: str, category: str, label: str) -> None:
    """把用户添加项追加到当前用户的 registry.json（服务器版按账号隔离）。"""
    fp = _user_registry_file()
    existing = {"stickers": []}
    if fp.exists():
        try:
            existing = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or "stickers" not in existing:
                existing = {"stickers": []}
        except Exception:
            existing = {"stickers": []}
    existing.setdefault("stickers", []).append({
        "id": sid, "file": file, "category": category, "label": label,
        "enabled": True,
    })
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(
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


def get_enabled_stickers() -> dict[str, StickerEntry]:
    """只返回启用中的表情包（组织器选图与管理页“可用”视图用）。"""
    return {sid: s for sid, s in _load_registry().items() if s.enabled}


def _write_registry_all(stickers: dict[str, StickerEntry]) -> None:
    """把当前用户的 registry.json 重写为全量状态。

    只写「用户自己的条目」：① 用户文件已有条目（合并结果的最新状态）；
    ② 被用户改过 label/category/enabled 的默认项（覆盖记录）。
    公共池（旧全局文件）条目不复制进用户文件——否则公共项会变成"自己可删项"，
    且删除后因公共池仍合并而复活。服务器版只写用户自己的文件（公共池不受影响）。"""
    fp = _user_registry_file()
    prev_ids = editable_ids()
    user_items = []
    for sid, s in stickers.items():
        if sid in _STICKERS_DEFAULT:
            d = _STICKERS_DEFAULT[sid]
            if s.label == d.label and s.category == d.category and s.enabled == d.enabled:
                continue        # 默认项未改动，不写文件
        elif sid not in prev_ids:
            continue            # 公共池条目：不是用户的，不写入用户文件
        user_items.append({"id": s.id, "file": s.file, "category": s.category,
                           "label": s.label, "enabled": bool(s.enabled)})
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(
        json.dumps({"stickers": user_items}, ensure_ascii=False, indent=2), encoding="utf-8")


def update_sticker(sid: str, new_label: str = None, new_category: str = None,
                   new_enabled: bool = None) -> StickerEntry:
    """修改表情包的 label / category / enabled。传 None 的字段不修改。

    Returns:
        更新后的 StickerEntry
    Raises:
        StickerUpdateError: sid 不存在 / 没有可更新的内容
    """
    if not sid or not isinstance(sid, str):
        raise StickerUpdateError("sid 不能为空")
    has_label = new_label and isinstance(new_label, str) and new_label.strip()
    has_category = new_category in ("可爱", "帅气")
    has_enabled = isinstance(new_enabled, bool)
    if not has_label and not has_category and not has_enabled:
        raise StickerUpdateError("没有可更新的内容")

    # 读→改→写整体加锁，防并发丢更新
    with _lock:
        stickers = _load_registry()
        if sid not in stickers:
            raise StickerUpdateError(f"表情包不存在: {sid}")
        # 服务器版：公共池条目（历史共享）不可修改，只允许改默认项（个人覆盖）与自己上传的
        if user_scope_key() and sid not in editable_ids() and sid not in _STICKERS_DEFAULT:
            raise StickerUpdateError("公共表情包不可修改（仅可修改自己上传的）")

        s = stickers[sid]
        if has_label:
            s.label = new_label.strip()
        if has_category:
            s.category = new_category
        if has_enabled:
            s.enabled = new_enabled
        _write_registry_all(stickers)
    logger.info("用户修改表情包: id=%s label=%s category=%s enabled=%s",
                sid, s.label, s.category, s.enabled)
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
        # 服务器版：公共池条目（历史共享）不可删除，仅可删自己上传的
        if user_scope_key() and sid not in editable_ids():
            raise StickerDeleteError("公共表情包不可删除（仅可删除自己上传的）")

        del stickers[sid]
        _write_registry_all(stickers)
    logger.info("用户删除表情包: id=%s", sid)


# ── 选图 ──────────────────────────────────────────
def pick_sticker(category: str = "可爱") -> StickerEntry | None:
    """从指定分类中随机选一张。无图则降级到可爱，仍无返回 None。"""
    global _PICK_COUNT
    if category not in VALID_CATEGORIES:
        category = "可爱"

    stickers = get_enabled_stickers()
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

    stickers = get_all_stickers()
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
    stickers = get_all_stickers()
    exact = [s for s in stickers.values() if s.label == label]
    if exact:
        with _lock: _PICK_COUNT += 1
        return random.choice(exact)
    return pick_sticker_by_meaning(label)


def get_all_stickers() -> dict[str, StickerEntry]:
    """选图用：只返回启用中的表情包。管理页全量列表用 list_all_stickers()。"""
    return get_enabled_stickers()
