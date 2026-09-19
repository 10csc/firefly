#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热更新 · 补丁产出器（v1：只支持 web 层）。

见 `docs/热更新/02_实现契约.md`。本脚本负责**发布端**的全部工作，且严格按模块铁律：
**审查（输入合法）→ 处理（组装+签名）→ 验证（重新读包校验+公钥自检）→ 输出**。

用法：
    python tools/build_hotupdate.py --init                    # 建立 base 基线（base 刚发布时跑一次）
    python tools/build_hotupdate.py --note "修语音条间距"       # 产出累积补丁（serial 自动 +1）
    python tools/build_hotupdate.py --init --note "…"         # 一次做完（新 base 首发补丁）

产物（默认 `hotupdate_dist/`，**gitignored**，上传到服务器即可）：
    <dist>/<base>/latest                       {"base":"0.8.1","serial":3}   ← 客户端先拿它
    <dist>/<base>/patch-<N>.json               {v, manifest_b64, sig}
    <dist>/<base>/patch-<N>.zip

设计要点（都踩过坑，别改）：
  · **累积式**：每个包都是 `base → 当前工作区` 的完整差异，客户端不必按序应用。
  · **只增/只改**：发现"基线里有、工作区没有"的文件 → **直接拒绝**（删文件必须发整包）。
  · **传规范化字节**（manifest_b64）而不是 manifest 对象，消灭跨语言 JSON 规范化风险。
  · **已存在的 serial 永不覆盖**（内容寻址+不可变）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "static"
CONFIG_PY = ROOT / "app" / "core" / "config.py"
GRADLE = ROOT / "android" / "app" / "build.gradle.kts"

DEFAULT_KEY = Path.home() / ".firefly" / "hotupdate_key.pem"
DEFAULT_BUILD = ROOT / ".tmp_hotupdate"          # 基线 + serial 计数（gitignored）
DEFAULT_DIST = ROOT / "hotupdate_dist"           # 发布区（gitignored，上传用）

_OPENSSL_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    "/usr/bin/openssl",
)


# ── 工具 ────────────────────────────────────────
def die(msg: str, code: int = 1):
    print(f"X {msg}")
    sys.exit(code)


def find_openssl() -> str:
    import os
    p = os.environ.get("OPENSSL") or shutil.which("openssl")
    if p and Path(p).is_file():
        return p
    for c in _OPENSSL_CANDIDATES:
        if Path(c).is_file():
            return c
    die("找不到 openssl（签名要用）。装一个或用环境变量 OPENSSL 指定。")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def canonical_bytes(obj) -> bytes:
    """★ 规范化**只有一处**：转发到 `app/hotupdate/net.py` 的同名函数。

    早期版本在这里自己写了一份 json.dumps(...)，那样就有两个地方定义签名口径 ——
    改了一处忘另一处，症状是"产出的补丁全部验签失败"。现在工具与客户端共用同一个实现。
    """
    sys.path.insert(0, str(ROOT / "app"))
    from hotupdate.net import canonical_bytes as _cb
    return _cb(obj)


def app_version() -> str:
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                  CONFIG_PY.read_text(encoding="utf-8"))
    if not m:
        die(f"读不到 APP_VERSION：{CONFIG_PY}")
    return m.group(1)


def version_code() -> int:
    m = re.search(r"versionCode\s*=\s*(\d+)", GRADLE.read_text(encoding="utf-8"))
    if not m:
        die(f"读不到 versionCode：{GRADLE}")
    return int(m.group(1))


def collect_web() -> dict:
    """{相对 app/static 的 posix 路径: {"sha256":…, "size":…}}"""
    out = {}
    for p in sorted(STATIC_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(STATIC_DIR).as_posix()
        out[rel] = {"sha256": sha256_file(p), "size": p.stat().st_size}
    if not out:
        die(f"静态目录为空：{STATIC_DIR}")
    return out


def sign(key: Path, data: bytes) -> bytes:
    r = subprocess.run([find_openssl(), "dgst", "-sha256", "-sign", str(key)],
                       input=data, capture_output=True)
    if r.returncode != 0:
        die("签名失败: " + r.stderr.decode("utf-8", "replace")[:300])
    return r.stdout


def verify_sig(key: Path, data: bytes, sig: bytes) -> bool:
    """用私钥导出公钥自检（产出后立刻验一次，验不过不许发布）。"""
    ossl = find_openssl()
    pub = subprocess.run([ossl, "pkey", "-in", str(key), "-pubout"],
                         capture_output=True)
    if pub.returncode != 0:
        return False
    tmp = ROOT / ".tmp_hotupdate" / "_pub.pem"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(pub.stdout)
    r = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(tmp)],
                       input=data, capture_output=True)
    # openssl 的 -verify 需要签名单独给；这里用 -signature 形式重来
    sigf = tmp.with_suffix(".sig")
    sigf.write_bytes(sig)
    r = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(tmp),
                        "-signature", str(sigf)],
                       input=data, capture_output=True)
    tmp.unlink(missing_ok=True)
    sigf.unlink(missing_ok=True)
    return r.returncode == 0


