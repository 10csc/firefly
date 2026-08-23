# -*- coding: utf-8 -*-
"""文件级双向同步引擎（A1）— 三端共用（PC / 安卓 / 服务器，全 Python 无依赖）

设计要点（与 02-A1 / 09-A1 卡一致）：
- 清单协商：对 user_data/{mode}/ 的文字类文件 + images/ 压缩图（blob）算 {path: {size, sha256, mtime}}；
- 裁决：仅本地有 → 上传；仅服务器有 → 下载；两边都有 → append-only 行级合并（确定性排序，
  两端各自合并后字节一致、sha 收敛）/ 文档新者胜 / blob 不合并；
- 范围审查以 storage.REGISTRY 的 sync 策略为唯一真相源：exclude 排除；stickers/ 等媒体本体
  维持本地策略不进清单；images/ 压缩图按 blob 放行（一层目录 + 扩展名白名单）；
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

from modules import storage

logger = logging.getLogger(__name__)

# ── 清单范围 ─────────────────────────────────────
TEXT_EXTS = {".md", ".jsonl", ".json", ".txt"}
EXCLUDE_DIRS = {".setting_fix", ".sync_backups", ".sync_conflicts"}
EXCLUDE_NAMES = {".round_count"}
# 媒体/同步策略不再由本模块硬编码（原 MEDIA_DIRS 已删）——以 storage.REGISTRY 的
# sync 策略为准（storage.is_syncable / is_blob）。


def _date_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def in_sync_scope(relpath: Path, mode_root: Path) -> bool:
    """是否属同步范围（审查约束，策略以 storage.REGISTRY 为唯一真相源）：
    - 结构审查：拒绝越界（..）、内部目录（EXCLUDE_DIRS）、内部文件（EXCLUDE_NAMES）；
    - 注册表 exclude（config/auth/.setting_fix）→ 排除；
    - blob（images/ 压缩图）→ 仅放行 {mode}/images/ 一层目录下白名单扩展名
      （.jpg/.jpeg/.png/.webp/.gif），不递归子目录；
    - 媒体本体（stickers/ 等 metadata 类）→ 排除（A0 媒体本地策略不变）；
    - 其余（含未注册路径）→ 文字类扩展名（TEXT_EXTS）放行。"""
    parts = Path(relpath).parts
    if not parts:
        return False
    if parts[0] in ("..", ".") or ".." in parts:
        return False
    if any(part in EXCLUDE_DIRS for part in parts):
        return False
    if Path(relpath).name in EXCLUDE_NAMES:
        return False
    posix = Path(relpath).as_posix()
    if not storage.is_syncable(posix):
        return False
    if storage.is_blob(posix):
        return True  # is_syncable 已做一层目录 + 扩展名白名单审查
    return Path(relpath).suffix.lower() in TEXT_EXTS


def scan_manifest(root: Path) -> dict:
    """扫描 {mode} 根目录在同步范围内的文件（文字类 + images/ blob）
    → {relpath: {"size","sha256","mtime"}}。"""
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
    """jsonl 行的 (去重键, 排序键)：
    - 去重键：有 seq 字段（int 化）→ "seq:<int>"；否则 → "sha:<行sha256>"（全行内容）；
    - 排序键（确定性重排，两端各自合并后字节一致、sha 收敛）：
      有 seq → (0, seq, 行sha前8位)；无 seq 有 _ts(float) → (1, 0, _ts, 行sha前8位)；
      无 _ts 有 time 字符串 → (1, 1, time字符串, 行sha前8位)；都没有 → (2, 行sha)。
      第 1 档内的 0/1 子档是类型分组（_ts 行与 time 行分档，避免 float/str 混比不可排序）。
      行sha前8位作同键 tie-break，保证同 seq/_ts/time 的不同行顺序也确定。"""
    sha = hashlib.sha256(line.encode("utf-8")).hexdigest()
    sha8 = sha[:8]
    try:
        rec = json.loads(line)
    except Exception:
        rec = None
    if isinstance(rec, dict):
        if "seq" in rec:
            try:
                seq = int(rec["seq"])
                return (f"seq:{seq}", (0, seq, sha8))
            except (TypeError, ValueError):
                pass
        if "_ts" in rec:
            try:
                return (f"sha:{sha}", (1, 0, float(rec["_ts"]), sha8))
            except (TypeError, ValueError):
                pass
        if isinstance(rec.get("time"), str):
            return (f"sha:{sha}", (1, 1, rec["time"], sha8))
    return (f"sha:{sha}", (2, sha))


def merge_jsonl(local_text: str, remote_text: str) -> tuple[str, list]:
    """append-only 行级合并（conversation.jsonl / pipeline.jsonl / proactive_log.jsonl）。
    去重键 = seq（int 化）或行全文 sha；同键异内容 → 后处理的一侧行（=远端）进冲突备份
    （本地行先处理=本地优先，不静默丢数据）。
    去重后按确定性排序键重排再拼接（见 _line_key）——两端各自合并的结果字节完全相等
    （sha 收敛），消除「本地在前、远端追加」导致的行序发散与重复全量重传。
    返回 (合并文本, 冲突行)。"""
    def parse(text):
        out = []
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            dedup, sort_key = _line_key(ln)
            out.append({"dedup": dedup, "sort": sort_key, "line": ln})
        return out

    rows = parse(local_text) + parse(remote_text)
    seen: dict = {}      # dedup 键 -> 已保留的行
    kept: list = []
    conflicts: list = []
    for row in rows:
        if row["dedup"] in seen:
            if seen[row["dedup"]] != row["line"]:
                conflicts.append(row["line"])   # 同键异内容：远端行备份（本地优先）
            continue
        seen[row["dedup"]] = row["line"]
        kept.append(row)
    kept.sort(key=lambda r: r["sort"])
    text = "\n".join(r["line"] for r in kept)
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


def _content_sha(content) -> str:
    """合并输入内容哈希（blob 判定用；bytes 直算，str 按 utf-8 编码）。"""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def apply_merge_file(root: Path, relpath: str, local_text: str, remote_text: str,
                     local_mtime: float, remote_mtime: float) -> tuple[str, list, bool]:
    """执行层裁决：返回 (新内容, 冲突行备份列表, 是否有变化)。
    - blob（images/ 不可变文件）：不做内容合并——同 sha 跳过；异 sha（uuid 命名下几乎
      不可能）记日志 + 保留本地 + 返回不变（changed=False）；
    - .jsonl：append 行级确定性合并；favorites.json：并集；
    - 其余文档类：本地已有文件时新者胜（mtime）。"""
    fp = root / relpath
    if storage.is_blob(relpath):
        if _content_sha(local_text) != _content_sha(remote_text):
            logger.warning("blob 合并请求异 sha（应罕见，uuid 命名），保留本地: %s", relpath)
        return local_text, [], False
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
