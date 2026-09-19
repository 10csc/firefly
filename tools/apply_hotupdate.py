#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热更新 · 补丁回归 harness（规范 §九「补丁级回归」）。

把补丁应用到一个**干净的 base 目录**，然后交给调用方去跑测试/冒烟。不碰仓库本体。

    python tools/apply_hotupdate.py --patch hotupdate_dist/0.8.1/patch-1.zip \\
        --base <干净 checkout 的 app/static 目录> --out .tmp_hotupdate/regress

输出就是一个"打过补丁的 app/static"，可直接 `--static <out>` 挂给测试用。
**不需要新代码树**（规范 §八：运行版 = base + 补丁序列，可重建）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATIC = ROOT / "app" / "static"


def _find_openssl() -> str:
    """本机 openssl 不在 PATH（Git for Windows 自带），所以显式找一遍。

    2026-09-19 踩到：这里原来直接 subprocess 调 "openssl" → WinError 2，
    而其它三个工具都有这个查找函数。同一套东西别写两份。
    """
    import os
    import shutil as _sh
    p = os.environ.get("OPENSSL") or _sh.which("openssl")
    if p and Path(p).is_file():
        return p
    for c in (r"C:\Program Files\Git\usr\bin\openssl.exe",
              r"C:\Program Files\Git\mingw64\bin\openssl.exe",
              "/usr/bin/openssl"):
        if Path(c).is_file():
            return c
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="把热更补丁应用到一份干净的 base 目录")
    ap.add_argument("--patch", required=True, help="patch-N.zip")
    ap.add_argument("--manifest", default="", help="patch-N.json（给了就顺带验签）")
    ap.add_argument("--base", default=str(DEFAULT_STATIC), help="干净 base 的 static 目录")
    ap.add_argument("--out", required=True, help="输出目录（base 的副本 + 补丁覆盖）")
    ap.add_argument("--verify-key", default="", help="私钥路径（给了就做签名自洽校验）")
    a = ap.parse_args()

    patch = Path(a.patch)
    base = Path(a.base)
    out = Path(a.out)
    if not patch.is_file():
        print(f"X 补丁不存在：{patch}")
        return 2
    if not base.is_dir():
        print(f"X base 目录不存在：{base}")
        return 2

    # 审查：清单与包必须自洽（与第 5 门禁同一套判断，这里再查一遍——
    # 回归 harness 的作用是"用同一份产物再验一次"，不能假定上游已经查过）
    files = []
    if a.manifest:
        mf = Path(a.manifest)
        if not mf.is_file():
            print(f"X 清单不存在：{mf}")
            return 2
        payload = json.loads(mf.read_text(encoding="utf-8"))
        import base64
        raw = base64.b64decode(payload["manifest_b64"])
        m = json.loads(raw.decode("utf-8"))
        files = m.get("files") or []
        if a.verify_key:
            import subprocess
            ossl = _find_openssl()
            if not ossl:
                print("X 找不到 openssl，无法验签（用 --verify-key 就要装）")
                return 2
            sig = base64.b64decode(payload["sig"])
            tmp = ROOT / ".tmp_hotupdate" / "_apply_pub.pem"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            pub = subprocess.run([ossl, "pkey", "-in", a.verify_key, "-pubout"],
                                 capture_output=True)
            if pub.returncode != 0:
                print("X 导出公钥失败：", pub.stderr.decode("utf-8", "replace")[:200])
                return 2
            tmp.write_bytes(pub.stdout)
            sigf = tmp.with_suffix(".sig")
            sigf.write_bytes(sig)
            r = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(tmp),
                                "-signature", str(sigf)], input=raw, capture_output=True)
            tmp.unlink(missing_ok=True)
            sigf.unlink(missing_ok=True)
            if r.returncode != 0:
                print("X 签名校验失败")
                return 2
            print("  V 签名校验通过")
        print(f"  V 清单声明 {len(files)} 个文件（base={m.get('base_version')} serial={m.get('serial')}）")

    print(f"\n=== 应用 {patch.name} → {out} ===")
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(base, out)
    n_before = sum(1 for p in out.rglob("*") if p.is_file())

    written = 0
    with zipfile.ZipFile(patch) as z:
        declared = {f["path"]: f["sha256"] for f in files} if files else None
        for arc in z.namelist():
            if not arc.startswith("web/"):
                print(f"X 包内有非 web/ 前缀的文件：{arc}")
                return 2
            rel = arc[len("web/"):]
            if ".." in rel.split("/") or rel.startswith("/"):
                print(f"X 路径越界：{arc}")
                return 2
            data = z.read(arc)
            got = hashlib.sha256(data).hexdigest()
            if declared is not None:
                if arc not in declared:
                    print(f"X 包内有清单没声明的文件：{arc}")
                    return 2
                if declared[arc] != got:
                    print(f"X {arc} 校验不符")
                    return 2
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            written += 1
    n_after = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"  V 覆盖 {written} 个文件；文件数 {n_before} → {n_after}"
          f"（只增只改 ⇒ 不应减少）")
    if n_after < n_before:
        print("X 文件数变少 —— 补丁不许删文件")
        return 2
    print(f"\n结果: PASS  打过补丁的静态目录：{out}")
    print("  回归用法：把该目录的内容与 base 做 diff 跑测试，或设 FIREFLY_STATIC_OVERRIDE 指过去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
