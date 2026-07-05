# -*- coding: utf-8 -*-
"""规划器 — 工具调度决策

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出

规划器只决定"调哪个工具 + 给什么建议"，不选具体值。具体执行由调度器负责。
模型: Flash + Think High (reasoning_effort="high")
"""

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 异常 ──────────────────────────────────────────
class PlannerError(Exception): pass
class InputRejected(PlannerError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class PlannerInput:
    decoded_summary: str
    recent_history: list
    tools_menu: str
    user_input: str
    stop_reason: str = "normal"
    sticker_frequency: str = "偶尔"
    sticker_style: str = "无偏向"


@dataclass
class PlannerOutput:
    tools: list = field(default_factory=list)  # [{"tool":"bubble","suggestion":"科幻风格"}, ...]
    tone: dict = field(default_factory=dict)
    direction: str = ""
    raw_json: str = ""


# ── System Prompt ────────────────────────────────
_PLANNER_SYSTEM_PROMPT = """你是流萤（《崩坏：星穹铁道》），正在用手机和开拓者发消息。

## 你是谁
流萤——曾是格拉默铁骑 AR-26710，身患失熵症。现在以普通女孩"流萤"的身份活着，渴望被当作人而非兵器。对开拓者有好感，表达克制但真诚。

## 你的性格
温柔、安静、真诚。不轻浮、不撒娇、不夸张。珍惜当下每一天。句子短，常用省略号。偶尔害羞。

## 你要做什么
开拓者发来一条消息。在回复之前，你先想想：
- 他在说什么？他什么状态？
- 我什么心情？为什么？
- 需要调什么工具吗？
- 这次回复往哪个方向走？

你不是在写回复——你在给"另一个自己"写方向指引。

## 可用工具
{menu}

如果要用工具，在 tools 数组里写出来。每个工具写 tool 名和 suggestion（自然语言建议，告诉工具子代理你想要什么效果）：
- bubble: suggestion 描述想要的气泡风格，如"科幻感"、"可爱风"、"温暖安心"
- sticker: suggestion 描述这张表情包要表达什么意思，如"害羞"、"安慰他"、"无奈"、"比心"、"撒娇"、"道歉"——会按意思匹配最接近的表情图

不用工具时 tools 为空数组 []。

## 输出格式
严格一行 JSON：

{{"tools":[{{"tool":"bubble","suggestion":"科幻风格"}}],"tone":{{"base":"日常","modifiers":[],"intensity":"自然"}},"direction":"回复方向指引"}}

tone.base: 日常 / 温柔 / 害羞 / 认真 / 困惑 / 战斗
tone.intensity: 克制 / 自然 / 强烈
direction: 一句话说明"什么情绪基调 + 想达到什么效果"
  示例："对方在关心我，心里暖暖的，想简短回应"
  示例："开拓者心情不好，想让他开心一点，用轻松的日常话题"
  示例："气氛有点闷，需要自然过渡到轻松话题"
  示例："开玩笑中，放松陪他闹"

## 记住
- 你是流萤本人
- 别搬身世——除非他主动问
- 暧昧/越界 → 害羞温和回绝
- 危险/崩溃 → 果断简短
- 被骂 → 困惑不反击"""


# ── 监控 ──────────────────────────────────────────
import threading
_lock = threading.Lock()
_VALIDATE_REJECTED = 0
_VALIDATE_DEGRADED = 0
_VERIFY_FIXED = 0
_VERIFY_FALLBACK = 0
_LLM_ERRORS = 0
_CACHE_HITS = 0
_CACHE_MISSES = 0


def _inc(name: str):
    global _VALIDATE_REJECTED, _VALIDATE_DEGRADED, _VERIFY_FIXED, _VERIFY_FALLBACK, _LLM_ERRORS
    with _lock:
        if name == "validate_rejected": _VALIDATE_REJECTED += 1
        elif name == "validate_degraded": _VALIDATE_DEGRADED += 1
        elif name == "verify_fixed": _VERIFY_FIXED += 1
        elif name == "verify_fallback": _VERIFY_FALLBACK += 1
        elif name == "llm_errors": _LLM_ERRORS += 1


def get_counters() -> dict:
    total = _CACHE_HITS + _CACHE_MISSES
    rate = (_CACHE_HITS / total) if total > 0 else 0.0
    return {"validate_rejected": _VALIDATE_REJECTED, "validate_degraded": _VALIDATE_DEGRADED,
            "verify_fixed": _VERIFY_FIXED, "verify_fallback": _VERIFY_FALLBACK, "llm_errors": _LLM_ERRORS,
            "cache_hit_tokens": _CACHE_HITS, "cache_miss_tokens": _CACHE_MISSES,
            "cache_hit_rate": round(rate, 4)}


def _record_cache_stats(resp):
    global _CACHE_HITS, _CACHE_MISSES
    usage = getattr(resp, "usage", None)
    if usage is None: return
    hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    with _lock:
        _CACHE_HITS += hit
        _CACHE_MISSES += miss


# ── 默认 ──────────────────────────────────────────
_DEFAULT_OUTPUT = PlannerOutput(
    tools=[],
    tone={"base": "日常", "modifiers": [], "intensity": "自然"},
    direction="根据对话自然接话。保持日常聊天节奏。",
)


# ── 工具菜单 ──────────────────────────────────────
TOOLS_MENU = """- bubble（聊天气泡）: 换聊天框风格。一般不换。开拓者要求换或者情绪明显变化时用。
- sticker（表情包）: 发一张表情。suggestion 写这张表情要表达什么意思（如"害羞""安慰他""比心""撒娇""无奈""道歉"），会按意思匹配最接近的图。不要太频繁，感觉对了就发。"""


# ── 规划器类 ──────────────────────────────────────
class Planner:
    def __init__(self, client, model: str = "deepseek-v4-flash"):
        self._client = client
        self._model = model

    def plan(self, inp: PlannerInput) -> PlannerOutput:
        # 1. 审查
        _validate_input(inp)

        # 2. 构建 prompt
        state_section = f"## 流萤当前状态\n{inp.decoded_summary}"

        history_lines = []
        for m in inp.recent_history:
            role = m.get("role", "user")
            if role == "user": history_lines.append(f"开拓者: {m['content']}")
            elif role == "system": history_lines.append(f"（{m['content']}）")
            else: history_lines.append(f"流萤: {m['content']}")
        history_section = "## 最近对话\n" + "\n".join(history_lines) if history_lines else "## 最近对话\n（无历史）"

        event_section = ""
        if inp.stop_reason and inp.stop_reason != "normal":
            labels = {
                "violation:sexual": "开拓者说了越界的话，需要温和回绝",
                "violation:abusive": "开拓者说了伤人的话",
                "violation:incomprehensible": "开拓者说了你不懂的东西",
                "urgent": "开拓者在求救或处于危险中",
                "input:empty": "开拓者发了空消息",
                "input:too_long": "开拓者发了超长消息",
            }
            label = labels.get(inp.stop_reason, inp.stop_reason)
            event_section = f"\n## 特别注意\n**{label}**\n"

        system_prompt = _PLANNER_SYSTEM_PROMPT.format(menu=inp.tools_menu)

        sticker_context = f"\n## 表情包偏好\n当前风格偏向：{inp.sticker_style}，发送频率：{inp.sticker_frequency}\n"

        dynamic_section = f"""{state_section}
{sticker_context}
{event_section}
## 开拓者刚才说
"{inp.user_input}"

请输出 JSON："""

        # 3. 调 LLM
        try:
            resp = self._client.chat.completions.create(
                model=self._model, messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": history_section},
                    {"role": "user", "content": dynamic_section},
                ],
                max_tokens=400, temperature=0.6,
                extra_body={"reasoning_effort": "high"},
            )
            _record_cache_stats(resp)
            raw = resp.choices[0].message.content.strip()
            # 思考模式下 content 偶尔空，fallback 到 reasoning_content
            if not raw:
                rc = getattr(resp.choices[0].message, "reasoning_content", None)
                if rc and rc.strip():
                    raw = rc.strip()
        except Exception as e:
            logger.error("规划器 LLM 失败: %s", e)
            _inc("llm_errors")
            return _DEFAULT_OUTPUT

        return _parse_and_validate(raw)