# ── 主流程 ──────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="热更新补丁产出器（v1 web 层）")
    ap.add_argument("--init", action="store_true", help="建立/刷新 base 基线")
    ap.add_argument("--note", default="", help="本补丁说明（写进 manifest/CHANGELOG）")
    ap.add_argument("--key", default=str(DEFAULT_KEY), help="私钥路径")
    ap.add_argument("--build-dir", default=str(DEFAULT_BUILD))
    ap.add_argument("--dist", default=str(DEFAULT_DIST))
    ap.add_argument("--base", default=None, help="base 版本（默认取 APP_VERSION）")
    a = ap.parse_args()

    base = a.base or app_version()
    vcode = version_code()
    build = Path(a.build_dir) / base
    dist = Path(a.dist) / base
    baseline_f = build / "baseline.json"
    serial_f = build / "serial.txt"
    key = Path(a.key).expanduser()

    print("=== ① 审查 ===")
    if not STATIC_DIR.is_dir():
        die(f"静态目录不存在：{STATIC_DIR}")
    cur = collect_web()
    print(f"  V 工作区 app/static：{len(cur)} 个文件")
    print(f"  V base = {base}（versionCode {vcode}）")

    # --init：写基线
    if a.init:
        build.mkdir(parents=True, exist_ok=True)
        if baseline_f.exists():
            old = json.loads(baseline_f.read_text(encoding="utf-8"))
            print(f"  ! 已存在基线（{len(old.get('files', {}))} 文件）→ 覆盖为新基线")
        baseline_f.write_text(json.dumps(
            {"base_version": base, "base_version_code": vcode, "files": cur},
            ensure_ascii=False, indent=1), encoding="utf-8")
        serial_f.write_text("0", encoding="utf-8")
        print(f"  V 基线已写入：{baseline_f}（serial 归零）")
        if not a.note:
            print("  （只建基线，未产出补丁。要出补丁请再跑一次不带 --init）")
            return 0

    if not baseline_f.is_file():
        die(f"没有基线：{baseline_f}\n  先跑一次 `--init`（在 base 版本刚发布时做）。")
    bl = json.loads(baseline_f.read_text(encoding="utf-8"))
    if bl.get("base_version") != base:
        die(f"基线 base 不符：基线 {bl.get('base_version')} vs 当前 {base}")
    base_files = bl["files"]

    # ★ 只增/只改：删除必须走整包
    removed = sorted(set(base_files) - set(cur))
    if removed:
        die("检测到**删除**的文件（补丁不允许删文件，请发整包）：\n    " + "\n    ".join(removed))

    changed = {k: v for k, v in cur.items()
               if k not in base_files or base_files[k]["sha256"] != v["sha256"]}
    if not changed and not a.init:
        die("与基线相比没有任何变化 —— 没有东西可发。")
    print(f"  V 变更文件 {len(changed)} 个（新增 {len(set(cur) - set(base_files))}，"
          f"修改 {len(set(changed) & set(base_files))}）")

    serial = int(serial_f.read_text(encoding="utf-8").strip() or "0") + 1
    dist.mkdir(parents=True, exist_ok=True)
    zip_name = f"patch-{serial}.zip"
    json_name = f"patch-{serial}.json"
    for n in (zip_name, json_name):
        if (dist / n).exists():
            die(f"发布区已存在 {n} —— 已发布的补丁**永不覆盖**。"
                f"（要么换 serial，要么先把旧产物移走归档）")

    print("\n=== ② 处理：打包 + 签名 ===")
    # 累积式：changed 已经是"相对 base 的完整差异"，直接全量进包
    zpath = dist / zip_name
    files_meta = []
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(changed):
            arc = f"web/{rel}"
            z.write(STATIC_DIR / rel, arc)
            files_meta.append({"path": arc,
                               "sha256": changed[rel]["sha256"],
                               "size": changed[rel]["size"]})
    zip_sha = sha256_file(zpath)
    zip_size = zpath.stat().st_size
    print(f"  V 包：{zip_name}  {zip_size/1024:.1f} KB  sha256 {zip_sha[:12]}…")

    manifest = {
        "format": 1,
        "base_version": base,
        "base_version_code": vcode,
        "serial": serial,
        "issued_at": int(time.time()),
        "cumulative": True,
        "layer": "web",
        "note": a.note or "(无说明)",
        "zip": {"name": zip_name, "sha256": zip_sha, "size": zip_size},
        "files": files_meta,
        "rollout_percent": 100,
        "min_safe_serial": 1,
        "revoked_serials": [],
    }
    raw = canonical_bytes(manifest)
    if not key.is_file():
        die(f"私钥不存在：{key}\n  先跑 `python tools/hotupdate_keys.py --gen`")
    sig = sign(key, raw)
    payload = {"v": 1,
               "manifest_b64": base64.b64encode(raw).decode("ascii"),
               "sig": base64.b64encode(sig).decode("ascii")}
    (dist / json_name).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    print(f"  V 清单：{json_name}（规范化 {len(raw)} 字节，签名 {len(sig)} 字节）")

    print("\n=== ③ 验证 ===")
    # 3.1 公钥自检签名
    if not verify_sig(key, raw, sig):
        (dist / json_name).unlink(missing_ok=True)
        (dist / zpath.name).unlink(missing_ok=True)
        die("签名自检失败，已删除产物。")
    print("  V 签名自检（openssl -verify）通过")
    # 3.2 重新读包，逐文件核对（防"写进去的和清单说的不一致"）
    with zipfile.ZipFile(zpath) as z:
        names = sorted(z.namelist())
        want = sorted(f["path"] for f in files_meta)
        if names != want:
            die(f"包内文件与清单不一致：包 {names} vs 清单 {want}")
        for f in files_meta:
            got = hashlib.sha256(z.read(f["path"])).hexdigest()
            if got != f["sha256"]:
                die(f"包内 {f['path']} 校验不符")
    print(f"  V 回读校验：{len(names)} 个文件 sha256 全部匹配")
    if sha256_file(zpath) != zip_sha:
        die("zip 本体 sha256 与清单不符（写出后又被改？）")
    print("  V zip 本体 sha256 匹配")

    print("\n=== ④ 输出 ===")
    (dist / "latest").write_text(
        json.dumps({"base": base, "serial": serial}, ensure_ascii=False),
        encoding="utf-8")
    serial_f.write_text(str(serial), encoding="utf-8")
    # CHANGELOG：规范 §八要求每条必填「序号/日期/改了哪些文件/为什么」——
    # 这是用户报 bug 时"我到底跑的什么代码"的唯一凭据，不能靠记忆。
    log = Path(a.dist) / "CHANGELOG.md"
    if not log.exists():
        log.write_text("# 热更新 CHANGELOG\n\n"
                       "> 每条对应一个不可变补丁。复现运行版 = base commit + 依次应用 1..N。\n"
                       "> 代码本体在 git（tag hot-<base>-<serial>），这里只记「改了什么 / 为什么」。\n\n",
                       encoding="utf-8")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"## {base} · serial {serial} · "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime())}\n")
        f.write(f"- 说明：{a.note or '(无)'}\n")
        f.write(f"- 包：`{zip_name}`（{zip_size / 1024:.1f} KB，sha256 `{zip_sha[:16]}…`）\n")
        f.write(f"- 文件（{len(files_meta)}）：\n")
        for fm in files_meta:
            f.write(f"  - `{fm['path']}`\n")
        f.write(f"- git tag：`hot-{base}-{serial}`（**发版时务必打**）\n\n")
    print(f"  V 发布区：{dist}")
    print(f"    latest            → {{'base':'{base}','serial':{serial}}}")
    print(f"    {json_name}")
    print(f"    {zip_name}")
    print(f"    CHANGELOG.md（已追加 serial {serial} 条目）")
    print(f"  V serial 计数推进到 {serial}")
    print(f"\n  下一步：python tools/check_hotupdate.py --dist {dist}   （第 5 门禁）")
    print(f"         然后整目录上传到服务器 hotupdate/{base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
