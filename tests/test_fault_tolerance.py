# -*- coding: utf-8 -*-
"""A3 上游容错测试：分类重试（429/Jitter/Retry-After）、401 不重试、端点冷却、
relay 心跳快速失败、relay 补重试且不连坐端点冷却、error_code 透传 /chat、
配额失败不占额度、单轮总预算 _deadline 硬顶（重试 sleep 前检查/截断）"""
import os
import sys
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_fault_"))
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


from modules.api_client import (_CompatClient, ApiError, relay_submit,
                                _relay_heartbeats, _COOLDOWNS)


class R:
    def __init__(self, status, body=None, headers=None, content=None):
        self.status_code = status
        self.body = body or {"choices": [{"message": {"content": "ok"}}]}
        self.headers = headers or {}
        self.text = content or ""

    def json(self):
        return self.body


class FakeResp:
    def __init__(self, status, content="", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = content
        self._json = content

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


print("=== A. 429 分类重试（Full Jitter + Retry-After） ===")
calls = {"n": 0}
r429 = FakeResp(429, content="rate limited", headers={"Retry-After": "1"})


def fake_post_429(url, **kw):
    calls["n"] += 1
    if calls["n"] == 1:
        return r429
    return FakeResp(200)


c = _CompatClient("k", "https://x.example/v1", caps={"thinking": False})
with patch("modules.api_client.requests.post", side_effect=fake_post_429):
    resp = c.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
check("A1 429 后重试成功", calls["n"] == 2 and resp.choices[0].message.content == "ok")

print("=== B. 401 不重试（确定性错误） ===")
calls2 = {"n": 0}


def fake_post_401(url, **kw):
    calls2["n"] += 1
    return FakeResp(401, content="unauthorized")


c2 = _CompatClient("k", "https://x.example/v1", caps={"thinking": False})
try:
    with patch("modules.api_client.requests.post", side_effect=fake_post_401):
        c2.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    check("B1 401 抛错", False)
except ApiError as e:
    check("B1 401 抛错", True)
    check("B2 401 分类 key_invalid", e.code == "key_invalid")
check("B3 401 只尝试 1 次", calls2["n"] == 1)

print("=== C. 端点冷却（连续失败后 60s 快速失败） ===")
calls3 = {"n": 0}


def fake_post_500(url, **kw):
    calls3["n"] += 1
    return FakeResp(500, content="boom")


c3 = _CompatClient("k", "https://x.example/v1", caps={"thinking": False})
try:
    with patch("modules.api_client.requests.post", side_effect=fake_post_500):
        c3.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
except ApiError as e:
    check("C1 5xx 重试耗尽后抛错", e.code == "server_error")
check("C2 重试 3 次（总 4 次尝试）", calls3["n"] == 4)
check("C3 端点进入冷却", _COOLDOWNS.get("https://x.example/v1", 0) > time.time())
before = calls3["n"]
try:
    with patch("modules.api_client.requests.post", side_effect=fake_post_500):
        c3.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
except ApiError as e:
    check("C4 冷却期快速失败 code=cooldown", e.code == "cooldown")
check("C5 冷却期不再发请求", calls3["n"] == before)

print("=== D. relay 心跳快速失败（>30s 无轮询） ===")
_relay_heartbeats["u-test"] = time.time() - 60
try:
    relay_submit("u-test", {"model": "m"}, "https://api.deepseek.com/v1", timeout=5)
    check("D1 心跳停滞 → 立即失败", False)
except ApiError as e:
    check("D1 心跳停滞 → 立即失败", True)
    check("D2 分类 relay_timeout", e.code == "relay_timeout")

print("=== E. relay 补重试（首次超时后成功） ===")
from modules.api_client import RelayClient
_relay_heartbeats["u-test2"] = time.time()   # 心跳新鲜
attempts = {"n": 0}


def fake_submit(user_key, payload, api_base, timeout=120.0):
    attempts["n"] += 1
    if attempts["n"] == 1:
        raise ApiError("APP 代发超时", code="relay_timeout")
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])


rc = RelayClient("u-test2", "https://api.deepseek.com/v1", timeout=5.0)
with patch("modules.api_client.relay_submit", side_effect=fake_submit):
    resp = rc.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
check("E1 relay 重试后成功", attempts["n"] == 2
      and resp.choices[0].message.content == "ok")

print("=== F. error_code 透传 /chat（模块降级携带分类码） ===")
from modules.analyzer import Analyzer, AnalyzerInput, AnalyzerOutput


class RaiseCompletions:
    def create(self, **kw):
        raise ApiError("rate limited", code="rate_limit")


class RaiseClient:
    _timeout = 30.0
    chat = type("Chat", (), {"completions": RaiseCompletions()})()


