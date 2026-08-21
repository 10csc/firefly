# -*- coding: utf-8 -*-
"""统一存储层（A2）— 路径公式/原子写/数据类型注册表的收口点

设计要点：
- **原子写**：`atomic_write_text/_bytes`（tmp + replace），供 favorite_store /
  setting_fix_store / memory_manager 等处复用（消灭 3 处各自实现）；
- **数据类型注册表 REGISTRY**：任何新数据类型 = 注册一行，声明
  位置/格式/是否媒体类/是否敏感/同步策略——导出、同步、备份链路的排除点都查这里
  （A0 媒体本地策略的执行面：媒体类注册项自动不进传输清单，元数据随行）；
- **一次性迁移**：`migrate_once`（目标缺则从旧源拷贝，幂等）收口
  「当前路径 + legacy 路径」重复模式。

约定：本模块不 import 业务模块（避免环），路径公式以 app_config 派生函数为主。
"""

import hashlib
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 原子写 ────────────────────────────────────────
def atomic_write_text(fp: Path, text: str) -> bool:
    """原子写文本：先写临时文件再 replace（防写盘中断留下半截文件）。
    返回是否成功（失败仅告警不抛——调用方自行降级）。"""
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_name(fp.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(fp)
        return True
    except OSError as e:
        logger.warning("原子写失败 %s: %s", fp, e)
        return False


def atomic_write_bytes(fp: Path, data: bytes) -> bool:
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_name(fp.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(fp)
        return True
    except OSError as e:
        logger.warning("原子写失败 %s: %s", fp, e)
        return False


def atomic_write_json(fp: Path, data) -> bool:
    try:
        text = json.dumps(data, ensure_ascii=False)
        return atomic_write_text(fp, text)
    except (TypeError, ValueError) as e:
        logger.warning("JSON 序列化失败 %s: %s", fp, e)
        return False


# ── 一次性迁移（幂等）─────────────────────────────
def migrate_once(target: Path, legacy: Path | None) -> bool:
    """legacy（旧路径）存在 且 target 不存在 → 拷贝。返回是否执行了迁移。
    幂等：target 存在后不再动；旧文件保留不删（防误删历史数据）。"""
    if target is None or legacy is None or target.exists() or not legacy.exists():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if legacy.is_file():
            target.write_bytes(legacy.read_bytes())
        else:
            import shutil
            shutil.copytree(legacy, target, dirs_exist_ok=True)
        logger.info("迁移 %s → %s", legacy, target)
        return True
    except OSError as e:
        logger.warning("迁移失败 %s → %s: %s", legacy, target, e)
        return False


# ── 数据类型注册表 ────────────────────────────────
# key 为逻辑类型名；rel 为相对 {mode} 根（或 USER_DIR 根）的路径模板（{mode} 可空）；
# media=True：媒体类（A0 铁律——服务器只传输不保存图片本体，元数据随行）；
# sensitive=True：敏感字段（导出/同步剥除，如 API Key）；
# sync：同步策略——merge-jsonl（append 行合并）/ merge-favorites（并集）/ newest（mtime 新者胜）/
#       metadata（媒体：只同步文字元数据）/ exclude（不进同步清单）。
REGISTRY: dict[str, dict] = {
    "conversation":    {"rel": "{mode}/data/conversation.jsonl", "format": "jsonl",
                        "media": False, "sensitive": False, "sync": "merge-jsonl",
                        "note": "对话历史（append-only，全局 seq）"},
    "memory":          {"rel": "{mode}/data/memory.md", "format": "markdown",
                        "media": False, "sensitive": False, "sync": "newest",
                        "note": "窗口外旧对话压缩存档（rest 整理）"},
    "memory_index":    {"rel": "{mode}/data/.memory_index", "format": "json",
                        "media": False, "sensitive": False, "sync": "newest"},
    "favorites":       {"rel": "{mode}/data/favorites.json", "format": "json",
                        "media": False, "sensitive": False, "sync": "merge-favorites"},
    "pipeline_log":    {"rel": "{mode}/data/pipeline.jsonl", "format": "jsonl",
                        "media": False, "sensitive": False, "sync": "merge-jsonl",
                        "note": "流水线观测日志"},
    "proactive_log":   {"rel": "{mode}/data/proactive_log.jsonl", "format": "jsonl",
                        "media": False, "sensitive": False, "sync": "merge-jsonl"},
    "journal":         {"rel": "{mode}/journal/手账.md", "format": "markdown",
                        "media": False, "sensitive": False, "sync": "newest"},
    "character":       {"rel": "{mode}/character/*.md", "format": "markdown",
                        "media": False, "sensitive": False, "sync": "newest"},
    "stickers":        {"rel": "stickers/", "format": "binary+registry",
                        "media": True, "sensitive": False, "sync": "metadata",
                        "note": "用户表情包：图片本体本地持有；注册表文字元数据（label/category/enabled/哈希）可同步"},
    "images":          {"rel": "{mode}/images/", "format": "binary+meta",
                        "media": True, "sensitive": False, "sync": "metadata",
                        "note": "聊天图片（A9）：图片本地持有，conversation 只存 img_id+desc"},
    "config":          {"rel": "config.json", "format": "json", "media": False,
                        "sensitive": True, "sync": "exclude",
                        "note": "含 API Key（供应商配置）——导出剥离、不进同步"},
    "auth":            {"rel": "auth.json", "format": "json", "media": False,
                        "sensitive": True, "sync": "exclude"},
    "setting_fix":     {"rel": "{mode}/.setting_fix/", "format": "jsonl+json",
                        "media": False, "sensitive": False, "sync": "exclude",
                        "note": "设定纠错中间态（本地工作区，不进同步）"},
}


def get_type(relpath: str) -> dict | None:
    """按相对路径匹配注册表项（用于导出/同步/备份的排除与策略查询）。"""
    rel = relpath.replace("\\", "/").lstrip("/")
    best = None
    for key, spec in REGISTRY.items():
        prefix = spec["rel"].replace("{mode}/", "").replace("{mode}", "")
        if prefix and (rel == prefix.rstrip("/") or rel.startswith(prefix)):
            if best is None or len(prefix) > len(best["spec"]["rel"].replace("{mode}/", "")):
                best = {"key": key, "spec": spec}
    return best


def is_media(relpath: str) -> bool:
    t = get_type(relpath)
    return bool(t and t["spec"].get("media")) if t else (relpath.split("/")[0] in ("stickers", "images"))


def is_sensitive(relpath: str) -> bool:
    t = get_type(relpath)
    if t:
        return bool(t["spec"].get("sensitive"))
    return relpath in ("config.json", "auth.json")


def sync_policy(relpath: str) -> str:
    t = get_type(relpath)
    if t:
        return t["spec"].get("sync", "exclude")
    return "exclude"


def file_sha256(fp: Path) -> str:
    """文件内容哈希（元数据随行用；同步清单同算法）。"""
    h = hashlib.sha256()
    try:
        with fp.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def safe_relative(name: str) -> Path | None:
    """相对路径审查（同步/存储边界）：拒绝绝对、`..`、盘符与非法字符。"""
    if not isinstance(name, str) or not name:
        return None
    n = name.replace("\\", "/")
    if n.startswith("/") or (len(n) >= 2 and n[1] == ":") or ".." in n.split("/"):
        return None
    if re.search(r"[\x00-\x1f]", n):
        return None
    return Path(n)


# 时间戳（备份命名等）
def date_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
