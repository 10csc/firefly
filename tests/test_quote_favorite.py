# -*- coding: utf-8 -*-
"""长按消息 → 引用 / 收藏：quote 净化、LLM 文本合成、回灌、收藏存储"""

import sys, os, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from pathlib import Path
import modules.conversation_store as cs
import modules.favorite_store as fs
from modules.conversation_store import (
    append_message, load_recent, hydrate_context, sanitize_quote,
    format_quote_for_llm, compose_user_text,
)
from modules.context_manager import ContextManager

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")


tmpdir = tempfile.mkdtemp()
try:
    # ── 引用净化 ──
    print("=== sanitize_quote ===")
    q = sanitize_quote({"who": "firefly", "type": "text", "content": " 你好呀 ", "evil": "<script>"})
    check("合法引用保留 who/type/content", q == {"who": "firefly", "type": "text", "content": "你好呀"})
    check("非法键被丢弃", q is not None and "evil" not in q)
    check("非 dict 返回 None", sanitize_quote("abc") is None)
    check("缺 type 返回 None", sanitize_quote({"who": "firefly", "content": "x"}) is None)
    check("非法 type 返回 None", sanitize_quote({"who": "firefly", "type": "video"}) is None)
    check("非法 who 容忍（无 who 仍可用）", sanitize_quote({"type": "text", "content": "x"}).get("who") is None)
    check("seq 强转 int", sanitize_quote({"type": "text", "content": "x", "seq": "42"})["seq"] == 42)
    check("seq 非法丢弃", "seq" not in sanitize_quote({"type": "text", "content": "x", "seq": "abc"}))
    long_q = sanitize_quote({"type": "text", "content": "长" * 5000})
    check("超长内容截断 2000", len(long_q["content"]) == 2000)
    check("sticker 引用保留 label/path", sanitize_quote({"type": "sticker", "label": "比心", "path": "s/x.webp"})["label"] == "比心")

    # ── LLM 文本合成 ──
    print("\n=== format/compose_user_text ===")
    check("firefly 引用前缀", format_quote_for_llm({"who": "firefly", "type": "text", "content": "早安"}) == "[引用流萤：「早安」]")
    check("user 引用前缀", format_quote_for_llm({"who": "user", "type": "text", "content": "早安"}) == "[引用开拓者：「早安」]")
    check("sticker 引用", format_quote_for_llm({"type": "sticker", "label": "比心"}) == "[引用对方：「[表情包：比心]」]")
    check("无效引用返回空", format_quote_for_llm(None) == "" and format_quote_for_llm({}) == "")
    check("compose 带引用", compose_user_text("我也好", {"who": "firefly", "type": "text", "content": "早安"}) == "[引用流萤：「早安」]\n我也好")
    check("compose 无引用原样", compose_user_text("你好", None) == "你好")

    # ── append_message 写盘净化 ──
    print("\n=== append_message 引用落盘 ===")
    _tmp_conv = Path(tmpdir) / "conv.jsonl"
    cs.conv_file = lambda mode="story": _tmp_conv
    cs._LEGACY_CONV = Path(tmpdir) / "legacy_missing.jsonl"
    seq, _ = append_message("user", {"type": "text", "content": "记得吗",
                                     "quote": {"who": "firefly", "type": "text", "content": "当然记得", "evil": 1}})
    rec = load_recent()[0]
    check("quote 已落盘且净化", rec["quote"] == {"who": "firefly", "type": "text", "content": "当然记得"})
    append_message("user", {"type": "text", "content": "没引用", "quote": "垃圾"})
    check("非法 quote 丢弃", "quote" not in load_recent()[-1])

    # ── hydrate 回灌带引用 ──
    print("\n=== hydrate 引用回灌 ===")
    _hyd_conv = Path(tmpdir) / "hyd.jsonl"
    cs.conv_file = lambda mode="story": _hyd_conv
    append_message("user", {"type": "text", "content": "好的",
                            "quote": {"who": "firefly", "type": "text", "content": "晚上见"}})
    append_message("firefly", {"type": "text", "content": "嗯！"})
    ctx = ContextManager()
    n = hydrate_context(ctx)
    check("回灌轮数=1", n == 1)
    recent = ctx.get_recent(5)
    user_msg = [m for m in recent if m["role"] == "user"][0]["content"]
    check("用户文本含引用前缀", "[引用流萤：「晚上见」]" in user_msg and "好的" in user_msg)

    # ── 收藏存储 ──
    print("\n=== favorite_store ===")
    fs.mode_data_dir = lambda mode="story": Path(tmpdir) / "favdata"
    rec1 = fs.add_favorite("story", {"who": "firefly", "type": "text", "content": "会找到的", "seq": 12})
    check("收藏 id=seq", rec1["id"] == "12")
    rec2 = fs.add_favorite("story", {"who": "user", "type": "sticker", "label": "比心", "path": "s/x.webp", "seq": 13})
    check("sticker 收藏", rec2["id"] == "13" and rec2["label"] == "比心")
    items = fs.list_favorites("story")
    check("列表最新在前", items[0]["id"] == "13" and len(items) == 2)
    check("收藏含 time", bool(rec1["time"]))
    # 按 seq 去重
    fs.add_favorite("story", {"who": "firefly", "type": "text", "content": "会找到的（更新）", "seq": 12})
    items = fs.list_favorites("story")
    check("同 seq 去重且更新内容", len(items) == 2 and items[0]["content"] == "会找到的（更新）")
    # 无 seq（本地即时渲染消息）也可收藏
    rec3 = fs.add_favorite("story", {"who": "user", "type": "text", "content": "刚才那条"})
    check("无 seq 生成 f 前缀 id", rec3["id"].startswith("f") and len(fs.list_favorites("story")) == 3)
    # 删除
    check("删除存在项", fs.delete_favorite("story", "13") is True)
    check("删除后列表少一条", len(fs.list_favorites("story")) == 2)
    check("删除不存在返回 False", fs.delete_favorite("story", "999") is False)
    # 审查约束
    for bad, desc in [
        (None, "非 dict 拒绝"),
        ({"who": "x", "type": "text", "content": "a"}, "非法 who 拒绝"),
        ({"who": "user", "type": "video"}, "非法 type 拒绝"),
        ({"who": "user", "type": "text", "content": ""}, "空内容拒绝"),
        ({"who": "user", "type": "sticker", "label": ""}, "空表情包拒绝"),
        ({"who": "user", "type": "text", "content": "长" * 3000}, "超长内容拒绝"),
    ]:
        try:
            fs.add_favorite("story", bad)
            check(desc + " → 抛异常", False)
        except fs.FavoriteError:
            check(desc + " → FavoriteError", True)
    # 上限
    fs.mode_data_dir = lambda mode="story": Path(tmpdir) / "favcap"
    for i in range(fs._MAX_FAVORITES + 10):
        fs.add_favorite("story", {"who": "user", "type": "text", "content": f"cap{i}", "seq": 1000 + i})
    items = fs.list_favorites("story")
    check("上限截断", len(items) == fs._MAX_FAVORITES)
    check("保留最新", items[0]["content"] == f"cap{fs._MAX_FAVORITES + 9}" and items[0]["seq"] == 1000 + fs._MAX_FAVORITES + 9)

finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
