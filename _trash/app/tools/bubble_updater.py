# -*- coding: utf-8 -*-
"""气泡更新器 — 聊天气泡风格切换

纯代码层。规划器决定换不换、换哪个（指定 key），气泡更新器只做校验和返回。
"""

import logging
from copy import deepcopy
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── 异常 ──────────────────────────────────────────
class BubbleUpdaterError(Exception): pass


# ── 气泡定义 ─────────────────────────────────────
@dataclass
class BubbleDef:
    key: str
    name: str
    style: str
    text_color: str
    cls: str              # 前端 CSS 主题类（app.js 容器类，纯 CSS 气泡）
    keywords: list = None   # 规划器 suggestion 匹配关键词，单一数据源

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []

_BUBBLES = {
    "bubble_rabbit":   BubbleDef("bubble_rabbit", "兔子在哪里？", "可爱", "#f0f0f0", "fb-rabbit",
                                 keywords=["可爱", "萌", "轻松", "活泼"]),
    "bubble_trotter":  BubbleDef("bubble_trotter", "次元扑满", "活泼", "#f0f0f0", "fb-trotter",
                                 keywords=["活泼", "游戏", "动感"]),
    "bubble_culture":  BubbleDef("bubble_culture", "星体培养皿", "科技/中性", "#f0f0f0", "fb-culture",
                                 keywords=["科幻", "科技", "中性", "默认", "日常"]),
    "bubble_tavern":   BubbleDef("bubble_tavern", "怪物酒馆", "暗色/酷", "#f0f0f0", "fb-tavern",
                                 keywords=["酷", "暗色", "战斗", "帅气"]),
    "bubble_cinema":   BubbleDef("bubble_cinema", "影城逐梦记", "文艺", "#f0f0f0", "fb-cinema",
                                 keywords=["文艺", "电影", "回忆", "安静"]),
    "bubble_warmth":   BubbleDef("bubble_warmth", "光阴莫负", "温柔/暖棕", "#864756", "fb-warmth",
                                 keywords=["温暖", "温柔", "暖心", "安抚"]),
}

import threading
_lock = threading.Lock()
_DEFAULT_BUBBLE = "bubble_culture"

_VALIDATE_DEGRADED = 0


def get_counters() -> dict:
    with _lock:
        return {"validate_degraded": _VALIDATE_DEGRADED}


# ── 气泡菜单（供规划器看）─────────────────────────
def bubble_menu() -> str:
    """返回气泡选项说明，嵌入规划器 tools_menu。含关键词，供 LLM 选气泡时参考。"""
    lines = ["可用的聊天气泡（括号内为风格关键词）："]
    for key, b in _BUBBLES.items():
        kw = "/".join(b.keywords) if b.keywords else ""
        lines.append(f"  {key}: {b.name}（{b.style}）关键词: {kw}")
    lines.append(f"默认: {_DEFAULT_BUBBLE}")
    return "\n".join(lines)


# ── 输出 ──────────────────────────────────────────
@dataclass
class BubbleResult:
    changed: bool
    bubble_key: str | None
    reason: str


# ── 主入口 ────────────────────────────────────────
def apply_bubble(bubble_key: str, triggered: bool = False) -> BubbleResult:
    """应用气泡切换。校验 key 合法性，不合法回退默认。

    Args:
        bubble_key: 规划器选定的气泡 key，或 None 表示不切换
        triggered: 是否有切换意图（false 时直接不切换）
    """
    global _VALIDATE_DEGRADED

    if not triggered or not bubble_key:
        return BubbleResult(changed=False, bubble_key=None, reason="未触发")

    if bubble_key not in _BUBBLES:
        logger.warning("bubble_key='%s' 不合法，降级为默认", bubble_key)
        with _lock: _VALIDATE_DEGRADED += 1
        bubble_key = _DEFAULT_BUBBLE

    bubble = _BUBBLES[bubble_key]
    return BubbleResult(
        changed=True,
        bubble_key=bubble_key,
        reason=f"切换到{bubble.name}（{bubble.style}）",
    )


# ── 查询 ──────────────────────────────────────────
def get_bubble_info(bubble_key: str) -> BubbleDef | None:
    return _BUBBLES.get(bubble_key)


def get_all_bubbles() -> dict[str, BubbleDef]:
    return deepcopy(_BUBBLES)


def get_default_bubble() -> str:
    return _DEFAULT_BUBBLE
