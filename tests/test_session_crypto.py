# -*- coding: utf-8 -*-
"""会话加密（敏感头应用层加密）—— 跨语言往返 + 回退语义。

对应 `docs/审计-服务器与开发版-2026-09-18.md` §2.1（明文 HTTP 的零成本对策）与
`docs/未完成事项清单.md` P0-2 的验收标准："抓包看不到明文 token/Key"。

这里验四件事（按重要性排序）：
  A. **JS 真加密 → Python 真解密**：跑 `tests/js/test_session_crypto.mjs`（Node + 真源码），
     把它的信封拿回来用**服务器私钥**解开，逐字节比对明文。这条是整条链唯一能提前抓出
     "两边都改错但互相自洽"的防线（文案级断言做不到）。
  B. 信封的**拒绝语义**：乱改一个字节、换 iv、换头名、垃圾串 —— 一律必须失败，
     且**失败时退回明文头**（老客户端不能被锁死）。
  C. 【硬要求】`Authorization` / `X-API-Key` / `X-API-Base` 之外的头**不进信封**（不白烧 RSA）。
  D. 公钥一致性：`app/static/js/session_crypto.js` 里的 n 必须就是**这把私钥**的公钥
     （否则线上是"服务器解不开自己签发的信封"——症状是所有人都退回明文，且没人报错）。

没有私钥时 SKIP（干净 clone / 没配环境的机器不该因此红），但**会显式打印 SKIP**。
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import session_crypto as SC                                       # noqa: E402

PASS = FAIL = SKIP = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def skip(desc, why=""):
    global SKIP
    SKIP += 1
    print(f"  - SKIP {desc}" + (f"   {why}" if why else ""))


KEY = Path(os.environ.get("FIREFLY_ENC_KEY")
           or (Path.home() / ".firefly" / "session_priv.pem")).expanduser()
os.environ["FIREFLY_ENC_ENABLED"] = "1"
os.environ["FIREFLY_ENC_KEY"] = str(KEY)
SC.reset_for_test()
HAVE_KEY = KEY.is_file() and SC.load_key() is not None


# ── 测试用的"客户端"（Python 版加密，与 JS 同口径）──────
def _mgf1(seed, length):
    out = b""
    c = 0
    while len(out) < length:
        out += hashlib.sha256(seed + c.to_bytes(4, "big")).digest()
        c += 1
    return out[:length]


def _oaep_encode(msg, k, n, e):
    h = 32
    if len(msg) > k - 2 * h - 2:
        raise ValueError("消息过长")
    lhash = hashlib.sha256(b"").digest()
    ps = b"\x00" * (k - len(msg) - 2 * h - 2)
    db = lhash + ps + b"\x01" + msg
    seed = os.urandom(h)
    db = bytes(a ^ b for a, b in zip(db, _mgf1(seed, k - h - 1)))
    seed = bytes(a ^ b for a, b in zip(seed, _mgf1(db, h)))
    em = b"\x00" + seed + db
    m = int.from_bytes(em, "big")
    if m >= n:
        raise ValueError("EM 超模数")
    return pow(m, e, n).to_bytes(k, "big")


def _keystream(key, iv, length):
    out = b""
    c = 0
    while len(out) < length:
        out += hmac.new(key, iv + c.to_bytes(4, "big"), hashlib.sha256).digest()
        c += 1
    return out[:length]


def client_env(values: dict) -> str:
    """按客户端口径造一个信封（供 B/C 组造"坏包"用）。"""
    n, e = SC.pubkey_params()
    k = (n.bit_length() + 7) // 8
    payload = json.dumps({"v": 1, "h": values}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    body = payload + hashlib.sha256(payload).digest()
    ck, iv = os.urandom(32), os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(body, _keystream(ck, iv, len(body))))
    wrapped = _oaep_encode(ck, k, n, e)
    b64u = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"enc.v1.{b64u(wrapped)}.{b64u(iv)}.{b64u(ct)}"


class _H:
    """假 HTTP handler（只要 headers.get）。"""

    def __init__(self, d):
        self.headers = _Headers(d)


class _Headers:
    def __init__(self, d):
        self.d = {k.lower(): v for k, v in d.items()}

    def get(self, k, default=None):
        return self.d.get(k.lower(), default)


print("=== A. ★ JS 真加密 → Python 真解密（跨语言往返）===")
cases = []
if not HAVE_KEY:
    skip("A 跨语言往返", f"没有私钥（{KEY}）—— 先跑 tools/build_session_keys.py --gen")
else:
    out_f = Path(tempfile.mkdtemp(prefix="hu_enc_")) / "cases.json"
    env = dict(os.environ)
    env["FIREFLY_ROOT"] = str(ROOT).replace("\\", "/")
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_session_crypto.mjs"), str(out_f)],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0 or not out_f.is_file():
        check("A1 Node 侧加密跑通", False, (r.stdout or "")[-160:] + (r.stderr or "")[-200:])
    else:
        data = json.loads(out_f.read_text(encoding="utf-8"))
        cases = data.get("cases") or []
        check("A1 Node 侧生成了信封", bool(cases), f"{len(cases)} 条")
        expect = {
            "webcrypto-中文与空格": {"Authorization": "Bearer 0f8a1b2c3d4e5f60718293a4b5c6d7e8",
                                 "X-API-Key": "sk-FAKEefghijklmnopqrstuvwxyz0123456789",
                                 "X-API-Base": "https://api.deepseek.com/v1"},
            "webcrypto-短值": {"Authorization": "Bearer x"},
            "webcrypto-长值(4096)": {"X-API-Key": "K" * 4096},
            "no-subtle-纯JS兜底": {"Authorization": "Bearer 0123456789abcdef0123456789abcdef",
                                "X-API-Key": "sk-FAKEn-js-path"},
        }
        for c in cases:
            want = expect.get(c["name"], {})
            try:
                got = SC.decrypt_envelope(c["env"])
            except Exception as ex:
                check(f"A2 {c['name']} 解密", False, f"{type(ex).__name__}: {ex}")
                continue
            check(f"A2 {c['name']} 解密且明文逐字节一致", got == want,
                  "" if got == want else f"得到 keys={sorted(got)}")
        # 公钥一致性（D 组提前在这里做，因为顺手）
        n_js = base64.b64decode(data["pub"]["n_b64"])
        n_py = SC.pubkey_params()[0].to_bytes(256, "big")
        check("A3 ★ JS 内置公钥 == 服务器私钥的公钥",
              n_js == n_py, f"js={hashlib.sha256(n_js).hexdigest()[:12]} "
                            f"py={hashlib.sha256(n_py).hexdigest()[:12]}")
        check("A4 JS 公钥指数是 65537", int(data["pub"]["e"]) == 65537)

print("=== B. 信封拒绝语义（坏包一律不许解出凭证）===")
if not HAVE_KEY:
    skip("B 坏包拒绝", "没有私钥")
else:
    good = client_env({"Authorization": "Bearer secret-token"})
    check("B1 好包能解开", SC.decrypt_envelope(good) == {"Authorization": "Bearer secret-token"})
    bad = []
    # ① 密文翻一位
    p = good.split(".", 4)
    ct = bytearray(base64.urlsafe_b64decode(p[4] + "=" * (-len(p[4]) % 4)))
    ct[0] ^= 0x01
    p2 = list(p)
    p2[4] = base64.urlsafe_b64encode(bytes(ct)).decode().rstrip("=")
    bad.append(("密文翻一位", ".".join(p2)))
    # ② wrapped key 翻一位
    wk = bytearray(base64.urlsafe_b64decode(p[2] + "=" * (-len(p[2]) % 4)))
    wk[5] ^= 0x01
    p3 = list(p)
    p3[2] = base64.urlsafe_b64encode(bytes(wk)).decode().rstrip("=")
    bad.append(("密钥信封翻一位", ".".join(p3)))
    # ③ 换 iv（用另一个包的 iv）
    other = client_env({"X-API-Key": "k"})
    p4 = list(p)
    p4[3] = other.split(".", 4)[3]
    bad.append(("iv 被替换", ".".join(p4)))
    # ④ 格式与垃圾
    bad.append(("前缀不对", "enc.v9." + ".".join(p[2:])))
    bad.append(("纯垃圾", "not-an-envelope"))
    bad.append(("空串", ""))
    bad.append(("段数不足", "enc.v1.abc"))
    for name, env in bad:
        try:
            out = SC.decrypt_envelope(env)
            check(f"B2 {name} → 必须失败", False, f"却解出了 {out}")
        except Exception:
            check(f"B2 {name} → 拒绝", True)

print("=== C. 头级行为：加密优先 + 明文回落 + 不误伤非敏感头 ===")
if not HAVE_KEY:
    skip("C 头级行为", "没有私钥")
else:
    SC.reset_for_test()
    env = client_env({"Authorization": "Bearer enc-value", "X-API-Key": "sk-FAKEenc"})
    got = SC.decrypt_headers(_H({"X-Firefly-Enc": env,
                                 "X-API-Mode": "proxy"}))
    check("C1 加密头解出正确值", got.get("Authorization") == "Bearer enc-value"
          and got.get("X-API-Key") == "sk-FAKEenc", str(sorted(got)))
    check("C2 非敏感头不进返回值（由调用方读原头）", "X-API-Mode" not in got)
    # 坏包 + 明文同时在：必须用明文（灰度期"宁可用明文也不让用户登不进去"）
    SC.reset_for_test()
    got = SC.decrypt_headers(_H({"X-Firefly-Enc": "enc.v1.bad.bad.bad",
                                 "Authorization": "Bearer plain-value"}))
    check("C3 ★ 信封坏了 → 退回明文头（不锁死老客户端）",
          got.get("Authorization") == "Bearer plain-value", str(got))
    check("C4 失败被计数（灰度观测要能看出还有多少人没升级）",
          SC.stats()["fail"] >= 1, str(SC.stats()))
    # 没有信封：明文照常
    SC.reset_for_test()
    got = SC.decrypt_headers(_H({"Authorization": "Bearer plain-only"}))
    check("C5 无信封 → 明文照常", got.get("Authorization") == "Bearer plain-only")
    check("C6 明文回落被计数", SC.stats()["plain"] == 1, str(SC.stats()))
    # required 开关：没有信封时拒绝
    os.environ["FIREFLY_ENC_REQUIRED"] = "1"
    try:
        got = SC.decrypt_headers(_H({"Authorization": "Bearer plain-only"}))
        check("C7 开了 REQUIRED 后明文请求被拒（交调用方 401）", "__required__" in got, str(got))
    finally:
        os.environ.pop("FIREFLY_ENC_REQUIRED", None)
    # 失败限流：拿垃圾信封刷不动作模幂
    SC.reset_for_test()
    for _ in range(SC._FAIL_LIMIT + 2):
        SC.decrypt_headers(_H({"X-Firefly-Enc": "enc.v1.x.x.x"}))
    st = SC.stats()
    check("C8 ★ 连续失败后进入限流（不再做模幂）",
          st["rejected"] >= 1, str(st))
    SC.reset_for_test()

print("=== D. 关掉开关时完全不参与（防止没配密钥的服务器白算）===")
os.environ["FIREFLY_ENC_ENABLED"] = "0"
try:
    SC.reset_for_test()
    got = SC.decrypt_headers(_H({"X-Firefly-Enc": "enc.v1.a.b.c",
                                 "Authorization": "Bearer x"}))
    check("D1 开关关闭时不尝试解密（直接明文）", got.get("Authorization") == "Bearer x")
    check("D2 开关关闭时不记加密计数", SC.stats()["enc"] == 0 and SC.stats()["fail"] == 0,
          str(SC.stats()))
finally:
    os.environ["FIREFLY_ENC_ENABLED"] = "1"
    SC.reset_for_test()

print("=== E. 私钥缺失/损坏时的降级（加密是加固，不该拖垮请求）===")
_saved = SC.key_path()
os.environ["FIREFLY_ENC_KEY"] = str(Path(tempfile.gettempdir()) / "no_such_key_firefly.pem")
SC.reset_for_test()
check("E1 私钥不存在 → load_key() 返回 None 且不抛", SC.load_key() is None)
got = SC.decrypt_headers(_H({"Authorization": "Bearer still-works"}))
check("E2 没有私钥时明文照常通过", got.get("Authorization") == "Bearer still-works")
broken = Path(tempfile.mkdtemp(prefix="hu_badkey_")) / "bad.pem"
broken.write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
                  encoding="utf-8")
os.environ["FIREFLY_ENC_KEY"] = str(broken)
SC.reset_for_test()
check("E3 私钥损坏 → 返回 None（不抛、不打崩服务）", SC.load_key() is None)
os.environ["FIREFLY_ENC_KEY"] = str(_saved)
SC.reset_for_test()

print("=== F. 服务器入口集成：_hdr() 真的走了解密（不是只写了函数没人调）===")
if not HAVE_KEY:
    skip("F 入口集成", "没有私钥")
else:
    sys.path.insert(0, str(ROOT / "server"))
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_ff_app_probe", ROOT / "server" / "app.py")
        # 只为拿 FireflyHandler 这个类；执行 app.py 会连带 import db/auth 等（服务器版依赖齐全）
        import app as _srv_app                                     # noqa: E402
        H = _srv_app.FireflyHandler
    except Exception as ex:
        skip("F 入口集成", f"server/app.py 在本机不可导入（{type(ex).__name__}）")
        H = None
    if H is not None:
        SC.reset_for_test()
        env = client_env({"Authorization": "Bearer from-envelope", "X-API-Key": "sk-FAKE-envelope"})

        class _Probe:
            """只借 FireflyHandler 的 _decrypt_once/_hdr（不跑 socket）。"""

            def __init__(self, headers):
                self.headers = _Headers(headers)

        _Probe._decrypt_once = H._decrypt_once
        _Probe._hdr = H._hdr
        _Probe._NO_HDR = H._NO_HDR
        p = _Probe({"X-Firefly-Enc": env, "User-Agent": "probe"})
        check("F1 ★ Authorization 从信封里读出来", p._hdr("Authorization") == "Bearer from-envelope",
              p._hdr("Authorization")[:20])
        check("F2 ★ X-API-Key 从信封里读出来", p._hdr("X-API-Key") == "sk-FAKE-envelope")
        check("F3 非敏感头照常直读", p._hdr("User-Agent") == "probe")
        check("F4 只解一次（第二次不重算）",
              SC.stats()["enc"] == 1, str(SC.stats()))
        # 老客户端（无信封）必须照常
        p2 = _Probe({"Authorization": "Bearer legacy-client"})
        check("F5 老客户端明文头照常读到", p2._hdr("Authorization") == "Bearer legacy-client")
        # REQUIRED 打开后：明文请求读不到 token（于是 do_POST 会 401）
        os.environ["FIREFLY_ENC_REQUIRED"] = "1"
        try:
            p3 = _Probe({"Authorization": "Bearer legacy-client"})
            check("F6 ★ 开了 REQUIRED 后明文请求读不到凭证（→ 上层 401）",
                  p3._hdr("Authorization") == "")
        finally:
            os.environ.pop("FIREFLY_ENC_REQUIRED", None)
        SC.reset_for_test()

print(f"\n统计: PASS={PASS} FAIL={FAIL} SKIP={SKIP}")
sys.exit(1 if FAIL else 0)