a = Analyzer(RaiseClient(), model="m", effort="high", mode="story")
out = a.analyze(AnalyzerInput(user_input="hi", recent_history=[]))
check("F1 分析器降级携带 error_code", out.degraded and out.error_code == "rate_limit")

# orchestrator 级：全阶段降级 → ChatResult.error_code 透传
from modules.context_manager import ContextManager
from orchestrator import handle_chat

session = {"context": ContextManager(), "memory_head": ""}
result = handle_chat("你好", session, RaiseClient())
check("F2 /chat 响应透传 error_code", result.error_code == "rate_limit")
check("F3 降级话术仍存在（不阻断）", len(result.messages) > 0)

print("=== G. 配额失败不占额度（成功记账/失败单独记录） ===")
from modules.api_client import QuotaClient
counted = {"ok": 0, "fail": 0}


def qcheck():
    return ""


def qcount():
    counted["ok"] += 1


def qfail():
    counted["fail"] += 1


# 成功路径（用全新端点，避免 C 段冷却残留）
qc = QuotaClient("op-key", "https://quota.example/v1", qcheck, timeout=5.0,
                 counter_fn=qcount, fail_fn=qfail)
with patch("modules.api_client.requests.post", return_value=FakeResp(200)):
    qc.chat.completions.create(model="z", messages=[{"role": "user", "content": "hi"}])
check("G1 成功计入 ok", counted["ok"] == 1 and counted["fail"] == 0)

# 失败路径
with patch("modules.api_client.requests.post", return_value=FakeResp(500, content="boom")):
    try:
        qc.chat.completions.create(model="z", messages=[{"role": "user", "content": "hi"}])
    except ApiError:
        pass
check("G2 失败单独记录、不占额度", counted["ok"] == 1 and counted["fail"] == 1)

# 配额不足路径：不发起调用
counted["ok"] = counted["fail"] = 0
qc2 = QuotaClient("op-key", "https://quota.example/v1", lambda: "今日额度已用完", timeout=5.0,
                  counter_fn=qcount, fail_fn=qfail)
with patch("modules.api_client.requests.post", side_effect=AssertionError("不应发起调用")):
    try:
        qc2.chat.completions.create(model="z", messages=[{"role": "user", "content": "hi"}])
    except ApiError as e:
        check("G3 配额不足抛 quota_exhausted", e.code == "quota_exhausted")
check("G4 配额不足不记账", counted["ok"] == 0 and counted["fail"] == 0)

print("=== H. relay 失败不连坐端点冷却 ===")
attempts_h = {"n": 0}


def fake_submit_timeout(user_key, payload, api_base, timeout=120.0):
    attempts_h["n"] += 1
    raise ApiError("APP 代发超时", code="relay_timeout")


rc_h = RelayClient("u-test3", "https://relay-nc.example/v1", timeout=5.0)
try:
    with patch("modules.api_client.relay_submit", side_effect=fake_submit_timeout):
        rc_h.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    check("H1 relay 重试耗尽抛错", False)
except ApiError as e:
    check("H1 relay 重试耗尽抛 relay_timeout", e.code == "relay_timeout")
check("H2 relay 失败不写冷却表（多用户不连坐）", "https://relay-nc.example/v1" not in _COOLDOWNS)
check("H3 relay 重试 2 次（总 3 次尝试）", attempts_h["n"] == 3)

print("=== I. 单轮总预算：deadline 已过 → 首次失败即抛 timeout ===")
import requests as _requests
calls_i = {"n": 0}


def fake_post_500_i(url, **kw):
    calls_i["n"] += 1
    return FakeResp(500, content="boom")


ci = _CompatClient("k", "https://deadline.example/v1", caps={"thinking": False})
ci._deadline = time.time() - 1   # 过去值：预算已耗尽
try:
    with patch("modules.api_client.requests.post", side_effect=fake_post_500_i):
        ci.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    check("I1 预算耗尽抛错", False)
except ApiError as e:
    check("I1 预算耗尽抛 code=timeout", e.code == "timeout")
check("I2 不再重试（只尝试 1 次）", calls_i["n"] == 1)

print("=== J. 单轮总预算：重试 delay 截断到 deadline（耗时不拖顶） ===")


def fake_post_down(url, **kw):
    raise _requests.ConnectionError("boom")


cj = _CompatClient("k", "https://deadline2.example/v1", caps={"thinking": False})
t0 = time.time()
cj._deadline = t0 + 2.5   # 剩余可用 = deadline-2 ≈ 0.5s，jitter delay 应被截断/放弃
try:
    with patch("modules.api_client.requests.post", side_effect=fake_post_down):
        cj.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    check("J1 预算耗尽抛错", False)
except ApiError as e:
    check("J1 预算耗尽抛 code=timeout", e.code == "timeout")
elapsed = time.time() - t0
check("J2 总耗时不超 deadline+余量（%.2fs）" % elapsed, elapsed <= 2.5 + 1.5)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
