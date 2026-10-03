# -*- coding: utf-8 -*-
"""attack_watch「单 IP 并发连接数突增」告警回归（安全审计 S-1 的监测半边）

漏洞：slowloris / 半开连接（70 条只发半截请求头的连接）**既不产生 429、也不触发
fail2ban/SSH 任何计数** ⇒ 原 `server/attack_watch.sh` 对这种"占满线程池打停全站"的
攻击**完全静默`（连告警都没有）。修复：新增连接维度巡检（`ss -tan` 数每 IP 的
established 连接数），超阈值即写标记 `kind=conn_surge`（去重键 = IP，epoch 只做冷却窗口）。

本测试两部分：
  A) **服务器脚本接线**（静态）：connection 巡检 / 阈值 / conn_surge 标记 / 去重状态齐全，
     且原有巡检（fail2ban 新封禁语义）未被破坏；
  B) **本地哨兵消费**：`tools/attack_sentinel.py` 能正确把 conn_surge 当独立事件、
     不落噪声档、并给出本地兜底结论（注入 stub 依赖，纯本地）。
"""
import importlib.util
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


print("== A. attack_watch.sh：连接数巡检接线（静态）==")
sh = (ROOT / "server" / "attack_watch.sh").read_text(encoding="utf-8")
check("A1 有连接数巡检函数 conn_count_scan()", "conn_count_scan()" in sh)
check("A2 用 ss 数每 IP 的 established 连接", "state established" in sh and "-tan" in sh)
check("A3 阈值可配（FIREFLY_WATCH_CONN_HWM / CON_HWM）",
      "FIREFLY_WATCH_CONN_HWM" in sh and "CON_HWM" in sh)
check("A4 产出 conn_surge 标记", '"conn_surge"' in sh)
# 标记通过 printf 的 `\"conn_ip\":...,\"conn_count\":...` 形式写入 JSON，
# 因此字段名两侧是已转义的引号，这里只校验字段名本身存在。
check("A5 标记带 conn_ip 与 conn_count", "conn_ip" in sh and "conn_count" in sh)
check("A6 去重状态（IP<TAB>epoch）+ 登记函数 commit_conn",
      ".attack_connip" in sh and "commit_conn" in sh)
check("A7 原有巡检未被破坏（fail2ban 新封禁语义仍在）",
      ".attack_seen" in sh and '"$LASTNEW"' in sh and "GW429\" -ge 10" in sh)


def _load_sentinel():
    if "paramiko" not in sys.modules:
        m = types.ModuleType("paramiko")
        m.SSHClient = type("SSHClient", (), {})
        m.AutoAddPolicy = type("AutoAddPolicy", (), {})
        sys.modules["paramiko"] = m
    if "requests" not in sys.modules:
        r = types.ModuleType("requests")
        r.Session = type("Session", (), {})
        sys.modules["requests"] = r
    spec = importlib.util.spec_from_file_location("sentinel_connsurge", ROOT / "tools" / "attack_sentinel.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sentinel_connsurge"] = mod
    spec.loader.exec_module(mod)
    return mod


print("== B. attack_sentinel.py：把 conn_surge 当独立事件处理（本地）==")
S = _load_sentinel()
flag = {"detected": True, "kind": "conn_surge", "time": "2026-10-03 12:00:00",
        "conn_ip": "1.2.3.4", "conn_count": 95, "fail2ban_bans": 0}
check("B1 conn_surge **不落噪声档**（否则新巡检永远不会弹窗）", S.is_noise(flag) is False)
other = dict(flag, conn_ip="5.6.7.8")
check("B2 不同 IP 的 conn_surge → 签名不同（各自成事件）",
      S.event_signature(flag) != S.event_signature(other))
check("B3 同 IP、时间戳不同 → 签名相同（冷却窗口内不重复弹）",
      S.event_signature(flag) == S.event_signature(dict(flag, time="2026-10-03 12:09:00")))
v = S.fallback_verdict(flag)
check("B4 兜底点明 IP 与连接数，且 need_user=True",
      "1.2.3.4" in v["analysis"] and "95" in v["analysis"] and v["need_user"] is True,
      f"v={v}")
check("B5 兜底把这类事件判定为「连接数异常」（不是普通限流）",
      "连接数" in v["attack_type"])

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
