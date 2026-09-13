# -*- coding: utf-8 -*-
"""托管（proxy）授权与额度回归测试 —— 阶段 1 · 任务 1.1（C-2，P0）

背景（修复前的事实）：
- `server/server_app.py` 只要请求头带 `X-API-Mode: proxy` 就进托管模式，
  用**运营者 Key** 直发；`users.quota_api_proxy` 这一列从未参与放行判定。
- `_proxy_quota_check()` 只查全站当日池，没有用户维度 → 一个账号可以吃满全站额度。
- `app_config.get_client()` 的托管 Key 回落链还含 `DEEPSEEK_API_KEY`（运营者按量计费主 Key），
  FIREFLY_PROXY_KEY 忘了配就会静默烧主余额。

本测试钉死修复后的四条不变量：
1. 无授权账号（quota=0 且非 admin）带 proxy 头 → 降级 relay，绝不用托管 Key；
2. admin 账号带 proxy 头 → 进托管；
3. quota=N 的账号第 N+1 次调用被拒（个人日额度），前 N 次放行；全站池仍然生效；
4. 托管 Key 只认 FIREFLY_PROXY_KEY（配了主 Key 也不回落）。

沙箱纪律：USER_DIR 指到临时目录，DB 建在临时目录；绝不触碰真实 user_data/ 与生产库。
"""
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

# 先设平台标记再导入 app_config：与 server_app 运行期一致（模型锁 / 注册表只认 bundled）
os.environ["FIREFLY_SERVER"] = "1"
os.environ.pop("FIREFLY_PROXY_KEY", None)
os.environ.pop("DEEPSEEK_API_KEY", None)

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_proxy_quota_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"
cfg.config = {}          # 不继承真实 config.json（那是本机用户的 Key/供应商）

import db
import server_app as srv

db.init_db(_tmp / "firefly.db")
cfg.set_proxy_quota_checker(srv._proxy_quota_check)
cfg.set_proxy_quota_counter(srv._proxy_quota_count)
cfg.set_proxy_quota_failer(srv._proxy_quota_fail)

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


DAY = time.strftime("%Y-%m-%d")

uid_plain = db.create_user("plain@test.local", "h", "s")
uid_quota = db.create_user("quota@test.local", "h", "s")
uid_admin = db.create_user("admin@test.local", "h", "s", role="admin")


class FakeHandler:
    """只提供 _setup_user_context 用到的两个东西：headers 与承载 token 的属性。"""

    def __init__(self, mode=""):
        self.headers = {"X-API-Mode": mode} if mode else {}
        self._ctx_token = None
        self._proxy_downgraded = False


def enter(uid, mode=""):
    """模拟一次请求进入：建用户上下文，返回 (handler, 上下文 dict)。"""
    h = FakeHandler(mode)
    srv.FireflyHandler._setup_user_context(h, db.get_user_by_id(uid))
    return h, (cfg._user_ctx.get() or {})


def leave(h):
    cfg.reset_user_context(h._ctx_token)


print("=== A. 无授权账号：proxy 头必须降级为 relay ===")
h, ctx = enter(uid_plain, "proxy")
check("A1 上下文里 proxy=False（降级成功）", ctx.get("proxy") is False)
check("A2 处理器记录了降级（响应头可观测的依据）", h._proxy_downgraded is True)
os.environ["FIREFLY_PROXY_KEY"] = "test-proxy-key"      # 就算运营者 Key 配好了
_client = cfg.get_client()
check("A3 拿到的是 RelayClient 而不是托管客户端",
      type(_client).__name__ == "RelayClient")
leave(h)

h, ctx = enter(uid_plain, "")
check("A4 不带 proxy 头同样走 relay（未授权账号无差别）",
      ctx.get("proxy") is False and h._proxy_downgraded is False)
leave(h)

print("=== B. admin 账号：proxy 头放行进托管 ===")
h, ctx = enter(uid_admin, "proxy")
check("B1 上下文 proxy=True", ctx.get("proxy") is True)
check("B2 未记录降级", h._proxy_downgraded is False)
check("B3 拿到 QuotaClient（托管客户端）",
      type(cfg.get_client()).__name__ == "QuotaClient")
leave(h)

print("=== C. 授权账号：个人日额度按 quota_api_proxy 生效 ===")
db.update_user_quota(uid_quota, 5)
_u = db.get_user_by_id(uid_quota)
check("C0 quota 已写入库", _u["quota_api_proxy"] == 5)
h, ctx = enter(uid_quota, "proxy")
check("C1 授权账号进托管", ctx.get("proxy") is True)
ok_all = True
for _i in range(5):
    if srv._proxy_quota_check() != "":
        ok_all = False
    db.proxy_usage_add(uid_quota, DAY, 1)     # 模拟成功调用记账
check("C2 前 5 次全部放行", ok_all)
check("C3 已用量 5/5", db.proxy_usage_user_get(uid_quota, DAY) == 5)
_msg6 = srv._proxy_quota_check()
check("C4 第 6 次被拒且提示个人额度", bool(_msg6) and "额度已用完" in _msg6)
check("C5 别的账号不受影响（A 的用量是 0）", db.proxy_usage_user_get(uid_plain, DAY) == 0)
leave(h)

