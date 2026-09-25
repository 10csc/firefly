# -*- coding: utf-8 -*-
"""会话密钥 · 敏感请求头解密（见 docs/审计-服务器与开发版-2026-09-18.md §2.1）。

全站明文 HTTP 是用户明确接受的现实（不买域名/证书），代价是 `Authorization`（登录
Bearer token）与 `X-API-Key`（用户自己的 DeepSeek Key）在链路上裸奔。这里做**零成本**
的应用层加密：客户端用服务器公钥把敏感头值装进信封（`X-Firefly-Enc`），服务器用私钥解开。

    信封：enc.v1.<b64url(key)>.<b64url(iv)>.<b64url(ct)>
      key = RSA-2048 / OAEP-SHA256 加密的 32B 内容密钥（每次请求新生成）
      ct  = HMAC-SHA256 计数器流异或的明文 JSON
            明文 = {"v":1,"h":{头名: 值}} + sha256(该 JSON)  ← 解错必然失败，不会碰巧出乱码凭据

**只用标准库**（`pow` 大整数 + hashlib/hmac）—— 与 `hotupdate/verify.py` 的取舍一致：
  · **签名/加密（用私钥）绝不自己写**，那是发布机的 openssl；
  · 这里的私钥是**服务器自己**的会话密钥（不是热更签名根），且**只做解密**：
    用私钥做一次模幂再把 OAEP 拆开。出错方向是"拒绝"或"解出乱码"，不会伪造出凭证。
  · 自实现与客户端（JS BigInt / WebCrypto）的一致性由 `tests/test_session_crypto.py`
    的**固定向量**兜住 —— 有向量就不怕"两边都改错但互相自洽"。

**向后兼容（硬要求）**：没有 `X-Firefly-Enc` 时一律按明文读（0.9.1 之前的客户端照常工作）。
这是灰度期必须的：老客户端不能因为服务端上了加密就登不进来。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 客户端把信封放这个头里；明文头与它二选一。
ENC_HEADER = "X-Firefly-Enc"
#: 信封前缀（版本化：以后换算法就走 enc.v2，老服务器会直接忽略新信封并退回明文）
PREFIX = "enc.v1"
#: 需要解密的三个头（客户端只加密这几个；其余头照明文）
SECRET_HEADERS = ("Authorization", "X-API-Key", "X-API-Base")

#: 私钥路径（部署时放服务器上，权限 600，**绝不进仓库**）
DEFAULT_KEY_PATH = "/opt/firefly/keys/session_priv.pem"

_SHA256_HLEN = 32
_lock = threading.RLock()
_key_cache = None            # (n, d, p, q, dp, dq, qinv)
_key_loaded = False
_fail_window = []            # 解密失败时间戳（防"拿垃圾信封逼服务器做模幂"）
_FAIL_LIMIT = 40             # 60 秒内失败次数上限
_FAIL_WINDOW_S = 60.0


# ── 环境开关（三个，部署时只需配第一个）──────────────
def enabled() -> bool:
    """服务器是否启用应用层加密。默认关（未配置私钥时加密没意义，且会白算）。"""
    return str(os.environ.get("FIREFLY_ENC_ENABLED") or "").strip() in ("1", "true", "yes", "on")


def required() -> bool:
    """是否**拒绝**未加密的请求。

    默认 **False**（灰度期）：老客户端还在发明文，一刀切会把所有人挡在门外。
    等确认所有在用的客户端都会加密（看 `stats()` 的明文回落次数）再开。
    """
    return str(os.environ.get("FIREFLY_ENC_REQUIRED") or "").strip() in ("1", "true", "yes", "on")


def key_path() -> Path:
    return Path(str(os.environ.get("FIREFLY_ENC_KEY") or DEFAULT_KEY_PATH)).expanduser()


# ── DER（PKCS#8 私钥）────────────────────────────────
def _der_tlv(b: bytes, i: int):
    """读一个 DER TLV（定长短/长形式长度）→ (tag, value, next_index)。"""
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


def _parse_pkcs8(der: bytes) -> tuple:
    """PKCS#8 `PrivateKeyInfo` → (n, e, d, p, q, dp, dq, qinv)。

    结构（RFC 5208 + PKCS#1 A.1.2）：
      SEQUENCE { INTEGER 0, SEQUENCE{ OID rsaEncryption, NULL }, OCTET STRING { RSAPrivateKey } }
    只认这一种；别的（加密私钥、EC）直接抛 —— 宁可起不来也不要"能跑但解错"。
    """
    tag, seq, _ = _der_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("PKCS#8 不是 SEQUENCE")
    tag, _ver, i = _der_tlv(seq, 0)
    if tag != 0x02:
        raise ValueError("缺版本号")
    tag, _alg, i = _der_tlv(seq, i)
    if tag != 0x30:
        raise ValueError("AlgorithmIdentifier 不是 SEQUENCE")
    tag, octets, _ = _der_tlv(seq, i)
    if tag != 0x04:
        raise ValueError("私钥不是 OCTET STRING（加密私钥不支持）")
    tag, rsa, _ = _der_tlv(octets, 0)
    if tag != 0x30:
        raise ValueError("RSAPrivateKey 不是 SEQUENCE")
    vals, i, tags = [], 0, []
    for _ in range(9):
        tag, v, i = _der_tlv(rsa, i)
        tags.append(tag)
        vals.append(int.from_bytes(v, "big"))
    if tags[:3] != [0x02, 0x02, 0x02]:
        raise ValueError("RSAPrivateKey 前三个字段必须是 INTEGER")
    _ver, n, e, d = vals[0], vals[1], vals[2], vals[3]
    p, q, dp, dq, qinv = vals[4], vals[5], vals[6], vals[7], vals[8]
    if n.bit_length() < 2048 or e < 3:
        raise ValueError("RSA 参数过弱（要求 ≥2048 位）")
    if (p * q) != n:
        raise ValueError("RSA 参数自检失败（p*q != n）")
    return n, e, d, p, q, dp, dq, qinv


def load_key(force: bool = False):
    """惰性加载私钥；不可用返回 None（**绝不抛** —— 加密是加固，不该拖垮请求）。"""
    global _key_cache, _key_loaded
    with _lock:
        if _key_loaded and not force:
            return _key_cache
        _key_loaded = True
        _key_cache = None
        p = key_path()
        try:
            if not p.is_file():
                logger.info("会话加密私钥不存在（%s）—— 敏感头按明文处理", p)
                return None
            raw = p.read_bytes()
            body = raw.decode("ascii", "replace")
            body = (body.replace("-----BEGIN PRIVATE KEY-----", "")
                        .replace("-----END PRIVATE KEY-----", ""))
            der = base64.b64decode("".join(body.split()), validate=True)
            _key_cache = _parse_pkcs8(der)
            logger.info("会话加密私钥已加载（%s，%d 位）", p, _key_cache[0].bit_length())
        except Exception as e:
            logger.warning("会话加密私钥加载失败（按明文处理）: %s: %s", type(e).__name__, e)
            _key_cache = None
        return _key_cache


def pubkey_params() -> tuple:
    """(n, e) —— 给门禁核对"客户端内置的公钥就是这把私钥的公钥"。"""
    k = load_key()
    return (None, None) if not k else (k[0], k[1])


# ── RSA / OAEP ───────────────────────────────────
def _rsa_decrypt(c: int, key: tuple) -> bytes:
    """私钥解密（走 CRT，比直算快 ~3 倍）+ **自检**：不满足 m^e mod n == c 就拒绝。

    自检这一步是自实现密码学的安全带：CRT 参数若有任何一位不对，结果就是"看起来是
    2048 位随机数"，而 OAEP 拆包会以极低概率碰巧通过。一次模幂换掉整类静默错误。
    """
    n, e, d, p, q, dp, dq, qinv = key
    k = (n.bit_length() + 7) // 8
    if c >= n or c <= 0:
        raise ValueError("密文越界")
    m1 = pow(c, dp, p)
    m2 = pow(c, dq, q)
    h = ((m1 - m2) * qinv) % p
    m = m2 + h * q
    if pow(m, e, n) != c:
        raise ValueError("RSA 自检失败（CRT 参数不一致）")
    return m.to_bytes(k, "big")


def _mgf1(seed: bytes, length: int) -> bytes:
    """MGF1-SHA256（RFC 8017 B.2.1）。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def _oaep_decode(em: bytes, label: bytes = b"") -> bytes:
    """OAEP-SHA256 去填充（RFC 8017 §7.1.2）。任何一步不符都抛（调用方按失败处理）。"""
    if len(em) < 2 * _SHA256_HLEN + 2:
        raise ValueError("EM 过短")
    if em[0] != 0x00:
        raise ValueError("EM 首字节非 0")
    lhash = hashlib.sha256(label).digest()
    masked_seed, masked_db = em[1:1 + _SHA256_HLEN], em[1 + _SHA256_HLEN:]
    seed = bytes(a ^ b for a, b in zip(masked_seed, _mgf1(masked_db, _SHA256_HLEN)))
    db = bytes(a ^ b for a, b in zip(masked_db, _mgf1(seed, len(masked_db))))
    if not hmac.compare_digest(db[:_SHA256_HLEN], lhash):
        raise ValueError("OAEP label 摘要不符")
    rest = db[_SHA256_HLEN:]
    i = 0
    while i < len(rest) and rest[i] == 0x00:
        i += 1
    if i >= len(rest) or rest[i] != 0x01:
        raise ValueError("OAEP 分隔符缺失")
    return rest[i + 1:]


