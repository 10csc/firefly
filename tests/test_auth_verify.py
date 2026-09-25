# -*- coding: utf-8 -*-
"""A7a /auth/verify 测试：token 校验 + 滚动续期。

★ 断言**从 auth 模块读常量**，不写死天数（见 docs/错误总结.md #12：断言写"性质"不写"快照"）。
  会话窗口 2026-09-25 由 30 天收紧到 7 天（明文 HTTP 的降险措施），当时就是因为这里写死了
  "30 天"而报了一次假失败 —— 顺手改成读常量。
"""
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


def _expire_in(days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + days * 86400))


# ── D0：会话期限口径（与审计 §2.1 的处置一致，防被改回去）──
print("=== D0. 会话期限口径 ===")
check(f"D0-1 会话有效期 = 7 天（当前 {auth_svc.SESSION_DAYS}）", auth_svc.SESSION_DAYS == 7)
check(f"D0-2 续期阈值 = 2 天且小于有效期（当前 {auth_svc.RENEW_BEFORE_DAYS}）",
      0 < auth_svc.RENEW_BEFORE_DAYS < auth_svc.SESSION_DAYS)

# 注册一个测试账号
mail_svc._codes["v@qq.com|register"] = {"code": "000000", "expire": 9999999999}
ok, err = auth_svc.register("v@qq.com", "pass12345", "1097936258", "000000",
                            "inst-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "127.0.0.1")
check("R1 注册成功", ok)

print(f"=== A. 新会话（剩余 {auth_svc.SESSION_DAYS} 天）不续期 ===")
ok, err, token = auth_svc.login("v@qq.com", "pass12345", "test")
check("A1 登录成功", ok)
user, renewed = auth_svc.verify_and_renew(token)
check("A2 校验返回用户", user is not None and user["email"] == "v@qq.com")
check("A3 剩余充足不续期", renewed is None)

print(f"=== B. 剩余 <{auth_svc.RENEW_BEFORE_DAYS} 天 → 续期到 {auth_svc.SESSION_DAYS} 天 ===")
near = _expire_in(auth_svc.RENEW_BEFORE_DAYS - 1)      # 明确落在阈值内（不写死 3 天）
db.create_session("tok-near", user["id"], "test", near)
user2, renewed2 = auth_svc.verify_and_renew("tok-near")
check("B1 校验通过", user2 is not None)
check("B2 触发续期", renewed2 is not None)
sess = db.get_session("tok-near")
check(f"B3 新过期时间约 {auth_svc.SESSION_DAYS} 天后",
      sess["expires_at"] > _expire_in(auth_svc.SESSION_DAYS - 1))
check("B4 续期后再次校验不再续（同一 token 幂等）",
      auth_svc.verify_and_renew("tok-near")[1] is None)

print("=== C. 无效/过期/封禁 → None ===")
check("C1 无效 token", auth_svc.verify_and_renew("not-a-token") == (None, None))
db.create_session("tok-expired", user["id"], "test", _expire_in(-1 / 1440))   # 1 分钟前
check("C2 过期 token", auth_svc.verify_and_renew("tok-expired") == (None, None))
check("C3 过期会话被清理", db.get_session("tok-expired") is None)
db.update_user_status(user["id"], "banned")
try:
    check("C4 封禁用户", auth_svc.verify_and_renew(token) == (None, None))
finally:
    db.update_user_status(user["id"], "active")

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
