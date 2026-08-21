# -*- coding: utf-8 -*-
"""A7 本地版认证代理测试：登录落 auth.json / state 隐式 verify 续期 / 401 清凭证 / 离线宽限"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_auth_store_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


from modules import auth_store


class FakeH:
    headers = {}
    def _json(self, data, status=200):
        self.data = data
        self.status = status


class AuthHandler(BaseHTTPRequestHandler):
    """伪认证服务器：/auth/login 返回 token+role；/auth/verify 校验 token 返回续期信息。"""
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        token = (self.headers.get("Authorization", "") or "").strip()
        if self.path == "/auth/login":
            resp = {"ok": True, "token": "srv-token-abc", "role": "admin" if body.get("email") == "a@qq.com" else "user"}
            self._send(resp)
        elif self.path == "/auth/verify":
            if token == "Bearer srv-token-abc":
                self._send({"ok": True, "email": "a@qq.com",
                            "renewed": True, "expires_at": "2099-01-01 00:00:00"})
            else:
                self._send({"error": "登录已过期，请重新登录"}, 401)
        elif self.path == "/auth/logout":
            self._send({"ok": True})
        else:
            self._send({"error": "unknown"}, 404)

    def _send(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
cfg.config["auth_server_url"] = f"http://127.0.0.1:{port}"

import routes

print("=== A. 登录代理 + auth.json 落盘（token 由本地后端持有） ===")
h = FakeH()
with patch("routes._read_json", return_value={"email": "a@qq.com", "password": "pass12345"}):
    routes.auth_proxy(h, "login")
check("A1 登录透传 ok", h.data.get("ok") is True)
check("A2 响应不含 token（仅本地后端持有）", "token" not in h.data)
check("A3 响应含 email 与服务端地址", h.data.get("email") == "a@qq.com" and "server" in h.data)
st = auth_store.get_state()
check("A4 auth.json 已落盘", st.get("logged_in") is True)
check("A5 凭证 email 正确", st.get("email") == "a@qq.com")
check("A6 10 天内离线可判定（宽限期）", st.get("offline_ok") is True)

print("=== B. /auth/state 隐式 verify（服务器续期 → 回写 expires_at） ===")
h2 = FakeH()
routes.auth_state(h2)
check("B1 state logged_in", h2.data.get("logged_in") is True)
check("B2 服务器续期回写 expires_at", h2.data.get("expires_at") == "2099-01-01 00:00:00")
check("B3 verified 标记", h2.data.get("verified") is True)

print("=== C. 服务器判定失效（401）→ 清凭证 ===")
with patch.object(auth_store, "_write_auth", wraps=auth_store._write_auth):
    auth_store.save_login("bad-token-x", "a@qq.com" if False else "b@qq.com", "2020-01-01 00:00:00")
h3 = FakeH()
routes.auth_state(h3)
check("C1 401 → 凭证清除", h3.data.get("logged_in") is False)
check("C2 auth.json 已删", not auth_store._auth_file().exists())

print("=== D. 网络不可达 → 保持离线宽限判定 ===")
auth_store.save_login("t-keep", "c@qq.com", time.strftime("%Y-%m-%d %H:%M:%S",
                                                          time.localtime(time.time() + 5 * 86400)))
cfg.config["auth_server_url"] = "http://127.0.0.1:9"   # 不存在的端口
h4 = FakeH()
routes.auth_state(h4)
check("D1 网络失败不误清凭证", h4.data.get("logged_in") is True)
check("D2 仍按本地宽限期判定 offline_ok", h4.data.get("offline_ok") is True)
auth_store.clear_login()
srv.shutdown()
print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
