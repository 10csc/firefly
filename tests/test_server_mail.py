# -*- coding: utf-8 -*-
"""邮件链路可诊断性回归 —— 2026-10-01 task-12（用户报"收不到验证码"，服务器零痕迹）

钉死的事：
- 发信成功/失败**都留服务端日志**（收件人打码、异常类型、SMTP 返回码）；
- **授权码永不进日志**：即使异常文本把授权码回显出来，也会被 `scrub_text` 打成 `***`；
- `_auth_reset_send` 的 **HTTP 响应恒 `{ok:true}`**（防枚举语义不许被日志改动破坏），
  未注册邮箱**不发信**但留日志、已注册发送失败也留日志；
- 运维自检入口（`mail.py` 的 CLI）对**未配置凭据 / 非 QQ 收件人 / 缺参数**一律拒绝（非 0 退出、不发信）；
- 全仓**没有**新增任何 HTTP 自检端点（公网不可能被当发信机）——静态断言。
- 自检/发信全部用桩 SMTP，**不连外网、不碰真实数据**。

运行：python tests/test_server_mail.py（模块级 runner，退出码 0 = 全绿）
"""
import contextlib
import io
import logging
import os
import smtplib
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(SERVER))
os.environ["FIREFLY_SERVER"] = "1"
SECRET = "SECRET-AUTH-CODE-0001"
os.environ["FIREFLY_SMTP_USER"] = "sender@qq.com"
os.environ["FIREFLY_SMTP_AUTH"] = SECRET

import db as srv_db                      # noqa: E402
import mail as mail_svc                  # noqa: E402
import auth_endpoints                    # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


# ── 日志捕获（挂到被检查的两个 logger 上，不污染 stdout）──
class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))

    def text(self):
        return "\n".join(self.lines)

    def clear(self):
        self.lines.clear()


CAPS = {}
for _name in ("mail", "auth_endpoints"):
    _lg = logging.getLogger(_name)
    _h = Capture()
    _h.setFormatter(logging.Formatter("%(message)s"))
    _lg.addHandler(_h)
    _lg.setLevel(logging.DEBUG)
    _lg.propagate = False           # 不往 root 传：测试输出保持干净
    CAPS[_name] = _h
LOG = lambda: CAPS["mail"].text() + "\n" + CAPS["auth_endpoints"].text()   # noqa: E731


def clear_logs():
    for h in CAPS.values():
        h.clear()


