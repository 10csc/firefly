# -*- coding: utf-8 -*-
"""组织器 — 模式化工具调度

story 模式：表情包调度器（看流萤刚发出的消息，决定配不配表情包、配哪张）
haruno 模式：旁白生成器（视觉小说式 RP——环境/动作描写，居中小字演出）

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
模型: story=Flash Non-think（简单分类）；haruno=Think High（创作任务）
"""

import logging, threading
from dataclasses import dataclass, field

from modules.llm_base import record_usage, record_error, parse_json

logger = logging.getLogger(__name__)
_lock = threading.Lock()


# ── 异常 ──────────────────────────────────────────
class OrganizerError(Exception): pass
class InputRejected(OrganizerError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class OrganizerInput:
    user_input: str                  # 开拓者刚才说的话
    reply_texts: list = field(default_factory=list)  # 流萤刚生成的回复文本（条列表）
    recent_history: list = field(default_factory=list)  # 最近几轮（含行为记录，供频率感知）
    mode: str = "story"


@dataclass
class OrganizerOutput:
    sticker_label: str = ""          # story：选中的表情包 label，"" = 不发
    narrations: list = field(default_factory=list)  # haruno：[{"text","style"}] style=scene|action
    raw_json: str = ""
    reasoning: str = ""              # 调试观测用


# ── 监控 ──────────────────────────────────────────
_ORGANIZE_COUNT = 0
_LLM_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {
            "organize_count": _ORGANIZE_COUNT,
            "llm_errors": _LLM_ERRORS,
        }


# ── Prompt ────────────────────────────────────────
_ORGANIZER_SYSTEM = """你是流萤的表情包助手。流萤刚打完一串短信，你帮她顺手挑一张表情包。

## 流萤的表情包习惯
- 她挺爱发表情包的：情绪明显的回复（开心/害羞/无奈/委屈/道歉/调侃/安慰）基本都会配一张
- 日常闲聊大约每 2-3 轮配一张；纯信息性回复、沉重话题可以不配
- 最近几轮已经连续发过表情包的话，这轮歇一歇
- 表情包放在文字后面，是语气的延伸——选和这串消息情绪最贴的那张

## 可用表情包（按含义选，输出 label 原文）
{sticker_labels}

## 输出格式（一行 JSON，禁止任何其他文字）
{{"sticker":"label原文"}} 或 {{"sticker":"无"}}"""


def _build_sticker_list() -> str:
    """从注册表构建 label 清单（按分类分组）。"""
    from tools.sticker_picker import get_all_stickers
    stickers = get_all_stickers()
    by_cat: dict[str, list[str]] = {}
    for s in stickers.values():
        by_cat.setdefault(s.category, []).append(s.label)
    lines = []
    for cat, labels in sorted(by_cat.items()):
        lines.append(f"- {cat}系: {'、'.join(sorted(set(labels)))}")
    return "\n".join(lines) if lines else "（无可用表情包）"


# ── haruno 模式：旁白生成器（视觉小说式 RP 演出）──
_NARRATION_SYSTEM = """你是流萤的故事演出助手。流萤刚发完一段话（可能分多条），你为这段对话配上环境/动作描写（旁白）。

## 你的产出是什么
旁白是"第三者视角"的演出文字，补充流萤的动作、神态和环境氛围。两种类型：
- scene：环境/事件描写（居中小字，无括号）——如"就在这时，一位少女挺身而出打跑了他们。"
- action：动作/神态描写（前端会自动加括号，**你输出的文本不要带括号**）——如"少女在仔细观察你"

## 旁白的位置（after 字段，关键）
流萤的话是分条发的，旁白可以插在任意两条消息之间。after 指定"插在第几条消息之后"：
- after=-1：放在所有消息**之前**（开场环境交代、大动作）——如"黄金时刻，霓虹初上"、少女冲上前
- after=0：插在**第 1 条**消息之后
- after=1：插在**第 2 条**消息之后
- 以此类推，after=n 表示插在 reply_texts 第 n+1 条之后
- 每个 after 至多一条旁白；不穿插时全用 -1

## 旁白写作规则
- 从流萤的视角出发描写她：她的动作、神态、看向开拓者的目光、周围的环境变化
- 动作要具体、克制：一个眼神、一次停顿、手指捏紧衣角——不要大段抒情
- 环境描写只在氛围确实变化时出现（人流、灯光、风），不要每轮都写
- 字数：每条 10-40 字。一条消息一个动作，不要堆砌
- 频率：**不是每轮都必须有**。纯对话轮（就是聊天）不配旁白；只有流萤有明显动作/神态/环境变化时才写
- 典型搭配：她说话的同时做了什么（说话前接、说话时做）、她听开拓者说话时的反应、她注意到的东西
- 分条节奏：流萤连发多条时，把最生动的动作插在两条之间（先做动作再发下一条），比全部堆前面更自然

## 绝对禁止
- 禁止写开拓者的动作和心理（那是用户的事，不代写）
- 禁止旁白里出现"流萤说/流萤问"（对话本身就是消息，旁白只写动作和环境）
- 禁止把短信内容复述进旁白
- 禁止每轮都输出旁白——空数组是常态，有内容才写

## 输出格式（一行 JSON，禁止任何其他文字）
{{"narrations":[{{"text":"旁白内容","style":"scene","after":-1}}]}}
没有旁白时：{{"narrations":[]}}"""


# ── 组织器类 ──────────────────────────────────────
class Organizer:
    def __init__(self, client, model: str = "deepseek-v4-flash", effort: str = "none",
                 mode: str = "story"):
        self._client = client
        self._model = model
        self._mode = mode
        # 思考模式：effort=none 显式关闭（默认，温度生效）；其他档位思考模式
        self._thinking = effort != "none"
        effort_map = {"low": "high", "high": "high", "max": "max"}
        self._effort = effort_map.get(effort, "high")

    def organize(self, inp: OrganizerInput) -> OrganizerOutput:
        if inp.mode != "story":
            return self._organize_narration(inp)
        return self._organize_sticker(inp)

    def _organize_sticker(self, inp: OrganizerInput) -> OrganizerOutput:
        global _ORGANIZE_COUNT, _LLM_ERRORS

        # 1. 审查
        _validate_input(inp)

        with _lock:
            _ORGANIZE_COUNT += 1

        # 2. 构建 prompt
        stable = _ORGANIZER_SYSTEM.format(sticker_labels=_build_sticker_list())

        recent_lines = []
        for m in inp.recent_history:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user":
                recent_lines.append(f"开拓者: {content}")
            elif role == "assistant":
                recent_lines.append(f"流萤: {content}")
            elif role == "system":
                recent_lines.append(content)  # [行为: 表情包] xx —— 频率感知
        recent_section = "\n".join(recent_lines) if recent_lines else "（无）"

        reply_section = "\n".join(f"- {t}" for t in inp.reply_texts)

        dynamic = (
            f"## 最近几轮（注意里面的表情包记录，控制频率）\n{recent_section}\n\n"
            f"## 开拓者刚才说\n{inp.user_input}\n\n"
            f"## 流萤刚打完的短信\n{reply_section}\n\n"
            "请输出 JSON："
        )

        # 3. 调 LLM
        # 官方文档：thinking 默认 enabled，必须显式 disabled 才是非思考模式。
        # 非思考模式下 temperature 生效、思考链不会吃掉 max_tokens。
        # response_format 强制 JSON 输出。
        try:
            if self._thinking:
                extra = {"thinking": {"type": "enabled"}, "reasoning_effort": self._effort}
            else:
                extra = {"thinking": {"type": "disabled"}}
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": stable},
                    {"role": "user", "content": dynamic},
                ],
                max_tokens=10000, temperature=0.3,
                response_format={"type": "json_object"},
                extra_body=extra,
            )
            record_usage("organizer", resp)
            raw = resp.choices[0].message.content.strip()
            rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
            if not raw and rc:
                raw = _extract_json(rc)
            if not raw and rc:
                raw = rc
        except Exception as e:
            logger.error("组织器 LLM 失败: %s", e)
            with _lock:
                _LLM_ERRORS += 1
            record_error("organizer", self._model, str(e))
            return OrganizerOutput()  # 降级：不发表情包

        out = _parse_and_validate(raw)
        out.reasoning = rc
        return out

    def _organize_narration(self, inp: OrganizerInput) -> OrganizerOutput:
        global _ORGANIZE_COUNT, _LLM_ERRORS

        # 1. 审查
        _validate_input(inp)

        with _lock:
            _ORGANIZE_COUNT += 1

        # 2. 构建 prompt
        stable = _NARRATION_SYSTEM

        recent_lines = []
        for m in inp.recent_history:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user":
                recent_lines.append(f"开拓者: {content}")
            elif role == "assistant":
                recent_lines.append(f"流萤: {content}")
            elif role == "system":
                recent_lines.append(content)  # 旁白/行为记录
        recent_section = "\n".join(recent_lines) if recent_lines else "（无）"

        reply_section = "\n".join(f"- {t}" for t in inp.reply_texts)

        dynamic = (
            f"## 最近几轮（含之前的旁白记录，避免重复描写）\n{recent_section}\n\n"
            f"## 开拓者刚才说\n{inp.user_input}\n\n"
            f"## 流萤刚发的话\n{reply_section}\n\n"
            "请输出旁白 JSON："
        )

        # 3. 调 LLM（创作任务：Think High 默认，让旁白有灵性）
        try:
            if self._thinking:
                extra = {"thinking": {"type": "enabled"}, "reasoning_effort": self._effort}
            else:
                extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": stable},
                    {"role": "user", "content": dynamic},
                ],
                max_tokens=10000,
                response_format={"type": "json_object"},
                extra_body=extra,
            )
            record_usage("organizer", resp)
            raw = resp.choices[0].message.content.strip()
            rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
            # 思考模式极端情况：content 为空时从 reasoning 提取 JSON 兜底
            if not raw and rc:
                raw = _extract_json(rc)
            if not raw and rc:
                raw = rc
        except Exception as e:
            logger.error("旁白生成器 LLM 失败: %s", e)
            with _lock:
                _LLM_ERRORS += 1
            record_error("organizer", self._model, str(e))
            return OrganizerOutput()  # 降级：无旁白

        out = _parse_narration(raw)
        out.reasoning = rc
        return out


