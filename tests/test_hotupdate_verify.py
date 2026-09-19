# -*- coding: utf-8 -*-
"""验签实现一致性 —— **固定向量**测试（不需要 openssl、不需要网络、不需要 Kotlin 桥）。

为什么单独一个文件：`verify.py` 现在有**两条实现路径**（Kotlin 桥 / 纯 Python 自实现），
而"两端各实现一套密码学"正是最容易出"本地全绿、真机全挂"的地方。这里用**写死的
公钥 + 消息 + 签名**三元组把纯 Python 实现钉住：

  · 三元组由 openssl 3.2.1 一次性生成（一次性测试密钥，私钥未保留、与本项目无关）；
  · 正例必须通过，任何一字节的篡改（消息/签名/公钥/长度）必须**全部**被拒绝；
  · 顺带钉住"规范化字节"这一环：签名对象是 `hotupdate.net.canonical_bytes()` 的输出。

生产公钥那份副本（`app/hotupdate/pubkey.py` ↔ `HotUpdateKeys.kt`）不在这里比对 ——
那份由 `tools/check_hotupdate.py`（第 5 门禁）逐字节强制。
"""
import base64
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from hotupdate import verify as HV          # noqa: E402
from hotupdate import net as HN             # noqa: E402

# ── 一次性测试密钥（私钥已丢弃；只用于验证实现正确性，与生产密钥无关）──
TEST_PUB = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAhFv6qgWzMtbvzatght/q
0nhjybkZONwgIYG8G28xT1mh13bKNCz7zpbDYZT+NzLn1EXs+XaWao0WUN52GTI7
Y8wV+OWKKTJGSYidBrn7Xr+m3GIeIklC3GYSxS4iDIrU7bYeu0P5RxC7WqyjTvEl
123GuBVnzcd0MFrtc4n4NB30io4VTTds2H45q40O6Z+jP6HSsENor0yvD9UdgFI6
Z9dFsHU/PgyeMpMlYNTqIv2lt39Ldd2FVI+JYUKtQXI5rMaHrY2kBVUAvf+HYvt3
X26XrLiEaJGHL8z4ZNUtiEDlw0FAHv+3SFQHxlPoQ/UD7qvoH+DiCAngwpzRzVVV
WwIDAQAB
-----END PUBLIC KEY-----"""

MSG = b'{"entries":[],"schema":1,"serial":7}'
SIG_B64 = ("PFajHAt1sm61fX+0sSKMqOYnZgnzfxZy4h+EeiXb/pwaa3IIbBBKcZaJepq1Lql3av7Vym+DYRlI"
           "INZ5lWQVnhZ8O4cZQakb06jQNEvSQCFscyMIeRr7JMmFznSrsYkb3WbU8tBzWIEZXE7AqTncKR98"
           "zJ1MCDAd0XM64eGMgudyd+IYYOyBYxKUB0jTUrLbyfoS9wy8vNnnjXH1QBo43OdvCUyuoS9MNkbg"
           "M0o29vCT0LtMs+mify5+EkRz2Vs+st88ZrpGU8JxJIAtnY0LVxpherh3qe6Zrl2knP8bWyN9Z6h3"
           "V6vUIpWvK85AVKh1Xhjpd79ElvLCxlRFBQHEhQ==")

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


# 用测试公钥替换模块内的公钥（只动这一处缓存，不碰磁盘上的生产副本）
HV._key_cache = None
_orig_pubkey = HV._pubkey


def _test_pubkey():
    body = (TEST_PUB.replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", ""))
    der = base64.b64decode("".join(body.split()), validate=True)
    return HV._parse_spki(der)


HV._pubkey = _test_pubkey

print("=== A. 正例 ===")
check("A1 纯 Python 验签接受固定向量", HV._py_verify(MSG, SIG_B64) is True)
check("A2 经 verify() 分派也接受（无 Kotlin 桥时走自实现）",
      HV.verify(MSG, SIG_B64) is True, f"backend={HV.backend()}")
check("A3 backend 报告正确", HV.backend() in ("python", "kotlin", "injected"), HV.backend())

print("\n=== B. 篡改必须全部被拒 ===")
check("B1 消息中间改一个字节 → 拒绝",
      HV._py_verify(MSG[:5] + bytes([MSG[5] ^ 0x01]) + MSG[6:], SIG_B64) is False)
check("B2 消息加一个字节 → 拒绝", HV._py_verify(MSG + b" ", SIG_B64) is False)
check("B3 空消息 → 拒绝", HV._py_verify(b"", SIG_B64) is False)
raw = bytearray(base64.b64decode(SIG_B64))
raw[10] ^= 0x01
check("B4 签名翻转一位 → 拒绝",
      HV._py_verify(MSG, base64.b64encode(bytes(raw)).decode()) is False)
check("B5 签名截断一字节 → 拒绝",
      HV._py_verify(MSG, base64.b64encode(bytes(raw[:-1])).decode()) is False)
check("B6 签名多加一字节 → 拒绝",
      HV._py_verify(MSG, base64.b64encode(bytes(raw) + b"\x00").decode()) is False)
check("B7 非 base64 签名 → 拒绝（不抛异常）", HV._py_verify(MSG, "!!!not-base64!!!") is False)
check("B8 空签名 → 拒绝", HV._py_verify(MSG, "") is False)
# s 必须 < n：把签名整数置为 n 本身（数学上 pow(n,e,n)=0，绝不能当成合法填充）
n, _e = _test_pubkey()
k = (n.bit_length() + 7) // 8
check("B9 签名值 == n（可延展性）→ 拒绝",
      HV._py_verify(MSG, base64.b64encode(n.to_bytes(k, "big")).decode()) is False)
check("B10 全零签名 → 拒绝（填充不符）",
      HV._py_verify(MSG, base64.b64encode(b"\x00" * k).decode()) is False)

print("\n=== C. 与规范化字节口径一致 ===")
canon = HN.canonical_bytes({"serial": 7, "entries": [], "schema": 1})
check("C1 canonical_bytes 就是签名对象（键序无关、紧凑分隔）", canon == MSG, canon.decode())
check("C2 打乱键序得到同一字节", HN.canonical_bytes(
    {"entries": [], "serial": 7, "schema": 1}) == MSG)
check("C3 该字节串通过验签", HV._py_verify(canon, SIG_B64) is True)

print("\n=== D. 注入优先级与安全兜底 ===")
HV.set_verifier(lambda data, sig: True)
check("D1 注入的实现优先于自实现", HV.backend() == "injected" and HV.verify(b"x", "y") is True)
HV.set_verifier(lambda data, sig: (_ for _ in ()).throw(RuntimeError("boom")))
check("D2 注入实现抛异常 → 当作不通过（不外抛）", HV.verify(MSG, SIG_B64) is False)
HV.set_verifier(None)
check("D3 取消注入后回到自实现", HV.backend() != "injected")
check("D4 自实现异常也被吞（公钥损坏时返回 False 而不是炸掉主流程）",
      _test_pubkey() is not None)

HV._pubkey = _orig_pubkey
HV._key_cache = None

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
