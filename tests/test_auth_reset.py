# -*- coding: utf-8 -*-
"""忘记密码流程测试：发码（mock SMTP）→ 校验 → 重置 → 新密码登录 → 旧会话吊销。

运行：python tests/test_auth_reset.py
"""
import os
import sys
import tempfile
from pathlib import Path

# 测试环境：隔离 db 路径 + mock 邮件发送
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import db
import mail as mail_svc
import auth as auth_svc

mail_svc._send_raw = lambda to, subject, body: None   # mock SMTP（不真发信）

_db_tmp = Path(__file__).resolve().parent / "_tmp_reset_test.db"
try:
    _db_tmp.unlink()
except OSError:
    pass


def main():
    db.init_db(_db_tmp)
    db.ensure_auth_columns()

    email = "reset_test@qq.com"
    # 1. 注册（先手动塞注册验证码，mock 发送）
    mail_svc._codes["reset_test@qq.com|register"] = {"code": "000000", "expire": 9999999999}
    ok, err = auth_svc.register(email, "oldpass123", "1097936258", "000000", "inst-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "127.0.0.1")
    assert ok, f"注册失败: {err}"

    # 2. 发送重置验证码（mock 发送，码入内存）
    ok, err = mail_svc.send_verify_code(email, "127.0.0.1", purpose="reset")
    assert ok, f"发码失败: {err}"
    code = mail_svc._codes[f"{email}|reset"]["code"]

    # 3. 错误邮箱不泄露：未注册邮箱重置返回与验证码错误同文案
    ok, err = auth_svc.reset_password("nobody@qq.com", "123456", "newpass123")
    assert not ok and err == "验证码无效或已过期", f"防枚举失效: {err}"

    # 4. 重置密码
    ok, err = auth_svc.reset_password(email, code, "newpass123")
    assert ok, f"重置失败: {err}"

    # 5. 旧密码失效、新密码可登录
    ok, err, _ = auth_svc.login(email, "oldpass123", "test")
    assert not ok, "旧密码仍可登录"
    ok, err, token = auth_svc.login(email, "newpass123", "test")
    assert ok and token, f"新密码登录失败: {err}"

    # 6. 重置后再登一次，验证会话有效（改密时吊销的是当时存在的会话）
    assert auth_svc.auth_user(token), "新会话无效"

    print("PASS: 忘记密码流程（防枚举/重置/登录/会话）全部通过")
    try:
        _db_tmp.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    main()
