# -*- coding: utf-8 -*-
"""会话历史持久化白盒测试"""

import sys, os, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.conversation_store import (
    append_message, load_recent, get_total_count, get_min_seq,
    hydrate_context, InputRejected,
)
from modules.context_manager import ContextManager
from pathlib import Path
import modules.conversation_store as cs

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")


tmpdir = tempfile.mkdtemp()
try:
    cs._CONV_FILE = Path(tmpdir) / "conversation.jsonl"
    cs._LEGACY_CONV = Path(tmpdir) / "legacy_missing.jsonl"

    # === 空文件 ===
    print("=== 空文件 ===")
    check("空文件 count=0", get_total_count() == 0)
    check("空文件 load=[]", load_recent() == [])
    check("空文件 min_seq=1", get_min_seq() == 1)

    # === 追加 ===
    print("\n=== 追加 ===")
    seq, t = append_message("user", {"type": "text", "content": "你好"})
    check("首条 seq=1", seq == 1)
    check("首条 time 非空", len(t) == 19)
    seq2, _ = append_message("firefly", {"type": "text", "content": "嗨"})
    check("第二条 seq=2", seq2 == 2)
    seq3, _ = append_message("firefly", {"type": "sticker", "path": "stickers/x.webp", "label": "比心"})
    check("第三条 seq=3", seq3 == 3)
    check("count=3", get_total_count() == 3)

    # === 加载 ===
    print("\n=== 加载 ===")
    msgs = load_recent(limit=150)
    check("load 3条", len(msgs) == 3)
    check("第1条 who=user", msgs[0]["who"] == "user")
    check("第2条 who=firefly", msgs[1]["who"] == "firefly")
    check("第3条 type=sticker", msgs[2]["type"] == "sticker")
    check("sticker 含 path", msgs[2]["path"] == "stickers/x.webp")
    check("sticker 含 label", msgs[2]["label"] == "比心")
    check("min_seq=1", get_min_seq() == 1)

    # === 分页 ===
    print("\n=== 分页 ===")
    for i in range(200):
        append_message("user", {"type": "text", "content": f"msg{i}"})
    check("总数=203", get_total_count() == 203)
    page1 = load_recent(limit=150)
    check("page1 数量=150", len(page1) == 150)
    check("page1 最大seq=203", page1[-1]["seq"] == 203)
    check("page1 最小seq=54", page1[0]["seq"] == 54)
    page2 = load_recent(limit=150, before_seq=54)
    check("page2 数量=53", len(page2) == 53)
    check("page2 最大seq=53", page2[-1]["seq"] == 53)
    check("page2 最小seq=1", page2[0]["seq"] == 1)
    page3 = load_recent(limit=150, before_seq=1)
    check("page3 空", page3 == [])

    # === 审查 ===
    print("\n=== 审查 ===")
    try:
        append_message("other", {"type": "text", "content": "x"})
        check("非法who → 抛异常", False)
    except InputRejected:
        check("非法who → InputRejected", True)
    try:
        append_message("user", {"type": "bad"})
        check("非法type → 抛异常", False)
    except InputRejected:
        check("非法type → InputRejected", True)
    try:
        append_message("user", {"type": "text", "content": ""})
        check("空content → 抛异常", False)
    except InputRejected:
        check("空content → InputRejected", True)
    try:
        append_message("user", {"type": "sticker", "path": ""})
        check("空path → 抛异常", False)
    except InputRejected:
        check("空path → InputRejected", True)

    # === 回灌 ===
    print("\n=== 回灌 ===")
    cs._CONV_FILE = Path(tmpdir) / "hydrate.jsonl"
    append_message("user", {"type": "text", "content": "早"})
    append_message("firefly", {"type": "text", "content": "早呀"})
    append_message("firefly", {"type": "sticker", "path": "stickers/x.webp", "label": "比心"})
    append_message("user", {"type": "text", "content": "在吗"})  # 未完成轮，应跳过
    ctx = ContextManager()
    n = hydrate_context(ctx)
    check("回灌轮数=1", n == 1)
    recent = ctx.get_recent(5)
    check("回灌含 user", any(m["role"] == "user" and "早" in m["content"] for m in recent))
    check("回灌含 assistant", any(m["role"] == "assistant" and "早呀" in m["content"] for m in recent))
    check("回灌含 sticker action", any(m["role"] == "system" and "比心" in m["content"] for m in recent))
    check("未完成轮未写入", not any(m.get("content") == "在吗" for m in recent))

finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
