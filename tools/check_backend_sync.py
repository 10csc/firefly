# -*- coding: utf-8 -*-
"""后端镜像一致性门禁（R-03；2026-10-27 T2 增 profile）。

校验 android/app/src/main/python/backend/{app,database} 与仓库源（app/、database/）一致。
（2026-09-14 起 knowledge/ 已归位进 app/assets/character/story/knowledge/，随 app/ 校验）

为什么需要：Gradle 的 syncBackend 只在**构建时**把仓库源拷到 APK 内嵌目录；仓库副本不是安卓端的
真源，但它是唯一能离线读到"安卓端 Python"的地方，审计/排障极易误当真源（实测曾漂移 4 缺失 + 17 差异）。

## 三档 profile（默认 dev，向后兼容旧调用）

  dev      干净 clone/日常：镜像目录不存在 → SKIP + exit 0（与旧行为一致）。
  release  发版前：镜像必须存在；缺失/漂移/运行期产物混入都必须 FAIL。
  deploy   部署前：同 release；不允许 --sync（必须先走构建，不能在部署前现场改镜像）。

## 运行期产物（P6-9）

`app/voice/_worker.log` 这类运行期日志写在源码目录，曾把安卓镜像门禁弄红。这里把
`.log/.tmp/.bak/.swp` 视为运行期产物：不参与比较、不参与 --sync；源码里真有同名代码文件时
应改文件名，而不是放宽门禁。

## 合法缺失 / 豁免

`EXEMPTIONS` 是唯一豁免表：键为相对源根的 posix 路径（如 `app/voice/_worker.log`），值为理由。
新增豁免必须用户批准并在本文件写清；不允许为了变绿随手加。

用法：
    python tools/check_backend_sync.py                 # dev 检查（旧调用）
    python tools/check_backend_sync.py --check         # 兼容旧文档的显式检查
    python tools/check_backend_sync.py --profile release
    python tools/check_backend_sync.py --sync          # 只允许 dev；从源重建镜像
    python tools/check_backend_sync.py --json          # 机器可读（M2 协调器用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROFILES = ("dev", "release", "deploy")
ROOT = Path(__file__).resolve().parent.parent
MIRROR_REL = Path("android") / "app" / "src" / "main" / "python" / "backend"
SOURCES = ("app", "database")
REQUIRED_SOURCES = SOURCES

SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
RUNTIME_SUFFIXES = {".log", ".tmp", ".bak", ".swp"}
SKIP_SUFFIX = {".pyc", ".pyo"} | RUNTIME_SUFFIXES

# 唯一豁免表：相对源根 posix 路径 → 理由。新增必须用户批准。
EXEMPTIONS: dict[str, str] = {
    # 2026-10-30（用户要求「别把隐私数据打进包」）：app/config.json 是**开发机个人配置**
    # （模型选择、effort 等），不得进发行包。gradle 的 syncBackend 已 exclude 它，
    # 所以镜像里本就不该有 —— 这里同步豁免，否则门禁会一直报「缺失」并逼着 --sync 把它塞回去。
    "app/config.json": "开发机个人配置，禁止进发行包（2026-10-30）",
}


def mirror_path(root: Path) -> Path:
    return Path(root) / MIRROR_REL


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


def _runtime_count(root: Path) -> int:
    n = 0
    if not root.is_dir():
        return 0
    for fp in root.rglob("*"):
        if fp.is_file() and fp.suffix.lower() in RUNTIME_SUFFIXES:
            n += 1
    return n


def compare(root: Path = ROOT) -> tuple[list, list, list]:
    """返回 (missing, changed, extra) 三个列表（相对镜像根的路径）。"""
    missing, changed, extra = [], [], []
    mirror = mirror_path(root)
    for name in SOURCES:
        src = _files(Path(root) / name)
        dst = _files(mirror / name)
        for rel, fp in src.items():
            key = f"{name}/{rel}"
            if key in EXEMPTIONS:
                continue
            d = mirror / name / rel
            if not d.exists():
                missing.append(key)
            elif _md5(fp) != _md5(d):
                changed.append(key)
        for rel in dst:
            key = f"{name}/{rel}"
            if key in EXEMPTIONS:
                continue
            if rel not in src:
                extra.append(key)
    return sorted(missing), sorted(changed), sorted(extra)


def do_sync(root: Path = ROOT) -> int:
    mirror = mirror_path(root)
    for name in SOURCES:
        src_root = Path(root) / name
        if not src_root.is_dir():
            continue
        dst_root = mirror / name
        dst_root.mkdir(parents=True, exist_ok=True)
        for rel, fp in _files(src_root).items():
            key = f"{name}/{rel}"
            if key in EXEMPTIONS:      # 豁免项不复制（如 app/config.json 不得进镜像）
                continue
            d = dst_root / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, d)
        # 与 Gradle Sync 同语义：删除镜像里源已不存在的文件（旧文件永久滞留，
        # 例如 2026-09-14 knowledge 归位后 database/dialogues_compiled 的旧拷贝）。
        for rel, fp in _files(dst_root).items():
            key = f"{name}/{rel}"
            if key in EXEMPTIONS:
                continue
            if not (src_root / rel).is_file():
                fp.unlink()
    print(f"已同步后端镜像: {mirror}")
    return 0


def _print_list(label: str, items: list, limit: int) -> None:
    if not items:
        return
    print(f"\n{label} {len(items)} 个:")
    for p in items[:limit]:
        print(f"  {label[:8].upper():<9}{p}")
    if len(items) > limit:
        print(f"  ... 其余 {len(items) - limit} 个")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="后端镜像一致性门禁（dev/release/deploy）")
    ap.add_argument("--profile", choices=PROFILES, default="dev")
    ap.add_argument("--root", default=str(ROOT), help="仓库根（测试用）")
    ap.add_argument("--check", action="store_true", help="兼容旧调用：显式检查（默认行为）")
    ap.add_argument("--sync", action="store_true", help="从源重建镜像（只允许 dev）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（供 M2 协调器消费）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = Path(args.root).resolve()
    mirror = mirror_path(root)
    strict = args.profile in ("release", "deploy")
    missing_sources = [s for s in REQUIRED_SOURCES if not (root / s).is_dir()]
    runtime_ignored = sum(_runtime_count(root / s) for s in SOURCES if (root / s).is_dir())

    result = {
        "profile": args.profile, "root": str(root), "mirror": str(mirror),
        "mirror_exists": mirror.is_dir(), "missing_sources": missing_sources,
        "missing": [], "changed": [], "extra": [], "exempted": len(EXEMPTIONS),
        "runtime_ignored": runtime_ignored, "checked": 0, "ok": True, "msg": "",
    }

    if missing_sources and strict:
        result["ok"] = False
        result["msg"] = f"必需源目录缺失: {missing_sources}"
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("=== 后端镜像一致性（app/ database/ → android backend/）===")
            print(f"  X {result['msg']}（release/deploy 不允许缺源）")
            print("\n结果: FAIL")
        return 1

    if not mirror.is_dir():
        result["ok"] = not strict
        result["msg"] = "镜像目录不存在（未构建安卓端）"
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif strict:
            print("=== 后端镜像一致性（app/ database/ → android backend/） ===")
            print(f"  X 镜像目录不存在: {mirror}（release/deploy 必须先构建/同步）")
            print("\n结果: FAIL")
        else:
            print(f"SKIP: {result['msg']}：{mirror}")
        return 0 if not strict else 1

    if args.sync:
        if strict:
            result["ok"] = False
            result["msg"] = "--sync 只允许 dev；release/deploy 必须先走 Gradle 构建（clean assembleRelease）"
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"X {result['msg']}")
            return 2
        return do_sync(root)

    missing, changed, extra = compare(root)
    checked = sum(len(_files(root / s)) for s in SOURCES if (root / s).is_dir())
    result.update({"missing": missing, "changed": changed, "extra": extra,
                   "checked": checked, "ok": not (missing or changed or extra)})
    total = len(missing) + len(changed) + len(extra)
    result["msg"] = "一致" if not total else f"{total} 处漂移"
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print("=== 后端镜像一致性（app/ database/ → android backend/）===")
    print(f"  profile={args.profile}  checked={checked}  exempted={len(EXEMPTIONS)}  "
          f"runtime_ignored={runtime_ignored}")
    if not total:
        print("结果: 一致 ✓")
        return 0
    _print_list("缺失（仓库有、镜像没有）", missing, 40)
    _print_list("内容不同", changed, 40)
    _print_list("镜像多余（源已删除）", extra, 20)
    print(f"\n共 {total} 处漂移。注意：该目录由 Gradle syncBackend 在**构建时**重建，")
    print("产物正确性不受影响；但仓库副本不是真源，勿据此判断安卓端行为。")
    print("修复：python tools/check_backend_sync.py --sync（或直接 assembleRelease）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
