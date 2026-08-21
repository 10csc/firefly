# -*- coding: utf-8 -*-
"""A7a /auth/verify 测试：token 校验 + 滚动续期（剩余<7天续到30天；无效/过期/封禁返回 None）"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import db
import mail as mail_svc
import auth as auth_svc

mail_svc._send_raw = lambda to, subject, body: None   # mock SMTP

_db = Path(tempfile.mkdtemp(prefix="firefly_test_verify_")) / "t.db"
db.init_db(_db)
db.ensure_auth_columns()

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


# 注册一个测试账号
mail_svc._codes["v@qq.com|register"] = {"code": "000000", "expire": 9999999999}
ok, err = auth_svc.register("v@qq.com", "pass12345", "1097936258", "000000",
                            "inst-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "127.0.0.1")
check("R1 注册成功", ok)

print("=== A. 新会话（剩余 30 天）不续期 ===")
ok, err, token = auth_svc.login("v@qq.com", "pass12345", "test")
check("A1 登录成功", ok)
user, renewed = auth_svc.verify_and_renew(token)
check("A2 校验返回用户", user is not None and user["email"] == "v@qq.com")
check("A3 剩余充足不续期", renewed is None)

print("=== B. 剩余 <7 天 → 续期到 30 天 ===")
near = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3 * 86400))
db.create_session("tok-near", user["id"], "test", near)
user2, renewed2 = auth_svc.verify_and_renew("tok-near")
check("B1 校验通过", user2 is not None)
check("B2 触发续期", renewed2 is not None)
sess = db.get_session("tok-near")
check("B3 新过期时间约 30 天后",
      sess["expires_at"] > time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(time.time() + 29 * 86400)))
check("B4 续期后再次校验不再续（同一 token 幂等）",
      auth_svc.verify_and_renew("tok-near")[1] is None)

print("=== C. 无效/过期/封禁 → None ===")
check("C1 无效 token", auth_svc.verify_and_renew("not-a-token") == (None, None))
expired = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60))
db.create_session("tok-expired", user["id"], "test", expired)
check("C2 过期 token", auth_svc.verify_and_renew("tok-expired") == (None, None))
check("C3 过期会话被清理", db.get_session("tok-expired") is None)
db.update_user_status(user["id"], "banned")
try:
    check("C4 封禁用户", auth_svc.verify_and_renew(token) == (None, None))
finally:
    db.update_user_status(user["id"], "active")

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