# ── 辅助函数 ──────────────────────────────────────
def _extract_json(text: str) -> str:
    """从思考内容中提取第一个 JSON 对象（content 为空时的兜底）。"""
    import re
    m = re.search(r'\{.*\}', text, re.S)
    return m.group(0) if m else ""


def _validate_input(inp: OrganizerInput):
    if not isinstance(inp, OrganizerInput):
        raise InputRejected(f"inp 必须为 OrganizerInput，实际: {type(inp).__name__}")
    if not isinstance(inp.user_input, str) or not inp.user_input.strip():
        raise InputRejected("user_input 为空")
    if not isinstance(inp.reply_texts, list) or not inp.reply_texts:
        raise InputRejected("reply_texts 为空")
    if not isinstance(inp.recent_history, list):
        inp.recent_history = []


def _parse_and_validate(raw: str) -> OrganizerOutput:
    data = parse_json(raw)
    if data is None:
        logger.warning("组织器 JSON 解析失败，降级不发: %s", raw[:100] if raw else "(empty)")
        return OrganizerOutput(raw_json=raw)

    label = data.get("sticker", "")
    if not isinstance(label, str):
        label = ""
    label = label.strip()
    if label in ("无", "none", "None", ""):
        label = ""

    return OrganizerOutput(sticker_label=label, raw_json=raw)


def _parse_narration(raw: str) -> OrganizerOutput:
    """解析旁白 JSON。输出 [{text, style, after}]，style 白名单 scene/action。"""
    data = parse_json(raw)
    if data is None:
        logger.warning("旁白 JSON 解析失败，降级无旁白: %s", raw[:100] if raw else "(empty)")
        return OrganizerOutput(raw_json=raw)

    narrations = []
    raw_list = data.get("narrations")
    if isinstance(raw_list, list):
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            style = str(item.get("style", "action")).strip()
            if style not in ("scene", "action"):
                style = "action"
            # 防御：LLM 可能仍输出带括号的 action（前端渲染会再加括号，必须剥掉）
            if text.startswith("（") and text.endswith("）"):
                text = text[1:-1].strip()
            elif text.startswith("(") and text.endswith(")"):
                text = text[1:-1].strip()
            # after：-1=全部前置（默认）；n=插在第 n+1 条消息之后
            try:
                after = int(item.get("after", -1))
            except (TypeError, ValueError):
                after = -1
            if text:
                narrations.append({"text": text, "style": style, "after": after})

    return OrganizerOutput(narrations=narrations, raw_json=raw)
