# -*- coding: utf-8 -*-
"""文件级双向同步引擎（A1）— 三端共用（PC / 安卓 / 服务器，全 Python 无依赖）

设计要点（与 02-A1 / 09-A1 卡一致）：
- 清单协商：对 user_data/{mode}/ 的**文字类**文件算 {path: {size, sha256, mtime}}；
- 裁决：仅本地有 → 上传；仅服务器有 → 下载；两边都有 → append-only 行级合并 / 文档新者胜；
- 媒体本地策略：stickers/ 二进制与 images/ 不进清单（服务器只经过文字描述）；
- 冲突兜底：文档类覆盖前双方各留备份（.sync_backups/），append 冲突行备份进 .sync_conflicts/。

本模块=纯函数（可测试）；HTTP 编排在 routes.py 的 /sync/* 端点（本地后端与服务器版共用，
服务器版经 _user_ctx 自动按用户目录隔离）。
"""

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 清单范围 ─────────────────────────────────────
TEXT_EXTS = {".md", ".jsonl", ".json", ".txt"}
EXCLUDE_DIRS = {".setting_fix", ".sync_backups", ".sync_conflicts"}
EXCLUDE_NAMES = {".round_count"}
# 媒体目录（永不上服务器）：sticker 文件本体 / 将来的图片
MEDIA_DIRS = {"stickers", "images"}


def _date_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def in_sync_scope(relpath: Path, mode_root: Path) -> bool:
    """是否属同步范围（文字类 + 排除媒体目录 + 排除内部目录）。审查约束。"""
    parts = Path(relpath).parts
    if not parts:
        return False
    if parts[0] in ("..", ".") or ".." in parts:
        return False
    if any(part in EXCLUDE_DIRS for part in parts):
        return False
    if parts[0] in MEDIA_DIRS:
        return False
    if Path(relpath).name in EXCLUDE_NAMES:
        return False
    return Path(relpath).suffix.lower() in TEXT_EXTS


def scan_manifest(root: Path) -> dict:
    """扫描 {mode} 根目录文字类文件 → {relpath: {"size","sha256","mtime"}}。"""
    out = {}
    try:
        for fp in sorted(root.rglob("*")):
            if not fp.is_file():
                continue
            rel = fp.relative_to(root)
            if not in_sync_scope(rel, root):
                continue
            try:
                st = fp.stat()
                digest = hashlib.sha256(fp.read_bytes()).hexdigest()
            except OSError:
                continue
            out[rel.as_posix()] = {"size": st.st_size, "sha256": digest,
                                   "mtime": round(st.st_mtime, 3)}
    except OSError as e:
        logger.warning("清单扫描失败: %s", e)
    return out


def _line_key(line: str) -> tuple:
    """jsonl 行排序/去重键：带 seq 用 seq，否则全文哈希。返回 (has_seq, key, line)。"""
    try:
        rec = json.loads(line)
        if isinstance(rec, dict) and "seq" in rec:
            return (True, str(rec["seq"]), line)
    except Exception:
        pass
    return (False, hashlib.sha256(line.encode("utf-8")).hexdigest(), line)


def merge_jsonl(local_text: str, remote_text: str) -> tuple[str, list]:
    """append-only 行级合并（conversation.jsonl / pipeline.jsonl / proactive_log.jsonl）。
    键 = seq（有 seq 字段时）或全文哈希；本地行优先保序在前，远端新增行依次追加；
    同键异内容 → 远端行进冲突备份（不静默丢数据）。返回 (合并文本, 冲突行)。"""
    def parse(text):
        out = []
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            has_seq, key, line = _line_key(ln)
            out.append({"key": key, "line": line})
        return out

    rows = parse(local_text) + parse(remote_text)
    seen: dict = {}      # key -> 已保留的行
    ordered: list = []
    conflicts: list = []
    for row in rows:
        if row["key"] in seen:
            if seen[row["key"]] != row["line"]:
                conflicts.append(row["line"])   # 同键异内容：远端行备份（本地优先）
            continue
        seen[row["key"]] = row["line"]
        ordered.append(row["line"])
    text = "\n".join(ordered)
    if text:
        text += "\n"
    return text, conflicts


