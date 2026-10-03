# -*- coding: utf-8 -*-
"""限流「令牌二级配额 = IP 层 1/3」回归（R-03 / 安全收尾 A3 / P7-3）

背景：S-2 已把记账键改成"IP 为主键、令牌为二级"，防绕过生效；但 `RATE_PER_TOKEN_MIN`
曾被取成 == `RATE_PER_MIN` ⇒ "一个人别拖累同 IP 所有人"（CGNAT）在**数值上无约束**。
R-03 把令牌层设为 **IP 层的三分之一**（3000 ⇒ 1000，可配 `FIREFLY_RATE_PER_TOKEN_MIN`）。

本测试钉死四件（纯单元，不涉网络/磁盘）：
  ① 常量确实是 1/3（且可配）；
  ② **单令牌**到自己的配额即被拒；
  ③ **同 IP 的另一令牌不受影响**（这是"细分"的意义所在）；
  ④ **轮换令牌不能放大该 IP 的总配额**（沿用审计脚本 B2 的形态）。
"""
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import download_lib as L       # noqa: E402

PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


def reset():
    L._RATE.clear()
    try:
        L._RATE_LAST.clear()
    except Exception:
        pass


print("== ① 常量：令牌层 = IP 层 1/3 ==")
check("①a RATE_PER_TOKEN_MIN == max(1, RATE_PER_MIN // 3)",
      L.RATE_PER_TOKEN_MIN == max(1, L.RATE_PER_MIN // 3),
      f"token={L.RATE_PER_TOKEN_MIN} ip={L.RATE_PER_MIN}")
check("①b 令牌层严格小于 IP 层（数值上真的在细分）",
      L.RATE_PER_TOKEN_MIN < L.RATE_PER_MIN,
      f"token={L.RATE_PER_TOKEN_MIN} ip={L.RATE_PER_MIN}")
check("①c 仍不放大总配额（令牌层 ≤ IP 层）", L.RATE_PER_TOKEN_MIN <= L.RATE_PER_MIN)

IP = "203.0.113.7"
TA, TB = IP + "|tokAAAA000000", IP + "|tokBBBB000000"

print("== ② 单令牌到自己的配额即被拒 ==")
reset()
ok_a = 0
for _ in range(L.RATE_PER_TOKEN_MIN + 10):
    if L._rate_ok(TA):
        ok_a += 1
check(f"②a 单令牌恰好放行 {L.RATE_PER_TOKEN_MIN} 次后被拒", ok_a == L.RATE_PER_TOKEN_MIN,
      f"ok={ok_a} expect={L.RATE_PER_TOKEN_MIN}")
check("②b 该令牌下一次仍被拒", L._rate_ok(TA) is False)

print("== ③ 同 IP 的另一令牌不受影响（细分的意义）==")
ok_b = sum(1 for _ in range(50) if L._rate_ok(TB))
check("③a 同 IP 的另一个令牌全部放行（未被 A 的用尽连坐）", ok_b == 50, f"ok={ok_b}")
check("③b 该 IP 的总计数为两者之和（IP 层仍在记账）",
      len(L._RATE.get(IP, [])) == L.RATE_PER_TOKEN_MIN + 50,
      f"ip_recs={len(L._RATE.get(IP, []))}")

print("== ④ 轮换令牌不能放大该 IP 的总配额 ==")
reset()
denies = 0
N = L.RATE_PER_MIN + 50
for i in range(N):
    if not L._rate_ok(f"{IP}|rot{i:012x}"):
        denies += 1
check(f"④a 同 IP 每请求换令牌喂 {N} 次 ⇒ 仍被拒 {denies} 次（>0 = 绕过被堵死）",
      denies > 0, f"denies={denies}")
check("④b 该 IP 的总记录数被顶在 RATE_PER_MIN 附近",
      len(L._RATE.get(IP, [])) >= L.RATE_PER_MIN, f"ip_recs={len(L._RATE.get(IP, []))}")

print("== ⑤ 不误伤：另一 IP 的令牌不受影响（IP 层隔离）==")
other_ok = sum(1 for _ in range(50) if L._rate_ok("198.51.100.9|tokCCCC000000"))
check("⑤a 另一个 IP 的令牌全部放行", other_ok == 50, f"ok={other_ok}")

print("== ⑥ 正常单账号每分钟可用量（前后对比的依据）==")
print(f"     单账号（令牌层）上限 = {L.RATE_PER_TOKEN_MIN} 次/分钟；"
      f"同 IP 全体合计 = {L.RATE_PER_MIN} 次/分钟")

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
