# -*- coding: utf-8 -*-
"""状态更新器 #1 — 关系/紧张/主动性的数值变化

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
透传 JudgeResult + 更新后的 state，供规划器消费。
mood 由 MoodUpdater 并行处理，本模块不涉及。
"""

import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

from .planning_judge import JudgeResult

logger = logging.getLogger(__name__)


# ── 常量 ──────────────────────────────────────────
_REQUIRED_DELTA_KEYS = frozenset({"affection_delta", "tension_delta"})

# ── 异常 ──────────────────────────────────────────
class StateUpdaterError(Exception):
    """状态更新器异常基类"""
    pass


class InputRejected(StateUpdaterError):
    """审查阶段：输入不合法"""
    pass


class OutputInvalid(StateUpdaterError):
    """验证阶段：LLM 输出不合法"""
    pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class StateResult:
    """状态更新结果 — 透传 JudgeResult + 原 state（未应用 delta）+ energy + 原始 delta。
    state 字段为 prev_state 副本，delta 尚未应用——由 finalize() 收口合成最终 state。
    mood 由 MoodUpdater 并行更新后由 orchestrator 合并，本模块不涉及。
    """
    result_mode: str
    stopped_at: int
    stop_reason: str
    reply_direct: Optional[dict] = None
    execution_plan: Optional[dict] = None
    state: dict = field(default_factory=dict)  # 透传 prev_state（delta 未应用）
    energy: int = 300
    affection_delta: float = 0.0   # 原始 delta，供 finalize 使用
    tension_delta: float = 0.0


# ── 默认状态 ──────────────────────────────────────
_DEFAULT_STATE = {
    "mood": [{"label": "安心", "intensity": 3}],
    "affection": 80.0,
    "tension": 15.0,
    "initiative": 50.0,
}


# ── 状态更新提示词 ────────────────────────────────
_STATE_UPDATER_PROMPT = """你是一个状态更新器。判断用户输入对流萤的**好感度**和**紧张度**产生什么影响。
只输出一行JSON，禁止任何其他内容。

## 流萤（仅用于判断触发类型）
格拉默铁骑，身患失熵症（寿命有限）。渴望被当作"人"。

## 好感度变化（affection_delta，范围 -0.8 ~ +0.3）

### 正面（+）
真诚关心(你还好吗/别太累): +0.2 ~ +0.3
日常温暖(闲聊/分享日常): +0.1 ~ +0.2
道歉(真诚): +0.1 ~ +0.2
浪漫/表白: +0.1 ~ +0.2
分享心事/脆弱面: +0.2 ~ +0.3
客观评价/无感: 0.0

### 负面（-）
敷衍/冷处理: -0.1
轻度辱骂(逃兵等不了解事实的攻击): -0.1
中度辱骂(被当AI/被迫谈死亡): -0.1 ~ -0.2
重度辱骂(怪物/兵器/消耗品/工具): -0.3 ~ -0.5
极深创伤(短命鬼/活不长/病秧子): -0.5 ~ -0.8

## 紧张度变化（tension_delta，范围 -5 ~ +25）

紧张仅表示互动中的"心跳感"——恋爱话题、一起做事的提议、被关心时的悸动。
**重要：辱骂/攻击不影响紧张度，那属于心情范畴。**

### 紧张上升（+）
被表白/被热烈夸赞: +3 ~ +8
主动表白/主动表达感情: +2 ~ +5
"一起做什么"的提议: +1 ~ +3
害羞/尴尬话题: +2 ~ +5
紧急求救: +15 ~ +25（十位数级，紧急情况可大幅上升）

### 紧张下降（-）
日常闲聊: -1 ~ -3
关心安抚: -2 ~ -5
道歉（缓和气氛）: -2 ~ -5
轻松玩笑: -1 ~ -2

### 不影响紧张
辱骂/攻击/冷处理 → tension_delta = 0（这些触发"低落"心情，不触发紧张）

## 约束
affection_delta: -0.8 ~ +0.3
tension_delta: -5 ~ +25

当前状态={current_state}
判定={stopped_at}/{stop_reason}

你必须输出。只输出一行JSON。
{{"affection_delta": 0, "tension_delta": 0}}"""


