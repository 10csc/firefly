# -*- coding: utf-8 -*-
"""后端镜像一致性门禁（R-03）：校验 android/app/src/main/python/backend/{app,knowledge,database}
与仓库源（app/、knowledge/、database/）是否一致。

为什么需要：Gradle 的 syncBackend 只在**构建时**把仓库源拷到 APK 内嵌目录，仓库里那份副本
被 .gitignore 忽略却实际存在——它**不是**安卓端的真源，但却是唯一能离线读到"安卓端 Python"
的地方，审计/排障极易误当真源（实测曾漂移 4 缺失 + 17 差异）。三门禁（check_version /
sync_frontends / build_frontend_bundle）都只校验前端与版本号，**不校验后端**。

用法：
    python tools/check_backend_sync.py           # 校验，漂移则退出码 1
    python tools/check_backend_sync.py --sync     # 从仓库源同步过去（构建前也可直接跑）

说明：镜像目录不存在时视为"未构建/未同步"，输出 SKIP 并返回 0（不阻塞干净 clone）。
"""
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / "android" / "app" / "src" / "main" / "python" / "backend"
SOURCES = {
    "app": ROOT / "app",
    "knowledge": ROOT / "knowledge",
    "database": ROOT / "database",
}
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def _md5(fp: Path) -> str:
    h = hashlib.md5()
    with fp.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> dict:
    out = {}
    if not root.is_dir():
        return out
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts) or fp.suffix.lower() in SKIP_SUFFIX:
            continue
        out[rel.as_posix()] = fp
    return out


def compare() -> tuple[list, list, list]:
    """返回 (missing, changed, extra) 三个列表（相对镜像根的路径）。"""
    missing, changed, extra = [], [], []
    root_rel = ROOT
    for name, src_root in SOURCES.items():
        src = _files(src_root)
        dst = _files(MIRROR / name)
        for rel, fp in src.items():
            d = MIRROR / name / rel
            if not d.exists():
                missing.append(f"{name}/{rel}")
            elif _md5(fp) != _md5(d):
                changed.append(f"{name}/{rel}")
        for rel in dst:
            if rel not in src:
                extra.append(f"{name}/{rel}")
    return sorted(missing), sorted(changed), sorted(extra)


def do_sync() -> int:
    for name, src_root in SOURCES.items():
        if not src_root.is_dir():
            continue
        dst_root = MIRROR / name
        dst_root.mkdir(parents=True, exist_ok=True)
        for rel, fp in _files(src_root).items():
            d = dst_root / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, d)
    print(f"已同步后端镜像: {MIRROR}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not MIRROR.is_dir():
        print(f"SKIP: 镜像目录不存在（未构建安卓端）：{MIRROR}")
        return 0
    if "--sync" in sys.argv:
        return do_sync()
    missing, changed, extra = compare()
    total = len(missing) + len(changed) + len(extra)
    print("=== 后端镜像一致性（app/ knowledge/ database/ → android backend/）===")
    if not total:
        print("结果: 一致 ✓")
        return 0
    if missing:
        print(f"\n缺失 {len(missing)} 个（仓库有、镜像没有）:")
        for p in missing[:40]:
            print(f"  MISSING  {p}")
        if len(missing) > 40:
            print(f"  ... 其余 {len(missing) - 40} 个（应从源码构建或跑 --sync）")
    if changed:
        print(f"\n内容不同 {len(changed)} 个:")
        for p in changed[:40]:
            print(f"  DIFF     {p}")
        if len(changed) > 40:
            print(f"  ... 其余 {len(changed) - 40} 个")
    if extra:
        print(f"\n镜像多余 {len(extra)} 个（源已删除）:")
        for p in extra[:20]:
            print(f"  EXTRA    {p}")
    print(f"\n共 {total} 处漂移。注意：该目录由 Gradle syncBackend 在**构建时**重建，")
    print("产物正确性不受影响；但仓库副本不是真源，勿据此判断安卓端行为。")
    print("修复：python tools/check_backend_sync.py --sync（或直接 assembleRelease）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