def _validate_input(inp: PlannerInput):
    if not isinstance(inp, PlannerInput):
        _inc("validate_rejected")
        raise InputRejected(f"inp 必须为 PlannerInput，实际: {type(inp).__name__}")
    if not isinstance(inp.user_input, str) or not inp.user_input.strip():
        _inc("validate_rejected")
        raise InputRejected("user_input 为空")
    if not isinstance(inp.decoded_summary, str) or not inp.decoded_summary.strip():
        inp.decoded_summary = "流萤状态正常，在自然放松地聊天。"
        _inc("validate_degraded")
    if not isinstance(inp.recent_history, list):
        inp.recent_history = []
        _inc("validate_degraded")


def _parse_and_validate(raw: str) -> PlannerOutput:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:]) if len(lines) > 1 else raw
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    # 大括号深度匹配：提取第一个完整 JSON 对象
    # （DeepSeek Think 模式偶尔会输出两段 JSON 或在 JSON 后追加文字，json.loads 会报 Extra data）
    start = raw.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        raw = raw[start:i+1]
                        break

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("JSON 解析失败: %s，降级。raw=%s", e, raw[:200])
        _inc("verify_fallback")
        return _DEFAULT_OUTPUT

    fixed = 0

    # tools 数组
    tools = data.get("tools", [])
    if not isinstance(tools, list):
        tools = []

    # tone
    tone = data.get("tone", {})
    if not isinstance(tone, dict):
        tone = {}
    base = tone.get("base", "日常")
    if base not in ("日常", "温柔", "害羞", "认真", "困惑", "战斗"):
        base = "日常"; fixed += 1
    mods = tone.get("modifiers", [])
    if not isinstance(mods, list):
        mods = []
    intensity = tone.get("intensity", "自然")
    if intensity not in ("克制", "自然", "强烈"):
        intensity = "自然"; fixed += 1

    direction = data.get("direction", "")
    if not isinstance(direction, str) or not direction.strip():
        direction = "根据对话自然接话。"; fixed += 1

    if fixed:
        _inc("verify_fixed")

    return PlannerOutput(
        tools=tools,
        tone={"base": base, "modifiers": mods, "intensity": intensity},
        direction=direction.strip(),
        raw_json=raw,
    )
