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
from modules.app_config import DEFAULT_MODE

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


def _merge_narrations(messages: list, narrations: list) -> list:
    """旁白按 after 位置插入消息流（视觉小说式穿插演出）。

    after=-1 前置；after=n 插在第 n+1 条消息之后；超出范围的追加末尾。
    返回的新列表不含 after 字段。
    """
    narr_messages = [
        {"type": "narration", "text": n["text"], "style": n["style"], "after": n.get("after", -1)}
        for n in narrations
    ]
    merged = [m for m in narr_messages if m["after"] < 0]
    for i, msg in enumerate(messages):
        # 前置旁白已在开头放入，这里只处理 after >= 0 的穿插
        merged.extend([m for m in narr_messages if m["after"] == i - 1 and m["after"] >= 0])
        merged.append(msg)
    merged.extend([m for m in narr_messages if m["after"] >= len(messages)])
    for m in merged:
        m.pop("after", None)
    return merged


# ── 开场演出（haruno 模式首条自动消息）────────────
# 流萤在黄金时刻第一次见到开拓者的固定场景。纯文本演出，不调 LLM。
# 旁白（scene/action）+ 流萤首条消息，写盘后供前端渲染。
_HARUNO_OPENING = {
    "narrations": [
        {"text": "黄金时刻，霓虹初上，人流如织。你初来乍到，正茫然四顾时，几个流氓围了上来。", "style": "scene"},
        {"text": "就在这时，一位少女快步冲上前，三下两下把流氓赶跑了。", "style": "scene"},
        {"text": "她喘着气转过身，仔细打量你。", "style": "action"},
    ],
    "first_message": "你还好吗？有没有哪里受伤？",
}


def haruno_opening() -> dict:
    """返回 haruno 开场演出消息序列（含旁白与首条消息，均已写盘）。"""
    from modules.conversation_store import append_message as _app
    msgs = []
    for n in _HARUNO_OPENING["narrations"]:
        m = {"type": "narration", "text": n["text"], "style": n["style"]}
        _app("firefly", m, mode="haruno")
        msgs.append(dict(m))
    m = {"type": "text", "content": _HARUNO_OPENING["first_message"]}
    seq, t = _app("firefly", m, mode="haruno")
    m["time"] = t
    msgs.append(m)
    return msgs


# ── 主动性：流萤主动发消息 ────────────────────────
# 轻量路径：不跑完整流水线，直接调 LLM 生成一条问候（结合最近对话与时段）。
# 写盘后前端轮询拉取渲染。触发条件由 check_initiative 判断。
# 前置条件：服务器进程存活（PC=exe 运行，安卓=App 进程存活）+ 前端页面打开轮询。
# 后台保活（安卓前台服务/PC 托盘）不在本次范围。
_active_lock = threading.Lock()
_active_last: dict[str, float] = {}   # {mode: 上次主动的 epoch 秒}，防连续主动


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
# 落盘持久化：pipeline.jsonl（{mode}/data/），重启后仍可查（诊断不依赖复现）
import json as _json
from pathlib import Path as _Path
from modules.app_config import mode_data_dir as _mode_data_dir
_PIPELINE_LOG: list[dict] = []
_PIPELINE_MAX = 200
_PIPELINE_ROTATE_BYTES = 8 * 1024 * 1024   # 文件超 8MB 轮转，保留最近 200 轮


def _pipeline_file(mode: str = DEFAULT_MODE) -> _Path:
    return _mode_data_dir(mode) / "pipeline.jsonl"


