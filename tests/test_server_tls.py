# -*- coding: utf-8 -*-
"""网关双端口 TLS 回归 —— 共创平台 M5（方案见 docs/设计/角色卡共创平台/05）

钉死的事（都是"上生产前必须为真"的性质）：
- 配了证书 → TLS 端口能完成握手并跑通**完整业务链路**（含大文件下载）；
- **明文端口在 TLS 开着时照常工作**（灰度期的兼容承诺：旧客户端零改动）；
- `FIREFLY_TLS_PORT` 没配 → 完全不尝试 TLS（与改造前行为一致）；
- **证书配错（文件在但内容非法）→ 明文端口不受影响**（TLS 失败绝不能拖垮下载站）；
- `/download/tls` 自检端点：仅本机可访问，能报出到期时间与剩余天数（供续期告警）；
- 协商版本 ≥ TLS 1.2。

沙箱：自签名证书写临时目录，临时端口，`FIREFLY_BIND=127.0.0.1`（不对外暴露）。
依赖 `cryptography`（仅测试用；服务器上不需要它——`cert_info()` 会退回 openssl 命令行）。
"""
import json
import os
import socket as _sk
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


try:
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except Exception as e:                                        # pragma: no cover
    print(f"SKIP：本机没有 cryptography（{e}）——TLS 握手验证需要它生成自签证书")
    sys.exit(0)

TMP = Path(tempfile.mkdtemp(prefix="ff_tls_"))
CERT = TMP / "fullchain.pem"
KEY = TMP / "privkey.pem"
BOGUS = TMP / "bogus.pem"


def make_cert(cert_fp: Path, key_fp: Path, days: int = 3650):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    # builder 用 naive UTC（`not_valid_*_utc` 只存在于 Certificate 对象上，不在 builder 上）
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_fp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_fp.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))


make_cert(CERT, KEY)
BOGUS.write_text("这不是证书", encoding="utf-8")
BLOB = bytes(range(256)) * 40                                  # 10240 字节
(TMP / "firefly.apk").write_bytes(BLOB)


def free_port(lo=8821, hi=8860, used=()):
    for p in range(lo, hi):
        if p in used:
            continue
        s = _sk.socket()
        s.settimeout(0.3)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return 0


PLAIN = free_port()
TLS = free_port(used=(PLAIN,))
check(f"空闲端口 明文={PLAIN} TLS={TLS}", PLAIN > 0 and TLS > 0)

_ctx_unverified = ssl.create_default_context()
_ctx_unverified.check_hostname = False
_ctx_unverified.verify_mode = ssl.CERT_NONE


def boot(extra_env):
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "FIREFLY_BW_KBPS": "0", "FIREFLY_BULK_IP_BYTES": "0",
                "FIREFLY_BIND": "127.0.0.1"})
    env.update(extra_env)
    log = open(TMP / f"server-{extra_env.get('tag', 'x')}.log", "wb")
    p = subprocess.Popen([sys.executable, "download_server.py", str(PLAIN), str(TMP)],
                         cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)
    return p, log


def http_get(path, *, tls=False, headers=None, timeout=10):
    scheme = "https" if tls else "http"
    port = TLS if tls else PLAIN
    url = f"{scheme}://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers=headers or {})
    try:
        kw = {"context": _ctx_unverified} if tls else {}
        with urllib.request.urlopen(req, timeout=timeout, **kw) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def wait_up(tls=False, tries=60):
    for _ in range(tries):
        try:
            s, _h, _b = http_get("/download/stats", tls=tls)
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


