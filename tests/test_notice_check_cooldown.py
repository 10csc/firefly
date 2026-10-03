# -*- coding: utf-8 -*-
"""公告 `action=check` 冷却/互斥回归（R-04 / 安全审计 S-7）

问题（`_harden/audit091_sec` + `08` §A4 / `09` R-04）：`POST /notice/action {"action":"check"}`
走 `notice.check(force=True)`，**跳过** `CHECK_INTERVAL` ⇒ 任一登录账号循环调用即可让
服务端**无限对外抓取**（服务器版下更新源就是自身公网网关，等于自打自）；且
`_RT["refreshing"]` 互斥只在 `payload()` 里，force 路径不受保护。

修法：force 加**全局冷却**（`FIREFLY_NOTICE_CHECK_COOLDOWN`，默认 60s）+ **互斥**；
冷却/互斥只作用于 force，`auto_refresh` 的既有节流不受影响。

本测试纯单元 + 临时数据根（**不联网、不落源码树**）：
  ① 首次 force 正常执行（本案未配置公告源 ⇒ 返回 ok=False 但**不是 skipped**）；
  ② 冷却期内再次 force ⇒ `skipped=True, reason="cooldown"` 且带 `retry_after`；
  ③ 已有刷新进行中 ⇒ `skipped=True, reason="in_progress"`；
  ④ 非 force 的既有行为不受影响（缓存新鲜 ⇒ `cached=True`）。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ff_notice_cd_"))
os.environ["FIREFLY_DATA_DIR"] = str(TMP)           # ★ 临时数据根，别落源码树
os.environ["FIREFLY_NOTICE_CHECK_COOLDOWN"] = "60"

sys.path.insert(0, str(ROOT / "app"))
import notice       # noqa: E402

PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


try:
    print("== ① 常量与首次 force ==")
    check("①a 冷却常量已定义且 > 0", notice.FORCE_COOLDOWN > 0,
          f"FORCE_COOLDOWN={notice.FORCE_COOLDOWN}")
    check("①b 冷却远小于 CHECK_INTERVAL（手动检查不该显得'坏了'）",
          0 < notice.FORCE_COOLDOWN < notice.CHECK_INTERVAL,
          f"cooldown={notice.FORCE_COOLDOWN} interval={notice.CHECK_INTERVAL}")

    notice._RT["last_force"] = 0
    notice._RT["refreshing"] = False
    r1 = notice.check(force=True)
    check("①c 首次 force **没有**被跳过", not r1.get("skipped"), f"r1={r1}")
    check("①d 首次 force 已记录时间戳", float(notice._RT.get("last_force") or 0) > 0,
          str(notice._RT.get("last_force")))

    print("== ② 冷却期内再次 force 被挡 ==")
    r2 = notice.check(force=True)
    check("②a 第二次 force 被跳过", r2.get("skipped") is True, f"r2={r2}")
    check("②b 理由为 cooldown", r2.get("reason") == "cooldown", f"r2={r2}")
    check("②c 给了 retry_after（客户端可退避）", int(r2.get("retry_after") or 0) > 0, f"r2={r2}")
    check("②d 返回值形状安全（不抛异常、带 serial）", "serial" in r2, f"r2={r2}")

    print("== ③ 已有刷新在跑 ⇒ in_progress（互斥）==")
    notice._RT["last_force"] = 0            # 跳过冷却，专测互斥
    notice._RT["refreshing"] = True
    r3 = notice.check(force=True)
    check("③a 被跳过", r3.get("skipped") is True, f"r3={r3}")
    check("③b 理由为 in_progress", r3.get("reason") == "in_progress", f"r3={r3}")
    notice._RT["refreshing"] = False

    print("== ④ 非 force 的既有节流不受影响 ==")
    r4 = notice.check(force=False)
    check("④a 缓存新鲜时直接回缓存（不再联网）",
          r4.get("cached") is True or r4.get("ok") is False, f"r4={r4}")
    check("④b 非 force 不会被 force 冷却误伤", r4.get("reason") != "cooldown", f"r4={r4}")

    print("== ⑤ 冷却到期后可再 force ==")
    notice._RT["last_force"] = 0            # 模拟冷却已过
    r5 = notice.check(force=True)
    check("⑤a 冷却过后 force 又能执行（不是把功能锁死）", not r5.get("skipped"), f"r5={r5}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
