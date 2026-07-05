# -*- coding: utf-8 -*-
"""消息编排器

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
模型: Flash + Non-think（不传 reasoning_effort）
职责: 接收回复文本 → LLM 分句 + 编排工具消息位置 → 输出消息序列

prompt 分稳定/动态两层（仿 reply_generator）：
- 稳定层 system：分句规则 + 流萤分条节奏样例（{composer_samples}），字节级稳定，跨会话命中缓存
- 动态层 user：当前语气 + 待拆分回复，每轮重算
"""

import logging
from pathlib import Path
from dataclasses import dataclass, field

from tools.sticker_picker import StickerEntry

logger = logging.getLogger(__name__)

# ── 异常 ──────────────────────────────────────────
class ComposerError(Exception): pass
class InputRejected(ComposerError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class ComposerInput:
    raw_text: str
    tone: dict
    sticker: StickerEntry | None = None


@dataclass
class ComposerOutput:
    messages: list[dict] = field(default_factory=list)


# ── 监控 ──────────────────────────────────────────
import threading
_lock = threading.Lock()
_COMPOSE_COUNT = 0
_COMPOSE_LLM_ERRORS = 0
_COMPOSE_DEGRADED = 0
_CACHE_HITS = 0
_CACHE_MISSES = 0


def _record_cache_stats(resp):
    """采集 DeepSeek prompt cache 命中统计。"""
    global _CACHE_HITS, _CACHE_MISSES
    usage = getattr(resp, "usage", None)
    if usage is None: return
    hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    with _lock:
        _CACHE_HITS += hit
        _CACHE_MISSES += miss


def get_counters() -> dict:
    with _lock:
        total = _CACHE_HITS + _CACHE_MISSES
        rate = (_CACHE_HITS / total) if total > 0 else 0.0
        return {"compose_count": _COMPOSE_COUNT, "compose_llm_errors": _COMPOSE_LLM_ERRORS,
                "compose_degraded": _COMPOSE_DEGRADED,
                "cache_hit_tokens": _CACHE_HITS, "cache_miss_tokens": _CACHE_MISSES,
                "cache_hit_rate": round(rate, 4)}


# ── 样本加载（模块级缓存，仿 reply_generator.load_character_context）──
_COMPOSER_SAMPLES_FILE = Path(__file__).resolve().parent.parent / "assets" / "character" / "composer_samples.md"
_COMPOSER_SAMPLES_CACHE: str | None = None
_COMPOSER_SAMPLES_LOCK = threading.Lock()


def load_composer_samples() -> str:
    """加载分条节奏样例。模块级缓存，首次读盘后复用。"""
    global _COMPOSER_SAMPLES_CACHE
    with _COMPOSER_SAMPLES_LOCK:
        if _COMPOSER_SAMPLES_CACHE is not None:
            return _COMPOSER_SAMPLES_CACHE
        if _COMPOSER_SAMPLES_FILE.exists():
            _COMPOSER_SAMPLES_CACHE = _COMPOSER_SAMPLES_FILE.read_text(encoding="utf-8").strip()
        else:
            _COMPOSER_SAMPLES_CACHE = ""
        return _COMPOSER_SAMPLES_CACHE


def reload_composer_samples() -> None:
    """清缓存，下次 load_composer_samples() 重新读盘。"""
    global _COMPOSER_SAMPLES_CACHE
    with _COMPOSER_SAMPLES_LOCK:
        _COMPOSER_SAMPLES_CACHE = None


# ── Prompt ────────────────────────────────────────
# 稳定层 system message —— 字节级稳定，跨会话命中缓存
_COMPOSE_STABLE_PROMPT = """你是流萤。你需要把一段回复文本拆成手机消息序列。

## 分句习惯
- 句子短，像真人发短信
- 省略号（…）是自然停顿，在停顿处可以新起一条消息
- 害羞或紧张时句子更短，放松时可以稍长
- 每段至少4个字，不能有空消息
- 不要改变原文的任何字词和标点

## 工具消息
- 文本中有 [sticker] 标记的地方，用 [STICKER] 单独占一行
- 表情包前后如果原文没有文字，不要凭空加文字

## 流萤分条节奏样例
{composer_samples}

输出格式：每行一条消息，前缀为 [MSG] 或 [STICKER]，禁止任何其他文字：
[MSG]第一条消息文本
[MSG]第二条消息文本
[STICKER]
[MSG]接着的消息文本"""


# 动态层 user message —— 每轮重算
_COMPOSE_DYNAMIC_TEMPLATE = """## 当前语气
{tone}

## 待拆分的回复
{raw_text}"""


# 纯规则编排器——不走 LLM，省成本 + 防幻觉
# 回复器的产出本身已经是适合短信发布的文本了，只需按自然断行切分 + [sticker] 替换

_MSG_MAX_CHARS = 30  # 单条超过此长度需要进一步切分
_MSG_MIN_CHARS = 2   # 单条最短长度

class Composer:
    def __init__(self, client=None, model: str = "deepseek-v4-flash"):
        pass  # client/model 保留兼容签名，但纯规则模式不再需要

    def compose(self, inp: ComposerInput) -> ComposerOutput:
        _validate_input(inp)

        if not inp.raw_text or not inp.raw_text.strip():
            return ComposerOutput(messages=[{"type": "text", "content": "嗯…信号好像不太好"}])

        text = inp.raw_text.strip()
        messages = []

        # 1. 按 [sticker] 切分段落
        segments = text.split("[sticker]")
        for i, seg in enumerate(segments):
            seg = seg.strip()
            if not seg:
                continue
            # 2. 每个段落按自然换行先拆，再按长度细分
            lines = seg.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = _split_sentences(line)
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    # 过长再切
                    sub = _split_long(part)
                    messages.extend(sub)

            # 3. 在 [sticker] 位置插入表情包（最后一个 segment 之后不插）
            if i < len(segments) - 1 and inp.sticker:
                messages.append({
                    "type": "sticker",
                    "path": inp.sticker.file,
                    "label": inp.sticker.label,
                })

        if not messages:
            messages = [{"type": "text", "content": "嗯…信号好像不太好"}]

        return ComposerOutput(messages=messages)


def _validate_input(inp: ComposerInput):
    if not isinstance(inp, ComposerInput):
        raise InputRejected(f"inp 必须为 ComposerInput，实际: {type(inp).__name__}")
    if not isinstance(inp.raw_text, str):
        raise InputRejected(f"raw_text 必须为 str，实际: {type(inp.raw_text).__name__}")
    if not isinstance(inp.tone, dict):
        inp.tone = {}


def _parse_response(response: str, sticker: StickerEntry | None) -> list[dict]:
    """解析 LLM 响应：[MSG]text → text 消息，[STICKER] → sticker 消息。"""
    messages = []
    for line in response.strip().split("\n"):
        line = line.strip()
        if line.startswith("[MSG]"):
            text = line[5:].strip()
            if text:
                messages.append({"type": "text", "content": text})
        elif line.startswith("[STICKER]"):
            if sticker:
                messages.append({
                    "type": "sticker",
                    "path": sticker.file,
                    "label": sticker.label,
                })

    # 如果解析出内容但不含 sticker 且原始文本有 [sticker] 标记，补插入
    if messages and sticker and "[sticker]" in response.lower():
        pass  # 已在上面处理

    return messages


def _degraded_fallback(inp: ComposerInput, reason: str) -> ComposerOutput:
    """降级：按句号/省略号/感叹号/问号切分，[sticker] 替换为 sticker 消息。"""
    global _COMPOSE_DEGRADED
    with _lock: _COMPOSE_DEGRADED += 1
    logger.warning("编排器降级 (%s): raw=%s", reason, inp.raw_text[:80])

    text = inp.raw_text.strip()
    if not text:
        return ComposerOutput(messages=[{"type": "text", "content": "嗯…信号好像不太好"}])

    messages = []
    # 先按 [sticker] 切分
    segments = text.split("[sticker]")
    for i, seg in enumerate(segments):
        # 按标点自然分句
        parts = _split_sentences(seg.strip())
        for p in parts:
            if p:
                messages.append({"type": "text", "content": p})
        # 在 [sticker] 位置插入表情包（最后一个 segment 后面不插）
        if i < len(segments) - 1 and inp.sticker:
            messages.append({
                "type": "sticker",
                "path": inp.sticker.file,
                "label": inp.sticker.label,
            })

    if not messages:
        messages = [{"type": "text", "content": "嗯…信号好像不太好"}]

    return ComposerOutput(messages=messages)


def _split_long(text: str, max_chars: int = 30) -> list[dict]:
    """太长时在逗号处切开。"""
    if len(text) <= max_chars:
        return [{"type": "text", "content": text}]
    result = []
    while len(text) > max_chars:
        cut = max_chars
        for sep in ("，", ",", "；", ";", "、"):
            idx = text.rfind(sep, 0, max_chars)
            if idx > max_chars // 2:
                cut = idx + 1
                break
        result.append({"type": "text", "content": text[:cut].strip()})
        text = text[cut:].strip()
    if text:
        result.append({"type": "text", "content": text})
    return result


def _split_sentences(text: str) -> list[str]:
    """按句号/感叹号/问号/省略号处切分，去掉末尾标点。
    短信不发句号——句号是分条信号，不是内容。
    """
    import re
    # 在句号、感叹号、问号、省略号处切开（标点本身从结果中去掉）
    parts = re.split(r"[。！？…]+", text)
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) >= 4:
            result.append(p)
        else:
            # 太短的合并到下一条
            if result:
                result[-1] += p
            else:
                result.append(p)
    return result