# ── 核心类 ────────────────────────────────────────
class StateUpdater:
    """状态更新器 — 好感度/紧张度的数值变化。主动性不由此模块处理。"""

    def __init__(self, client, model: str = "deepseek-v4-flash"):
        if client is None:
            raise InputRejected("client 不能为 None")
        if not model:
            raise InputRejected("model 不能为空")
        self._client = client
        self._model = model

    def update(
        self,
        user_input: str,
        judge_result: JudgeResult,
        current_state: dict,
        energy: int,
    ) -> StateResult:
        """主入口。流程：审查 → LLM判定 → 验证 → 输出。
        仅产出 raw delta，不应用 delta 到 state（由 finalize() 收口）。
        mood/initiative 透传不修改。返回原始 delta 供 finalize 使用。
        """
        # 1. 审查阶段
        if not isinstance(user_input, str):
            raise InputRejected(f"user_input 必须为 str，实际: {type(user_input).__name__}")
        if not isinstance(judge_result, JudgeResult):
            raise InputRejected(f"judge_result 必须为 JudgeResult，实际: {type(judge_result).__name__}")
        if not isinstance(current_state, dict):
            raise InputRejected(f"current_state 必须为 dict，实际: {type(current_state).__name__}")
        if not isinstance(energy, int) or energy < 0:
            raise InputRejected(f"energy 必须为非负整数，实际: {energy}")
        for key in ("affection", "tension", "initiative"):
            if key not in current_state:
                raise InputRejected(f"current_state 缺少字段: {key}")

        # 2. LLM 判定（仅产出好感/紧张 raw delta）
        affection_delta = 0.0
        tension_delta = 0.0
        try:
            deltas = self._call_llm(user_input, judge_result, current_state)
            self._validate_deltas(deltas)
            affection_delta = deltas["affection_delta"]
            tension_delta = deltas["tension_delta"]
        except (json.JSONDecodeError, OutputInvalid) as e:
            logger.error("状态更新器 LLM 输出异常，delta 归零: %s", e)
        except InputRejected:
            raise
        except Exception as e:
            logger.error("状态更新器 API 调用失败，delta 归零: %s", e)

        # 3. 输出 —— state 透传 prev（delta 未应用），由 finalize() 合成
        return StateResult(
            result_mode=judge_result.result_mode,
            stopped_at=judge_result.stopped_at,
            stop_reason=judge_result.stop_reason,
            reply_direct=judge_result.reply_direct,
            execution_plan=judge_result.execution_plan,
            state=deepcopy(current_state),
            energy=energy,
            affection_delta=affection_delta,
            tension_delta=tension_delta,
        )

    # ── 私有方法 ─────────────────────────────────
    def _call_llm(self, user_input: str, judge_result: JudgeResult, current_state: dict) -> dict:
        state_json = json.dumps(current_state, ensure_ascii=False)
        prompt = _STATE_UPDATER_PROMPT.format(
            current_state=state_json,
            stopped_at=judge_result.stopped_at,
            stop_reason=judge_result.stop_reason,
        )

        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"用户输入: {user_input}"},
            ],
            max_tokens=500,
            temperature=0.0,
        )
        if not resp.choices:
            raise OutputInvalid("API 返回空 choices")
        content = resp.choices[0].message.content
        if content is None:
            raise OutputInvalid("API 返回 content 为 None")
        return self._parse_llm_json(content)

    def _parse_llm_json(self, raw: str) -> dict:
        if not raw or not raw.strip():
            raise OutputInvalid("LLM 返回空内容")
        raw = raw.strip()
        start = raw.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == "{":
                    depth += 1
                elif raw[i] == "}":
                    depth -= 1
                    if depth == 0:
                        raw = raw[start : i + 1]
                        break
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise OutputInvalid(f"解析结果不是 dict: {type(result).__name__}")
        return result

    def _validate_deltas(self, deltas: dict):
        missing = _REQUIRED_DELTA_KEYS - deltas.keys()
        if missing:
            raise OutputInvalid(f"deltas 缺少字段: {missing}")
        for key in ("affection_delta", "tension_delta"):
            val = deltas.get(key)
            if not isinstance(val, (int, float)):
                raise OutputInvalid(f"{key} 不是数字: {type(val).__name__} = {val}")
        if not (-1.0 <= deltas["affection_delta"] <= 1.0):
            raise OutputInvalid(f"affection_delta 超出合理范围: {deltas['affection_delta']}")
        if not (-10 <= deltas["tension_delta"] <= 30):
            raise OutputInvalid(f"tension_delta 超出合理范围: {deltas['tension_delta']}")


# ── finalize：raw delta → 最终 state 合成（纯代码，由 orchestrator 调用）──

def finalize(
    prev_state: dict,
    affection_delta: float,
    tension_delta: float,
    rates: dict,
    user_input: str,
) -> dict:
    """收口 raw delta → 最终 state 的全部合成。

    流程：审查 → 倍率应用 → 紧张自动消退 → 主动性培养 → 钳位验证 → 输出
    由 orchestrator 在 compute_rates 后调用。mood 不由此处理（由 merge_moods 覆盖）。
    """
    if not isinstance(prev_state, dict):
        raise InputRejected(f"prev_state 必须为 dict，实际: {type(prev_state).__name__}")
    for key in ("affection", "tension", "initiative"):
        if key not in prev_state:
            raise InputRejected(f"prev_state 缺少字段: {key}")

    # 取 prev 值
    prev_affection = float(prev_state.get("affection", 80.0))
    prev_tension = float(prev_state.get("tension", 15.0))
    prev_initiative = float(prev_state.get("initiative", 50.0))

    # 倍率应用
    aff_rate = float(rates.get("affection", 1.0))
    ten_rate = float(rates.get("tension", 1.0))
    ini_rate = float(rates.get("initiative", 1.0))

    new_affection = prev_affection + affection_delta * aff_rate
    new_tension = prev_tension + tension_delta * ten_rate

    # 紧张自动消退（每轮 -1.5）
    new_tension = max(0.0, new_tension - 1.5)

    # initiative 剥离为不变属性，不再动态计算（保持恒等于初始值 50.0）
    new_initiative = prev_initiative

    # 钳位
    new_affection = max(0.0, min(100.0, new_affection))
    if new_affection >= 100.0:
        logger.info("affection 触及上限 %.1f -> 100", new_affection)
    new_tension = max(0.0, new_tension)  # 不设上限——短期数值
    new_initiative = max(0.0, min(100.0, new_initiative))

    return {
        "affection": new_affection,
        "tension": new_tension,
        "initiative": new_initiative,
    }
