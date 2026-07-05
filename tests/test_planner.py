# -*- coding: utf-8 -*-
"""规划器白盒测试 — tools 数组格式"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modules.planner import (
    Planner, PlannerInput, PlannerOutput,
    InputRejected, TOOLS_MENU,
    _validate_input, _parse_and_validate,
)

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 1. 输入审查
# ══════════════════════════════════════════════════
print("=== 输入审查 ===")
try:
    _validate_input("not_planner_input")
    check("非PlannerInput→抛异常", False)
except InputRejected:
    check("非PlannerInput→InputRejected", True)

inp = PlannerInput(decoded_summary="ok", recent_history=[], tools_menu=TOOLS_MENU, user_input="")
try:
    _validate_input(inp)
    check("user_input空→抛异常", False)
except InputRejected:
    check("user_input空→InputRejected", True)

# sticker_frequency + sticker_style 默认值
inp_default = PlannerInput(decoded_summary="ok", recent_history=[], tools_menu=TOOLS_MENU, user_input="hi")
check("默认sticker_frequency=偶尔", inp_default.sticker_frequency == "偶尔")
check("默认sticker_style=无偏向", inp_default.sticker_style == "无偏向")

# 自定义值
inp_custom = PlannerInput(decoded_summary="ok", recent_history=[], tools_menu=TOOLS_MENU,
                          user_input="hi", sticker_frequency="经常", sticker_style="喜欢")
check("自定义sticker_frequency", inp_custom.sticker_frequency == "经常")
check("自定义sticker_style", inp_custom.sticker_style == "喜欢")


# ══════════════════════════════════════════════════
# 2. 正常 JSON 解析
# ══════════════════════════════════════════════════
print("\n=== 正常解析 ===")

j = '{"tools":[{"tool":"bubble","suggestion":"科幻风格"},{"tool":"sticker","suggestion":"比心"}],"tone":{"base":"温柔","modifiers":["关心"],"intensity":"克制"},"direction":"回应气泡切换，语气轻松。"}'
r = _parse_and_validate(j)
check("tools→2个", len(r.tools) == 2)
check("tool[0]=bubble", r.tools[0]["tool"] == "bubble")
check("suggestion=科幻", r.tools[0]["suggestion"] == "科幻风格")
check("tone.base=温柔", r.tone["base"] == "温柔")
check("direction非空", len(r.direction) > 0)

# 无工具
j2 = '{"tools":[],"tone":{"base":"日常","modifiers":[],"intensity":"自然"},"direction":"随便聊聊。"}'
r2 = _parse_and_validate(j2)
check("tools=空数组", len(r2.tools) == 0)


# ══════════════════════════════════════════════════
# 3. 降级
# ══════════════════════════════════════════════════
print("\n=== 降级 ===")

r = _parse_and_validate("垃圾")
check("垃圾JSON→默认", len(r.tools) == 0 and r.tone["base"] == "日常")

r = _parse_and_validate('{"tone":{},"direction":"x"}')
check("缺tools→空数组", len(r.tools) == 0)


# ══════════════════════════════════════════════════
# 4. PlannerOutput 结构
# ══════════════════════════════════════════════════
print("\n=== PlannerOutput ===")
out = PlannerOutput()
check("默认tools=[]", out.tools == [])
check("默认tone=日常", out.tone == {})


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
