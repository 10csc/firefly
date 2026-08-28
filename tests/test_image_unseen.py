# -*- coding: utf-8 -*-
"""图片不可见兜底测试（0.8.1）：
图片消息的 llm_parts 注入校验 + _handle_direct("image:unseen") 兜底话术。
思路：mock handle_chat 捕获最终 user_input（不解码窗口细节——窗口语义见 test_chat_window.py）。"""
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_image_unseen_"))
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
def _cap(user_input, session, client_, **kw):
    captured["text"] = user_input
    return type("R", (), {"messages": [{"type": "text", "content": "ok"}],
                          "error_code": None, "success": True})()

def _session(sid, mode="story"):
    return {"lock": threading.Lock(), "memory_head": "", "mode": mode}

def _run(body):
    h = type("H", (), {})()
    h.headers = {"Content-Type": "application/json"}
    h._json = lambda d, status=200: setattr(h, "_resp", d)
    t1 = threading.Thread(target=lambda: routes.chat(h)); t1.start()
    # 等待合并窗口注册（2026-08-25 修 flaky：原硬编码 sleep(0.2) 在冷启动/高负载下
    # 主请求尚未走到窗口注册段，flush 落空 → 主请求等满 5s 窗口、join(4) 超时假失败；
    # 改为轮询 active 窗口出现再 flush，最多 3s）
    _deadline = time.time() + 3.0
    while time.time() < _deadline:
        with routes._CHAT_WINDOW_LOCK:
            _registered = any(w.get("active") for w in routes._CHAT_WINDOWS.values())
        if _registered:
            break
        time.sleep(0.05)
    h2 = type("H", (), {})(); h2.headers = {"Content-Type": "application/json"}
    h2._json = lambda d, status=200: setattr(h2, "_r", d)
    with patch("routes._read_json", return_value={"session_id": "s1", "mode": "story"}):
        routes.chat_flush(h2)
    t1.join(timeout=4)
    return captured.get("text", "")

print("=== A. 无 desc 图片 → 看不见提示词（不假装看见） ===")
with patch("routes.get_session", side_effect=_session), \
     patch("routes.handle_chat", side_effect=_cap), \
     patch("routes._read_json", return_value={"messages": [{"type": "image", "img_id": "img_nonexistent"}],
                                              "session_id": "s1", "mode": "story"}), \
     patch("routes._write_replies", side_effect=lambda r, m: r.messages if hasattr(r, "messages") else r):
    txt = _run(None)   # _read_json 已 patch，传 None 即可
check("A1 无描述图片注入看不见提示词",
      "看不见" in txt and "你已经发了图片吗" in txt)
check("A2 含'如实回应'指引（不假装看见的语义）", "如实" in txt)

print("=== B. 有 desc 图片 → 正常按描述回复 ===")
captured.clear()
with patch("routes.get_session", side_effect=_session), \
     patch("routes.handle_chat", side_effect=_cap), \
     patch("routes._read_json", return_value={"messages": [{"type": "image", "img_id": "img_x", "desc": "一只猫"}],
                                              "session_id": "s1", "mode": "story"}), \
     patch("routes._write_replies", side_effect=lambda r, m: r.messages if hasattr(r, "messages") else r):
    txt = _run(None)
check("B1 有描述图片按描述注入", "图片：一只猫" in txt)
check("B2 有描述时不注入看不见提示", "看不见" not in txt)

print("=== C. _handle_direct 数据完整性 ===")
import orchestrator as orch
replies = orch._handle_direct("image:unseen")
check("C1 image:unseen 兜底话术存在", any("看不见" in r for r in replies))
check("C2 未知 reason 仍走通用兜底", orch._handle_direct("x:y") != [])

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
