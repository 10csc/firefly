# -*- coding: utf-8 -*-
"""分析器 — 理解用户输入、核查事实、整理所需知识

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
模型: Flash + Think High
"""

import json, logging, threading
from dataclasses import dataclass, field
from pathlib import Path

from modules.llm_base import load_slot, format_history, extract_json, record_usage, record_error, parse_json

logger = logging.getLogger(__name__)
_lock = threading.Lock()


# ── 异常 ──────────────────────────────────────────
class AnalyzerError(Exception): pass
class InputRejected(AnalyzerError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class AnalyzerInput:
    user_input: str
    recent_history: list = field(default_factory=list)
    retrieved_knowledge: str = ""     # 检索到的世界知识（RAG 预留）
    retrieved_memory: str = ""        # 检索到的记忆（RAG 预留）
    environment: str = ""             # 环境信息（时段描述等）


@dataclass
class AnalyzerOutput:
    intent: str = ""
    fact_check: list = field(default_factory=list)  # [{"claim","verdict","note"}]
    knowledge_query: list = field(default_factory=list)  # 关键词（RAG 预留）
    memory_query: list = field(default_factory=list)     # 关键词（RAG 预留）
    summary: str = ""
    raw_json: str = ""
    reasoning: str = ""               # 模型思考过程（调试观测用）


# ── 默认输出（降级用，每次生成新实例避免跨请求污染）──
def _default_output() -> AnalyzerOutput:
    return AnalyzerOutput(
        intent="normal",
        fact_check=[],
        summary="无法分析，按正常聊天自然接话。",
    )


# ── 监控 ──────────────────────────────────────────
_ANALYZE_COUNT = 0
_LLM_ERRORS = 0
_LLM_CALLS = 0


def get_counters() -> dict:
    with _lock:
        return {
            "analyze_count": _ANALYZE_COUNT,
            "llm_errors": _LLM_ERRORS,
        }


# ── Prompt（按模式，剧情专属段仅 story 注入）────────
_ANALYZER_SYSTEM = """你是一个分析助手。你的任务是在流萤回复之前，分析开拓者刚才发来的消息。

## 你是谁
你是流萤的"分析层"——负责理解消息、核查事实、整理有用知识。
你不是在写回复，你是在分析。不要生成回复文本，只输出分析结果。

## 角色上下文
{core}

## 人际关系与认知边界
{identity}

## 用户补充的剧情设定（与核心设定同等权威）
{user_setting}

{story_extra}

## 分析任务

### 1. 开拓者为什么这么说？（intent）
推测意图：关心 / 调侃 / 求助 / 试探 / 分享日常 / 越界 / 提问 / 其他
**重要**：中文经常省略主语。结合历史对话，明确判断"这句话说的是谁"（说的是开拓者自己、说的是流萤、还是说的别人）。判断错了会导致回复驴唇不对马嘴。

### 2. 开拓者说的内容属实吗？（fact_check）
逐条核查用户消息中涉及的每个具体说法：
- "真实"：在设定文件中有明确记载 → 注明出处
- "不存在"：设定文件中没有的角色/事件/概念 → 标记为不存在
- "错误前提"：涉及流萤当前状态（能走能动/能做物理动作）→ 标记为错误
- 无法判断 → 标记为"不确定"

### 3. 结合检索到的知识和记忆，整理有用信息（summary）
将分析结果、检索到的知识、检索到的记忆整合成一段话，
供组织器直接使用。语言简洁，不要遗漏关键信息。

### 4. 还需要什么信息？（knowledge_query / memory_query）
哪些信息有助于更准确地回复，但当前未提供？
列出关键词，供后续 RAG 实现在下一轮加载。

### 5. 场景推进（角色扮演铁律，最高优先）
开拓者的消息可能是在**推进场景**——"你来了，请坐""我已经到门口了""进来吧"这类意味着当前场景已经推进（已见面/已到店）。**直接接住推进后的场景**：他说到了，就是到了，回复围绕"现在就在一起"展开。
与既往约定的时间/地点不一致（比如约的周六、今天却是周二）→ 视为剧情跳跃，**不质疑、不纠正、不提**，顺其自然。只有开拓者明确表达困惑时才解释。

## 输出格式（一行 JSON，禁止任何其他文字）
{{"intent":"意图","fact_check":[{{"claim":"开拓者说的具体说法","verdict":"真实/不存在/错误前提/不确定","note":"说明"}}],"knowledge_query":["关键词"],"memory_query":["关键词"],"summary":"分析结果整理"}}"""


# story 模式专属事实核查参考（剧情世界观），haruno 不注入
_ANALYZER_STORY_EXTRA = """### 剧情事实核查补充（story 模式）
涉及流萤当前状态的常见错误前提：能去匹诺康尼 / 能离开医疗舱 / 能吃东西 / 能做物理动作。
仅在开拓者消息明确涉及这些物理动作时标记为"错误前提"（重伤在医疗舱，无法入梦）。
流萤的身体状态是背景不是话题——summary 里不要反复强调医疗舱/恢复状态，回复内容与其他普通聊天无异。"""


_ANALYZER_SYSTEMS = {
    "story": _ANALYZER_SYSTEM,
    "haruno": _ANALYZER_SYSTEM,
}


# ── 分析器类 ──────────────────────────────────────
class Analyzer:
    def __init__(self, client, model: str = "deepseek-v4-flash", effort: str = "high",
                 mode: str = "story"):
        self._client = client
        self._model = model
        self._mode = mode
        # 思考模式：effort=none 显式关闭（温度才生效）；默认 high
        self._thinking = effort != "none"
        effort_map = {"low": "high", "high": "high", "max": "max"}
        self._effort = effort_map.get(effort, "high")

    def analyze(self, inp: AnalyzerInput) -> AnalyzerOutput:
        global _ANALYZE_COUNT, _LLM_ERRORS

        # 1. 审查
        _validate_input(inp)

        with _lock:
            _ANALYZE_COUNT += 1

        # 2. 构建 prompt
        core = load_slot("core", self._mode)
        identity = load_slot("identity", self._mode)
        user_setting = load_slot("用户设定", self._mode)
        story_extra = _ANALYZER_STORY_EXTRA if self._mode == "story" else ""

        stable = _ANALYZER_SYSTEM.format(
            core=core, identity=identity, user_setting=user_setting, story_extra=story_extra,
        )

        # 历史格式化复用 llm_base.format_history（含 system 行为行与主动标记——
        # 与 polisher/organizer 口径一致，prompt 更完整）
        history_section = format_history(inp.recent_history)

        env_section = f"## 当前环境\n{inp.environment}\n" if inp.environment else ""
        knowledge_section = f"## 检索到的相关知识\n{inp.retrieved_knowledge}\n" if inp.retrieved_knowledge else ""
        memory_section = f"## 检索到的记忆\n{inp.retrieved_memory}\n" if inp.retrieved_memory else ""

        knowledge_query_hint = "（以上已提供检索到的知识，此字段记录还缺什么）"
        memory_query_hint = "（以上已提供检索到的记忆，此字段记录还缺什么）"

        dynamic = f"""## 最近对话
{history_section}

{env_section}{knowledge_section}{memory_section}## 开拓者刚才说
{inp.user_input}

## 分析提示
- {knowledge_query_hint}
- {memory_query_hint}
- 有检索到的内容就利用，没有就不用。
- 只输出 JSON。"""

        # 3. 调 LLM
        try:
            if self._thinking:
                # 思考模式下 temperature 无效（官方文档），不传避免误导
                extra = {"thinking": {"type": "enabled"}, "reasoning_effort": self._effort}
            else:
                extra = {"thinking": {"type": "disabled"}}
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": stable},
                    {"role": "user", "content": dynamic},
                ],
                max_tokens=10000,
                extra_body=extra,
            )
            record_usage("analyzer", resp)
            raw = resp.choices[0].message.content.strip()
            rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
            # 思考模式极端情况：content 为空时从 reasoning 提取 JSON 兜底
            # （用 llm_base.extract_json，处理字符串引号/转义，比贪婪正则健壮）
            if not raw and rc:
                raw = extract_json(rc) or rc
        except Exception as e:
            logger.error("分析器 LLM 失败: %s", e)
            with _lock:
                _LLM_ERRORS += 1
            record_error("analyzer", self._model, str(e))
            return _default_output()

        out = _parse_and_validate(raw)
        out.reasoning = rc
        return out


