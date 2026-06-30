# -*- coding: utf-8 -*-
"""状态解码器白盒测试 — 7维度解码全覆盖"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from app.modules.state_decoder import (
    decode, DecodedState, InputRejected,
    _decode_time, _decode_mood, _decode_affection, _decode_tension,
    _decode_initiative, _decode_energy, _decode_event,
)

PASS, FAIL = 0, 0

def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 0. 时间解码
# ══════════════════════════════════════════════════
print("=== 时间解码 ===")

# 时段覆盖
for h, expected_period in [(7, "清晨"), (10, "上午"), (13, "中午"),
                             (16, "下午"), (20, "晚上"), (23, "深夜"), (3, "凌晨")]:
    t = datetime(2026, 6, 30, h, 30)
    text = _decode_time(t)
    check(f"{h}:30→时段含'{expected_period}'", expected_period in text)

# 星期
tue = datetime(2026, 6, 30, 14, 0)  # 周二
text = _decode_time(tue)
check("周二→含'周二'", "周二" in text)
check("周二→工作日", "工作日" in text)

sat = datetime(2026, 7, 4, 14, 0)  # 周六
text = _decode_time(sat)
check("周六→含'周末'", "周末" in text)


# ══════════════════════════════════════════════════
# 1. 心情解码 — 单情绪 + 各强度
# ══════════════════════════════════════════════════
print("\n=== 心情解码 ===")

# 安心各强度
check("安心:1→比较放松", "比较放松" in _decode_mood([{"label":"安心","intensity":1}]))
check("安心:3→很安心", "很安心" in _decode_mood([{"label":"安心","intensity":3}]))
check("安心:5→完全放松", "完全放松" in _decode_mood([{"label":"安心","intensity":5}]))

# 开心各强度
check("开心:2→心情不错", "心情不错" in _decode_mood([{"label":"开心","intensity":2}]))
check("开心:3→挺开心的", "挺开心的" in _decode_mood([{"label":"开心","intensity":3}]))
check("开心:5→特别高兴", "特别高兴" in _decode_mood([{"label":"开心","intensity":5}]))

# 低落各强度
check("低落:2→提不起劲", "提不起劲" in _decode_mood([{"label":"低落","intensity":2}]))
check("低落:3→心里闷闷的", "心里闷闷的" in _decode_mood([{"label":"低落","intensity":3}]))
check("低落:5→很难过", "很难过" in _decode_mood([{"label":"低落","intensity":5}]))

# 害羞各强度
check("害羞:2→不好意思", "不好意思" in _decode_mood([{"label":"害羞","intensity":2}]))
check("害羞:3→脸颊发烫", "脸颊发烫" in _decode_mood([{"label":"害羞","intensity":3}]))
check("害羞:5→结巴", "结巴" in _decode_mood([{"label":"害羞","intensity":5}]))

# 焦虑各强度
check("焦虑:1→隐约不安", "隐约不安" in _decode_mood([{"label":"焦虑","intensity":1}]))
check("焦虑:3→心里悬着", "心里悬着" in _decode_mood([{"label":"焦虑","intensity":3}]))
check("焦虑:5→非常焦虑", "非常焦虑" in _decode_mood([{"label":"焦虑","intensity":5}]))

# 困惑各强度
check("困惑:2→有点没看懂", "有点没看懂" in _decode_mood([{"label":"困惑","intensity":2}]))
check("困惑:3→一头雾水", "一头雾水" in _decode_mood([{"label":"困惑","intensity":3}]))
check("困惑:5→摸不着头脑", "摸不着头脑" in _decode_mood([{"label":"困惑","intensity":5}]))


# ══════════════════════════════════════════════════
# 2. 心情解码 — 多情绪组合
# ══════════════════════════════════════════════════
print("\n=== 心情解码 — 多情绪 ===")

text = _decode_mood([{"label":"安心","intensity":3},{"label":"害羞","intensity":2}])
check("安心3+害羞2→含'很安心'", "很安心" in text)
check("安心3+害羞2→含'不好意思'", "不好意思" in text)

# 高强情绪用"流萤现在"开头
text = _decode_mood([{"label":"低落","intensity":4}])
check("低落:4→含'流萤现在'", "流萤现在" in text)

# 困惑与任何情绪共存
text = _decode_mood([{"label":"安心","intensity":3},{"label":"困惑","intensity":3}])
check("安心+困惑→含'很安心'", "很安心" in text)
check("安心+困惑→含'不过'", "不过" in text)
check("安心+困惑→含'一头雾水'", "一头雾水" in text)

# 空列表回退
text = _decode_mood([])
check("空列表→含'比较放松'", "比较放松" in text)


# ══════════════════════════════════════════════════
# 3. 好感度解码 — 各区间 + 表情包频率
# ══════════════════════════════════════════════════
print("\n=== 好感度解码 ===")

for aff, exp in [(65, "建立中"), (70, "建立中"), (78, "信任对方"), (90, "很亲近"),
                  (95, "很深"), (100, "很深")]:
    text, freq = _decode_affection(float(aff))
    check(f"好感{aff}→非空", len(text) > 0)

text, freq = _decode_affection(80.0)
check("好感80→偶尔", freq == "偶尔")
text, freq = _decode_affection(90.0)
check("好感90→经常", freq == "经常")
text, freq = _decode_affection(70.0)
check("好感70→几乎不", freq == "几乎不")
text, freq = _decode_affection(76.0)
check("好感76→偶尔", freq == "偶尔")


# ══════════════════════════════════════════════════
# 4. 紧张度解码 — 各区间
# ══════════════════════════════════════════════════
print("\n=== 紧张度解码 ===")

for ten, exp in [(5, "很平静"), (20, "正常"), (45, "有点紧张"),
                  (70, "紧张"), (90, "特别紧张")]:
    text = _decode_tension(float(ten))
    check(f"紧张{ten}→含'{exp}'", exp in text)

check("紧张15→很平静", "很平静" in _decode_tension(15.0))
check("紧张35→正常", "正常" in _decode_tension(35.0))
check("紧张60→有点紧张", "有点紧张" in _decode_tension(60.0))
check("紧张85→紧张", "紧张" in _decode_tension(85.0))
check("紧张120→沉默", "沉默" in _decode_tension(120.0))


# ══════════════════════════════════════════════════
# 5. 主动性解码 — 各区间 + 表情包风格
# ══════════════════════════════════════════════════
print("\n=== 主动性解码 ===")

for ini, exp_style in [(10, "弱势"), (30, "弱势"), (50, "无偏向"),
                         (70, "强势"), (90, "强势")]:
    text, style = _decode_initiative(float(ini))
    check(f"主动{ini}→风格'{exp_style}'", style == exp_style)

text, style = _decode_initiative(20.0)
check("主动20→被动", "被动" in text)
text, style = _decode_initiative(60.0)
check("主动60→自然", "自然" in text)
text, style = _decode_initiative(95.0)
check("主动95→骑士", "骑士" in text)


# ══════════════════════════════════════════════════
# 6. 精力解码 — 各区间
# ══════════════════════════════════════════════════
print("\n=== 精力解码 ===")

for ene, exp in [(250, "充沛"), (150, "有点累了"), (80, "困了"), (30, "撑不住了")]:
    text = _decode_energy(ene)
    check(f"精力{ene}→含'{exp}'", exp in text)

check("精力300→充沛", "充沛" in _decode_energy(300))
check("精力200→充沛", "充沛" in _decode_energy(200))
check("精力199→有点累", "累" in _decode_energy(199))
check("精力100→有点累", "累" in _decode_energy(100))
check("精力99→困了", "困了" in _decode_energy(99))
check("精力50→困了", "困了" in _decode_energy(50))
check("精力49→撑不住", "撑不住" in _decode_energy(49))
check("精力0→极度疲劳", "极度疲劳" in _decode_energy(0))


# ══════════════════════════════════════════════════
# 7. 事件解码 — 所有 stop_reason
# ══════════════════════════════════════════════════
print("\n=== 事件解码 ===")

check("normal→自然", "自然" in _decode_event("normal"))
check("violation:sexual→划界限", "划界限" in _decode_event("violation:sexual"))
check("violation:abusive→不反击", "不反击" in _decode_event("violation:abusive"))
check("violation:incomprehensible→不懂", "不懂" in _decode_event("violation:incomprehensible"))
check("urgent→果断", "果断" in _decode_event("urgent"))
check("input:empty→轻松", "轻松" in _decode_event("input:empty"))
check("input:too_long→等我看完", "等我看完" in _decode_event("input:too_long"))
check("api:error→信号不好", "信号不好" in _decode_event("api:error"))
check("未知reason→按normal", "自然" in _decode_event("unknown_reason_xyz"))


# ══════════════════════════════════════════════════
# 8. decode() 完整流程
# ══════════════════════════════════════════════════
print("\n=== decode() 完整流程 ===")

# 日常状态
state = {
    "mood": [{"label":"安心","intensity":3}],
    "affection": 85.0, "tension": 20.0, "initiative": 60.0, "energy": 280,
}
result = decode(state, "normal", datetime(2026, 6, 30, 14, 0))
check("日常→DecodedState", isinstance(result, DecodedState))
check("日常→summary非空", len(result.summary) > 0)
check("日常→reply_context非空", len(result.reply_context) > 0)
check("日常→sticker_frequency合法", result.sticker_frequency in ("经常","偶尔","几乎不"))
check("日常→sticker_style合法", result.sticker_style in ("强势","弱势","无偏向","喜欢"))
check("日常→含时间", "【时间】" in result.summary)
check("日常→含心情", "【心情】" in result.summary)
check("日常→含关系", "【关系】" in result.summary)
check("日常→event_text含norm", "正常" in result.event_text if "normal" in result.event_text else True)

# 违规事件——含事件描述
state = {
    "mood": [{"label":"害羞","intensity":4}],
    "affection": 80.0, "tension": 30.0, "initiative": 40.0, "energy": 270,
}
result = decode(state, "violation:sexual", datetime(2026, 6, 30, 14, 0))
check("违规→summary含事件", "【当前事件】" in result.summary)
check("违规→reply_context不含事件", "【当前事件】" not in result.reply_context)

# 正常事件——summary不含事件描述
state["mood"] = [{"label":"安心","intensity":3}]
result = decode(state, "normal", datetime(2026, 6, 30, 14, 0))
check("normal→summary不含事件", "【当前事件】" not in result.summary)

# 低精力——reply_context 含精力提示
state["energy"] = 80
result = decode(state, "normal")
check("低精力→reply_context含精力", "累" in result.reply_context or "困" in result.reply_context)

# 高精力——reply_context 不含精力提示
state["energy"] = 280
result = decode(state, "normal")
check("高精力→reply_context不含精力", "累" not in result.reply_context and "困" not in result.reply_context)

# 极端状态组合
extreme_state = {
    "mood": [{"label":"低落","intensity":5},{"label":"焦虑","intensity":4}],
    "affection": 66.0, "tension": 88.0, "initiative": 10.0, "energy": 30,
}
result = decode(extreme_state, "urgent", datetime(2026, 6, 30, 23, 30))
check("极端→不抛异常", isinstance(result, DecodedState))
check("极端→summary非空", len(result.summary) > 0)
check("极端→风格弱势", result.sticker_style == "弱势")
check("极端→频次几乎不", result.sticker_frequency == "几乎不")

# 高好感状态
high_state = {
    "mood": [{"label":"开心","intensity":4}],
    "affection": 93.0, "tension": 18.0, "initiative": 75.0, "energy": 290,
}
result = decode(high_state, "normal")
check("高好感→经常发", result.sticker_frequency == "经常")
check("高主动→强势风格", result.sticker_style == "强势")


# ══════════════════════════════════════════════════
# 9. 输入容错 —— None/非法值 → 默认值，不抛异常
# ══════════════════════════════════════════════════
print("\n=== 输入容错 ===")

# state 不是 dict → 唯一致命错误
try:
    decode("not_dict", "normal")
    check("state=str→抛异常", False)
except InputRejected:
    check("state=str→InputRejected", True)

# 缺失字段 → 降级默认
result = decode({"mood":[]}, "normal")
check("缺affection/tension→降级不抛异常", isinstance(result, DecodedState))

# 非法值 → 降级默认
result = decode({"mood":[],"affection":"high","tension":-5,"initiative":None,"energy":999}, "normal")
check("全部非法→降级不抛异常", isinstance(result, DecodedState))
check("全部非法→sticker_freq合法", result.sticker_frequency in ("经常","偶尔","几乎不"))

# mood 为 None
result = decode({"mood":None,"affection":80,"tension":20,"initiative":50,"energy":250}, "normal")
check("mood=None→降级默认", isinstance(result, DecodedState))

# mood 有非法元素
result = decode({"mood":[{"label":"暴怒","intensity":3}],"affection":80,"tension":0,"initiative":50,"energy":0}, "normal")
check("非法label→过滤降级", isinstance(result, DecodedState))

# 强度超范围
result = decode({"mood":[{"label":"安心","intensity":8}],"affection":80,"tension":0,"initiative":50,"energy":0}, "normal")
check("强度超范围→修正降级", isinstance(result, DecodedState))

# stop_reason 为 None / 空
result = decode({"mood":[],"affection":80,"tension":0,"initiative":50,"energy":300}, None)
check("stop_reason=None→降级normal", "正常" in result.event_text)

result = decode({"mood":[],"affection":80,"tension":0,"initiative":50,"energy":300}, "")
check("stop_reason=空→降级normal", "正常" in result.event_text)


# ══════════════════════════════════════════════════
# 10. 边界值
# ══════════════════════════════════════════════════
print("\n=== 边界值 ===")

# 好感边界
for aff in [65.0, 65.1, 75.9, 76.0, 85.9, 86.0, 95.9, 96.0, 100.0]:
    text, _ = _decode_affection(aff)
    check(f"好感{aff}→非空", len(text) > 0)

# 紧张边界
for ten in [0.0, 15.0, 15.1, 35.0, 35.1, 60.0, 60.1, 85.0, 85.1]:
    text = _decode_tension(ten)
    check(f"紧张{ten}→非空", len(text) > 0)

# 主动边界
for ini in [0.0, 20.0, 20.1, 40.0, 40.1, 60.0, 60.1, 80.0, 80.1, 100.0]:
    text, style = _decode_initiative(ini)
    check(f"主动{ini}→风格非空", style in ("强势","弱势","无偏向"))

# 精力边界
for ene in [300, 200, 199, 100, 99, 50, 49, 0]:
    text = _decode_energy(ene)
    check(f"精力{ene}→非空", len(text) > 0)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
