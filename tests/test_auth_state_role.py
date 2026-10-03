# -*- coding: utf-8 -*-
"""`GET /auth/state` 回带 `role` —— 2026-10-01 收口（clientpack 实测：本地模式拿不到 role，
官方卡勾选框只能整块隐藏）

钉死的事：
- 已登录 ⇒ 响应里有 `role`（来源优先级：本次 verify 响应 > 本地 auth.json 登录时存的那份）；
- **离线宽限期**（verify 不可达）⇒ 仍能回出登录时存下的 role（否则一断网勾选框又没了）；
- 登录时服务器给的 role 会被**持久化**（`save_login(..., role=...)`）；
- **未登录口径不变**：`logged_in=false` + 空字符串字段（只多一个 `role`，前端不必分支）；
- 服务器模式下 `/auth/state` 仍是 403（"请用 /auth/me"）—— 不改判定。
"""
import json
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_auth_role_"))
import modules.app_config as cfg      # noqa: E402

cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

from modules import auth_store as store   # noqa: E402  （AUTH_FILE 在 import 时按 cfg.USER_DIR 定）
import routes_auth as ra                  # noqa: E402

PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}", flush=True)


class FakeH:
    def __init__(self, path="/auth/state"):
        self.path = path
        self.headers = {}
        self.status = None
        self.body = b""

    def _json(self, data, status=200):
        self.status = status
        self.body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def data(self):
        return json.loads(self.body.decode("utf-8"))


ra._is_server = lambda: False             # 走本地版实现

print("== 1. 登录把 role 一起存下来（否则离线时无从得知）==")
check("save_login 带 role 落盘", store.save_login("tok-1", "admin@x.com", "2099-01-01 00:00:00", role="admin"))
st = store.get_state()
check(f"get_state 回带 role（{st.get('role')!r}）", st.get("logged_in") is True and st.get("role") == "admin")

print("== 2. /auth/state：verify 可达 ⇒ 用服务器现算的 role ==")
ra._auth_forward = lambda e, body, token: (200, {"ok": True, "role": "user",
                                                "expires_at": "2099-01-02 00:00:00"})
h = FakeH()
ra.auth_state(h)
d = h.data()
check(f"200 且 role 来自 verify（{d.get('role')!r}）",
      h.status == 200 and d.get("role") == "user" and d.get("verified") is True)
check(f"expires_at 用服务器回的新值（{d.get('expires_at')!r}）", d.get("expires_at") == "2099-01-02 00:00:00")

print("== 3. verify 不可达（离线宽限期）⇒ 回登录时存下的 role ==")
ra._auth_forward = lambda e, body, token: (None, "网络不可达")
h2 = FakeH()
ra.auth_state(h2)
d2 = h2.data()
check(f"离线也有 role（{d2.get('role')!r}）", h2.status == 200 and d2.get("role") == "admin")
check("离线仍标记 verified=False 且保留登录态", d2.get("verified") is False and d2.get("logged_in") is True)

print("== 4. 未登录口径不变（只多一个空 role）==")
store.clear_login()
h3 = FakeH()
ra.auth_state(h3)
d3 = h3.data()
check(f"logged_in=False 且 role=''（{d3}）",
      d3.get("logged_in") is False and d3.get("role") == "" and d3.get("email") == "")

print("== 5. 服务器模式下 /auth/state 仍是 403（判定没被我改）==")
ra._is_server = lambda: True
h4 = FakeH()
ra.auth_state(h4)
check(f"服务器模式 ⇒ 403（实际 {h4.status}）", h4.status == 403)

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
