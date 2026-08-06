# -*- coding: utf-8 -*-
"""LLM 子代理检索 — 替代本地向量 RAG

背景（决策依据）：
- 安卓端是必须目标，本地 embedding 模型（95MB + numpy 索引）在安卓上不可行；
  本模块纯 API 调用，零本地依赖。
- 情感/角色扮演类 agent 的核心是对角色的全局理解——一件事不仅包含自身，
  也包含间接关联（相关的人、过去的约定、类似情境）。向量 top-k 截断丢失
  间接关联；把设定知识库整体注入 LLM 输出压缩摘要，天然全局。
- 知识库作为 system 稳定前缀，缓存命中率高；输出经 LLM 压缩总结。

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
模型: Flash + Non-think（temperature 此时才生效，低温度降随机性）
"""

import logging, threading
from pathlib import Path
from dataclasses import dataclass, field

from modules.app_config import ROOT
from modules.llm_base import format_history, record_usage, record_error

logger = logging.getLogger(__name__)
_lock = threading.Lock()


# ── 异常 ──────────────────────────────────────────
class LlmRetrieverError(Exception): pass
class InputRejected(LlmRetrieverError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class RetrieveInput:
    user_input: str                       # 开拓者刚才说的话
    recent_history: list = field(default_factory=list)  # 最近对话（话题连续性）


@dataclass
class RetrieveOutput:
    knowledge: str = ""                   # 压缩知识摘要
    raw: str = ""                         # LLM 原始输出（调试观测用）


# ── 知识库加载（模块级缓存）────────────────────────
# 与 build_index.py 同源：knowledge/（含 story/ 个人经历）+ database/dialogues_compiled/
_SOURCE_DIRS = (
    ROOT / "knowledge",
    ROOT / "database" / "dialogues_compiled",
)
# 动态用户数据/过长原文/草稿不进知识库（与 build_index 排除规则一致）
_EXCLUDE_NAMES = {"index.md", "手账.md", "memory.md", "dialogue-transcripts.md"}
_KNOWLEDGE_CACHE: str | None = None
_KNOWLEDGE_STATS: dict = {}


def _load_knowledge() -> str:
    """拼接全部设定资料库文本。模块级缓存（内容只在文件变更后重建）。"""
    global _KNOWLEDGE_CACHE, _KNOWLEDGE_STATS
    with _lock:
        if _KNOWLEDGE_CACHE is not None:
            return _KNOWLEDGE_CACHE
        parts = []
        total_chars = 0
        file_count = 0
        for d in _SOURCE_DIRS:
            if not d.exists():
                continue
            for fp in sorted(d.rglob("*.md")):
                if fp.name in _EXCLUDE_NAMES:
                    continue
                if fp.stem.endswith("_draft"):
                    continue
                try:
                    text = fp.read_text(encoding="utf-8")
                except OSError:
                    continue
                total_chars += len(text)
                file_count += 1
                parts.append(f"## {fp.relative_to(ROOT)}\n{text}")
        _KNOWLEDGE_CACHE = "\n\n".join(parts)
        _KNOWLEDGE_STATS = {"files": file_count, "chars": total_chars}
        logger.info("知识库加载: %d 文件, %d 字符", file_count, total_chars)
        return _KNOWLEDGE_CACHE


def get_knowledge_stats() -> dict:
    _load_knowledge()
    return dict(_KNOWLEDGE_STATS)


# ── Prompt（稳定层：知识库 + 指令，跨请求缓存命中）──
_SYSTEM_PROMPT = """你是流萤的设定资料检索助手。以下是流萤的完整设定资料库，包含她的身份、经历、人际关系、关键台词与场景原文。

## 设定资料库
{knowledge}

## 任务
根据最近对话与开拓者的最新消息，从资料库中找出与当前话题**直接或间接相关**的所有内容，输出一份压缩知识摘要，供主模型参考。要求：
- 覆盖：直接相关的事实、关联的人物与事件、可用于回复的原文台词
- 全局性：一件事不仅找它本身，还要找间接关联的内容（相关的人、过去的约定、类似的情境）
- 只输出资料库中存在的内容，禁止编造
- 合并同类信息，丢弃无关内容，控制篇幅
- 只输出摘要正文，不要任何解释、前后缀或标题

## 绝对禁止（输出即失败）
- 禁止输出对话体：任何"流萤：""开拓者："开头的一问一答形式。资料库中的对话原文只能作为引用片段嵌入摘要（如"她说过：'……'"),不得模拟流萤当场说话
- 禁止以第一人称扮演流萤回复开拓者（"我想去……""你愿意和我说说吗"等）
- 你是检索助手，不是流萤。你的读者是分析层，不是开拓者"""


# ── 监控 ──────────────────────────────────────────
_RETRIEVE_COUNT = 0
_LLM_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {"retrieve_count": _RETRIEVE_COUNT, "llm_errors": _LLM_ERRORS}


# ── 子代理类 ──────────────────────────────────────
class LlmRetriever:
    def __init__(self, client, model: str = "deepseek-v4-flash",
                 temperature: float = 0.0, effort: str = "none"):
        self._client = client
        self._model = model
        try:
            self._temperature = max(0.0, min(2.0, float(temperature)))
        except (TypeError, ValueError):
            self._temperature = 0.0
        # 思考模式：effort=none 显式关闭（默认，温度生效）；其他档位思考模式（温度无效）
        self._thinking = effort != "none"
        effort_map = {"low": "high", "high": "high", "max": "max"}
        self._effort = effort_map.get(effort, "high")

    def retrieve(self, inp: RetrieveInput) -> RetrieveOutput:
        global _RETRIEVE_COUNT, _LLM_ERRORS

        # 1. 审查
        _validate_input(inp)

        with _lock:
            _RETRIEVE_COUNT += 1

        # 2. 构建 prompt
        knowledge = _load_knowledge()
        sys_prompt = _SYSTEM_PROMPT.format(knowledge=knowledge)
        history_section = format_history(inp.recent_history) if inp.recent_history else "（无）"
        dynamic = f"## 最近对话\n{history_section}\n\n## 开拓者刚才说\n{inp.user_input}"

        # 3. 调 LLM（默认 Non-think：temperature 生效，0 温度保证摘要格式稳定）
        try:
            if self._thinking:
                extra = {"thinking": {"type": "enabled"}, "reasoning_effort": self._effort}
            else:
                extra = {"thinking": {"type": "disabled"}}
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": dynamic},
                ],
                max_tokens=10000, temperature=self._temperature,
                extra_body=extra,
            )
            record_usage("llm_retriever", resp)
            raw = resp.choices[0].message.content.strip()
            rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
            if not raw and rc:
                raw = rc
        except Exception as e:
            logger.error("子代理检索 LLM 失败: %s", e)
            with _lock:
                _LLM_ERRORS += 1
            record_error("llm_retriever", self._model, str(e))
            return RetrieveOutput(knowledge="", raw="")

        # 4. 验证：空输出 / 对话体污染（模拟流萤回复而非摘要）→ 降级为空
        if not raw or _looks_like_dialogue(raw):
            logger.warning("子代理检索输出异常（空或对话体），降级为空知识: %s", raw[:80])
            return RetrieveOutput(knowledge="", raw=raw)

        return RetrieveOutput(knowledge=raw, raw=raw)


def _looks_like_dialogue(raw: str) -> bool:
    """检测对话体污染：摘要里出现'流萤：'或'开拓者：'开头的连续对白行。"""
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    if len(lines) < 2:
        return False
    dialogue_lines = 0
    for l in lines:
        if l.startswith("流萤：") or l.startswith("开拓者："):
            dialogue_lines += 1
    return dialogue_lines >= 2


# ── 辅助函数 ──────────────────────────────────────
def _validate_input(inp: RetrieveInput):
    if not isinstance(inp, RetrieveInput):
        raise InputRejected(f"inp 必须为 RetrieveInput，实际: {type(inp).__name__}")
    if not isinstance(inp.user_input, str) or not inp.user_input.strip():
        raise InputRejected("user_input 为空")
    if not isinstance(inp.recent_history, list):
        raise InputRejected("recent_history 必须为 list")


def reset_knowledge_cache():
    """强制重建知识库缓存（设定文件变更后调用）。"""
    global _KNOWLEDGE_CACHE
    with _lock:
        _KNOWLEDGE_CACHE = None
