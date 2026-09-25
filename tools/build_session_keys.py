#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话加密密钥：生成密钥对 + 把公钥写进前端常量（见 app/session_crypto.py）。

    python tools/build_session_keys.py --gen            # 生成到 ~/.firefly/session_priv.pem
    python tools/build_session_keys.py --sync           # 用已有私钥刷新前端公钥常量
    python tools/build_session_keys.py --show           # 打印当前公钥（n 的 base64）

**为什么用 openssl 生成而不是自己造密钥**：生成 RSA 密钥要用随机素数，自己写必然出事
（`hotupdate/verify.py` 的取舍同理：**用私钥/造密钥绝不手写**）。本工具只做两件安全的事：
  1. 调 `openssl genpkey` 生成 PKCS#8 私钥（零第三方依赖，Git for Windows 自带 3.2.1）；
  2. 从**私钥**里算出公钥参数，并把 base64(n) 写进 `app/static/js/session_crypto.js`。

为什么用私钥反算公钥而不是 `openssl rsa -pubout`：公钥的 SPKI 里嵌着 RSA 公钥 DER
（又是"字符串里抽二进制"的老坑，见 docs/错误总结.md #14）。这里直接解析私钥
（PKCS#8 里有现成的 n 与 e），**同一份解析函数也被服务端解密用**，两边不可能对不上。

私钥**绝不进仓库**（`.gitignore` 已有 `*.pem`）；部署时单文件传到
`/opt/firefly/keys/session_priv.pem`（权限 600），并在 systemd 里设：
    Environment=FIREFLY_ENC_ENABLED=1
"""
from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

DEFAULT_KEY = Path.home() / ".firefly" / "session_priv.pem"
JS_FILE = ROOT / "app" / "static" / "js" / "session_crypto.js"
PLACEHOLDER = "__SESSION_N_B64__"

_OPENSSL_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    "/usr/bin/openssl",
)


def die(msg: str, code: int = 1):
    print(f"X {msg}")
    sys.exit(code)


def find_openssl() -> str:
    import os
    import shutil
    p = os.environ.get("OPENSSL") or shutil.which("openssl")
    if p and Path(p).is_file():
        return p
    for c in _OPENSSL_CANDIDATES:
        if Path(c).is_file():
            return c
    die("找不到 openssl（生成密钥要用）。装一个或用环境变量 OPENSSL 指定。")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def gen(path: Path) -> None:
    if path.exists():
        die(f"私钥已存在，不覆盖：{path}\n  （要轮换就先备份再删，轮换后必须 --sync 刷新前端）")
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([find_openssl(), "genpkey", "-algorithm", "RSA",
                        "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(path)],
                       capture_output=True)
    if r.returncode != 0:
        die("openssl 生成失败: " + r.stderr.decode("utf-8", "replace")[:300])
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"V 私钥已生成：{path}（**绝不进仓库**；部署时单文件传到服务器 keys/，权限 600）")


def sync_js(key: Path) -> int:
    """把公钥写进前端常量；返回公钥 n 的字节长度（供调用方做健全性检查）。"""
    import session_crypto as SC          # noqa: E402  （与服务端解密共用同一份解析）
    SC._key_loaded = False
    SC._key_cache = None
    key_abs = key.resolve()
    n, e = None, None
    body = key_abs.read_text(encoding="ascii")
    body = body.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "")
    der = base64.b64decode("".join(body.split()), validate=True)
    n, e = SC._parse_pkcs8(der)[:2]
    if e != 65537:
        print(f"! 公钥指数是 {e}（非 65537）—— 前端常量写死 65537，请改 session_crypto.js")
    n_b64 = base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode("ascii")
    txt = JS_FILE.read_text(encoding="utf-8")
    import re
    new = re.sub(r'n_b64:\s*"[^"]*"', f'n_b64: "{n_b64}"', txt, count=1)
    if new == txt and PLACEHOLDER not in txt and n_b64 not in txt:
        die(f"没能在 {JS_FILE} 里找到 n_b64 字段")
    JS_FILE.write_text(new, encoding="utf-8")
    print(f"V 公钥已写入 {JS_FILE.relative_to(ROOT).as_posix()}（n = {n.bit_length()} 位）")
    print(f"  指纹 sha256(n)[:16] = {__import__('hashlib').sha256(n.to_bytes((n.bit_length()+7)//8,'big')).hexdigest()[:16]}")
    return n.bit_length() // 8


def show(key: Path) -> None:
    import session_crypto as SC          # noqa: E402
    body = key.read_text(encoding="ascii")
    body = body.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "")
    der = base64.b64decode("".join(body.split()), validate=True)
    n, e = SC._parse_pkcs8(der)[:2]
    print(f"n({n.bit_length()} 位) b64 = {base64.b64encode(n.to_bytes((n.bit_length()+7)//8,'big')).decode('ascii')}")
    print(f"e = {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="会话加密密钥（生成 / 同步公钥 / 查看）")
    ap.add_argument("--gen", action="store_true", help="生成新密钥对")
    ap.add_argument("--sync", action="store_true", help="用已有私钥刷新前端公钥常量")
    ap.add_argument("--show", action="store_true", help="打印公钥参数")
    ap.add_argument("--key", default=str(DEFAULT_KEY), help="私钥路径")
    a = ap.parse_args()
    key = Path(a.key).expanduser()

    if a.gen:
        gen(key)
    if a.show:
        show(key)
        return 0
    if a.sync or a.gen:
        if not key.is_file():
            die(f"私钥不存在：{key}")
        sync_js(key)
        print("\n下一步：跑 `python tools/check_session_keys.py`（门禁会核对"
              "「前端常量 == 这把私钥的公钥」，并做一次真加密往返）")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