# ── 桩 SMTP（不发真信）──
class FakeSMTP:
    mode = "ok"                     # ok | authfail | leakfail | sendfail
    attempts = []
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        FakeSMTP.attempts.append((host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pwd):
        if FakeSMTP.mode == "authfail":
            raise smtplib.SMTPAuthenticationError(
                535, b"Login Fail. Please enter your authorization code")
        if FakeSMTP.mode == "leakfail":       # 故意把授权码回显进异常文本
            raise smtplib.SMTPAuthenticationError(535, ("bad auth " + pwd).encode())
        return 235, b"ok"

    def sendmail(self, frm, to, msg):
        if FakeSMTP.mode == "sendfail":
            raise smtplib.SMTPSenderRefused(550, b"sender refused", frm)
        FakeSMTP.sent.append((frm, to, msg))


_real_smtp_ssl = smtplib.SMTP_SSL
smtplib.SMTP_SSL = FakeSMTP
_TMP = Path(tempfile.mkdtemp(prefix="ff_mail_test_"))
srv_db.init_db(_TMP / "firefly.db")
srv_db.ensure_auth_columns()
REGISTERED = "registered@qq.com"
srv_db.create_user(REGISTERED, "h", "s", install_id="inst-" + "a" * 32, role="user")

print("== 1. 打码与脱敏工具 ==")
check("邮箱只留首字符 + 域名", mail_svc.mask_email("abcdef@qq.com") == "a***@qq.com")
check("本地部分不落日志", "abcdef" not in mail_svc.mask_email("abcdef@qq.com"))
check("空值/非邮箱串处理安全",
      mail_svc.mask_email("") == "(空)" and mail_svc.mask_email("随意输入的内容") == "***")
check("授权码脱敏生效", mail_svc.scrub_text(f"err {SECRET} tail") == "err *** tail")
check("QQ 返回码取得到", mail_svc.qq_return_code(
    smtplib.SMTPAuthenticationError(535, b"x")) == "535")

print("== 2. 成功发信留日志（且收件人打码）==")
clear_logs()
FakeSMTP.mode = "ok"
mail_svc._send_raw("user1@qq.com", "流萤 · 测试", "正文")
check("日志记「SMTP 已接收」（不谎称已进收件箱）", "SMTP 已接收" in LOG() and "收件方决定" in LOG())
check("日志里收件人已打码", "u***@qq.com" in LOG() and "user1@qq.com" not in LOG())
check("日志不含授权码", SECRET not in LOG())
check("确实发出了（桩收到 1 封）", len(FakeSMTP.sent) == 1)

print("== 3. 失败发信留日志（异常类型 + SMTP 返回码）==")
clear_logs()
FakeSMTP.mode = "authfail"
try:
    mail_svc._send_raw("user2@qq.com", "流萤 · 测试", "正文")
    check("认证失败会抛异常（调用方能感知）", False)
except smtplib.SMTPAuthenticationError:
    check("认证失败会抛异常（调用方能感知）", True)
check("日志含「发送失败」与异常类型", "发送失败" in LOG() and "SMTPAuthenticationError" in LOG())
check("日志含 QQ 返回码 smtp_code=535", "smtp_code=535" in LOG())
check("日志不含授权码", SECRET not in LOG())

print("== 4. 授权码回显兜底（异常文本里带授权码也不能漏）==")
clear_logs()
FakeSMTP.mode = "leakfail"
try:
    mail_svc._send_raw("user3@qq.com", "流萤 · 测试", "正文")
except Exception:
    pass
check("★ 日志中搜不到授权码", SECRET not in LOG())
check("日志里出现脱敏痕迹 ***", "***" in LOG())

print("== 5. 自己发给自己 → 明确警告（用户这次踩的坑）==")
clear_logs()
FakeSMTP.mode = "ok"
mail_svc._send_raw("sender@qq.com", "流萤 · 测试", "正文")
check("日志提示「自己发给自己」", "自己发给自己" in LOG())

print("== 6. 未配置凭据：报错 + 留日志，不发信 ==")
clear_logs()
_saved_auth = mail_svc.SMTP_AUTH
mail_svc.SMTP_AUTH = ""
try:
    mail_svc._send_raw("user4@qq.com", "x", "y")
    check("未配置时抛 RuntimeError", False)
except RuntimeError:
    check("未配置时抛 RuntimeError", True)
check("日志说明未配置且放弃发送", "未配置 SMTP 凭据" in LOG())
ok, err = mail_svc.test_send("user4@qq.com")
check("test_send 也拿得到失败原因", ok is False and "SMTP 未配置" in err)
mail_svc.SMTP_AUTH = _saved_auth

print("== 7. send_verify_code 各「没发出去」分支都留日志 ==")
clear_logs()
FakeSMTP.mode = "ok"
ok, err = mail_svc.send_verify_code("not-an-email", "203.0.113.1")
check("格式错：返回失败且日志留痕", ok is False and "未发送：邮箱格式不正确" in LOG())
email_a = "rate_gap@qq.com"
mail_svc.send_verify_code(email_a, "203.0.113.2")
clear_logs()
ok, err = mail_svc.send_verify_code(email_a, "203.0.113.2")
check("重发间隔内不发信且日志留痕",
      ok is False and "频繁" in err and "重发间隔" in LOG())
clear_logs()
for i in range(10):
    mail_svc.send_verify_code(f"ip_limit_{i}@qq.com", "203.0.113.3")
clear_logs()
ok, err = mail_svc.send_verify_code("ip_limit_10@qq.com", "203.0.113.3")
check("IP 超限不发信且日志留痕",
      ok is False and "次数过多" in err and "已达上限" in LOG())
clear_logs()
ok, err = mail_svc.send_verify_code("ok_path@qq.com", "203.0.113.4", purpose="reset")
check("成功路径日志记「验证码已发送」（不记码本身）",
      ok is True and "验证码已发送" in LOG() and mail_svc._codes["ok_path@qq.com|reset"]["code"] not in LOG())

print("== 8. 端点：防枚举响应不许被日志改动 ==")
auth_endpoints._shrink_active = lambda: False       # 收缩开关固定关闭（沙箱里没有 /opt 文件）


class FakeH:
    def __init__(self, body, ip="198.51.100.7"):
        self._body = body
        self._ip = ip
        self.status = None
        self.out = None

    def _read_body(self):
        return self._body

    def _real_ip(self):
        return self._ip

    def _json(self, obj, status=200):
        self.status = status
        self.out = obj


_h = FakeH({"email": "nobody-here@qq.com"})
auth_endpoints.AuthEndpointsMixin._auth_reset_send(_h)
check("未注册邮箱：响应恰好 {ok:true,error:''}", _h.status == 200 and _h.out == {"ok": True, "error": ""})

_sent_calls = []
_real_svc = mail_svc.send_verify_code
mail_svc.send_verify_code = lambda *a, **k: (_sent_calls.append(a), (True, ""))[1]
_h2 = FakeH({"email": "nobody-here@qq.com"})
clear_logs()
auth_endpoints.AuthEndpointsMixin._auth_reset_send(_h2)
check("★ 未注册邮箱**不发信**（现有语义不变）", _sent_calls == [])
check("但日志明确写了未注册分支", "未注册邮箱" in LOG() and "未发信" in LOG())

clear_logs()
mail_svc.send_verify_code = lambda *a, **k: (False, "邮件发送失败（SMTPAuthenticationError）")
_h3 = FakeH({"email": REGISTERED})
auth_endpoints.AuthEndpointsMixin._auth_reset_send(_h3)
check("★ 已注册但发信失败：响应仍恰好 {ok:true,error:''}（防枚举）",
      _h3.status == 200 and _h3.out == {"ok": True, "error": ""})
check("失败原因落在服务端日志", "send_ok=False" in LOG() and "SMTPAuthenticationError" in LOG())

clear_logs()
def _boom(*a, **k):
    raise RuntimeError("socket exploded")
mail_svc.send_verify_code = _boom
_h4 = FakeH({"email": REGISTERED})
auth_endpoints.AuthEndpointsMixin._auth_reset_send(_h4)
check("发信抛异常：响应仍是 {ok:true} 且日志记异常",
      _h4.out == {"ok": True, "error": ""} and "发信异常" in LOG() and "socket exploded" in LOG())

clear_logs()
auth_endpoints._shrink_active = lambda: True
_h5 = FakeH({"email": REGISTERED})
auth_endpoints.AuthEndpointsMixin._auth_reset_send(_h5)
check("收缩模式：503 且日志留痕", _h5.status == 503 and "收缩模式" in LOG())
auth_endpoints._shrink_active = lambda: False

clear_logs()
mail_svc.send_verify_code = lambda *a, **k: (False, "邮件发送失败（SMTPAuthenticationError）")
_h6 = FakeH({"email": REGISTERED})
auth_endpoints.AuthEndpointsMixin._auth_mail_send(_h6)
check("注册发码通道失败：429 + 日志留痕",
      _h6.status == 429 and _h6.out["ok"] is False and "register-send" in LOG())
mail_svc.send_verify_code = _real_svc

print("== 9. 运维自检入口（CLI）：拒绝未配置/非 QQ/缺参数，且不发信 ==")
FakeSMTP.mode = "ok"
FakeSMTP.attempts.clear()


def run_selftest(argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = mail_svc._selftest_main(argv)
    return code, buf.getvalue() + err.getvalue()


_save_user, _save_auth = mail_svc.SMTP_USER, mail_svc.SMTP_AUTH
_save_env = (os.environ.pop("FIREFLY_SMTP_USER", None), os.environ.pop("FIREFLY_SMTP_AUTH", None))
mail_svc.SMTP_USER, mail_svc.SMTP_AUTH = "", ""
_n0 = len(FakeSMTP.attempts)
code, out = run_selftest(["someone@qq.com"])
check("未配置凭据 → 退出码 2 且提示补 Environment=", code == 2 and "缺少 SMTP 凭据" in out)
check("未配置凭据 → 不发信", len(FakeSMTP.attempts) == _n0)
mail_svc.SMTP_USER, mail_svc.SMTP_AUTH = _save_user, _save_auth
if _save_env[0] is not None:
    os.environ["FIREFLY_SMTP_USER"] = _save_env[0]
if _save_env[1] is not None:
    os.environ["FIREFLY_SMTP_AUTH"] = _save_env[1]

code, out = run_selftest([])
check("缺收件人 → 退出码 2（打用法）", code == 2 and "用法示例" in out)

_n1 = len(FakeSMTP.attempts)
code, out = run_selftest(["someone@gmail.com"])
check("非 QQ 收件人 → 退出码 2 且不发信",
      code == 2 and "必须是 QQ 邮箱" in out and len(FakeSMTP.attempts) == _n1)

code, out = run_selftest(["someone@qq.com"])
check("★ 正常自检 → 退出码 0、输出打码、明说「已接收≠进收件箱」",
      code == 0 and "s***@qq.com" in out and "SMTP 已接收" in out and "收件箱" in out)
check("自检输出不含授权码", SECRET not in out)

code, out = run_selftest(["sender@qq.com"])
check("自检自己发给自己 → 明确警告", code == 0 and "自己发给自己" in out)

print("== 10. 静态断言：没有新增任何 HTTP 自检端点（防被当发信机）==")
app_src = (SERVER / "app.py").read_text(encoding="utf-8")
mail_src = (SERVER / "mail.py").read_text(encoding="utf-8")
check("app.py 里没有 mail-selftest 路由",
      "mail-selftest" not in app_src and "mail_selftest" not in app_src)
check("mail.py 不引入 http 服务（自检只在 CLI 里）",
      "http.server" not in mail_src and "do_GET" not in mail_src)

smtplib.SMTP_SSL = _real_smtp_ssl
import shutil                                # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
