# -*- coding: utf-8 -*-
"""Kimi 复审 #1/#2/#3 回归：
#1 context 写盘带 time（记忆整理时间戳前缀从此非空）
#2 图片选择 cancel 恢复计时（前端逻辑，用静态检查确认监听存在）
#3 server_app /time 公开免登录（本地版 _resolve_today 服务器优先可用）
"""
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

import modules.app_config as cfg
import modules.conversation_store as cs  # noqa: F401

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_kimi_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0
def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  V {desc}")
    else:
        FAIL += 1; print(f"  X {desc}")

print("=== #1 context 条目带 time ===")
from modules.context_manager import ContextManager
cm = ContextManager(token_capacity=100000)
cm.add_turn("你好", "你好呀")
cm.add_action("表情包", "比心")
cm.add_proactive_turn("我回来了")
full = cm.get_full()
check("C1 user 条目带 time", bool(full[0].get("time")))
check("C2 assistant 条目带 time", bool(full[1].get("time")))
check("C3 system 行为条目带 time", any(m.get("role") == "system" and m.get("time") for m in full))
check("C4 格式 YYYY-MM-DD HH:MM:SS", full[0]["time"].count("-") == 2 and len(full[0]["time"]) == 19)

print("=== #1 记忆切片时间戳非空 ===")
from modules.memory_manager import MemoryManager
mm = MemoryManager(client=type("C", (), {"chat": type("c", (), {"completions": None})})(), mode="story")
# 直接构造带 time 的历史（add_turn 已写入；这里模拟跨轮）
hist = [{"role": "user", "content": "第一条", "time": "2026-08-23 10:00:00"},
        {"role": "assistant", "content": "收到", "time": "2026-08-23 10:00:05"},
        {"role": "user", "content": "第二条", "time": "2026-08-23 11:00:00"}]
txt = mm._slice_new_dialogue(hist, 1)
check("C5 切片含时间戳前缀", "[2026-08-23 11:00:00]" in txt)
check("C6 已整理轮不出现", "第一条" not in txt)

print("=== #2 图片 cancel 监听存在 ===")
js = (ROOT / "app/static/js/chat_media.js").read_text(encoding="utf-8")
check("C7 cancel 监听已加", 'addEventListener("cancel"' in js)

print("=== #3 server_app /time 公开（免登录）===")
import os
os.environ["FIREFLY_ADMIN_TOKEN"] = "t"
import server_app
srv = server_app.ThreadingHTTPServer(("127.0.0.1", 0), server_app.FireflyHandler)
port = srv.server_address[1]
import threading
threading.Thread(target=srv.serve_forever, daemon=True).start()
import urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/time", timeout=5) as r:
        import json
        d = json.loads(r.read().decode())
        check("C8 /time 免登录 200", r.status == 200)
        check("C9 返回 date 格式", len(str(d.get("date", ""))) == 10 and d.get("date", "").count("-") == 2)
finally:
    srv.shutdown()

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
