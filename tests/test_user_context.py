# -*- coding: utf-8 -*-
"""用户上下文（contextvars）测试 — 服务器版多用户隔离的基础

验证：无上下文时本地版行为不变 / 上下文设置后路径与 Key 隔离 / 多线程互不干扰 / reset 恢复。
"""
import sys, os, json, tempfile, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# 隔离 user_data：测试不碰真实用户数据
import modules.app_config as cfg
_tmp = tempfile.mkdtemp(prefix="firefly_test_uctx_")
cfg.USER_DIR = __import__("pathlib").Path(_tmp)
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")

# ══════════════════════════════════════════════════
print("=== A. 无上下文时本地版行为不变 ===")
check("A1 默认 mode_data_dir = USER_DIR/story/data", cfg.mode_data_dir() == cfg.USER_DIR / "story" / "data")
check("A2 默认 get_api_key 走 config", cfg.get_api_key() == cfg.config.get("api_key", ""))

# ══════════════════════════════════════════════════
print("\n=== B. 上下文设置后路径与 Key 隔离 ===")
tok = cfg.set_user_context(
    user_dir=cfg.USER_DIR / "u-test-1",
    api_key="user-key-1", api_base=cfg.API_BASE)
check("B1 上下文下 mode_data_dir 指向用户目录",
      cfg.mode_data_dir() == cfg.USER_DIR / "u-test-1" / "story" / "data")
check("B2 上下文下 get_api_key 用用户 Key", cfg.get_api_key() == "user-key-1")
check("B3 上下文下 mode_journal_dir 隔离",
      cfg.mode_journal_dir() == cfg.USER_DIR / "u-test-1" / "story" / "journal")
check("B4 上下文下 mode_character_dir 隔离",
      cfg.mode_character_dir() == cfg.USER_DIR / "u-test-1" / "story" / "character")
# get_client：有用户上下文 = 服务器版 → 默认 relay 模式（RelayClient，用户 Key 在 APP 端，
# 服务器不持有）；无上下文 = 本地版 → direct 模式
from modules.api_client import RelayClient, _CompatClient
c = cfg.get_client()
check("B5 上下文下 get_client 返回 RelayClient（relay 模式，Key 不落服务器）",
      isinstance(c, RelayClient) and c._user_key == str(cfg.USER_DIR / "u-test-1"))
check("B6 RelayClient 使用上下文 api_base", c._api_base == cfg.API_BASE)

cfg.reset_user_context(tok)
check("B7 reset 后恢复默认", cfg.mode_data_dir() == cfg.USER_DIR / "story" / "data")
check("B8 reset 后 get_api_key 恢复", cfg.get_api_key() == cfg.config.get("api_key", ""))

# 非法 api_base 回退官方
tok2 = cfg.set_user_context(user_dir=cfg.USER_DIR / "u-test-2", api_key="k2",
                            api_base="http://evil.com/v1")
c2 = cfg.get_client()
check("B9 非法 api_base 回退官方", c2._api_base == cfg.API_BASE)
cfg.reset_user_context(tok2)

# ══════════════════════════════════════════════════
print("\n=== C. 多线程上下文互不干扰（并发隔离）===")
# ThreadingHTTPServer 每请求一线程：两个线程各自设置上下文，互不串
results = {}
barrier = threading.Barrier(2)
def worker(name, key):
    t = cfg.set_user_context(user_dir=cfg.USER_DIR / name, api_key=key)
    barrier.wait()   # 两个线程同时持有各自上下文
    # 停顿确认：读自己上下文时另一线程已设置
    results[name] = (cfg.mode_data_dir(), cfg.get_api_key())
    cfg.reset_user_context(t)

t1 = threading.Thread(target=worker, args=("u-thread-a", "key-a"))
t2 = threading.Thread(target=worker, args=("u-thread-b", "key-b"))
t1.start(); t2.start()
t1.join(5); t2.join(5)
check("C1 线程 A 上下文正确",
      results.get("u-thread-a") == (cfg.USER_DIR / "u-thread-a" / "story" / "data", "key-a"))
check("C2 线程 B 上下文正确",
      results.get("u-thread-b") == (cfg.USER_DIR / "u-thread-b" / "story" / "data", "key-b"))
check("C3 线程结束后主线程不受影响", cfg.mode_data_dir() == cfg.USER_DIR / "story" / "data")

# 异常路径：重复 reset 同一 token → contextvars 抛 RuntimeError（保护机制；
# 正常路径 try/finally 每个 token 只 reset 一次，不会触发）
try:
    cfg.reset_user_context(tok)   # 已在上面 reset 过
    check("C4 重复 reset 抛 RuntimeError（保护机制）", False)
except RuntimeError:
    check("C4 重复 reset 抛 RuntimeError（保护机制）", True)

# ══════════════════════════════════════════════════
print("\n=== D. 服务器版数据目录落盘（conversation_store 走上下文）===")
from modules.conversation_store import append_message, conv_file, load_recent
tok3 = cfg.set_user_context(user_dir=cfg.USER_DIR / "u-test-3")
append_message("user", {"type": "text", "content": "你好"}, mode="story")
check("D1 上下文下 conversation 写到用户目录", conv_file().exists() and "u-test-3" in str(conv_file()))
msgs = load_recent(mode="story")
check("D2 用户目录可读回自己的消息", len(msgs) == 1 and msgs[0]["content"] == "你好")
cfg.reset_user_context(tok3)
check("D3 reset 后默认目录无该消息", load_recent(mode="story") == [])

print("\n=== 统计 ===")
print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
