# -*- coding: utf-8 -*-
"""编排器 — 对话流水线总调度

当前流程：向量检索 → 分析器 → 回复器（全权生成）→ 组织器（工具调度：表情包）→ 记录
"""

import logging, threading, time
from datetime import datetime
from dataclasses import dataclass, field

from modules.analyzer import Analyzer, AnalyzerInput
from modules.organizer import Organizer, OrganizerInput
from modules.polisher import Polisher, PolisherInput
from modules.context_manager import ContextManager
from modules.llm_retriever import LlmRetriever, RetrieveInput
from modules.llm_base import get_token_stats as _get_token_stats

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────
@dataclass
class ChatResult:
    messages: list[dict] = field(default_factory=list)
    bubble: str | None = None


# ── 降级话术 ─────────────────────────────────────
_DIRECT_REPLIES = {
    "input:empty":    ["嗯…怎么啦？想说什么就说吧"],
    "input:too_long": ["你说了好多…我慢慢看，等一下哦"],
    "api:error":      ["嗯…信号好像不太好，你等一下哦"],
}


def _handle_direct(reason: str) -> list[str]:
    return _DIRECT_REPLIES.get(reason, ["嗯？我走神了…你刚才说了什么？"])


# ── 环境 ─────────────────────────────────────────
def _get_environment() -> str:
    now = datetime.now()
    h = now.hour
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    d = f"{now.month}月{now.day}日 {wd} "
    if 5 <= h < 8:       return f"{d}清晨，天刚亮。"
    elif 8 <= h < 12:    return f"{d}上午。"
    elif 12 <= h < 14:   return f"{d}中午。"
    elif 14 <= h < 18:   return f"{d}下午。"
    elif 18 <= h < 21:   return f"{d}傍晚。"
    elif 21 <= h < 24:   return f"{d}夜晚。"
    else:                return f"{d}深夜，万籁俱寂。"


# ── 监控 ─────────────────────────────────────────
_lock = threading.Lock()
_CHAT_COUNT = 0
_DIRECT_COUNT = 0
_ORCH_ERRORS = 0

# ── 流水线观测：每轮各阶段的输入/输出/思考过程 ──────
# 落盘持久化：pipeline.jsonl（user_data/data/），重启后仍可查（诊断不依赖复现）
_PIPELINE_LOG: list[dict] = []
_PIPELINE_MAX = 200
import json as _json
from pathlib import Path as _Path
_PIPELINE_FILE = _Path(__file__).resolve().parent.parent / "user_data" / "data" / "pipeline.jsonl"
_PIPELINE_ROTATE_BYTES = 8 * 1024 * 1024   # 文件超 8MB 轮转，保留最近 200 轮