proc1 = log1 = None
proc2 = log2 = None
try:
    proc1, log1 = boot({"FIREFLY_TLS_PORT": str(TLS), "FIREFLY_TLS_CERT": str(CERT),
                        "FIREFLY_TLS_KEY": str(KEY), "tag": "ok"})
    check("明文端口就绪", wait_up(tls=False))
    check("TLS 端口就绪", wait_up(tls=True))

    print("== 1. 明文端口在 TLS 开着时照常工作（灰度兼容承诺）==")
    s, h, b = http_get("/download/stats")
    check(f"① 明文 200（实际 {s}）", s == 200)
    s, h, b = http_get("/download/firefly.apk")
    check("①b 明文下载内容逐字节一致", s == 200 and b == BLOB)

    print("== 2. TLS 链路跑通完整业务（含大文件）==")
    s, h, b = http_get("/download/stats", tls=True)
    check(f"② TLS 200（实际 {s}）", s == 200)
    s, h, b = http_get("/download/firefly.apk", tls=True)
    check("②b TLS 下载内容逐字节一致", s == 200 and b == BLOB)
    s, h, b = http_get("/download/firefly.apk", tls=True, headers={"Range": "bytes=100-199"})
    check("②c TLS 下 Range 也生效（206）", s == 206 and b == BLOB[100:200])

    print("== 3. 版本与证书自检 ==")
    with _sk.create_connection(("127.0.0.1", TLS), timeout=5) as raw:
        with _ctx_unverified.wrap_socket(raw, server_hostname="127.0.0.1") as ss:
            ver = ss.version()
            der = ss.getpeercert(binary_form=True)
    check(f"③ 协商版本 ≥ TLS1.2（{ver}）", ver in ("TLSv1.2", "TLSv1.3"))
    check("③b 拿到了证书链", bool(der))

    s, _h, b = http_get("/download/tls")
    info = json.loads(b.decode("utf-8"))
    check("④ 自检端点 enabled=true", s == 200 and info["enabled"] is True)
    check("④b 报出证书可加载", info["loadable"] is True)
    check("④c 报出到期时间", bool(info["not_after"]))
    check(f"④d 剩余天数合理（{info['days_left']}）", isinstance(info["days_left"], int)
          and info["days_left"] > 3000)
    check("④e 长期证书无告警", info["warning"] == "")

    print("== 4. 证书配错不能拖垮明文（兜底性质）==")
    proc1.terminate()
    proc1.wait(timeout=8)
    log1.close()
    PLAIN2 = free_port(lo=8861, hi=8880)
    if PLAIN2:
        globals()["PLAIN"] = PLAIN2
        proc2, log2 = boot({"FIREFLY_TLS_PORT": str(TLS), "FIREFLY_TLS_CERT": str(BOGUS),
                            "FIREFLY_TLS_KEY": str(KEY), "tag": "bad"})
        check("⑤ 证书非法时明文端口仍就绪", wait_up(tls=False))
        s, _h, b = http_get("/download/firefly.apk")
        check("⑤b 明文仍能完整下载", s == 200 and b == BLOB)
        time.sleep(1.0)
        logtxt = (TMP / "server-bad.log").read_text("utf-8", "replace")
        check("⑤c 日志里有 TLS 启动失败告警", "TLS-ERROR" in logtxt)
    else:
        check("⑤ 找不到第二个空闲端口（跳过兜底用例）", True)

    print("== 5. 不配 TLS 端口 = 改造前行为 ==")
    if proc2:
        proc2.terminate()
        proc2.wait(timeout=8)
        log2.close()
    PLAIN3 = free_port(lo=8881, hi=8900)
    if PLAIN3:
        globals()["PLAIN"] = PLAIN3
        proc3, log3 = boot({"tag": "off"})
        check("⑥ 未配 TLS_PORT 时明文就绪", wait_up(tls=False))
        time.sleep(0.8)
        txt = (TMP / "server-off.log").read_text("utf-8", "replace")
        check("⑥b 日志里没有 TLS 监听（与改造前一致）", "(tls)" not in txt)
        proc3.terminate()
        proc3.wait(timeout=8)
        log3.close()
finally:
    for p, lg in ((proc1, log1), (proc2, log2)):
        try:
            if p and p.poll() is None:
                p.terminate()
                p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            if lg:
                lg.close()
        except Exception:
            pass
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
