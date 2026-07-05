# -*- coding: utf-8 -*-
"""回复生成器白盒测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.reply_generator import (
    ReplyGenerator, ReplyInput, ReplyOutput,
    InputRejected, get_counters,
    _validate_input, _format_tone,
)

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 1. _format_tone
# ══════════════════════════════════════════════════
print("=== _format_tone ===")

text = _format_tone({"base": "温柔", "modifiers": ["关心"], "intensity": "克制"})
check("完整tone→含'温柔'", "温柔" in text)
check("完整tone→含'关心'", "关心" in text)
check("完整tone→含'克制'", "克制" in text)

text = _format_tone({})
check("空tone→含'日常'", "日常" in text)

text = _format_tone(None)
check("tone=None→含'日常'", "日常" in text)


# ══════════════════════════════════════════════════
# 2. ReplyInput + _validate_input
# ══════════════════════════════════════════════════
print("\n=== 输入审查 ===")

inp = ReplyInput(
    tone={"base": "日常", "modifiers": [], "intensity": "自然"},
    direction="自然接话。",
    recent_history=[],
    user_input="你好",
    tools_summary="有一个比心表情包可用。",
)
_validate_input(inp)  # 不抛异常
check("正常输入→通过审查", True)
check("tools_summary记录", "比心" in inp.tools_summary)

# tools_summary 空 → 默认
inp_no = ReplyInput(tone={}, direction="x", recent_history=[], user_input="hi", tools_summary="")
check("tools_summary空→记录", inp_no.tools_summary == "")

# user_input 为空
try:
    _validate_input(ReplyInput(tone={}, direction="x", recent_history=[], user_input=""))
    check("空user_input→抛异常", False)
except InputRejected:
    check("空user_input→InputRejected", True)

# 非 ReplyInput
try:
    _validate_input("not_input")
    check("非ReplyInput→抛异常", False)
except InputRejected:
    check("非ReplyInput→InputRejected", True)

# state_desc 已移除——状态系统暂时跳过

# direction 空 → 降级
inp3 = ReplyInput(tone={}, direction="", recent_history=[], user_input="hi")
_validate_input(inp3)
check("direction空→降级填充", "自然接话" in inp3.direction)

# recent_history=None → 降级
inp4 = ReplyInput(tone={}, direction="x", recent_history=None, user_input="hi")
_validate_input(inp4)
check("history=None→降级为[]", inp4.recent_history == [])


# ══════════════════════════════════════════════════
# 3. ReplyOutput 结构
# ══════════════════════════════════════════════════
print("\n=== ReplyOutput ===")

out = ReplyOutput(raw="嗯…你好呀")
check("正常输出→raw非空", len(out.raw) > 0)
check("ReplyOutput无messages字段", not hasattr(out, "messages"))


# ══════════════════════════════════════════════════
# 4. 计数器
# ══════════════════════════════════════════════════
print("\n=== 计数器 ===")
c = get_counters()
check("计数器含generate_count", "generate_count" in c)
check("计数器含cache_hit_rate", "cache_hit_rate" in c)
check("计数器含llm_errors", "llm_errors" in c)
check("计数器不含verify_filtered", "verify_filtered" not in c)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