def _keystream(key: bytes, iv: bytes, length: int) -> bytes:
    """HMAC-SHA256 计数器流（与客户端逐字节一致；见 JS session_crypto.js）。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, iv + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _b64u_decode(s: str) -> bytes:
    t = str(s).replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    return base64.b64decode(t, validate=True)


# ── 信封 ─────────────────────────────────────────
def decrypt_envelope(env: str) -> dict:
    """解开客户端信封 → {头名: 值}。**任何异常都抛**（调用方决定退回明文还是拒绝）。"""
    key = load_key()
    if not key:
        raise ValueError("服务器未配置会话私钥")
    parts = str(env or "").split(".", 4)
    if len(parts) != 5 or parts[0] != "enc" or parts[1] != "v1":
        raise ValueError("信封格式不识别")
    wrapped = _b64u_decode(parts[2])
    iv = _b64u_decode(parts[3])
    ct = _b64u_decode(parts[4])
    if len(iv) != 16 or not ct:
        raise ValueError("iv/密文长度非法")
    n = key[0]
    k = (n.bit_length() + 7) // 8
    if len(wrapped) != k:
        raise ValueError("密钥信封长度非法")
    content_key = _oaep_decode(_rsa_decrypt(int.from_bytes(wrapped, "big"), key))
    if len(content_key) != 32:
        raise ValueError("内容密钥长度非法")
    ks = _keystream(content_key, iv, len(ct))
    body = bytes(a ^ b for a, b in zip(ct, ks))
    if len(body) <= _SHA256_HLEN:
        raise ValueError("明文过短")
    payload, digest = body[:-_SHA256_HLEN], body[-_SHA256_HLEN:]
    if not hmac.compare_digest(hashlib.sha256(payload).digest(), digest):
        raise ValueError("明文摘要不符")
    import json
    obj = json.loads(payload.decode("utf-8"))
    if not isinstance(obj, dict) or obj.get("v") != 1 or not isinstance(obj.get("h"), dict):
        raise ValueError("明文结构非法")
    out = {str(k2): str(v) for k2, v in obj["h"].items()}
    if len(out) > 8:
        raise ValueError("明文字段过多")
    return out


# ── 给 HTTP 处理器用的一步式入口 ────────────────────
_COUNTER = {"enc": 0, "plain": 0, "fail": 0, "rejected": 0}


def stats() -> dict:
    """灰度观测：加密/明文回落/失败各多少次（决定何时开 `required`）。"""
    with _lock:
        return dict(_COUNTER)


def _too_many_failures() -> bool:
    """失败限流：拿垃圾信封刷服务器做 2048 位模幂，是一个**便宜的 DoS 面**。

    60 秒内解密失败超过阈值 ⇒ 这段时间内不再尝试解密（请求按"未加密"处理，
    并计入 rejected）。正常用户不受影响（他们不会连续失败 40 次）。
    """
    now = time.time()
    with _lock:
        while _fail_window and now - _fail_window[0] > _FAIL_WINDOW_S:
            _fail_window.pop(0)
        return len(_fail_window) >= _FAIL_LIMIT


def _note_failure() -> None:
    with _lock:
        _COUNTER["fail"] += 1
        _fail_window.append(time.time())


def decrypt_headers(handler) -> dict:
    """从请求里取出敏感头（**加密优先、明文回落**）。

    返回 {头名: 值}（只含实际出现的头）。**永不抛**：
      · 有合法信封 → 用解出来的值（覆盖同名明文头）；
      · 信封坏了但私钥在 → 记一次失败并**退回明文头**（老客户端/中间设备改包都不会被锁死）；
      · `FIREFLY_ENC_REQUIRED=1` 且既没信封也没明文 → 返回 `{"__required__": ""}` 交给调用方拒绝。
    """
    out = {}
    plain = {}
    for name in SECRET_HEADERS:
        try:
            v = handler.headers.get(name)
        except Exception:
            v = None
        if v:
            plain[name] = str(v).strip()
    env = ""
    try:
        env = str(handler.headers.get(ENC_HEADER) or "").strip()
    except Exception:
        pass

    if env and enabled():
        if _too_many_failures():
            with _lock:
                _COUNTER["rejected"] += 1
            logger.warning("会话加密：失败次数超限，本请求按未加密处理")
            return plain or {"__required__": ""}
        try:
            got = decrypt_envelope(env)
            with _lock:
                _COUNTER["enc"] += 1
            out.update(plain)
            out.update(got)          # 解出来的值优先
            return out
        except Exception as e:
            _note_failure()
            logger.warning("会话加密：信封解密失败，退回明文（%s: %s）",
                           type(e).__name__, str(e)[:120])
    elif plain:
        with _lock:
            _COUNTER["plain"] += 1

    if required() and not env:
        # 灰度开关打开后，明文请求一律拒绝（客户端升级完成才允许开）
        with _lock:
            _COUNTER["rejected"] += 1
        return {"__required__": ""}
    return plain


def reset_for_test() -> None:
    """测试用：清空缓存与计数（不动环境变量）。"""
    global _key_cache, _key_loaded
    with _lock:
        _key_cache = None
        _key_loaded = False
        _fail_window.clear()
        for k in _COUNTER:
            _COUNTER[k] = 0
