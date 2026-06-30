# -*- coding: utf-8 -*-
"""气泡更新器 — 流萤聊天气泡风格切换工具

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出

非常用工具。仅在流萤有明显情绪倾向或开拓者要求时触发。
规划器决定是否触发，气泡子代理（代码层）决定具体切换哪个。
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── 异常 ──────────────────────────────────────────
class BubbleUpdaterError(Exception): pass
# 注意：验证阶段不抛异常，OutputInvalid 已移除。输出异常直接修正 + WARNING。


# ── 气泡定义 ─────────────────────────────────────
@dataclass
class BubbleDef:
    key: str
    name: str
    style: str
    text_color: str
    asset: str  # 素材路径（相对于 bubbles 目录）


_BUBBLES = {
    "bubble_rabbit":   BubbleDef("bubble_rabbit", "兔子在哪里？", "可爱", "#f0f0f0", "bubbleStyle2.svg"),
    "bubble_trotter":  BubbleDef("bubble_trotter", "次元扑满", "活泼", "#f0f0f0", "bubbleStyle3.svg"),
    "bubble_culture":  BubbleDef("bubble_culture", "星体培养皿", "中性", "#f0f0f0", "bubbleStyle1.svg"),
    "bubble_tavern":   BubbleDef("bubble_tavern", "怪物酒馆", "酷", "#f0f0f0", "bubbleStyle4.svg"),
    "bubble_cinema":   BubbleDef("bubble_cinema", "影城逐梦记", "文艺", "#f0f0f0", "bubbleStyle5.svg"),
    "bubble_warmth":   BubbleDef("bubble_warmth", "光阴莫负", "温柔", "#864756", "bubbleStyle6/main.png"),
}

_DEFAULT_BUBBLE = "bubble_culture"


# ── 容错辅助 ──────────────────────────────────────
def _safe_list(value, default: list) -> list:
    """None/非列表 → 默认值 + WARNING"""
    if value is None or not isinstance(value, list):
        logger.warning("参数为 None 或非列表，使用默认值")
        return default
    return value


def _safe_float(value, default: float) -> float:
    """None/非数字 → 默认值 + WARNING"""
    if value is None or not isinstance(value, (int, float)):
        logger.warning("参数为 None 或非数字(%s)，使用默认值 %s", type(value).__name__ if value is not None else "None", default)
        return default
    return float(value)


def _safe_int(value, default: int, lo: int, hi: int) -> int:
    """None/非数字/越界 → 默认值 + WARNING"""
    if value is None or not isinstance(value, (int, float)):
        logger.warning("参数为 None 或非数字，使用默认值 %s", default)
        return default
    v = int(value)
    if v < lo or v > hi:
        logger.warning("参数越界(%s)，裁剪到 [%s,%s]", v, lo, hi)
        return max(lo, min(hi, v))
    return v

# ── 输出结构 ──────────────────────────────────────
@dataclass
class BubbleResult:
    changed: bool
    bubble_key: str | None
    reason: str


# ── 主入口 ────────────────────────────────────────
def select_bubble(
    mood_list: list,
    affection: float,
    tension: float,
    stop_reason: str,
    current_hour: int,
    user_requested: bool = False,
    planner_triggered: bool = False,
) -> BubbleResult:
    """根据状态选择气泡。

    Args:
        mood_list: 当前心情 [{"label": str, "intensity": int}, ...]
        affection: 好感度 65-100
        tension: 紧张度 0+
        stop_reason: 来自 JudgeResult
        current_hour: 当前小时 0-23
        user_requested: 开拓者是否明确要求切换
        planner_triggered: 规划器是否决定触发

    Returns:
        BubbleResult — 切换结果
    """
    # 1. 审查 —— 只拒绝致命错误，其他容错降级
    mood_list = _safe_list(mood_list, [{"label": "安心", "intensity": 2}])
    affection = _safe_float(affection, 80.0)
    tension = _safe_float(tension, 15.0)
    stop_reason = stop_reason if isinstance(stop_reason, str) and stop_reason else "normal"
    current_hour = _safe_int(current_hour, 14, 0, 23)

    # 2. 匹配
    bubble_key = _match_priority(mood_list, affection, tension, stop_reason,
                                  current_hour, user_requested)
    if not planner_triggered and not user_requested:
        bubble_key = None

    # 3. 验证 —— 不抛异常，异常降级为"不切换"
    if bubble_key is not None and bubble_key not in _BUBBLES:
        logger.warning("bubble_key='%s' 不在合法集合，降级为不切换", bubble_key)
        bubble_key = None

    # 4. 最终输出
    if bubble_key is None:
        return BubbleResult(changed=False, bubble_key=None,
                           reason="无触发条件，保持当前气泡")

    bubble = _BUBBLES[bubble_key]
    return BubbleResult(
        changed=True,
        bubble_key=bubble_key,
        reason=f"切换到{bubble.name}（{bubble.style}风格）",
    )


# ── 优先级匹配 ────────────────────────────────────
def _match_priority(
    mood_list: list,
    affection: float,
    tension: float,
    stop_reason: str,
    current_hour: int,
    user_requested: bool,
) -> str | None:
    """按优先级 1-6 匹配，命中即停"""

    # 优先级 1: 开拓者明确要求切换 — 根据上下文选，默认可爱
    if user_requested:
        return "bubble_rabbit"

    # 优先级 2: 紧急/战斗语气
    if stop_reason == "urgent":
        return "bubble_tavern"

    # 优先级 3: 温柔安抚 — 好感高 + 对方低落 + 晚上/深夜
    has_low_spirit = any(m["label"] == "低落" for m in mood_list)
    is_night = current_hour >= 18 or current_hour < 6
    if has_low_spirit and affection >= 86 and is_night:
        return "bubble_warmth"

    # 优先级 4: 开心 mood 强度 ≥ 3
    for m in mood_list:
        if m["label"] == "开心" and m["intensity"] >= 3:
            return "bubble_rabbit"

    # 优先级 5: 深层坦白/回忆 — 紧张>60 + 好感≥86
    if tension > 60 and affection >= 86:
        return "bubble_cinema"

    # 优先级 6: 无触发
    return None


# ── 气泡信息查询 ──────────────────────────────────
def get_bubble_info(bubble_key: str) -> BubbleDef | None:
    """根据 key 获取气泡完整信息"""
    return _BUBBLES.get(bubble_key)


def get_all_bubbles() -> dict[str, BubbleDef]:
    """获取所有气泡定义，用于规划器工具描述"""
    return dict(_BUBBLES)


def get_default_bubble() -> str:
    """获取默认气泡 key"""
    return _DEFAULT_BUBBLE
