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


# ── 编排器 ────────────────────────────────────────
class Composer:
    def __init__(self, client, model: str = "deepseek-v4-flash"):
        self._client = client
        self._model = model

    def compose(self, inp: ComposerInput) -> ComposerOutput:
        global _COMPOSE_COUNT, _COMPOSE_LLM_ERRORS, _COMPOSE_DEGRADED

        # 1. 审查
        _validate_input(inp)

        # 2. 处理空文本或仅有 sticker 的文本
        if not inp.raw_text.strip():
            return _degraded_fallback(inp, "空文本")

        # 3. 如果没有 sticker 且文本很短（单句），直接分句
        if "[sticker]" not in inp.raw_text and len(inp.raw_text) < 20:
            return ComposerOutput(messages=[{"type": "text", "content": inp.raw_text.strip()}])

        # 4. 构建 prompt（稳定层 system + 动态层 user）
        tone_text = inp.tone.get("base", "日常") if isinstance(inp.tone, dict) else "日常"
        samples = load_composer_samples()

        stable = _COMPOSE_STABLE_PROMPT.format(composer_samples=samples)
        dynamic = _COMPOSE_DYNAMIC_TEMPLATE.format(tone=tone_text, raw_text=inp.raw_text)

        # 5. 调 LLM (Non-think)
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": stable},
                    {"role": "user", "content": dynamic},
                ],
                max_tokens=256, temperature=0,
            )
            _record_cache_stats(resp)
            raw = resp.choices[0].message.content.strip()
            # 思考模式下 content 偶尔空，fallback 到 reasoning_content
            if not raw:
                rc = getattr(resp.choices[0].message, "reasoning_content", None)
                if rc and rc.strip():
                    raw = rc.strip()
        except Exception as e:
            logger.error("编排器 LLM 失败: %s", e)
            with _lock: _COMPOSE_LLM_ERRORS += 1
            return _degraded_fallback(inp, f"LLM 异常: {e}")

        with _lock: _COMPOSE_COUNT += 1

        # 6. 解析
        messages = _parse_response(raw, inp.sticker)
        if not messages:
            with _lock: _COMPOSE_DEGRADED += 1
            return _degraded_fallback(inp, "解析结果为空")

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


def _split_sentences(text: str) -> list[str]:
    """按中文标点自然分句，每段 >= 4 字。"""
    import re
    # 在句号、感叹号、问号、省略号处切开
    parts = re.split(r"(?<=[。！？…])", text)
    result = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) >= 4:
            if buf:
                result.append(buf)
                buf = ""
            result.append(p)
        else:
            buf += p
    if buf:
        if result:
            result[-1] += buf
        else:
            result.append(buf)
    return result
