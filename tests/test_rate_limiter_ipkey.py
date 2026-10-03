# -*- coding: utf-8 -*-
"""反代全局限流：**IP 为主键** + 令牌二级 + LRU 逐出（安全审计 S-2 回归）

旧实现的漏洞（`_harden/audit091_sec/_audit_t1b_limiter_unit.py` 复现）：
  记账键 = "IP|令牌指纹" ⇒ **同一 IP、每请求换一个 Authorization** 即可完全绕过限流
  （同 IP 3050 次 0 拒绝）；且 `_RATE` 超 5000 键时**整体 `clear()`** ⇒ 攻击者喂几千个
  键就能把**其他客户端**的限流计数一并抹掉（让被打的 IP 立刻重新获得配额）。

本测试把审计脚本的场景**固化成断言**（不再是一次性演示）：
  A) 固定键：IP 层配额生效（第 RATE_PER_MIN+1 次开始拒）；
  B2) 同一 IP、每请求换令牌：**开始被拒**（绕过被堵死）；
  B) 不同 IP 各自计数（互不牵连、不误伤）；
  C) 喂几千个不同键：**不清空**已有客户端的计数（原实现会整体 clear）；
  D) 滑动窗口：61 秒前的陈旧记录被剔除；
  E) LRU 逐出只淘汰最久未用的键，且**不清空全局**。

纯隔离单测（不涉网络、不写磁盘）。
"""
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import download_lib as L   # noqa: E402

PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


RATE = L.RATE_PER_MIN

print("== A. 两层配额各自生效（R-03 后令牌层 = IP 层 1/3）==")
L._RATE.clear()
first_deny = None
for i in range(1, RATE + 60):
    if not L._rate_ok("1.2.3.4"):              # ★ 纯 IP 键（无令牌）⇒ 只受 IP 层约束
        first_deny = i
        break
check(f"A1 纯 IP 键：第 {RATE + 1} 次开始拒（实际 {first_deny}）", first_deny == RATE + 1,
      f"first_deny={first_deny}")

L._RATE.clear()
TOK = L.RATE_PER_TOKEN_MIN
first_deny_tok = None
for i in range(1, TOK + 60):
    if not L._rate_ok("1.2.3.4|deadbeef0000"):  # 带令牌 ⇒ 令牌层先到
        first_deny_tok = i
        break
check(f"A2 带令牌键：第 {TOK + 1} 次（令牌层）开始拒（实际 {first_deny_tok}）",
      first_deny_tok == TOK + 1, f"first_deny={first_deny_tok}")
check("A3 令牌层严格小于 IP 层（细分真的生效）", TOK < RATE, f"token={TOK} ip={RATE}")

print("== B2. 同一 IP、每请求换令牌指纹：绕过被堵死（必须开始被拒）==")
L._RATE.clear()
N = RATE + 50
denies = sum(1 for i in range(N) if not L._rate_ok("1.2.3.4|%012x" % i))
check(f"B2 同 IP 轮换令牌喂 {N} 次 → 拒绝 {denies} 次 > 0（旧实现恒为 0）", denies > 0,
      f"denies={denies}")
check("B2b 拒绝次数符合 IP 层配额（3001 起拒 → 50 次）", denies == 50, f"denies={denies}")

print("== B. 不同 IP 各自计数（不同出口 IP 相互独立，不误伤）==")
L._RATE.clear()
denies_b = sum(1 for i in range(N) if not L._rate_ok("10.0.0.%d|%d" % (i % 200, i)))
check("B 200 个不同 IP 各 ~15 次 → 0 拒绝（正确：不同 IP 独立）", denies_b == 0,
      f"denies={denies_b}")

print("== C. 喂几千个不同键：不得清空他人计数（旧实现整体 clear()）==")
L._RATE.clear()
TOK2 = L.RATE_PER_TOKEN_MIN
victim_tok = "9.9.9.9|victim000000"
for _ in range(TOK2 - 1):
    L._rate_ok(victim_tok)
victim_ip = "9.9.9.8"                       # 另一个 IP，只受 IP 层约束
for _ in range(RATE - 1):
    L._rate_ok(victim_ip)
v_tok_before = len(L._RATE[victim_tok])
v_ip_before = len(L._RATE[victim_ip])
check(f"C0 两个受害者计数已就位（令牌层 {v_tok_before} / IP 层 {v_ip_before}）",
      v_tok_before == TOK2 - 1 and v_ip_before == RATE - 1,
      f"tok={v_tok_before} ip={v_ip_before}")
for i in range(5200):                       # 旧实现 >5000 触发整体 clear()
    L._rate_ok("k%05d" % i)
check("C1 攻击者喂 5200 个键后，**两个受害者的计数都仍在**（未被整体清空）",
      len(L._RATE.get(victim_tok, [])) == v_tok_before
      and len(L._RATE.get(victim_ip, [])) == v_ip_before,
      f"tok now={len(L._RATE.get(victim_tok, []))} ip now={len(L._RATE.get(victim_ip, []))}")
check("C2 令牌层受害者未被重置：再放行 1 次后即被拒（而非又能用满一整轮）",
      L._rate_ok(victim_tok) is True and L._rate_ok(victim_tok) is False)
check("C2b IP 层受害者未被重置：再放行 1 次后即被拒",
      L._rate_ok(victim_ip) is True and L._rate_ok(victim_ip) is False)
check("C3 攻击者自己的键也仍在（不是 clear 后重建的少量键）",
      "k00000" in L._RATE and "k05199" in L._RATE)

print("== D. 滑动窗口：陈旧记录被剔除 ==")
L._RATE.clear()
recs = L._RATE.setdefault("1.2.3.4|deadbeef0000", [])
now = time.time()
recs[:] = [now - 61.0] * (RATE + 10)
check("D 61 秒前的记录全部过期 → 本次放行", L._rate_ok("1.2.3.4|deadbeef0000") is True)

print("== E. LRU 逐出：只淘汰最久未用，不清空全局 ==")
L._RATE.clear()
L._RATE_LAST.clear()
_orig_max = L.MAX_RATE_KEYS
L.MAX_RATE_KEYS = 100
try:
    for i in range(150):
        L._rate_ok("ip-%03d" % i)
    check("E1 键数被压在 MAX_RATE_KEYS 附近（逐出生效，未无限增长）",
          len(L._RATE) <= L.MAX_RATE_KEYS + 5, f"keys={len(L._RATE)}")
    check("E2 最新的键仍在（淘汰的是最久未用的，不是整体清空）", "ip-149" in L._RATE)
    check("E3 最老的键已被淘汰", "ip-000" not in L._RATE)
finally:
    L.MAX_RATE_KEYS = _orig_max

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