def merge_favorites(local_text: str, remote_text: str) -> tuple[str, list]:
    """收藏并集（按 seq 或 f 前缀 id 去重，现有 favorite_store 同语义）。"""
    def parse(text):
        try:
            arr = json.loads(text)
            return arr if isinstance(arr, list) else []
        except Exception:
            return []

    items = parse(local_text) + parse(remote_text)
    seen = set()
    kept = []
    for it in items:
        if not isinstance(it, dict):
            continue
        k = str(it.get("seq", "")) or str(it.get("id", "")) or json.dumps(it, sort_keys=True)
        if k in seen:
            continue
        seen.add(k)
        kept.append(it)
    try:
        kept.sort(key=lambda x: (x.get("seq", 0) if isinstance(x.get("seq"), int) else 1 << 62,
                                 str(x.get("time", ""))))
    except Exception:
        pass
    return json.dumps(kept, ensure_ascii=False, indent=1), []


def merge_docs_by_mtime(local: dict, remote: dict) -> tuple[str, str, str]:
    """文档类（md/无 seq 的 json）：新者胜（mtime），返回 (winner, loser_text_preview?)。
    返回 (谁赢 local|remote, 双方备份文件名, 是否需要同步)。"""
    if local.get("sha256") == remote.get("sha256"):
        return ("same", "", False)
    if (remote.get("mtime", 0) or 0) > (local.get("mtime", 0) or 0):
        return ("remote", "", True)
    return ("local", "", True)


def backup_file(fp: Path, backups_dir: Path, tag: str) -> Path:
    """覆盖前备份（文档类冲突各留一份）。返回备份路径。"""
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
        dst = backups_dir / f"{fp.name}.{tag}.{_date_stamp()}.bak"
        shutil.copy2(fp, dst)
        return dst
    except OSError as e:
        logger.warning("同步备份失败: %s", e)
        return backups_dir / ""


def plan_sync(local_m: dict, remote_m: dict) -> dict:
    """清单对比 → 行动方案。
    {to_upload: [path], to_download: [path], to_merge: [path], same: [path]}
    to_merge = 两边都有且内容不同的 append-only/文档类（执行层再按类型做行合并或新者胜）。"""
    to_upload, to_download, to_merge, same = [], [], [], []
    for path, info in local_m.items():
        r = remote_m.get(path)
        if r is None:
            to_upload.append(path)
        elif r.get("sha256") != info.get("sha256"):
            to_merge.append(path)
        else:
            same.append(path)
    for path in remote_m:
        if path not in local_m:
            to_download.append(path)
    return {"to_upload": to_upload, "to_download": to_download,
            "to_merge": to_merge, "same": same}


def apply_merge_file(root: Path, relpath: str, local_text: str, remote_text: str,
                     local_mtime: float, remote_mtime: float) -> tuple[str, list, bool]:
    """执行层裁决：返回 (新内容, 冲突行备份列表, 是否有变化)。本地已有文件时新者胜。"""
    fp = root / relpath
    if relpath.endswith(".jsonl"):
        merged, conflicts = merge_jsonl(local_text, remote_text)
        changed = merged.rstrip("\n") != local_text.rstrip("\n")
        return merged, conflicts, changed
    if relpath.endswith("favorites.json"):
        merged, conflicts = merge_favorites(local_text, remote_text)
        changed = merged != local_text
        return merged, conflicts, changed
    # 文档类：新者胜（本地 mtime 更新则保留本地；否则用远程）
    if (remote_mtime or 0) > (local_mtime or 0):
        if remote_text != local_text:
            return remote_text, [], True
        return local_text, [], False
    return local_text, [], False
