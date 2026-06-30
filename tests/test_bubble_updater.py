# -*- coding: utf-8 -*-
"""气泡更新器白盒测试 — 6级优先级全覆盖"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools.bubble_updater import (
    select_bubble, BubbleResult, BubbleDef,
    get_bubble_info, get_all_bubbles, get_default_bubble,
    _BUBBLES, _match_priority,
)

PASS, FAIL = 0, 0

def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 0. 气泡定义完整性
# ══════════════════════════════════════════════════
print("=== 气泡定义 ===")

all_bubbles = get_all_bubbles()
check("6个气泡", len(all_bubbles) == 6)
for key in ("bubble_rabbit","bubble_trotter","bubble_culture","bubble_tavern","bubble_cinema","bubble_warmth"):
    check(f"{key}存在", key in all_bubbles)
    info = get_bubble_info(key)
    check(f"{key}→BubbleDef", isinstance(info, BubbleDef))
    check(f"{key}→name非空", len(info.name) > 0)
    check(f"{key}→asset非空", len(info.asset) > 0)

check("默认气泡=culture", get_default_bubble() == "bubble_culture")


# ══════════════════════════════════════════════════
# 1. 无触发 → 不切换
# ══════════════════════════════════════════════════
print("\n=== 无触发 ===")

# planner没触发，用户没要求 → 不切换
result = select_bubble(
    mood_list=[{"label":"安心","intensity":3}],
    affection=80.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=False,
)
check("无触发→changed=False", not result.changed)
check("无触发→bubble_key=None", result.bubble_key is None)

# planner触发但优先级6无匹配
result = select_bubble(
    mood_list=[{"label":"安心","intensity":3}],
    affection=80.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("planner触发但无匹配→changed=False", not result.changed)


# ══════════════════════════════════════════════════
# 2. 优先级1: 用户要求 → bubble_rabbit
# ══════════════════════════════════════════════════
print("\n=== 优先级1: 用户要求 ===")

result = select_bubble(
    mood_list=[{"label":"安心","intensity":3}],
    affection=80.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=True, planner_triggered=False,
)
check("用户要求→changed=True", result.changed)
check("用户要求→兔子", result.bubble_key == "bubble_rabbit")

# 用户要求覆盖其他触发——即使状态匹配优先级3
result = select_bubble(
    mood_list=[{"label":"低落","intensity":4}],
    affection=90.0, tension=20.0, stop_reason="normal",
    current_hour=22, user_requested=True, planner_triggered=True,
)
check("用户要求>温暖安抚→兔子优先", result.bubble_key == "bubble_rabbit")


# ══════════════════════════════════════════════════
# 3. 优先级2: 紧急 → bubble_tavern
# ══════════════════════════════════════════════════
print("\n=== 优先级2: 紧急 ===")

result = select_bubble(
    mood_list=[{"label":"焦虑","intensity":4}],
    affection=85.0, tension=40.0, stop_reason="urgent",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("紧急→changed=True", result.changed)
check("紧急→怪物酒馆", result.bubble_key == "bubble_tavern")

# 用户要求覆盖紧急
result = select_bubble(
    mood_list=[{"label":"焦虑","intensity":4}],
    affection=85.0, tension=40.0, stop_reason="urgent",
    current_hour=14, user_requested=True, planner_triggered=True,
)
check("用户要求>紧急", result.bubble_key == "bubble_rabbit")


# ══════════════════════════════════════════════════
# 4. 优先级3: 温柔安抚 → bubble_warmth
# ══════════════════════════════════════════════════
print("\n=== 优先级3: 温柔安抚 ===")

# 完整触发：好感高 + 对方低落 + 深夜
result = select_bubble(
    mood_list=[{"label":"低落","intensity":4}],
    affection=90.0, tension=30.0, stop_reason="normal",
    current_hour=22, user_requested=False, planner_triggered=True,
)
check("深夜安抚→光阴莫负", result.bubble_key == "bubble_warmth")

# 晚上 (18-22)
result = select_bubble(
    mood_list=[{"label":"低落","intensity":3}],
    affection=86.0, tension=30.0, stop_reason="normal",
    current_hour=20, user_requested=False, planner_triggered=True,
)
check("晚上安抚→光阴莫负", result.bubble_key == "bubble_warmth")

# 凌晨
result = select_bubble(
    mood_list=[{"label":"低落","intensity":3}],
    affection=90.0, tension=30.0, stop_reason="normal",
    current_hour=3, user_requested=False, planner_triggered=True,
)
check("凌晨安抚→光阴莫负", result.bubble_key == "bubble_warmth")

# 好感不够——不触发
result = select_bubble(
    mood_list=[{"label":"低落","intensity":4}],
    affection=80.0, tension=30.0, stop_reason="normal",
    current_hour=22, user_requested=False, planner_triggered=True,
)
check("好感不够→不触发温暖", result.bubble_key != "bubble_warmth")

# 没低落——不触发
result = select_bubble(
    mood_list=[{"label":"安心","intensity":3}],
    affection=90.0, tension=30.0, stop_reason="normal",
    current_hour=22, user_requested=False, planner_triggered=True,
)
check("没低落→不触发温暖", result.bubble_key != "bubble_warmth")

# 白天——不触发
result = select_bubble(
    mood_list=[{"label":"低落","intensity":4}],
    affection=90.0, tension=30.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("白天→不触发温暖", result.bubble_key != "bubble_warmth")


# ══════════════════════════════════════════════════
# 5. 优先级4: 开心 → bubble_rabbit
# ══════════════════════════════════════════════════
print("\n=== 优先级4: 开心 ===")

result = select_bubble(
    mood_list=[{"label":"开心","intensity":3}],
    affection=85.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("开心:3→兔子", result.bubble_key == "bubble_rabbit")

result = select_bubble(
    mood_list=[{"label":"开心","intensity":5}],
    affection=85.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("开心:5→兔子", result.bubble_key == "bubble_rabbit")

# 开心强度不够——不触发
result = select_bubble(
    mood_list=[{"label":"开心","intensity":2}],
    affection=85.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("开心:2→不触发", result.bubble_key != "bubble_rabbit")


# ══════════════════════════════════════════════════
# 6. 优先级5: 深层坦白 → bubble_cinema
# ══════════════════════════════════════════════════
print("\n=== 优先级5: 深层坦白 ===")

result = select_bubble(
    mood_list=[{"label":"害羞","intensity":3}],
    affection=90.0, tension=65.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("高好感+高紧张→影城", result.bubble_key == "bubble_cinema")

result = select_bubble(
    mood_list=[{"label":"安心","intensity":3}],
    affection=86.0, tension=90.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("紧张90+好感86→影城", result.bubble_key == "bubble_cinema")

# 紧张不够
result = select_bubble(
    mood_list=[{"label":"害羞","intensity":3}],
    affection=90.0, tension=50.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("紧张不够→不触发影城", result.bubble_key != "bubble_cinema")

# 好感不够
result = select_bubble(
    mood_list=[{"label":"害羞","intensity":3}],
    affection=80.0, tension=70.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("好感不够→不触发影城", result.bubble_key != "bubble_cinema")


# ══════════════════════════════════════════════════
# 7. 优先级竞争：温暖 vs 兔子 vs 影城
# ══════════════════════════════════════════════════
print("\n=== 优先级竞争 ===")

# 深夜低落+开心同时存在 → 温暖优先（P3 > P4）
result = select_bubble(
    mood_list=[{"label":"低落","intensity":4},{"label":"开心","intensity":3}],
    affection=90.0, tension=30.0, stop_reason="normal",
    current_hour=22, user_requested=False, planner_triggered=True,
)
check("低落+开心深夜→温暖优先(P3>P4)", result.bubble_key == "bubble_warmth")

# 非深夜低落+紧张+高好感 → 影城优先（开心没触发时P5命中）
result = select_bubble(
    mood_list=[{"label":"低落","intensity":4}],
    affection=90.0, tension=70.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("低落+高紧张白天→影城(P5)", result.bubble_key == "bubble_cinema")

# 紧急覆盖一切 (P2)
result = select_bubble(
    mood_list=[{"label":"低落","intensity":4},{"label":"开心","intensity":3}],
    affection=95.0, tension=30.0, stop_reason="urgent",
    current_hour=22, user_requested=False, planner_triggered=True,
)
check("紧急覆盖温暖", result.bubble_key == "bubble_tavern")


# ══════════════════════════════════════════════════
# 8. 输入容错 —— None/非法值 → 默认值，不抛异常
# ══════════════════════════════════════════════════
print("\n=== 输入容错 ===")

# mood_list=None → 降级默认
result = select_bubble(None, 80, 20, "normal", 14)
check("mood=None→降级不抛异常", isinstance(result, BubbleResult))

# affection=None → 降级默认
result = select_bubble([], None, 20, "normal", 14)
check("affection=None→降级", isinstance(result, BubbleResult))

# tension=None → 降级默认
result = select_bubble([], 80, None, "normal", 14)
check("tension=None→降级", isinstance(result, BubbleResult))

# stop_reason=None → 降级 normal
result = select_bubble([], 80, 20, None, 14)
check("stop_reason=None→降级normal", isinstance(result, BubbleResult))

# hour=None → 降级 14
result = select_bubble([], 80, 20, "normal", None)
check("hour=None→降级14", isinstance(result, BubbleResult))

# hour 越界 → 裁剪
result = select_bubble([], 80, 20, "normal", 25)
check("hour=25→裁剪不抛异常", isinstance(result, BubbleResult))

# affection 极低 → 容错（不抛异常）
result = select_bubble([], 30, 20, "normal", 14)
check("affection=30→容错降级", isinstance(result, BubbleResult))

# tension 负值 → 容错
result = select_bubble([], 80, -5, "normal", 14)
check("tension=-5→容错降级", isinstance(result, BubbleResult))


# ══════════════════════════════════════════════════
# 9. BubbleResult 结构
# ══════════════════════════════════════════════════
print("\n=== BubbleResult 结构 ===")

result = select_bubble(
    mood_list=[{"label":"开心","intensity":4}],
    affection=85.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=True,
)
check("切换→changed=True", result.changed)
check("切换→bubble_key非空", result.bubble_key is not None)
check("切换→reason非空", len(result.reason) > 0)

result = select_bubble(
    mood_list=[{"label":"安心","intensity":3}],
    affection=80.0, tension=20.0, stop_reason="normal",
    current_hour=14, user_requested=False, planner_triggered=False,
)
check("不切换→changed=False", not result.changed)
check("不切换→bubble_key=None", result.bubble_key is None)


# ══════════════════════════════════════════════════
# 10. _match_priority 独立测试
# ══════════════════════════════════════════════════
print("\n=== _match_priority ===")

# 逐个验证每个优先级的内部路径
r = _match_priority([{"label":"安心","intensity":3}], 80, 20, "normal", 14, user_requested=True)
check("_match:用户要求→兔子", r == "bubble_rabbit")

r = _match_priority([{"label":"焦虑","intensity":4}], 85, 40, "urgent", 14, user_requested=False)
check("_match:紧急→酒馆", r == "bubble_tavern")

r = _match_priority([{"label":"低落","intensity":4}], 90, 30, "normal", 22, user_requested=False)
check("_match:温暖→光阴莫负", r == "bubble_warmth")

r = _match_priority([{"label":"开心","intensity":3}], 85, 20, "normal", 14, user_requested=False)
check("_match:开心→兔子", r == "bubble_rabbit")

r = _match_priority([{"label":"害羞","intensity":3}], 90, 70, "normal", 14, user_requested=False)
check("_match:坦白→影城", r == "bubble_cinema")

r = _match_priority([{"label":"安心","intensity":3}], 80, 20, "normal", 14, user_requested=False)
check("_match:无匹配→None", r is None)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
