# -*- coding: utf-8 -*-
"""旧服务请求闸门测试（0.8.1）：/sync/upload|download（已下线的手动云备份端点）

旧客户端（v0.7.x/v0.8.0）调用面 54 端点中仅这 2 个被新服务器移除，
命中即旧服务 → ①可读文案（非裸 404）②该账号一次性升级提醒邮件（去重）。
校验点：
- O1/O2 响应格式：POST→200+ok:false；GET→410+ok:false（旧前端两个分支都能读到文案）
- O3 首次触发发邮件，同一账号再次调用不再发（DB 去重）
- O4 不同账号独立去重
- O5 不同 kind 独立去重（每类型一期一次）
- O6 未登录（无效 token）→ 401 且不发邮件
- O7 新客户端端点（/sync/manifest）不受旧闸门影响（正常走 routes，无邮件）
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

os.environ["FIREFLY_SERVER"] = "1"          # 必须在 import server_app 之前
os.environ["FIREFLY_ADMIN_TOKEN"] = "test-token"

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_old_client_"))
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


# ── 1. 邮件捕获（mock SMTP）────────────
_sent = []
import mail as mail_svc
mail_svc._send_raw = lambda to, subject, body: _sent.append(
    {"to": to, "subject": subject, "body": body})
mail_svc.SMTP_USER = "test@qq.com"
mail_svc.SMTP_AUTH = "testauth"

# ── 2. 起真实 HTTP 服务器（复用 FireflyHandler 全链路）────────
import db
import auth as auth_svc
import server_app

db.init_db(_tmp / "firefly.db")
db.ensure_auth_columns()

srv = server_app.ThreadingHTTPServer(("127.0.0.1", 0), server_app.FireflyHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# 注册两个测试账号（直接注入验证码，与 test_auth_verify 同法）
def _reg(email, inst):
    mail_svc._codes[f"{email}|register"] = {"code": "000000", "expire": 9999999999}
    ok, err = auth_svc.register(email, "pass12345", "1097936258", "000000", inst, "127.0.0.1")
    assert ok, err
    ok, err, token = auth_svc.login(email, "pass12345", "test")
    assert ok, err
    return token

tok_a = _reg("olda@qq.com", "inst-" + "a" * 32)
tok_b = _reg("oldb@qq.com", "inst-" + "b" * 32)


def call(method, path, token=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    try:
        return status, json.loads(payload)
    except Exception:
        return status, payload


print("=== A. DB 去重（mark_notice） ===")
uid_a = auth_svc.auth_user(tok_a)["id"]
# 注意：这里用 k1/k2 试错 kind，避免占用 B 组要验证的 "old-client" 首触发
check("O1 首次标记返回 True（应发送）", db.mark_notice(uid_a, "k1") is True)
check("O2 同账号同 kind 再次标记 False（去重）", db.mark_notice(uid_a, "k1") is False)
check("O3 同账号不同 kind 重新允许（每类型一次）", db.mark_notice(uid_a, "k2") is True)
check("O4 不同账号独立", db.mark_notice(uid_a + 999, "k1") is True)

print("=== B. HTTP 全链路（真实 handler） ===")
import urllib.error  # noqa

_sent.clear()
st, data = call("POST", "/sync/upload", token=tok_a)
check("O5 POST /sync/upload → 200（旧前端 up.ok 分支）", st == 200)
check("O6 返回 ok:false + 可读文案", isinstance(data, dict) and data.get("ok") is False
      and "请更新客户端" in str(data.get("error", "")))
time.sleep(0.6)   # 邮件走 daemon 线程，等待送达
check("O7 首次命中触发升级邮件（1 封）", len(_sent) == 1 and _sent[0]["to"] == "olda@qq.com"
      and "客户端更新提醒" in _sent[0]["subject"])

_sent.clear()
st2, data2 = call("POST", "/sync/upload", token=tok_a)
check("O8 同账号重复调用 → 不再发邮件（去重）", st2 == 200 and len(_sent) == 0)

_sent.clear()
st3, data3 = call("GET", "/sync/download?mode=chat", token=tok_a)
check("O9 GET /sync/download → 410（旧前端 !resp.ok 分支）", st3 == 410)
check("O10 download 文案可读", isinstance(data3, dict) and "请更新客户端" in str(data3.get("error", "")))
check("O11 同 kind 已发过（upload）→ download 不再重复发", len(_sent) == 0)
call("GET", "/sync/download?mode=chat", token=tok_a)
check("O12 download 重复调用去重", len(_sent) == 0)

_sent.clear()
call("POST", "/sync/upload", token=tok_b)
time.sleep(0.6)
check("O13 另一个账号独立发送（不串账号）", len(_sent) == 1 and _sent[0]["to"] == "oldb@qq.com")

_sent.clear()
st4, data4 = call("POST", "/sync/upload", token="invalid-token")
check("O14 未登录（无效 token）→ 401", st4 == 401)
check("O15 未登录不发邮件", len(_sent) == 0)
st5, _ = call("POST", "/sync/upload")
check("O16 无 token → 401", st5 == 401)

_sent.clear()
st6, data6 = call("GET", "/sync/manifest?mode=chat", token=tok_a)
check("O17 新客户端端点不受闸门影响（GET 正常响应）", st6 == 200 and isinstance(data6, dict)
      and _sent == [])
check("O18 新端点响应非旧服务文案", not ("请更新客户端" in str(data6)))

srv.shutdown()
print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
