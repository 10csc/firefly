# -*- coding: utf-8 -*-
"""状态解码器 — 数值 → 自然语言描述

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出

纯代码层。不调 LLM，不调 API。解码器只知道"什么状态"，
不知道"为什么是这个状态"——那是规划器的事。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# ── 异常 ──────────────────────────────────────────
class DecoderError(Exception): pass
class InputRejected(DecoderError): pass
# 注意：验证阶段不抛异常，OutputInvalid 已移除。输出异常直接修正 + WARNING。


# ── 容错辅助 ──────────────────────────────────────
def _safe_number(value, default: float, lo: float, hi: float | None) -> float:
    """安全取数值：None/非数字/越界 → 默认值 + WARNING"""
    if value is None:
        logger.warning("state 字段为 None，使用默认值 %s", default)
        return default
    if not isinstance(value, (int, float)):
        logger.warning("state 字段类型错误(%s)，使用默认值 %s", type(value).__name__, default)
        return default
    if value < lo or (hi is not None and value > hi):
        logger.warning("state 字段越界(%s)，裁剪到 [%s,%s]", value, lo, hi or "∞")
        return max(lo, min(hi, value)) if hi is not None else max(lo, value)
    return float(value)


def _safe_mood_list(value) -> list:
    """安全取心情列表：None/非列表/空列表 → 默认安心:2"""
    if value is None or not isinstance(value, list) or len(value) == 0:
        logger.warning("mood 为空或非法类型，回退默认 [安心:2]")
        return [{"label": "安心", "intensity": 2}]
    cleaned = []
    for m in value:
        if not isinstance(m, dict):
            logger.warning("mood 元素非 dict，跳过: %s", m)
            continue
        label = m.get("label", "")
        if label not in VALID_MOODS:
            logger.warning("mood label 非法 '%s'，跳过", label)
            continue
        intensity = m.get("intensity", 2)
        if not isinstance(intensity, (int, float)) or not (1 <= intensity <= 5):
            logger.warning("mood intensity 非法 %s，修正为 2", intensity)
            intensity = 2
        cleaned.append({"label": label, "intensity": int(intensity)})
    if not cleaned:
        logger.warning("mood 清理后为空，回退默认 [安心:2]")
        return [{"label": "安心", "intensity": 2}]
    return cleaned


# ── 常量 ──────────────────────────────────────────
VALID_MOODS = ("安心", "开心", "低落", "害羞", "焦虑", "困惑")
VALID_STICKER_FREQUENCY = ("经常", "偶尔", "几乎不")

# ── 心情文字映射 ─────────────────────────────────
_MOOD_TEXT = {
    "安心": {1: "比较放松", 2: "比较放松", 3: "很安心", 4: "完全放松，非常自在", 5: "完全放松，非常自在"},
    "开心": {1: "心情不错", 2: "心情不错", 3: "挺开心的", 4: "特别高兴，忍不住想分享", 5: "特别高兴，忍不住想分享"},
    "低落": {1: "有点提不起劲", 2: "有点提不起劲", 3: "心里闷闷的", 4: "很难过，不想多说话", 5: "很难过，不想多说话"},
    "害羞": {1: "稍微有点不好意思", 2: "稍微有点不好意思", 3: "脸颊发烫", 4: "特别害羞，说话都结巴了", 5: "特别害羞，说话都结巴了"},
    "焦虑": {1: "隐约不安", 2: "隐约不安", 3: "心里悬着，放不下", 4: "非常焦虑，不停想这件事", 5: "非常焦虑，不停想这件事"},
    "困惑": {1: "有点没看懂", 2: "有点没看懂", 3: "一头雾水", 4: "完全摸不着头脑", 5: "完全摸不着头脑"},
}

# ── 时段映射 ─────────────────────────────────────
# (起始小时, 结束小时, 标签, 影响描述)
_TIME_PERIODS = [
    (6, 9, "清晨", "问候语偏暖，可能说早安"),
    (9, 12, "上午", "日常节奏，可能问今天有什么计划"),
    (12, 14, "中午", "关心吃饭，午休时间"),
    (14, 18, "下午", "日常闲聊，可能问困不困"),
    (18, 22, "晚上", "语气偏暖，关心今天过得怎样"),
    (22, 24, "深夜", "句子更短更温柔，关心怎么还没睡"),
    (0, 6, "凌晨", "意外+关心——这个点了还没睡？"),
]

# ── 事件方向指引 ─────────────────────────────────
_EVENT_GUIDANCE = {
    "normal": "正常聊天——自然地接话就好。",
    "violation:sexual": "开拓者说了性暗示的话。流萤害羞但温和地划界限——不吵架不冷漠，句子变短、语气迟疑。如果对方坚持则语气转认真。",
    "violation:abusive": "开拓者说了伤人的话。流萤困惑，不反击——会问'你为什么这么说'。语气冷静但有一点点受伤，不激烈。",
    "violation:incomprehensible": "开拓者说了流萤完全不懂的东西。流萤困惑地表示不会/不懂——不假装懂，不强行接话。可能自嘲一句。",
    "urgent": "开拓者在求救或处于危险中。语气瞬间切换——句子极短，省略号消失，果断直接。萨姆态特征但不切换人称。",
    "input:empty": "开拓者发了空消息。温柔问一句怎么啦，轻松自然。",
    "input:too_long": "开拓者发了超长消息。幽默地说等我看完，放松语气。不做实质性回应。",
    "api:error": "系统出了问题。用信号不好解释，轻松带过。不做实质性回应。",
}


# ── 输出结构 ──────────────────────────────────────
@dataclass
class DecodedState:
    """数值解码后的自然语言状态快照"""
    time_text: str
    mood_text: str
    affection_text: str
    tension_text: str
    initiative_text: str
    energy_text: str
    event_text: str
    summary: str
    sticker_frequency: str
    sticker_style: str
    reply_context: str


# ── 主入口 ────────────────────────────────────────
def decode(state: dict, stop_reason: str, now: datetime | None = None) -> DecodedState:
    """将 StateUpdater 输出的数值 state 解码为自然语言描述。

    Args:
        state: {"mood": [...], "affection": float, "tension": float, "initiative": float, "energy": int}
        stop_reason: 来自 JudgeResult.stop_reason
        now: 当前时间，默认 datetime.now()

    Returns:
        DecodedState — 自然语言状态快照
    """
    # 1. 审查
    _validate_input(state, stop_reason)
    if now is None:
        now = datetime.now()

    # 2. 解码 —— 所有字段容错：None/缺失/非法类型 → 默认值
    mood_list = _safe_mood_list(state.get("mood"))
    affection = _safe_number(state.get("affection"), 80.0, 65, 100)
    tension = _safe_number(state.get("tension"), 15.0, 0, None)
    initiative = _safe_number(state.get("initiative"), 50.0, 0, 100)
    energy = _safe_number(state.get("energy"), 300, 0, 300)
    stop_reason = stop_reason if isinstance(stop_reason, str) and stop_reason else "normal"

    time_text = _decode_time(now)
    mood_text = _decode_mood(mood_list)
    affection_text, sticker_frequency = _decode_affection(affection)
    tension_text = _decode_tension(tension)
    initiative_text, sticker_style = _decode_initiative(initiative)
    energy_text = _decode_energy(energy)
    event_text = _decode_event(stop_reason)

    # 汇总
    parts = [time_text, mood_text, affection_text + "，" + sticker_frequency + "发。", tension_text, initiative_text, energy_text]
    if stop_reason != "normal":
        parts.append(event_text)
    summary = "\n".join(parts)

    reply_parts = [mood_text, affection_text]
    if energy < 100:
        reply_parts.append(energy_text)
    reply_context = " ".join(reply_parts)

    # 3. 验证 + 修正（不抛异常，异常直接降级）
    result = DecodedState(
        time_text=time_text, mood_text=mood_text,
        affection_text=affection_text, tension_text=tension_text,
        initiative_text=initiative_text, energy_text=energy_text,
        event_text=event_text, summary=summary,
        sticker_frequency=sticker_frequency, sticker_style=sticker_style,
        reply_context=reply_context,
    )
    result = _validate_output(result)

    # 4. 最终输出
    return result


# ── 输入验证 ──────────────────────────────────────
def _validate_input(state: dict, stop_reason: str):
    """只拒绝致命错误。字段缺失/越界 → 容错辅助函数处理 + WARNING。"""
    if not isinstance(state, dict):
        raise InputRejected(f"state 必须为 dict，实际: {type(state).__name__}")
    # 其余字段的容错在 decode() 中由 _safe_* 函数处理
    # 此处只做最基本的类型检查，不再逐字段抛异常


def _validate_output(result: DecodedState) -> DecodedState:
    """验证输出合法性。不抛异常——发现异常直接修正 + WARNING。"""
    fixed = False
    if not result.summary:
        result.summary = "【状态】信息不足，流萤在自然放松地聊天。"
        logger.warning("summary 为空，降级为默认描述"); fixed = True
    if not result.reply_context:
        result.reply_context = "流萤现在感觉比较放松，相处自然。"
        logger.warning("reply_context 为空，降级为默认描述"); fixed = True
    if result.sticker_frequency not in VALID_STICKER_FREQUENCY:
        result.sticker_frequency = "偶尔"
        logger.warning("sticker_frequency='%s' 不合法，降级为'偶尔'", result.sticker_frequency); fixed = True
    if result.sticker_style not in ("强势", "弱势", "无偏向", "喜欢"):
        result.sticker_style = "无偏向"
        logger.warning("sticker_style='%s' 不合法，降级为'无偏向'", result.sticker_style); fixed = True
    if fixed:
        logger.warning("输出验证修正了 %d 项，对话继续", fixed)
    return result


# ── 零、时间解码 ──────────────────────────────────
def _decode_time(now: datetime) -> str:
    hour = now.hour
    weekday = now.weekday()  # 0=周一

    period_label = "晚上"
    period_hint = ""
    for start, end, label, hint in _TIME_PERIODS:
        if start <= hour < end:
            period_label = label
            period_hint = hint
            break

    weekday_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    day_label = weekday_names[weekday]
    is_weekend = weekday >= 5

    if is_weekend:
        return f"【时间】{day_label}{period_label}——周末，{period_hint}。"
    else:
        return f"【时间】{day_label}{period_label}——工作日，{period_hint}。"


# ── 一、心情解码 ──────────────────────────────────
def _decode_mood(mood_list: list) -> str:
    if not mood_list:
        return "【心情】流萤现在比较放松。"

    confusion = None
    texts = []
    for m in mood_list:
        label = m["label"]
        intensity = int(m["intensity"])
        text = _MOOD_TEXT.get(label, {}).get(intensity, "")

        if label == "困惑":
            confusion = text
        else:
            if intensity >= 4:
                texts.append(f"流萤现在{text}")
            else:
                texts.append(text)

    if texts:
        result = "。".join(texts) if len(texts) > 1 else texts[0]
    else:
        result = "流萤现在比较放松"

    if confusion:
        result += f"。不过{confusion}"

    return f"【心情】{result}。"


# ── 二、好感度解码 ────────────────────────────────
def _decode_affection(affection: float) -> tuple[str, str]:
    if 96 <= affection <= 100:
        text = "你们的关系很深——流萤完全信任你，可以把一切托付给你。她会为你担心，表达虽然克制但心意很明显。"
    elif 86 <= affection < 96:
        text = "你们已经很亲近了，流萤信任你，在你面前可以做自己。她会主动关心，愿意说心里话。"
    elif 76 <= affection < 86:
        text = "流萤信任你，相处自在。可以自然地聊天，偶尔展示真实的自己。"
    else:
        text = "关系还在建立中，流萤保持礼貌距离。说话比较客气，不会主动暴露脆弱。"

    if affection >= 90:
        freq = "经常"
    elif affection >= 76:
        freq = "偶尔"
    else:
        freq = "几乎不"

    return f"【关系】{text}", freq


# ── 三、紧张度解码 ────────────────────────────────
def _decode_tension(tension: float) -> str:
    if tension <= 15:
        text = "很平静——完全放松，没有任何心跳感。"
    elif tension <= 35:
        text = "正常——在自然放松地聊天。"
    elif tension <= 60:
        text = "有点紧张——心跳微微加速，说话可能稍微犹豫。"
    elif tension <= 85:
        text = "紧张——明显的心跳加速，说话更小心，句子可能变短。"
    else:
        text = "特别紧张——小鹿乱撞，可能不知所措，句子极短或突然沉默。"
    return f"【紧张度】{text}"


# ── 四、主动性解码 ────────────────────────────────
def _decode_initiative(initiative: float) -> tuple[str, str]:
    if initiative <= 20:
        text = "流萤现在比较被动，等待对方主导，很少主动起话题。回复较短，容易害羞。"
        style = "弱势"
    elif initiative <= 40:
        text = "流萤整体被动但有回应，偶尔主动问一句。不扛话题但有来有回。"
        style = "弱势"
    elif initiative <= 60:
        text = "流萤节奏自然，有来有回，不抢也不退。正常聊天。"
        style = "无偏向"
    elif initiative <= 80:
        text = "流萤比较主动，会自然地找话题。回复可能稍长，会关心和引导对话。"
        style = "强势"
    else:
        text = "流萤很主动，扛话题，会提议一起做什么。表达直接但不霸道——骑士风格。"
        style = "强势"

    return f"【主动度】{text}", style


# ── 五、精力解码 ──────────────────────────────────
def _decode_energy(energy: int) -> str:
    if energy >= 200:
        text = "精力充沛——状态很好，反应快，回复正常。"
    elif energy >= 100:
        text = "有点累了——反应正常但可能偶尔走神，句子稍微变短。"
    elif energy >= 50:
        text = "困了——明显疲劳，句子变短，省略号增多，反应变慢。"
    else:
        text = "快撑不住了——极度疲劳，可能主动说困了、暗示要休息。"
    return f"【精力】{text}"


# ── 六、事件解码 ──────────────────────────────────
def _decode_event(stop_reason: str) -> str:
    guidance = _EVENT_GUIDANCE.get(stop_reason)
    if guidance is None:
        logger.info("未知 stop_reason='%s'，按 normal 处理", stop_reason)
        guidance = _EVENT_GUIDANCE["normal"]
    return f"【当前事件】{guidance}"