def _record_pipeline(entry: dict):
    with _lock:
        _PIPELINE_LOG.append(entry)
        if len(_PIPELINE_LOG) > _PIPELINE_MAX:
            _PIPELINE_LOG.pop(0)
    # 落盘（失败静默，不影响主流程）
    try:
        _PIPELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _PIPELINE_FILE.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        if _PIPELINE_FILE.stat().st_size > _PIPELINE_ROTATE_BYTES:
            lines = _PIPELINE_FILE.read_text(encoding="utf-8").splitlines()
            _PIPELINE_FILE.write_text("\n".join(lines[-_PIPELINE_MAX:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def get_pipeline_log(limit: int = 20) -> list[dict]:
    """返回最近的流水线记录（/pipeline 调试面板）。内存为空（重启后）时从落盘文件恢复。"""
    with _lock:
        if _PIPELINE_LOG:
            return list(_PIPELINE_LOG[-limit:])
    try:
        lines = _PIPELINE_FILE.read_text(encoding="utf-8").splitlines()
        return [_json.loads(l) for l in lines[-limit:]]
    except Exception:
        return []


def get_counters() -> dict:
    with _lock:
        base = _get_token_stats()
        return {
            "chat_count": _CHAT_COUNT, "direct_count": _DIRECT_COUNT,
            "orch_errors": _ORCH_ERRORS, **base,
        }


# ── 主入口 ──────────────────────────────────────
def handle_chat(
    user_input: str,
    session: dict,
    client,
    analyzer_model: str = "deepseek-v4-flash",
    organizer_model: str = "deepseek-v4-flash",
    polisher_model: str = "deepseek-v4-flash",
    memory_head: str = "",
    polisher_effort: str = "high",
    polisher_temperature: float = 0.5,
    hint: str = "",
) -> ChatResult:
    global _CHAT_COUNT, _DIRECT_COUNT, _ORCH_ERRORS
    with _lock:
        _CHAT_COUNT += 1

    ctx: ContextManager = session["context"]

    # ── 前置规则 ──────────────────────────────
    if not user_input or not user_input.strip():
        with _lock:
            _DIRECT_COUNT += 1
        return ChatResult(
            messages=[{"type":"text","content":m} for m in _handle_direct("input:empty")],
            bubble=None)

    if len(user_input) > 2000:
        with _lock:
            _DIRECT_COUNT += 1
        return ChatResult(
            messages=[{"type":"text","content":m} for m in _handle_direct("input:too_long")],
            bubble=None)

    # ── 0. 环境 + 知识获取 ──────────────────────
    environment = _get_environment()
    # LLM 子代理检索：知识库整体注入（system 稳定前缀，缓存高命中），
    # 输出压缩知识摘要。无本地模型依赖（安卓端可行），全局性覆盖。
    # 话题锚点只取上一条用户消息：指代消解；话题理解归 analyzer（20 轮历史），
    # 子代理不重复接收流萤自产回复（噪音 + miss 成本 + 话题漂移）。
    try:
        anchor = [m for m in ctx.get_recent(10) if m.get("role") == "user"][-1:]
        _rt0 = time.perf_counter()
        r_out = LlmRetriever(client).retrieve(RetrieveInput(
            user_input=user_input,
            recent_history=anchor,
        ))
        _rt1 = time.perf_counter()
        retrieved_knowledge = r_out.knowledge
        retrieved_memory = ""   # 子代理输出为混合摘要，两层合并
    except Exception as e:
        retrieved_knowledge = ""
        retrieved_memory = ""
        _rt0 = _rt1 = time.perf_counter()

    try:
        # ── 1. 分析器 ──────────────────────────
        input_text = user_input
        if hint == "typing_long":
            input_text = "（开拓者正在输入了很久，但还没有发送。你可以先问：怎么了？有什么想和我说的吗？）"
        elif hint == "still_typing":
            input_text = f"{user_input}\n（注意：开拓者还在输入第二条消息，可能还有下文）"

        _t0 = time.perf_counter()
        analyzer = Analyzer(client, model=analyzer_model)
        analysis = analyzer.analyze(AnalyzerInput(
            user_input=input_text,
            recent_history=ctx.get_recent(20),
            retrieved_knowledge=retrieved_knowledge,
            retrieved_memory=retrieved_memory,
            environment=environment,
        ))
        _t1 = time.perf_counter()

        # ── 2. 回复器（全权生成回复文本）────────
        polisher = Polisher(client, model=polisher_model,
                            effort=polisher_effort, temperature=polisher_temperature)
        polish_output = polisher.polish(PolisherInput(
            user_input=user_input,
            analyzer_summary=analysis.summary,
            analyzer_intent=analysis.intent,
            analyzer_fact_check=analysis.fact_check,
            recent_history=ctx.get_recent(15),
            memory_head=memory_head,
            environment=environment,
        ))
        _t2 = time.perf_counter()
        messages = list(polish_output.messages)

        # ── 3. 组织器（工具调度：表情包）─────────
        # 失败只损失表情包，不影响文本回复
        org_output = None
        try:
            organizer = Organizer(client, model=organizer_model)
            org_output = organizer.organize(OrganizerInput(
                user_input=user_input,
                reply_texts=[m["content"] for m in messages if m.get("type") == "text"],
                recent_history=ctx.get_recent(5),
            ))
            if org_output.sticker_label:
                from tools.sticker_picker import pick_sticker_by_label
                entry = pick_sticker_by_label(org_output.sticker_label)
                if entry:
                    messages.append({"type": "sticker", "path": entry.file, "label": entry.label})
        except Exception as e:
            logger.warning("工具调度失败（跳过表情包）: %s", e)
        _t3 = time.perf_counter()

        _record_pipeline({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_input": user_input,
            "hint": hint,
            "environment": environment,
            "retriever": {
                "elapsed": round(_rt1 - _rt0, 2),
                "knowledge": retrieved_knowledge,     # 完整内容落盘，诊断不依赖截断
                "memory": retrieved_memory,
            },
            "analyzer": {
                "elapsed": round(_t1 - _t0, 2),
                "intent": analysis.intent,
                "fact_check": analysis.fact_check,
                "summary": analysis.summary,
                "raw_json": analysis.raw_json,        # LLM 原始输出（解析前）
                "reasoning": analysis.reasoning,
            },
            "polisher": {
                "elapsed": round(_t2 - _t1, 2),
                "raw": polish_output.raw,
                "reasoning": polish_output.reasoning,
            },
            "organizer": {
                "elapsed": round(_t3 - _t2, 2),
                "sticker_label": org_output.sticker_label if org_output else "(调度失败)",
                "raw": org_output.raw_json if org_output else "",
            },
            "messages": messages,
        })

    except Exception as e:
        logger.error("编排器异常 [%s]: %s", type(e).__name__, e)
        with _lock:
            _ORCH_ERRORS += 1
        _record_pipeline({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_input": user_input,
            "hint": hint,
            "environment": environment,
            "error": f"{type(e).__name__}: {e}",
        })
        messages = [{"type":"text","content":m} for m in _handle_direct("api:error")]
        return ChatResult(messages=messages, bubble=None)

    # ── 4. 记录历史 ────────────────────────────
    texts = [m["content"] for m in messages if m.get("type") == "text"]
    ctx.add_turn(user_input, " ".join(texts) if texts else "(表情包)")
    for m in messages:
        if m.get("type") == "sticker":
            ctx.add_action("表情包", m.get("label", "表情"))

    return ChatResult(messages=messages, bubble=None)
