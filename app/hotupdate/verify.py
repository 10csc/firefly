# -*- coding: utf-8 -*-
"""热更新/公告 · 验签桥（契约 §八）。

**验签的两条路径**：
  · 安卓（有 Chaquopy）：转发给 Kotlin `HotUpdateBridge.verifyRsaSha256`（`java.security`，
    平台自带、零依赖）；
  · PC / 桌面（无 Kotlin 桥）：用**标准库自实现** RSA PKCS#1 v1.5 + SHA-256 **验签**
    （`_py_verify`，见下）。Chaquopy 与 PyInstaller 是两个构建产物，PC 版此前因为
    "没有桥"而**永远验签失败** —— 表现为"公告与热更新在 PC 上静默永不生效"，
    没有报错、没有日志、用户只会觉得"怎么从来没见过更新"。这是 2026-09-19 补的洞。

关于"手写密码学 = 事故"（原注释）的边界，说清楚免得后人误改：
  · **签名（带私钥）** 绝不自己写 —— 用 openssl（发布机）。
  · **验签（只有公钥）** 的风险面小得多：没有密钥材料，出错方向是"拒绝"或"误接受"。
    这里用最保守的写法：长度、`0x00 0x01` 前缀、`0xFF` 填充串、分隔 `0x00`、
    DigestInfo 逐段比对，任何一段不符即 False；末尾用 `hmac.compare_digest` 定长比较。
  · 它**只在没有 Kotlin 桥时才被使用**，安卓路径不受影响。
  · 自实现与 Kotlin 的行为一致性由 `tests/test_hotupdate_verify.py` 的固定向量兜住。

第三条路径：测试注入（`set_verifier`）。优先级 = 注入 > Kotlin > 自实现。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading

logger = logging.getLogger(__name__)

# 测试/桌面注入点：fn(data: bytes, sig_b64: str) -> bool
_override = None

# SHA-256 的 DigestInfo 前缀（RFC 8017 §9.2 Note 1）：
#   SEQUENCE(0x30 0x31) { SEQUENCE(0x30 0x0d) { OID 2.16.840.1.101.3.4.2.1, NULL }, OCTET STRING(0x04 0x20) }
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
_MIN_PAD = 8          # PKCS#1 v1.5 要求填充至少 8 字节 0xFF

_key_lock = threading.Lock()
_key_cache = None


def set_verifier(fn) -> None:
    """注入验签实现（仅测试与桌面调试用）。传 None 恢复原生。"""
    global _override
    _override = fn


def native_available() -> bool:
    """当前环境有没有 Kotlin 桥（安卓有、桌面没有）。"""
    try:
        from java import jclass  # noqa: F401  （Chaquopy 专有）
        return True
    except Exception:
        return False


def backend() -> str:
    """当前会实际使用的验签实现（排障用：状态页直接显示它）。"""
    if _override is not None:
        return "injected"
    if native_available():
        return "kotlin"
    return "python"


def available() -> bool:
    """有没有可用的验签实现（**self-implemented 也算** —— 这才是"能用"的真口径）。"""
    return True


# ── 纯 Python 验签（PC / 无 Kotlin 桥时）──────────────
def _der_tlv(b: bytes, i: int):
    """读一个 DER TLV（只支持定长短/长形式长度），返回 (tag, value, next_index)。"""
    if i + 2 > len(b):
        raise ValueError("DER 截断")
    tag = b[i]
    i += 1
    ln = b[i]
    i += 1
    if ln & 0x80:
        n = ln & 0x7F
        if n == 0 or i + n > len(b):
            raise ValueError("DER 长度非法")
        ln = int.from_bytes(b[i:i + n], "big")
        i += n
    if i + ln > len(b):
        raise ValueError("DER 值截断")
    return tag, b[i:i + ln], i + ln


def _parse_spki(der: bytes):
    """X.509 SubjectPublicKeyInfo → (n, e)。只认 rsaEncryption 结构，别的形态直接抛。"""
    tag, seq, _ = _der_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("SPKI 不是 SEQUENCE")
    tag, _alg, i = _der_tlv(seq, 0)          # AlgorithmIdentifier
    if tag != 0x30:
        raise ValueError("AlgorithmIdentifier 不是 SEQUENCE")
    tag, bits, _ = _der_tlv(seq, i)          # BIT STRING
    if tag != 0x03 or not bits or bits[0] != 0:
        raise ValueError("公钥位串非法")
    tag, rsa, _ = _der_tlv(bits, 1)          # RSAPublicKey
    if tag != 0x30:
        raise ValueError("RSAPublicKey 不是 SEQUENCE")
    tag, n_b, i = _der_tlv(rsa, 0)
    if tag != 0x02:
        raise ValueError("modulus 不是 INTEGER")
    tag, e_b, _ = _der_tlv(rsa, i)
    if tag != 0x02:
        raise ValueError("exponent 不是 INTEGER")
    n = int.from_bytes(n_b, "big")
    e = int.from_bytes(e_b, "big")
    if n <= 0 or e <= 1:
        raise ValueError("RSA 参数非法")
    return n, e


def _pubkey():
    global _key_cache
    if _key_cache is None:
        with _key_lock:
            if _key_cache is None:
                from hotupdate.pubkey import PUBLIC_KEY_PEM
                body = (PUBLIC_KEY_PEM.replace("-----BEGIN PUBLIC KEY-----", "")
                        .replace("-----END PUBLIC KEY-----", ""))
                der = base64.b64decode("".join(body.split()), validate=True)
                _key_cache = _parse_spki(der)
    return _key_cache


def _py_verify(data: bytes, sig_b64: str) -> bool:
    """RSA-2048 / SHA-256 / PKCS#1 v1.5 验签。任何异常一律 False。"""
    try:
        sig = base64.b64decode(sig_b64, validate=True)
        n, e = _pubkey()
        k = (n.bit_length() + 7) // 8
        if len(sig) != k:
            return False
        s = int.from_bytes(sig, "big")
        if s >= n:                      # 防 RSA 签名可延展性（s 必须 < n）
            return False
        em = pow(s, e, n).to_bytes(k, "big")

        tail = _SHA256_DIGESTINFO + hashlib.sha256(data).digest()
        pad = k - len(tail) - 3
        if pad < _MIN_PAD:
            return False
        if em[0] != 0x00 or em[1] != 0x01:
            return False
        if em[2:2 + pad] != b"\xff" * pad:
            return False
        if em[2 + pad] != 0x00:
            return False
        return hmac.compare_digest(em[3 + pad:], tail)
    except Exception as ex:
        logger.warning("纯 Python 验签异常（当作不通过）: %s: %s", type(ex).__name__, ex)
        return False


# ── 对外 ────────────────────────────────────────────
def _native(data: bytes, sig_b64: str) -> bool:
    from java import jclass
    bridge = jclass("com.firefly.android.HotUpdateBridge")
    return bool(bridge.verifyRsaSha256(data, sig_b64))


def verify(data: bytes, sig_b64: str) -> bool:
    """验签。**任何异常/缺实现都返回 False** —— 绝不"出错就当通过"。"""
    if not data or not sig_b64:
        return False
    if _override is not None:
        fn = _override
    elif native_available():
        fn = _native
    else:
        fn = _py_verify                # PC / 桌面：标准库自实现
    try:
        return bool(fn(data, sig_b64))
    except Exception as e:
        logger.warning("验签不可用或失败（当作不通过）: %s: %s", type(e).__name__, e)
        return False
