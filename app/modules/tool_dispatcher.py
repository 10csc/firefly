# -*- coding: utf-8 -*-
"""工具调度器 — 规划器 tools 数组 → 实际工具调用 → 结果描述

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出

不调 LLM。pre_dispatch 执行工具、生成 tools_summary。标记替换（[sticker]→图片消息）由 composer 负责。
"""

import logging
import threading
from dataclasses import dataclass

from tools.bubble_updater import apply_bubble, BubbleResult
from tools.sticker_picker import pick_sticker_by_meaning, StickerEntry

logger = logging.getLogger(__name__)

# ── 异常 ──────────────────────────────────────────
class DispatcherError(Exception): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class PreResult:
    bubble: BubbleResult | None
    tools_summary: str
    picked_sticker: StickerEntry | None

    @property
    def bubble_key(self) -> str | None:
        if self.bubble and self.bubble.changed:
            return self.bubble.bubble_key
        return None


# ── 监控 ──────────────────────────────────────────
_lock = threading.Lock()
_PRE_COUNT = 0
_EXEC_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {"pre_count": _PRE_COUNT, "exec_errors": _EXEC_ERRORS}


# ── Bubble 子代理 ─────────────────────────────────
# 从 bubble_updater 读取气泡定义，按 keywords 匹配规划器的 suggestion

def _match_bubble(suggestion: str) -> str | None:
    """根据规划器的建议文字，从 bubble_updater 单一数据源匹配气泡 key。"""
    from tools.bubble_updater import get_all_bubbles

    bubbles = get_all_bubbles()
    s = suggestion.lower()
    best_key, best_score = None, 0
    for key, b in bubbles.items():
        kw_list = b.keywords if b.keywords else []
        score = sum(1 for kw in kw_list if kw in s)
        if score > best_score:
            best_score = score
            best_key = key
    return best_key


# ── Sticker 子代理 ────────────────────────────────
# 规划器 suggestion（"想表达的意思"，如"害羞"/"安慰他"/"比心"）→ pick_sticker_by_meaning 按 label 语义匹配


# ── pre_dispatch ──────────────────────────────────
def pre_dispatch(tools: list[dict]) -> PreResult:
    """执行规划器要求的工具，生成 tools_summary。

    tools: [{"tool":"bubble","suggestion":"科幻风格"}, {"tool":"sticker","suggestion":"害羞"}]
    sticker：suggestion 作为"想表达的意思"直接交给 pick_sticker_by_meaning 按 label 语义匹配。
    """
    global _PRE_COUNT, _EXEC_ERRORS
    with _lock:
        _PRE_COUNT += 1

    if not isinstance(tools, list):
        tools = []

    summaries = []
    bubble_result = None
    picked_sticker = None

    for item in tools:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool", "")
        suggestion = item.get("suggestion", "")

        if tool == "bubble":
            key = _match_bubble(suggestion)
            if key:
                bubble_result = apply_bubble(key, triggered=True)
                if bubble_result.changed:
                    summaries.append(f"已切换聊天气泡——{suggestion}风格。")
            else:
                with _lock:
                    _EXEC_ERRORS += 1
                logger.warning("bubble suggestion='%s' 无匹配", suggestion)

        elif tool == "sticker":
            meaning = suggestion
            picked_sticker = pick_sticker_by_meaning(meaning)
            if picked_sticker:
                summaries.append(
                    f"已选表情包：{picked_sticker.label}（表示{meaning}）。"
                    f"回复中在合适位置插入 [sticker] 标记来发送。"
                )
            else:
                summaries.append("想发表情包但当前没有合适的。")

    tools_summary = "\n".join(summaries) if summaries else ""

    return PreResult(
        bubble=bubble_result,
        tools_summary=tools_summary,
        picked_sticker=picked_sticker,
    )