print("=== D. 全站池仍然生效（与个人额度叠加） ===")
db.set_setting("proxy_daily_budget", "6")
h, ctx = enter(uid_admin, "proxy")           # admin 个人不限，只受全站池约束
_msg = srv._proxy_quota_check()              # 全站已用 5（quota 账号）< 6 → 放行
check("D1 全站 5/6 时仍放行", _msg == "")
db.proxy_usage_add(uid_admin, DAY, 1)        # 全站到 6
_msg = srv._proxy_quota_check()
check("D2 全站到顶后 admin 也被拒", bool(_msg) and "额度已用完" in _msg)
db.set_setting("proxy_daily_budget", "5000")
leave(h)

print("=== E. 托管 Key 只认 FIREFLY_PROXY_KEY（不回落主 Key） ===")
h, ctx = enter(uid_admin, "proxy")
os.environ.pop("FIREFLY_PROXY_KEY", None)
os.environ["DEEPSEEK_API_KEY"] = "operator-main-key-should-not-be-used"
check("E1 只有主 Key → 托管客户端拿不到（拒绝静默烧主余额）", cfg.get_client() is None)
os.environ["FIREFLY_PROXY_KEY"] = "test-proxy-key"
check("E2 配上 FIREFLY_PROXY_KEY → 托管客户端可用",
      type(cfg.get_client()).__name__ == "QuotaClient")
os.environ.pop("DEEPSEEK_API_KEY", None)
leave(h)

print("=== F. 管理台额度入口（新增，C-2 配套） ===")
db.update_user_quota(uid_quota, 0)
check("F0 写 0 即收回授权",
      db.get_user_by_id(uid_quota)["quota_api_proxy"] == 0)
check("F1 db.update_user_quota 负值被夹到 0",
      db.update_user_quota(uid_quota, -7) and
      db.get_user_by_id(uid_quota)["quota_api_proxy"] == 0)
check("F2 db.update_user_quota 上限夹到 100000",
      db.update_user_quota(uid_quota, 10 ** 9) and
      db.get_user_by_id(uid_quota)["quota_api_proxy"] == 100000)


class FakeAdmin:
    """走真实 AdminHandler.do_POST 分支（_authed 打桩为已授权）。"""

    def __init__(self, path, body):
        payload = json.dumps(body).encode("utf-8")
        self.path = path
        self.headers = {"Content-Length": str(len(payload))}
        self.rfile = io.BytesIO(payload)
        self.data = None
        self.status = 200

    def _json(self, data, status=200):
        self.data = data
        self.status = status

    def _authed(self):
        return True


_a = FakeAdmin("/admin/api/users/quota", {"user_id": uid_quota, "quota": 30})
srv.AdminHandler.do_POST(_a)
check("F3 POST /admin/api/users/quota 生效",
      _a.data.get("ok") is True and db.get_user_by_id(uid_quota)["quota_api_proxy"] == 30)
_a = FakeAdmin("/admin/api/users/quota", {"user_id": uid_quota, "quota": -1})
srv.AdminHandler.do_POST(_a)
check("F4 负数被拒（ok=False）", _a.data.get("ok") is False)
_a = FakeAdmin("/admin/api/users/quota", {"user_id": uid_quota, "quota": "abc"})
srv.AdminHandler.do_POST(_a)
check("F5 非整数被拒（ok=False）", _a.data.get("ok") is False)
check("F6 原额度未被非法请求改动",
      db.get_user_by_id(uid_quota)["quota_api_proxy"] == 30)

print("=== G. 降级必须可观测（响应头 + 日志） ===")
Sub = type("SubFireflyHandler", (srv.FireflyHandler,), {})
_h = object.__new__(Sub)
_headers = []
_h.send_header = lambda k, v: _headers.append((k, v))
_h._proxy_downgraded = True
_h._cors_headers()
check("G1 降级时回 X-API-Mode-Effective: relay",
      ("X-API-Mode-Effective", "relay") in _headers)
_headers2 = []
_h2 = object.__new__(Sub)
_h2.send_header = lambda k, v: _headers2.append((k, v))
_h2._proxy_downgraded = False
_h2._cors_headers()
check("G2 正常请求不带该头（零噪音）",
      not any(k == "X-API-Mode-Effective" for k, _ in _headers2))
check("G3 CORS 头仍然照常发出",
      any(k == "Access-Control-Allow-Origin" for k, _ in _headers))

print("=== H. 前端管理台静态守卫 ===")
admin_html = (ROOT / "server" / "frontend" / "admin.html").read_text(encoding="utf-8")
check("H1 管理台有额度设置入口", "/admin/api/users/quota" in admin_html)
check("H2 有 setQuota 函数与二次确认", "function setQuota" in admin_html and "confirm(" in admin_html)
check("H3 表格列数与 colspan 一致（9 列）",
      admin_html.count('colspan="9"') >= 2 and "托管额度/日" in admin_html)

print("=== I. 真实 user_data 零污染守卫 ===")
check("I1 测试数据全在临时目录",
      str(cfg.USER_DIR).startswith(str(Path(tempfile.gettempdir()))) or "firefly_test" in str(cfg.USER_DIR))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
