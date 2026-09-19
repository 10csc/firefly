# -*- coding: utf-8 -*-
"""快照清单（_manifest.json）口径与核对（阶段 2.8 自 restore.py 拆出，纯移动）

打包端（routes_snapshot）与恢复端（restore.py）核对用**同一份**哈希口径，
故独立成模块（两侧都依赖的数据层），避免"两边各写一份、日后悄悄漂移"。
依赖方向：restore.py → 本模块（顶层 import）；本模块不 import restore
（`_pack_restorable` 在函数内 lazy import，防循环）。
"""

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 导出/备份打包排除的内部目录（同步冲突备份/设定纠错中间态，不是用户数据）。
# 3.8 起由本模块承载，restore.py 顶层 re-import（两侧同一份，防漂移）。
_EXPORT_EXCLUDE_DIRS = {".sync_backups", ".setting_fix", ".sync_conflicts"}


def _sha256_file(fp: Path) -> str:
    """文件 sha256（读失败返回 ""，调用方按"无法核对"处理，不致命）。"""
    h = hashlib.sha256()
    try:
        with open(fp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _pack_digest(entries) -> str:
    """一个包（或 stickers 集合）的 sha256 汇总：对 `(相对路径, 文件sha256)` 排序后逐行哈希。

    口径：`sha256( "{rel}\\0{filehash}\\n" ... )` —— 路径参与哈希（改名/换文件一定变），
    排序保证与打包顺序无关。空集合返回 sha256("") 而不是报错（"这个包在快照里没有文件"是合法状态）。"""
    h = hashlib.sha256()
    for rel, fh in sorted(entries):
        h.update(f"{rel}\0{fh}\n".encode("utf-8"))
    return h.hexdigest()


def _read_snapshot_manifest(zf) -> dict | None:
    """读快照里的 `_manifest.json`（3.4）。缺失/损坏 → None（旧 zip 走现逻辑）。"""
    try:
        raw = zf.read("_manifest.json")
    except KeyError:
        return None
    except Exception as e:
        logger.warning("快照 manifest 读取失败，按旧格式恢复: %s", e)
        return None
    try:
        man = json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.warning("快照 manifest 解析失败，按旧格式恢复: %s", e)
        return None
    return man if isinstance(man, dict) else None


def _verify_snapshot_manifest(zf, entries, man: dict) -> dict:
    """按 manifest 核对 zip 内容，产出可回传的摘要（3.4）。

    核对口径：对每个清单里的包，用**同一份** `_pack_digest` 复算 zip 条目哈希并比对。
    `verified=False` 只告警不中止 —— 快照可能被手工改过（用户有权改自己的备份），
    此时"照旧恢复 + 明确告知不可信"比"直接拒绝"更符合备份工具的定位；真正的安全审查
    （zip slip / 炸弹 / 大小）在 `_zip_safe_entries` 与 `_validate_snapshot_zip`，与本核对无关。

    **刻意不做过滤**：清单不用于"只恢复清单里的条目"。手改 zip 里多出来的目录仍按现逻辑走
    （宁可多恢复，不可因为清单缺一条就静默丢数据）；清单的作用是**核对 + 上报**。"""
    from infra.sync.restore import _pack_restorable   # lazy：防与 restore 顶层互引成环
    per_top: dict[str, list] = {}
    for name, info in entries:
        parts = name.split("/")
        if len(parts) < 2:
            continue
        rel = "/".join(parts[1:])
        if any(p in _EXPORT_EXCLUDE_DIRS for p in parts[1:]):
            continue
        try:
            with zf.open(info) as f:
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            per_top.setdefault(parts[0], []).append((rel, h.hexdigest()))
        except Exception:
            per_top.setdefault(parts[0], [])
    out_packs, ok_all, missing = [], True, []
    for p in man.get("packs") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        pid = str(p["id"])
        actual = per_top.get(pid)
        if actual is None:
            missing.append(pid)
            ok_all = False
            out_packs.append({"id": pid, "in_manifest": True, "in_zip": False, "ok": False})
            continue
        ok = (not p.get("sha256")) or (_pack_digest(actual) == p.get("sha256"))
        ok_all = ok_all and ok
        out_packs.append({"id": pid, "in_manifest": True, "in_zip": True,
                          "files": len(actual), "ok": ok})
    # zip 里有、清单里没有的顶层包目录（手改/旧清单）也报出来
    man_ids = {str(p.get("id")) for p in (man.get("packs") or []) if isinstance(p, dict)}
    extra = sorted(t for t in per_top
                   if t not in man_ids and t != "stickers" and _pack_restorable(t))
    if missing or extra or not ok_all:
        logger.warning("快照清单核对：verified=%s 缺失=%s 清单外=%s",
                       ok_all, missing or "-", extra or "-")
    return {"snapshot_version": man.get("snapshot_version", 1),
            "app_version": man.get("app_version", ""),
            "created_at": man.get("created_at", ""),
            "packs": out_packs, "extra_in_zip": extra, "missing_in_zip": missing,
            "verified": ok_all and not missing}
