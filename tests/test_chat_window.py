# -*- coding: utf-8 -*-
"""聊天合并窗口时序测试（0.8.1 问题回归）：
1) 窗口内多条消息合并为一批（真实路由窗口，mock handle_chat）
2) hint 续期：打字中窗口不提前提交
3) flush 立即提交
4) _CHAT_WINDOW_MAX_MSGS=10 上限：达到即提交（连续消息最多合并 10 条）
"""
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg
import modules.conversation_store as cs  # noqa: F401

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_window_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0
def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  V {desc}")
    else:
        FAIL += 1; print(f"  X {desc}")

import routes

captured = {}
def _capture(user_input, session, client_, **kw):
    captured["user_input"] = user_input
    captured["n"] = len((user_input or "").split("\n"))
    return type("R", (), {"messages": [{"type": "text", "content": "ok"}],
                          "error_code": None, "success": True})()

def _session(sid, mode="story"):
    return {"lock": threading.Lock(), "memory_head": "", "mode": mode}

def _chat(body):
    import json
    h = type("H", (), {})()
    h.headers = {"Content-Type": "application/json"}
    h._json = lambda d, status=200: setattr(h, "_resp", d)
    h._resp = None
    with patch("routes.get_session", side_effect=_session), \
         patch("routes.handle_chat", side_effect=_capture), \
         patch("routes._read_json", return_value=dict(body)), \
         patch("routes._write_replies", side_effect=lambda r, m: r.messages if hasattr(r, "messages") else r):
        routes.chat(h)
    return h

def _hint(body):
    h = type("H", (), {})()
    h.headers = {"Content-Type": "application/json"}
    h._json = lambda d, status=200: setattr(h, "_resp", d)
    with patch("routes.get_session", side_effect=_session), \
         patch("routes._read_json", return_value=dict(body)):
        routes.chat_hint(h)

def _flush(body):
    h = type("H", (), {})()
    h.headers = {"Content-Type": "application/json"}
    h._json = lambda d, status=200: setattr(h, "_resp", d)
    with patch("routes._read_json", return_value=dict(body)):
        routes.chat_flush(h)

def _msg(txt):
    return {"messages": [{"type": "text", "content": txt}], "session_id": "s1", "mode": "story"}

print("=== A. 两条消息 5 秒内 → 合并一批 ===")
captured.clear()
t1 = threading.Thread(target=lambda: _chat(_msg("第一条"))); t1.start()
time.sleep(0.3)
_chat(_msg("第二条"))   # 副请求 queued
time.sleep(5.5)
t1.join(timeout=3)
check("A1 窗口到期提交", not t1.is_alive())
check("A2 两条合并为一批", captured.get("user_input") == "第一条\n第二条")

print("=== B. hint 续期：打字中不提前提交 ===")
captured.clear()
t1 = threading.Thread(target=lambda: _chat(_msg("第一条"))); t1.start()
time.sleep(0.3)
# 每 1.5 秒发一次 hint（共 6 秒 > 5 秒窗口）→ 主请求仍挂起
for i in range(4):
    time.sleep(1.5)
    _hint({"session_id": "s1", "mode": "story"})
check("B1 hint 续期 6 秒后仍在等待", t1.is_alive())
# 停止 hint：窗口自然到期
time.sleep(5.2)
t1.join(timeout=3)
check("B2 停止续期后 5 秒提交", not t1.is_alive())

print("=== C. flush 立即提交 ===")
captured.clear()
t1 = threading.Thread(target=lambda: _chat(_msg("第一条"))); t1.start()
time.sleep(0.3)
_flush({"session_id": "s1", "mode": "story"})
t1.join(timeout=3)
check("C1 flush 立即结束窗口", not t1.is_alive())

print("=== D. 10 条上限：达到即提交（不等 5 秒） ===")
captured.clear()
t0 = time.time()
t1 = threading.Thread(target=lambda: _chat(_msg("第1条"))); t1.start()
time.sleep(0.15)
for i in range(2, 11):
    _chat(_msg(f"第{i}条"))
t1.join(timeout=3)
elapsed = time.time() - t0
check("D1 10 条达上限立即提交（<4 秒，不等 5 秒窗口）", not t1.is_alive() and elapsed < 4.0)
check("D2 10 条全部合并", captured.get("n") == 10)

print("=== E. 11 条：第 11 条开新批 ===")
# 上面 D 已提交一批（win.active=False）；第 11 条应成为新主请求并等窗口
captured.clear()
t1 = threading.Thread(target=lambda: _chat(_msg("第11条"))); t1.start()
time.sleep(0.3)
h2 = _chat(_msg("第12条"))
check("E1 第12条副请求 queued", h2._resp == {"queued": True})
time.sleep(5.2)
t1.join(timeout=3)
check("E2 新批提交（2 条）", not t1.is_alive() and captured.get("n") == 2)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
