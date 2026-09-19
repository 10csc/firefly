# -*- coding: utf-8 -*-
"""攻击哨兵回归（2026-09-18 修的四件）。纯本地逻辑，**不连服务器、不弹窗、不发请求**。

病：服务器上「检测到攻击」的弹窗**一次封禁弹 4-5 次**（2026-08-28 16:01-16:05 连续 5 条、
2026-09-18 22:03-22:07 连续 4 条，各自其实只有**一个**来源 IP，且都是公网 SSH 扫描这种
日常噪声）。根因是两头：
  · `server/attack_watch.sh` 统计「**最近 5 分钟**里的 Ban 行数」，一次封禁在窗口内每分钟
    都命中 → 标记文件每分钟被重写、`time` 每次都变；
  · `tools/attack_sentinel.py` 的去重键正是 `flag["time"]` → **去重完全失效**。

本测试钉死修复后的四件事：
1. 去重键 = **内容签名**（与 time 无关），同一次封禁的不同时间戳必须同签名；
2. 单次 SSH 封禁按**噪声档**处理 → 只写日报、不弹窗、不调模型；
3. 降噪阈值边界（429 / SSH 失败 / 连封多个 IP 才升级为真事件）；
4. 模型输出为空/异常 → **本地兜底**，且 `need_user` 不再恒为 True
   （09-18 那次模型返回 `{"analysis":"","need_user":true}`，弹窗里「分析」是空的却写着
   「需要你行动：是」）。

依赖：`tools/attack_sentinel.py` 顶层 import paramiko / requests（哨兵本来就要）。
"""
import importlib.util
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sentinel", ROOT / "tools" / "attack_sentinel.py")
S = importlib.util.module_from_spec(spec)
sys.modules["sentinel"] = S
spec.loader.exec_module(S)

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


# 09-18 真实标记（当时弹了 4 次的那一个）
REAL = {"detected": True, "time": "2026-09-18 22:02:05", "fail2ban_bans": 1,
        "rate_limited": 0, "ssh_fails": 0, "gateway_429": 0, "banned": "47.104.232.1"}

print("=== A. 去重键：同一封禁的不同时间戳必须同签名 ===")
same_1 = dict(REAL, time="2026-09-18 22:03:01")
same_2 = dict(REAL, time="2026-09-18 22:07:01")
check("A1 同一次封禁、时间戳不同 → 签名相同（旧实现按 time 去重会当成 2 个事件）",
      S.event_signature(same_1) == S.event_signature(same_2))
other_ip = dict(REAL, banned="1.2.3.4")
check("A2 换了来源 IP → 签名不同（是真新事件）", S.event_signature(REAL) != S.event_signature(other_ip))
burst = dict(REAL, fail2ban_bans=4)
check("A3 封禁数变了 → 签名不同", S.event_signature(REAL) != S.event_signature(burst))
check("A4 签名不含 time 字段", "2026-09-18 22:02:05" not in str(S.event_signature(REAL)))

print("=== B. 噪声档判定 ===")
check("B1 单次 SSH 封禁（09-18 那条真实标记）→ 噪声", S.is_noise(REAL) is True)
check("B2 一次封禁 + 主服务 429 ≥5 → 不是噪声",
      S.is_noise(dict(REAL, rate_limited=5)) is False)
check("B3 SSH 失败 ≥5 → 不是噪声", S.is_noise(dict(REAL, ssh_fails=5)) is False)
check("B4 网关 429 ≥10 → 不是噪声", S.is_noise(dict(REAL, gateway_429=10)) is False)
check("B5 连封 3 个 IP → 不是噪声", S.is_noise(dict(REAL, fail2ban_bans=3)) is False)

print("=== C. 模型输出异常时的兜底 ===")
empty = S._ensure_verdict({"analysis": "", "need_user": True}, REAL)
check("C1 analysis 为空 → 用本地兜底（不再弹空窗）", len(empty["analysis"]) > 10)
check("C2 兜底里 need_user 为 False（单次封禁不需要人管）", empty["need_user"] is False)
check("C3 兜底说清了被封的 IP", "47.104.232.1" in empty["analysis"])
check("C4 非 dict 输出也不炸", isinstance(S._ensure_verdict("不是JSON", REAL), dict))
lacking = S._ensure_verdict({"analysis": "有点内容"}, REAL)
check("C5 缺字段被补齐（severity/attack_type/actions/need_user）",
      all(k in lacking for k in ("severity", "attack_type", "actions", "need_user")))
high = S._ensure_verdict({"analysis": "严重", "severity": "critical"}, REAL)
check("C6 严重档 need_user 自动为 True", high["need_user"] is True)
app = S.fallback_verdict(dict(REAL, fail2ban_bans=0, rate_limited=9))
check("C7 非封禁类兜底 → severity=medium 且需要人看", app["severity"] == "medium"
      and app["need_user"] is True)

print("=== D. 服务器侧脚本的「新封禁」语义（静态检查）===")
sh = (ROOT / "server" / "attack_watch.sh").read_text(encoding="utf-8")
check("D1 有状态文件（记住已上报的 Ban 时间）", ".attack_seen" in sh)
check("D2 只取比上次更新的 Ban", "$0 > last" in sh)
check("D3 标记里的 time 用**封禁时间**而不是当前时间", '"$LASTNEW"' in sh)
check("D4 标记带被封 IP", '"banned"' in sh)
check("D5 标记有保留期（保证哨兵一定读到）", "GRACE" in sh and "-mmin" in sh)
check("D6 仍保留应用层阈值（429 等）", "RATE\" -ge 5" in sh and "GW429\" -ge 10" in sh)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
