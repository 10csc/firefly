# -*- coding: utf-8 -*-
"""server_app 拆分（阶段 2 · 任务 2.4）的**结构守卫**

`server/` 不进 git（用户既定决策），本卡用「时间戳 tar 备份到 _harden/ + 本文件」代替 commit 回滚点。
拆分：`server_app.py`（720 行）→ `quota.py`（额度）· `admin.py`（管理台 + 运维开关）·
`auth_endpoints.py`（认证 mixin）· `app.py`（装配 + FireflyHandler 分发 + main）；
`server_app.py` 退化为**入口层**（硬置平台标记 + re-export + `__main__`）。

守四件事：
1. 入口层职责单一：硬置平台标记 → re-export → `__main__`，不再定义 handler；
2. 装配体（app.py）拥有 FireflyHandler / AdminHandler 转发 / main，且 `FireflyHandler` 混入了
   认证 mixin（7 个 `_auth_*` 方法来自 auth_endpoints.py，方法体未改）；
3. 运维面（admin.py）与额度面（quota.py）各自归属正确；
4. 测试/运维经 `server_app.*` 取用的名字一个不少（`ThreadingHTTPServer` 属"顺带导入"，
   少一个 tests/test_kimi_fixes.py、test_old_client_gate.py 就起不了临时服务）。
"""
import inspect
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import os

os.environ["FIREFLY_SERVER"] = "1"      # 与真实入口一致（_load_config 的模型锁按服务器版走）

import server_app as srv            # noqa: E402
import admin as srv_admin           # noqa: E402
import app as srv_app               # noqa: E402
import auth_endpoints as srv_auth   # noqa: E402
import quota as srv_quota           # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


entry_src = (SERVER / "server_app.py").read_text(encoding="utf-8")
app_src = (SERVER / "app.py").read_text(encoding="utf-8")

print("=== A. 入口层职责单一 ===")
check("A1 入口仍硬置平台标记（不是 setdefault）",
      'os.environ["FIREFLY_SERVER"] = "1"' in entry_src)
check("A2 入口不再定义 FireflyHandler / AdminHandler",
      "class FireflyHandler" not in entry_src and "class AdminHandler" not in entry_src)
check("A3 入口保留 __main__ 入口（systemd ExecStart 路径不变）",
      '__name__ == "__main__"' in entry_src and "main()" in entry_src)
check("A4 入口行数收敛到 60 行内（拆分前 720 行）", len(entry_src.splitlines()) <= 60)

print("=== B. 装配体（app.py）===")
check("B1 FireflyHandler / AdminHandler 由 app.py 持有",
      hasattr(srv_app, "FireflyHandler") and hasattr(srv_app, "AdminHandler"))
check("B2 main() 在 app.py 且含平台确认打印",
      "def main():" in app_src and "[平台] FIREFLY_SERVER=" in app_src)
check("B3 FireflyHandler 继承认证 mixin", srv_auth.AuthEndpointsMixin in srv_app.FireflyHandler.__mro__)
check("B4 认证端点方法来自 auth_endpoints 模块",
      all(getattr(srv_app.FireflyHandler, n).__module__ == "auth_endpoints"
          for n in ("_auth_mail_send", "_auth_register", "_auth_login",
                    "_auth_reset_send", "_auth_reset_password", "_auth_logout", "_auth_verify")))
check("B5 请求上下文/分发仍在 FireflyHandler 自身",
      all(getattr(srv_app.FireflyHandler, n).__module__ == "app"
          for n in ("_setup_user_context", "_cors_headers", "_bearer_user", "do_GET", "do_POST")))

print("=== C. 运维面与额度面归属 ===")
check("C1 AdminHandler 在 admin.py 且继承 ResponseMixin",
      getattr(srv_admin.AdminHandler, "__module__", "") == "admin"
      and "ResponseMixin" in [c.__name__ for c in srv_admin.AdminHandler.__mro__])
check("C2 运维开关/令牌/留痕在 admin.py",
      all(hasattr(srv_admin, n) for n in ("SHRINK_FILE", "_shrink_active", "ADMIN_TOKEN",
                                          "_log_recent", "_apply_settings", "_download_stats")))
check("C3 admin.py 自算 FRONTEND_DIR（打断 admin ↔ app 循环）",
      hasattr(srv_admin, "FRONTEND_DIR") and "FRONTEND_DIR = _FRONTEND_SRC" in
      (SERVER / "admin.py").read_text(encoding="utf-8"))
check("C4 额度钩子与预算在 quota.py",
      all(hasattr(srv_quota, n) for n in ("PROXY_DAILY_BUDGET", "_get_budget", "_user_quota",
                                          "_proxy_quota_check", "_proxy_quota_count", "_proxy_quota_fail")))
check("C5 额度检查用到用户维度与全站池（1.1 的修复语义仍在）",
      "proxy_usage_user_get" in inspect.getsource(srv_quota._proxy_quota_check)
      and "proxy_usage_get" in inspect.getsource(srv_quota._proxy_quota_check))

print("=== D. 经 server_app.* 取用的名字一个不少 ===")
_NEEDED = ("ThreadingHTTPServer", "SimpleHTTPRequestHandler", "FireflyHandler", "AdminHandler",
           "main", "PORT", "STATIC_DIR", "FRONTEND_DIR", "_proxy_quota_check",
           "_proxy_quota_count", "_proxy_quota_fail", "_get_budget", "_shrink_active", "ADMIN_TOKEN")
_missing = [n for n in _NEEDED if not hasattr(srv, n)]
check(f"D1 {len(_NEEDED)} 个对外名字全部可达", not _missing)
if _missing:
    print("    缺失:", _missing)
check("D2 入口暴露的就是新模块里的同一批对象",
      srv.FireflyHandler is srv_app.FireflyHandler and srv.AdminHandler is srv_admin.AdminHandler
      and srv._proxy_quota_check is srv_quota._proxy_quota_check)

print("=== E. 备份（server/ 无 git 掩护）===")
_harden = sorted((ROOT / "_harden").glob("server_pre_2.4_*.tar.gz"))
check("E1 拆分前的 server/ tar 备份存在（回滚点）", bool(_harden))
if _harden:
    print("    ", _harden[0].relative_to(ROOT), _harden[0].stat().st_size, "bytes")

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