# ── 辅助函数 ──────────────────────────────────────
def _validate_input(inp: AnalyzerInput):
    if not isinstance(inp, AnalyzerInput):
        raise InputRejected(f"inp 必须为 AnalyzerInput，实际: {type(inp).__name__}")
    if not isinstance(inp.user_input, str) or not inp.user_input.strip():
        raise InputRejected("user_input 为空")
    if not isinstance(inp.recent_history, list):
        raise InputRejected("recent_history 必须为 list")


def _parse_and_validate(raw: str) -> AnalyzerOutput:
    data = parse_json(raw)
    if data is None:
        logger.warning("分析器 JSON 解析失败，降级")
        return _default_output()

    intent = data.get("intent", "normal")
    if not isinstance(intent, str) or not intent.strip():
        intent = "normal"

    fact_check = data.get("fact_check", [])
    if not isinstance(fact_check, list):
        fact_check = []
    fact_check = [fc for fc in fact_check if isinstance(fc, dict)]
    for fc in fact_check:
        fc.setdefault("claim", "")
        fc.setdefault("verdict", "不确定")
        fc.setdefault("note", "")

    kq = data.get("knowledge_query", [])
    if not isinstance(kq, list):
        kq = []
    mq = data.get("memory_query", [])
    if not isinstance(mq, list):
        mq = []

    summary = data.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        summary = "分析结果：正常聊天。按分析结果自然接话。"

    return AnalyzerOutput(
        intent=intent,
        fact_check=fact_check,
        knowledge_query=kq,
        memory_query=mq,
        summary=summary,
        raw_json=raw,
    )