def _record_pipeline(entry: dict, mode: str = DEFAULT_MODE):
    with _lock:
        _PIPELINE_LOG.append(entry)
        if len(_PIPELINE_LOG) > _PIPELINE_MAX:
            _PIPELINE_LOG.pop(0)
    # 落盘（失败静默，不影响主流程）
    try:
        fp = _pipeline_file(mode)
        fp.parent.mkdir(parents=True, exist_ok=True)
        with fp.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        if fp.stat().st_size > _PIPELINE_ROTATE_BYTES:
            lines = fp.read_text(encoding="utf-8").splitlines()
            fp.write_text("\n".join(lines[-_PIPELINE_MAX:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def get_pipeline_log(limit: int = 20, mode: str = DEFAULT_MODE) -> list[dict]:
    """返回最近的流水线记录（/pipeline 调试面板）。内存为空（重启后）时从落盘文件恢复。"""
    with _lock:
        if _PIPELINE_LOG:
            return list(_PIPELINE_LOG[-limit:])
    try:
        lines = _pipeline_file(mode).read_text(encoding="utf-8").splitlines()
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
    retriever_model: str = "deepseek-v4-flash",
    retriever_effort: str = "none",
    analyzer_effort: str = "high",
    polisher_effort: str = "high",
    organizer_effort: str = "none",
    retriever_temperature: float = 0.0,
    polisher_temperature: float = 0.5,
    memory_head: str = "",
    hint: str = "",
    mode: str = DEFAULT_MODE,
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
    # haruno 模式：无知识库可检索，直接跳过（省一次 LLM 调用）。
    try:
        if mode == "haruno":
            retrieved_knowledge = ""
            retrieved_memory = ""
            _rt0 = _rt1 = time.perf_counter()
        else:
            anchor = [m for m in ctx.get_recent(10) if m.get("role") == "user"][-1:]
            _rt0 = time.perf_counter()
            r_out = LlmRetriever(client, model=retriever_model,
                                 temperature=retriever_temperature,
                                 effort=retriever_effort, mode=mode).retrieve(RetrieveInput(
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
        analyzer = Analyzer(client, model=analyzer_model, effort=analyzer_effort, mode=mode)
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
                            effort=polisher_effort, temperature=polisher_temperature, mode=mode)
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

        # ── 3. 组织器（story=表情包调度；haruno=旁白演出）──
        # 失败只损失表情包/旁白，不影响文本回复
        org_output = None
        try:
            organizer = Organizer(client, model=organizer_model, effort=organizer_effort, mode=mode)
            org_output = organizer.organize(OrganizerInput(
                user_input=user_input,
                reply_texts=[m["content"] for m in messages if m.get("type") == "text"],
                recent_history=ctx.get_recent(5),
                mode=mode,
            ))
            if org_output.sticker_label:
                from tools.sticker_picker import pick_sticker_by_label
                entry = pick_sticker_by_label(org_output.sticker_label)
                if entry:
                    messages.append({"type": "sticker", "path": entry.file, "label": entry.label})
            # haruno 旁白：after=-1 前置；after=n 插在第 n+1 条消息之后（视觉小说式穿插演出）
            if org_output.narrations:
                messages = _merge_narrations(messages, org_output.narrations)
        except Exception as e:
            logger.warning("工具调度失败（跳过表情包/旁白）: %s", e)
        _t3 = time.perf_counter()

        _record_pipeline({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "user_input": user_input,
            "hint": hint,
            "environment": environment,
            # 本轮实际生效的节点配置（调试：验证配置调整是否落地）
            "config": {
                "retriever": {"model": retriever_model, "effort": retriever_effort, "temperature": retriever_temperature},
                "analyzer": {"model": analyzer_model, "effort": analyzer_effort},
                "polisher": {"model": polisher_model, "effort": polisher_effort, "temperature": polisher_temperature},
                "organizer": {"model": organizer_model, "effort": organizer_effort},
            },
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
        }, mode=mode)

    except Exception as e:
        logger.error("编排器异常 [%s]: %s", type(e).__name__, e)
        with _lock:
            _ORCH_ERRORS += 1
        _record_pipeline({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "user_input": user_input,
            "hint": hint,
            "environment": environment,
            "error": f"{type(e).__name__}: {e}",
        }, mode=mode)
        messages = [{"type":"text","content":m} for m in _handle_direct("api:error")]
        return ChatResult(messages=messages, bubble=None)

    # ── 4. 记录历史 ────────────────────────────
    texts = [m["content"] for m in messages if m.get("type") == "text"]
    ctx.add_turn(user_input, " ".join(texts) if texts else "(表情包)")
    for m in messages:
        if m.get("type") == "sticker":
            ctx.add_action("表情包", m.get("label", "表情"))
        elif m.get("type") == "narration":
            # 旁白进上下文：回复器下轮能看到"她做了什么动作/环境如何"
            ctx.add_action("旁白", m.get("text", ""))

    return ChatResult(messages=messages, bubble=None)


# ── 主动性：流萤主动发消息 ────────────────────────
# 轻量路径：不跑完整流水线，直接调 LLM 生成一条问候（结合最近对话与时段）。
# 写盘后前端轮询拉取渲染。触发条件由 check_initiative 判断。
_ACTIVE_PROMPT = """你是流萤。现在是{environment}，开拓者有一阵子没说话了。
结合最近对话的语境，你想主动给他发一条消息——自然、简短，像平时发短信一样（分1-2条，总长不超过30字）。
可以是问候、分享一件小事、问他在干嘛——不要刻意、不要抒情、不要没话找话。

最近对话：
{history}

只输出你要发的消息（不加前缀，不加引号）："""


def check_initiative(session: dict, client, mode: str = DEFAULT_MODE,
                     model: str = "deepseek-v4-flash", interval_minutes: int = 0) -> dict:
    """检查并触发主动消息。返回 {"sent": bool, "messages": [...]}。

    触发条件：
    1. interval_minutes > 0（0 = 关闭）
    2. 对话非空
    3. 最后一条**用户**消息距今超过 interval 分钟
       （用户说过的最后一句话是起点；流萤的回复不算，否则对话正常停顿反而永不触发）
    4. 距上次流萤主动超过 interval 分钟（防连续主动）
    5. 加锁防并发重复触发
    """
    if interval_minutes <= 0:
        return {"sent": False, "messages": []}

    now = time.time()
    with _active_lock:
        # 防连续主动：距上次主动足够久
        if now - _active_last.get(mode, 0) < interval_minutes * 60:
            return {"sent": False, "messages": []}

        # 找最后一条用户消息的时间
        from modules.conversation_store import load_recent
        recent = load_recent(limit=30, mode=mode)
        last_user_time = None
        for m in reversed(recent):
            if m.get("who") == "user":
                last_user_time = m.get("time")
                break
        if not last_user_time:
            return {"sent": False, "messages": []}
        try:
            last_dt = datetime.strptime(str(last_user_time), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return {"sent": False, "messages": []}
        elapsed = (datetime.now() - last_dt).total_seconds() / 60
        if elapsed < interval_minutes:
            return {"sent": False, "messages": []}

        # 生成主动消息
        history_lines = []
        for m in recent[-4:]:
            role = "开拓者" if m.get("who") == "user" else "流萤"
            if m.get("type") == "text" and m.get("content"):
                history_lines.append(f"{role}: {m['content']}")
        history_section = "\n".join(history_lines) if history_lines else "（无）"

        prompt = _ACTIVE_PROMPT.format(environment=_get_environment(), history=history_section)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": "你是流萤，正在用手机给开拓者发短信。"},
                          {"role": "user", "content": prompt}],
                max_tokens=500,
                extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
            )
            raw = resp.choices[0].message.content.strip()
            rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
            if not raw and rc:
                raw = rc
            # 清理可能的 [MSG] 前缀或引号
            text = raw.replace("[MSG]", "").strip().strip("\"'「」")
            if not text:
                return {"sent": False, "messages": []}
            from modules.llm_base import record_usage
            record_usage("initiative", resp)
        except Exception as e:
            logger.warning("主动消息生成失败: %s", e)
            return {"sent": False, "messages": []}

        # 写盘 + 进上下文 + 记录主动时间
        from modules.conversation_store import append_message as _app
        seq, t = _app("firefly", {"type": "text", "content": text}, mode=mode)
        if "context" in session:
            session["context"].add_turn("（流萤主动发来消息）", text)
        _active_last[mode] = time.time()
        return {"sent": True, "messages": [{"type": "text", "content": text, "time": t, "seq": seq}]}
