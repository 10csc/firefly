# -*- coding: utf-8 -*-
"""气泡更新器白盒测试 — 简化版：AI 选 key，校验即用"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools.bubble_updater import (
    apply_bubble, BubbleResult, BubbleDef, bubble_menu,
    get_bubble_info, get_all_bubbles, get_default_bubble, _BUBBLES,
)

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 1. 气泡定义
# ══════════════════════════════════════════════════
print("=== 气泡定义 ===")
all_b = get_all_bubbles()
check("6个气泡", len(all_b) == 6)
check("默认=bubble_culture", get_default_bubble() == "bubble_culture")
menu = bubble_menu()
for key in _BUBBLES:
    check(f"菜单含{key}", key in menu)


# ══════════════════════════════════════════════════
# 2. apply_bubble — 正常
# ══════════════════════════════════════════════════
print("\n=== apply_bubble ===")

for key in _BUBBLES:
    r = apply_bubble(key, triggered=True)
    check(f"{key}→changed", r.changed)
    check(f"{key}→key正确", r.bubble_key == key)

r = apply_bubble(None, triggered=True)
check("key=None→不切换", not r.changed)

r = apply_bubble("bubble_rabbit", triggered=False)
check("triggered=False→不切换", not r.changed)


# ══════════════════════════════════════════════════
# 3. apply_bubble — 容错
# ══════════════════════════════════════════════════
print("\n=== 容错 ===")

r = apply_bubble("bubble_dragon", triggered=True)
check("非法key→降级默认", r.changed and r.bubble_key == get_default_bubble())

r = apply_bubble("", triggered=True)
check("空key→不切换", not r.changed)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
