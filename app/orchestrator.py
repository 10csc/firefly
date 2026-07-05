# -*- coding: utf-8 -*-
"""编排器 — 对话流水线总调度

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出

当前流程（简化版）：
  1. judge → 2. direct 路径 → 3. planner → 4. pre_dispatch → 5. reply
  → 6. refiner → 7. composer → 8. record

已跳过状态系统（Adder/Decayer/StateUpdater/merge/finalize/decode）。
等"像流萤"之后再接回。
"""

import logging
import threading
from dataclasses import dataclass, field

from modules.planning_judge import PlanningJudge, JudgeResult
from modules.context_manager import ContextManager
from modules.planner import Planner, PlannerInput, TOOLS_MENU
from modules.tool_dispatcher import pre_dispatch
from modules.reply_generator import ReplyGenerator, ReplyInput
from modules.composer import Composer, ComposerInput
from modules.refiner import Refiner

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────
@dataclass
class ChatResult:
    messages: list[dict] = field(default_factory=list)
    bubble: str | None = None
    state: dict = field(default_factory=dict)


# ── direct 路径静态话术 ─────────────────────────────
_DIRECT_REPLIES = {
    "input:empty":    ["嗯…怎么啦？想说什么就说吧"],
    "input:too_long": ["你说了好多…我慢慢看，等一下哦"],
    "api:error":      ["嗯…信号好像不太好，你等一下哦"],
}


def _handle_direct(result: JudgeResult) -> list[str]:
    return _DIRECT_REPLIES.get(result.stop_reason, ["嗯？我走神了…你刚才说了什么？"])


# ── 编排监控 ────────────────────────────────────────
_lock = threading.Lock()
_CHAT_COUNT = 0
_DIRECT_COUNT = 0
_ORCH_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {"chat_count": _CHAT_COUNT, "direct_count": _DIRECT_COUNT, "orch_errors": _ORCH_ERRORS}


# ── 主入口 ──────────────────────────────────────────
def handle_chat(
    user_input: str,
    session: dict,
    client,
    model: str = "deepseek-v4-flash",
    memory_head: str = "",
    reply_model: str = None,
    reply_effort: str = "high",
    reply_temperature: float = 0.5,
) -> ChatResult:
    """编排整条对话流水线（简化版，跳过状态系统）。

    Args:
        user_input: 用户消息原文
        session: {"context": ContextManager, "state": dict, "memory_head": str}
        client: OpenAI client 实例
        model: 默认模型名（judge/planner/composer/refiner 用）
        memory_head: 会话记忆头部，注入 reply 稳定层
        reply_model: 回复器模型（前端可配）
        reply_effort: 回复器思考等级
        reply_temperature: 回复器温度

    Returns:
        ChatResult: messages + bubble + state
    """
    global _CHAT_COUNT, _DIRECT_COUNT, _ORCH_ERRORS
    with _lock:
        _CHAT_COUNT += 1

    ctx: ContextManager = session["context"]
    prev_state = session.get("state", {})

    # ── 1. 规划判断器 ──────────────────────────────
    judge = PlanningJudge(client=client, model=model)
    result = judge.judge(
        user_input,
        session_context={
            "violation_history": session.get("violation_history", False),
            "state": prev_state,
            "recent_history": ctx.get_full(),
        },
    )

    # 违规记录
    if result.stopped_at == 0 and "violation:sexual" in result.stop_reason:
        session["violation_history"] = True

    # ── 2. direct 路径 ─────────────────────────────
    if result.result_mode == "direct":
        with _lock:
            _DIRECT_COUNT += 1
        messages = [{"type": "text", "content": m} for m in _handle_direct(result)]
        return ChatResult(messages=messages, bubble=None, state=prev_state)

    # ── 3. 规划器（状态相关输入用静态默认值）─────────
    planner = Planner(client, model=model)
    plan = planner.plan(PlannerInput(
        decoded_summary="流萤在星核猎手飞船医疗舱中，心情平稳。",
        recent_history=ctx.get_full(),
        tools_menu=TOOLS_MENU,
        user_input=user_input,
        stop_reason=result.stop_reason,
        sticker_frequency="偶尔",
        sticker_style="可爱",
    ))

    # ── 4. 前置调度：工具执行 ────────────────────────
    pre = pre_dispatch(tools=plan.tools)

    # ── 5. 回复生成（状态相关输入用静态默认值）────────
    reply_gen = ReplyGenerator(
        client, model=reply_model or model,
        effort=reply_effort, temperature=reply_temperature,
    )
    reply_out = reply_gen.generate(ReplyInput(
        tone=plan.tone,
        direction=plan.direction,
        recent_history=ctx.get_full(),
        user_input=user_input,
        tools_summary=pre.tools_summary,
    ), memory_head=memory_head)

    raw_reply = reply_out.raw

    # ── 6. 后向微调：检查硬事实错误 ──────────────────
    try:
        refiner = Refiner(client, model=model)
        refined = refiner.refine(raw_reply)
        if refined and refined.strip():
            raw_reply = refined.strip()
    except Exception as e:
        logger.warning("Refiner 异常，降级原回复: %s", e)
        with _lock:
            _ORCH_ERRORS += 1

    # ── 7. 消息编排：分句 + 插表情包 ────────────────
    composer = Composer(client, model=model)
    composed = composer.compose(ComposerInput(
        raw_text=raw_reply,
        tone=plan.tone,
        sticker=pre.picked_sticker,
    ))

    # ── 8. 记录历史 ────────────────────────────────
    if result.stop_reason not in ("input:empty", "api:error"):
        texts = [m["content"] for m in composed.messages if m.get("type") == "text"]
        reply_text = " ".join(texts) if texts else ""
        ctx.add_turn(user_input, reply_text)
        for m in composed.messages:
            if m.get("type") == "sticker":
                ctx.add_action("表情包", m.get("label", "表情"))
        if pre.bubble and pre.bubble.changed:
            ctx.add_action("气泡切换", pre.bubble.bubble_key)

    bubble = pre.bubble.bubble_key if (pre.bubble and pre.bubble.changed) else None
    return ChatResult(
        messages=composed.messages,
        bubble=bubble,
        state=prev_state,
    )
