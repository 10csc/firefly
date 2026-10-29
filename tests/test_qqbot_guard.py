# -*- coding: utf-8 -*-
"""QQ 群机器人防护层测试。

按 [开发规范](../docs/参考/开发规范.md)：每条断言都要有**独立 oracle**——
真实落盘、真实 HTTP 请求、或手写固定期望值。本文件刻意**不**做以下事：
- 不用被测函数自己算期望值再断言等于自己；
- 不 mock `time.time()` 去"制造"限流，而是真实连续调用触发；
- 不捕获异常后继续 PASS。

覆盖了什么：
1. 配额账本**跨重启**不失效（重启进程不能绕过每日限额——限流最容易漏的洞）
2. 准入矩阵（白名单 / 自身消息 / 其他 bot / 停机开关）
3. 用户级每分钟限流与高频熔断（真实连续调用触发）
4. 复读与超长输入
5. CQ 码解析（手写期望值，含转义还原）
6. **真实 HTTP**：起本地服务捕获 NapCat 实际收到的请求体，验证 `auto_escape=True`
   ——这条保证用户消息里的 `[CQ:...]` 不会被当指令解析。

没覆盖什么（本轮不做，需登记）：
- 真实 NapCat 联调（服务器尚未安装 Node.js 与 NapCat）
- 真实 LLM 流水线（需专用 Key 与预算批准）
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parent.parent / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from qqbot import config as qqcfg      # noqa: E402
from qqbot import guard as guard_mod   # noqa: E402
from qqbot import onebot               # noqa: E402


# ── 真实 HTTP 捕获（独立 oracle：验证实际发出的报文，不是函数返回值）──
class _Capture:
    last_path = None
    last_body = None


def _start_capture_server():
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            _Capture.last_path = self.path
            _Capture.last_body = json.loads(raw.decode("utf-8"))
            payload = json.dumps({"status": "ok", "retcode": 0,
                                  "data": {"message_id": 12345}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ── 1. 配额账本：跨重启不失效 ────────────────────
def test_quota_persists_across_restart(tmp_path, monkeypatch):
    """重启进程不得绕过每日限额——这是限流实现最容易漏的洞。"""
    path = str(tmp_path / "usage.json")
    store = guard_mod.QuotaStore(path)
    limit = 3

    assert [store.take("g:1", limit) for _ in range(limit)] == [True, True, True]
    assert store.take("g:1", limit) is False, "第 4 次必须被拒"

    # 独立 oracle：扔掉对象，用同一文件**重新构造**（等价于进程重启）
    reborn = guard_mod.QuotaStore(path)
    assert reborn.peek("g:1") == 3, "重启后用量必须还是 3"
    assert reborn.take("g:1", limit) is False, "重启后仍须拒绝，不能免费续杯"


def test_quota_separate_scopes(tmp_path):
    """群额度与个人额度互不干扰。"""
    store = guard_mod.QuotaStore(str(tmp_path / "u.json"))
    assert store.take("g:1", 1) is True
    assert store.take("g:1", 1) is False
    assert store.take("u:100", 5) is True, "个人额度不应被群额度耗尽影响"
    assert store.peek("u:100") == 1


# ── 2. 准入矩阵 ────────────────────────────────
@pytest.mark.parametrize("group,user,self_id,is_bot,expected,reason", [
    ("1097936258", "10001", "999", False, True, ""),                    # 正常
    ("777777",     "10001", "999", False, False, "group_not_allowed"),  # 不在白名单
    ("1097936258", "999",   "999", False, False, "self_message"),       # 机器人自己
    ("1097936258", "10001", "999", True,  False, "other_bot"),          # 别的 bot
])
def test_admit_matrix(tmp_path, group, user, self_id, is_bot, expected, reason):
    g = guard_mod.Guard({"1097936258"}, 50, 15, 3, 500,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    assert g.admit(group, user, self_id, is_bot) == (expected, reason)


def test_admit_fail_closed_on_empty_allowlist(tmp_path):
    """白名单为空 = 拒绝一切，绝不因为配置缺失变成"谁都能用"。"""
    g = guard_mod.Guard(set(), 50, 15, 3, 500,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    ok, reason = g.admit("1097936258", "10001", "999", False)
    assert ok is False and reason == "allowlist_empty"


def test_kill_switch_blocks_everything(tmp_path):
    g = guard_mod.Guard({"1097936258"}, 50, 15, 3, 500,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    g.set_kill(True)
    assert g.admit("1097936258", "10001", "999", False) == (False, "kill_switch")


# ── 3. 限流与熔断（真实连续调用触发，不改时间）────────
def test_user_daily_limit(tmp_path):
    g = guard_mod.Guard({"1097936258"}, 50, 2, 30, 500,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    assert g.take("1097936258", "10001")[0] is True
    assert g.take("1097936258", "10001")[0] is True
    ok, reason = g.take("1097936258", "10001")
    assert ok is False and reason == "user_daily_exhausted"


def test_group_daily_limit(tmp_path):
    """不同用户轮流说，群总额度照样会用尽。"""
    g = guard_mod.Guard({"1097936258"}, 3, 50, 60, 500,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    assert all(g.take("1097936258", str(10000 + i))[0] for i in range(3))
    ok, reason = g.take("1097936258", "10999")
    assert ok is False and reason == "group_daily_exhausted"


def test_burst_cooldown(tmp_path):
    """短时间连发 → 熔断。不 mock 时间，真实连点触发。"""
    g = guard_mod.Guard({"1097936258"}, 50, 50, 60, 500,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    reasons = []
    for i in range(7):
        ok, _ = g.take("1097936258", "10001")
        if not ok:
            reasons.append("rate")
            continue
        _, _, reason = g.screen(f"第{i}条", "10001")
        reasons.append(reason)
    assert "burst_triggered" in reasons, f"未触发熔断: {reasons}"


# ── 4. 内容闸 ──────────────────────────────────
def test_screen_empty_and_repeat(tmp_path):
    g = guard_mod.Guard({"1097936258"}, 50, 50, 60, 100,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    assert g.screen("", "10001") == (False, "", "empty_input")
    assert g.screen("   ", "10001") == (False, "", "empty_input")
    assert g.screen("你好", "10001")[0] is True
    ok, _, reason = g.screen("你好", "10001")
    assert ok is False and reason == "repeat_message"


def test_screen_truncates_long_input(tmp_path):
    g = guard_mod.Guard({"1097936258"}, 50, 50, 60, 50,
                        guard_mod.QuotaStore(str(tmp_path / "u.json")))
    ok, out, reason = g.screen("あ" * 200, "10001")
    assert ok is True and len(out) == 50 and reason == ""


# ── 5. CQ 码解析（手写期望值）────────────────────
def test_parse_cq_string():
    text, ats = onebot.parse_message("[CQ:at,qq=10001] 你好啊")
    assert text == "你好啊", f"期望 '你好啊'，实际 {text!r}"
    assert ats == ["10001"]


def test_parse_array_format():
    msg = [{"type": "at", "data": {"qq": "10001"}},
           {"type": "text", "data": {"text": " 在吗"}}]
    text, ats = onebot.parse_message(msg)
    assert text == "在吗"
    assert ats == ["10001"]


def test_parse_unescapes_entities():
    """CQ 码内的转义实体必须还原，否则用户看到 &#91; 这种乱码。"""
    text, _ = onebot.parse_message("a&amp;b&#91;c&#93;")
    assert text == "a&b[c]"


