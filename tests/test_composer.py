# -*- coding: utf-8 -*-
"""消息编排器白盒测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.composer import (
    Composer, ComposerInput, ComposerOutput,
    InputRejected, get_counters,
    _validate_input, _parse_response, _split_sentences, _degraded_fallback,
)

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 1. ComposerInput + _validate_input
# ══════════════════════════════════════════════════
print("=== 输入审查 ===")

inp = ComposerInput(raw_text="嗯…你好呀", tone={"base": "日常"}, sticker=None)
_validate_input(inp)
check("正常输入→通过审查", True)

# 非 ComposerInput
try:
    _validate_input("not_input")
    check("非ComposerInput→抛异常", False)
except InputRejected:
    check("非ComposerInput→InputRejected", True)

# raw_text 非 str
try:
    _validate_input(ComposerInput(raw_text=123, tone={}))
    check("raw_text非str→抛异常", False)
except InputRejected:
    check("raw_text非str→InputRejected", True)

# tone 非 dict → 降级
inp2 = ComposerInput(raw_text="hi", tone="not_dict")
_validate_input(inp2)
check("tone非dict→降级为空", inp2.tone == {})


# ══════════════════════════════════════════════════
# 2. ComposerOutput 结构
# ══════════════════════════════════════════════════
print("\n=== ComposerOutput ===")

out = ComposerOutput(messages=[{"type": "text", "content": "你好"}, {"type": "sticker", "file": "a.png", "label": "测试"}])
check("两条消息", len(out.messages) == 2)
check("第一条text", out.messages[0]["type"] == "text")
check("第二条sticker", out.messages[1]["type"] == "sticker")

out2 = ComposerOutput()
check("默认空列表", out2.messages == [])


# ══════════════════════════════════════════════════
# 3. _parse_response
# ══════════════════════════════════════════════════
print("\n=== 响应解析 ===")

from tools.sticker_picker import StickerEntry

sticker = StickerEntry(id="test", file="test.png", label="测试", category="无偏向")

# 正常
resp = "[MSG]嗯…辛苦啦\n[MSG]这么晚还没休息呀…\n[STICKER]\n[MSG]明天还要上班呢"
msgs = _parse_response(resp, sticker)
check("解析→4条", len(msgs) == 4)
check("第1条type=text", msgs[0]["type"] == "text")
check("第1条内容正确", msgs[0]["content"] == "嗯…辛苦啦")
check("第3条type=sticker", msgs[2]["type"] == "sticker")

# 无 sticker 对象 → 跳过[STICKER]行
resp2 = "[MSG]你好\n[STICKER]\n[MSG]再见"
msgs2 = _parse_response(resp2, None)
check("无sticker→跳过标记行", len(msgs2) == 2)
check("无sticker→全是text", all(m["type"] == "text" for m in msgs2))

# 空响应
msgs3 = _parse_response("", sticker)
check("空响应→空列表", msgs3 == [])

# 非法行忽略
resp4 = "[MSG]你好\n垃圾行\n[MSG]再见"
msgs4 = _parse_response(resp4, sticker)
check("非法行→忽略", len(msgs4) == 2)

# 空MSG文本
resp5 = "[MSG]\n[MSG]有内容"
msgs5 = _parse_response(resp5, sticker)
check("空MSG→跳过", len(msgs5) == 1)


# ══════════════════════════════════════════════════
# 4. _split_sentences（降级分句）
# ══════════════════════════════════════════════════
print("\n=== 降级分句 ===")

parts = _split_sentences("嗯…辛苦啦。这么晚还没休息呀…明天还要上班呢。")
check("分句→≥3条", len(parts) >= 3)

parts = _split_sentences("你好")
check("短文本→1条", len(parts) == 1)

parts = _split_sentences("")
check("空文本→0条", len(parts) == 0)

parts = _split_sentences("嗯。好。的。")
check("超短句→合并为一条", len(parts) == 1)


# ══════════════════════════════════════════════════
# 5. _degraded_fallback
# ══════════════════════════════════════════════════
print("\n=== 降级回退 ===")

inp = ComposerInput(raw_text="嗯…辛苦啦。[sticker]早点休息。", tone={"base": "温柔"})
result = _degraded_fallback(inp, "测试降级")
check("降级→有消息", len(result.messages) > 0)
check("降级→含text", any(m["type"] == "text" for m in result.messages))

# 带 sticker
inp_sticker = ComposerInput(
    raw_text="你好。[sticker]再见。",
    tone={},
    sticker=StickerEntry(id="s", file="s.png", label="s", category="无偏向"),
)
result2 = _degraded_fallback(inp_sticker, "测试")
check("降级sticker→含sticker消息", any(m["type"] == "sticker" for m in result2.messages))

# 空文本
inp_empty = ComposerInput(raw_text="", tone={})
result3 = _degraded_fallback(inp_empty, "空")
check("降级空→默认消息", result3.messages[0]["content"] == "嗯…信号好像不太好")


# ══════════════════════════════════════════════════
# 6. 计数器
# ══════════════════════════════════════════════════
print("\n=== 计数器 ===")
c = get_counters()
check("计数器含compose_count", "compose_count" in c)
check("计数器含compose_llm_errors", "compose_llm_errors" in c)
check("计数器含compose_degraded", "compose_degraded" in c)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
