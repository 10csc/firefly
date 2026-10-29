# -*- coding: utf-8 -*-
"""更新清单签名器（2026-10-22 安全复审）

背景：客户端自动更新原来只校验 https + 域名 + 清单里的 sha256；清单本身走 HTTP 且无签名，
网络中间人可以篡改 assets.exe.url + sha256，让 PC 静默安装任意 exe。本工具用**现有热更
RSA 私钥**对规范化的 update.json 签名，客户端用 hotupdate.verify 验签后才使用。

用法：
    python tools/sign_update_manifest.py            # 签名并写回 server/update.json
    python tools/sign_update_manifest.py --check    # 只校验现有签名

签名对象：server/update_manifest.py::sanitize() 之后的清单（去掉 sig 字段）再由
app/hotupdate/net.py::canonical_bytes 规范化。客户端必须用完全相同的字段与顺序。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
SERVER = ROOT / "server"
DEFAULT_MANIFEST = SERVER / "update.json"
DEFAULT_KEY = Path.home() / ".firefly" / "hotupdate_key.pem"
_OPENSSL_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    "/usr/bin/openssl",
)


def die(msg: str, code: int = 1):
    print("X " + msg)
    raise SystemExit(code)


def find_openssl() -> str:
    p = shutil.which("openssl")
    if p and Path(p).is_file():
        return p
    for c in _OPENSSL_CANDIDATES:
        if Path(c).is_file():
            return c
    die("找不到 openssl（签名需要；可用 OPENSSL 环境变量指定）")


def load_modules():
    sys.path.insert(0, str(APP))
    sys.path.insert(0, str(SERVER))
    import update_manifest  # noqa: E402
    from hotupdate.net import canonical_bytes  # noqa: E402
    from hotupdate import verify as hu_verify  # noqa: E402
    return update_manifest, canonical_bytes, hu_verify


def signed_bytes(manifest: dict, update_manifest, canonical_bytes) -> bytes:
    raw = {k: v for k, v in (manifest or {}).items() if k != "sig"}
    return canonical_bytes(update_manifest.sanitize(raw))


def main() -> int:
    ap = argparse.ArgumentParser(description="更新清单签名/校验")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--key", default=str(DEFAULT_KEY))
    ap.add_argument("--check", action="store_true", help="只校验，不写回")
    a = ap.parse_args()
    update_manifest, canonical_bytes, hu_verify = load_modules()
    mp = Path(a.manifest)
    if not mp.is_file():
        die(f"清单不存在：{mp}")
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        die("清单不是对象")
    data = signed_bytes(manifest, update_manifest, canonical_bytes)
    sig = str(manifest.get("sig") or "").strip()
    if a.check:
        ok = False
        if sig:
            try:
                ok = bool(hu_verify.verify(data, sig))
            except Exception as e:
                print(f"X 验签异常：{type(e).__name__}: {e}")
        print(("V 更新清单签名有效" if ok else "X 更新清单签名缺失或无效"))
        print(f"  manifest={mp}")
        print(f"  canonical_sha256={hashlib.sha256(data).hexdigest()[:16]}")
        return 0 if ok else 1
    key = Path(a.key).expanduser()
    if not key.is_file():
        die(f"私钥不存在：{key}（默认 ~/.firefly/hotupdate_key.pem）")
    openssl = find_openssl()
    p = subprocess.run([openssl, "dgst", "-sha256", "-sign", str(key)],
                       input=data, capture_output=True)
    if p.returncode != 0:
        die("openssl 签名失败：" + (p.stderr or b"").decode("utf-8", "replace")[:300])
    sig = base64.b64encode(p.stdout).decode("ascii")
    # 写回：只保留 sanitize 后的正式字段 + sig；未知字段不落盘（避免签名对象漂移）。
    clean = update_manifest.sanitize({k: v for k, v in manifest.items() if k != "sig"})
    clean["sig"] = sig
    if not hu_verify.verify(canonical_bytes({k: v for k, v in clean.items() if k != "sig"}), sig):
        die("签名自检失败（客户端公钥与私钥不匹配）")
    mp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("V 更新清单已签名")
    print(f"  manifest={mp}")
    print(f"  canonical_sha256={hashlib.sha256(data).hexdigest()[:16]}")
    print(f"  sig_len={len(sig)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