# ── 6. 真实 HTTP：验证实际发出的报文 ───────────────
def test_send_uses_auto_escape(tmp_path, monkeypatch):
    """用户内容里的 CQ 码不得被解析：靠 auto_escape=True 兜住。"""
    httpd = _start_capture_server()
    port = httpd.server_address[1]
    monkeypatch.setenv("FIREFLY_QQBOT_ONEBOT_URL", f"http://127.0.0.1:{port}")
    try:
        onebot.send_group_text("1097936258", "[CQ:at,qq=10001] 你好")
    finally:
        httpd.shutdown()

    assert _Capture.last_path == "/send_group_msg", _Capture.last_path
    body = _Capture.last_body
    assert body["group_id"] == 1097936258
    assert body["message"] == "[CQ:at,qq=10001] 你好"
    assert body["auto_escape"] is True, "auto_escape 必须为 True，否则用户内容会被当 CQ 码执行"


def test_send_rejects_empty(tmp_path, monkeypatch):
    with pytest.raises(onebot.OneBotError) as e:
        onebot.send_group_text("1097936258", "   ")
    assert e.value.code == "empty_text"


def test_send_rejects_bad_group(tmp_path):
    with pytest.raises(onebot.OneBotError) as e:
        onebot.send_group_text("not-a-number", "你好")
    assert e.value.code == "bad_group"


def test_send_truncates_overlong(tmp_path, monkeypatch):
    httpd = _start_capture_server()
    port = httpd.server_address[1]
    monkeypatch.setenv("FIREFLY_QQBOT_ONEBOT_URL", f"http://127.0.0.1:{port}")
    try:
        onebot.send_group_text("1097936258", "x" * 5000)
    finally:
        httpd.shutdown()
    assert len(_Capture.last_body["message"]) <= 1500


def test_api_failure_is_reported(tmp_path, monkeypatch):
    """retcode != 0 必须报错，不允许"发出去了就算成功"。"""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            payload = json.dumps({"status": "failed", "retcode": 1200,
                                  "data": None}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("FIREFLY_QQBOT_ONEBOT_URL",
                       f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        with pytest.raises(onebot.OneBotError) as e:
            onebot.send_group_text("1097936258", "你好")
    finally:
        httpd.shutdown()
    assert e.value.code == "api_failed"


# ── 7. 脱敏 ────────────────────────────────────
def test_mask_id():
    # 10 位号码 → 首 1 位 + 8 个星 + 末 1 位
    assert guard_mod.mask_id("1097936258") == "1" + "*" * 8 + "8"
    assert guard_mod.mask_id("") == "***"
    assert guard_mod.mask_id("12") == "***"
    assert "1097936258" not in guard_mod.mask_id("1097936258"), "打码后不得残留完整号码"
