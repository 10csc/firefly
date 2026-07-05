# -*- coding: utf-8 -*-
"""工具调度器白盒测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.tool_dispatcher import pre_dispatch, PreResult, get_counters

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 1. 气泡调度
# ══════════════════════════════════════════════════
print("=== 气泡 ===")

pre = pre_dispatch([{"tool": "bubble", "suggestion": "换个科幻风格的"}])
check("科幻→切换", pre.bubble is not None and pre.bubble.changed)
check("科幻→bubble_culture", pre.bubble.bubble_key == "bubble_culture")
check("摘要含已切换", "已切换" in pre.tools_summary)

pre = pre_dispatch([{"tool": "bubble", "suggestion": "可爱的"}])
check("可爱→bubble_rabbit", pre.bubble.bubble_key == "bubble_rabbit")

pre = pre_dispatch([{"tool": "bubble", "suggestion": "温暖安心的感觉"}])
check("温暖→bubble_warmth", pre.bubble.bubble_key == "bubble_warmth")

pre = pre_dispatch([])
check("空tools→无切换", pre.bubble is None)
check("空tools→空摘要", pre.tools_summary == "")


# ══════════════════════════════════════════════════
# 2. 表情包
# ══════════════════════════════════════════════════
print("\n=== 表情包 ===")

pre = pre_dispatch([{"tool": "sticker", "suggestion": "比心"}])
check("sticker比心→有图", pre.picked_sticker is not None)
check("摘要含'已选'", "已选" in pre.tools_summary)

pre = pre_dispatch([{"tool": "sticker", "suggestion": "想发一个害羞的表情"}])
check("sticker害羞→有图或说明", pre.tools_summary != "")


# ══════════════════════════════════════════════════
# 3. PreResult
# ══════════════════════════════════════════════════
print("\n=== PreResult ===")
pre = pre_dispatch([{"tool": "bubble", "suggestion": "可爱风格"}])
check("PreResult", isinstance(pre, PreResult))
check("bubble_key可访问", pre.bubble_key is not None)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
