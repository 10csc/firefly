# -*- coding: utf-8 -*-
"""组织器 — 工具调度器（当前工具：表情包）

架构调整（方案B）：不再决定回复内容（那是回复器的事）。
看流萤刚发出的消息，决定要不要配表情包、配哪张。
未来的工具（气泡切换、主动消息等）也挂在这里。
模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
模型: Flash Non-think（简单分类任务，无需推理）
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
    recent_history: list = field(default_factory=list)  # 最近几轮（含表情包行为记录，供频率感知）


@dataclass
class OrganizerOutput:
    sticker_label: str = ""          # 选中的表情包 label，"" = 不发
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


# ── 组织器类 ──────────────────────────────────────
class Organizer:
    def __init__(self, client, model: str = "deepseek-v4-flash", effort: str = "none"):
        self._client = client
        self._model = model
        # 思考模式：effort=none 显式关闭（默认，温度生效）；其他档位思考模式
        self._thinking = effort != "none"
        effort_map = {"low": "high", "high": "high", "max": "max"}
        self._effort = effort_map.get(effort, "high")

    def organize(self, inp: OrganizerInput) -> OrganizerOutput:
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


# ── 辅助函数 ──────────────────────────────────────
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
