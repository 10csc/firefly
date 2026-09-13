# -*- coding: utf-8 -*-
"""编排器 — 对话流水线总调度

当前流程：检索器（LLM 子代理）→ 分析器 → 回复器（全权生成）→ 组织器（表情包/旁白）→ 记录
"""

import logging, threading, time, random
from datetime import datetime
from dataclasses import dataclass, field

from modules.analyzer import Analyzer, AnalyzerInput
from modules.organizer import Organizer, OrganizerInput
from modules.polisher import Polisher, PolisherInput, DEGRADED_TEXT
from modules.context_manager import ContextManager
from modules.llm_retriever import LlmRetriever, RetrieveInput, has_knowledge
from modules.llm_base import get_token_stats as _get_token_stats
from modules.app_config import DEFAULT_MODE, user_name

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────
@dataclass
class ChatResult:
    messages: list[dict] = field(default_factory=list)
    bubble: str | None = None
    error_code: str | None = None   # LLM 调用失败分类（前端人话提示；None=正常）


# ── 降级话术 ─────────────────────────────────────
_DIRECT_REPLIES = {
    "input:empty":    ["嗯…怎么啦？想说什么就说吧"],
    "input:too_long": ["你说了好多…我慢慢看，等一下哦"],
    "api:error":      [DEGRADED_TEXT],   # 与 polisher 降级输出同源（2026-09-04 合并）
}


def _handle_direct(reason: str) -> list[str]:
    return _DIRECT_REPLIES.get(reason, ["嗯？我走神了…你刚才说了什么？"])


def _err_code(e: Exception) -> str:
    """从异常提取错误分类码（ApiError 自带；其他异常归 unknown）。"""
    from modules.api_client import ApiError
    return e.code if isinstance(e, ApiError) else "unknown"


def _first_degraded_code(*outputs) -> str | None:
    """A3：模块级降级（LLM 失败被吞）时取首个非空 error_code 透传给 /chat。
    流水线已降级不阻断；前端据此显示人话提示（Key 无效/余额/限流…）。"""
    for o in outputs:
        if o is not None and getattr(o, "degraded", False):
            code = getattr(o, "error_code", "") or ""
            if code:
                return code
    return None


# ── A3 超时预算：单轮硬顶 210s（与网关 600s / 前端 4 分钟对齐的收紧）──
# 硬顶落实双保险：阶段入口 _stage_timeout 按 min(阶段预算, 剩余总预算) 设 client._timeout，
# 并把 deadline 挂到 client._deadline；api_client 重试循环在每次 sleep 前也检查 _deadline
# （耗尽或 delay 截断到 0 即抛 timeout），重试不再拖过单轮总预算。
_TURN_BUDGET_SEC = 210.0
_STAGE_TIMEOUT_THINK = 90.0    # 分析/回复（思考档）单阶段超时
_STAGE_TIMEOUT_NON_THINK = 30.0  # 检索/组织（非思考）单阶段超时


def _stage_timeout(client, effort: str, deadline: float, stage: str) -> float:
    """按阶段设客户端超时（min(阶段预算, 剩余总预算)）；剩余不足 5s → 立即放弃本轮。
    同时把 deadline 挂到 client._deadline，供 api_client 重试循环在 sleep 前检查硬顶。
    返回本阶段超时秒数。client 是 _CompatClient / RelayClient / QuotaClient 任意一种。"""
    now = time.time()
    remaining = deadline - now
    if remaining <= 5.0:
        from modules.api_client import ApiError
        raise ApiError(f"单轮总预算（{int(_TURN_BUDGET_SEC)}s）已耗尽", code="timeout")
    budget = _STAGE_TIMEOUT_THINK if effort != "none" else _STAGE_TIMEOUT_NON_THINK
    sec = min(max(budget, 5.0), max(remaining, 5.0))
    try:
        client._timeout = sec
        client._deadline = deadline   # 自定义 client 无属性时静默跳过（不约束）
    except Exception:
        pass
    logger.info("[PIPELINE] %s 阶段超时预算 %.0fs（剩余 %.0fs）", stage, sec, remaining)
    return sec


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


# ── 开场演出（预设包首条自动消息）────────────
# 演出脚本 = 包资产 opening.json（用户副本 {mode}/character/opening.json 优先）；
# 包无此文件 → 无开场。haruno 的脚本内容按 3.8 结尾流萤的想象：两人都是学生，
# 开拓者从很远星球来、被流氓围住，流萤挺身而出救他。


def _load_opening(mode: str) -> dict:
    from modules.llm_base import resolve_character_file
    import json
    fp = resolve_character_file("opening.json", mode)
    if not fp.exists():
        return {"narrations": [], "first_messages": []}   # 包无开场 = 正常，非失败
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return {
            "narrations": [n for n in data.get("narrations", []) if n.get("text")],
            "first_messages": [t for t in data.get("first_messages", []) if t],
        }
    except Exception as e:
        logger.warning("%s 开场脚本加载失败（降级无开场）: %s: %s", mode, fp, e)
        return {"narrations": [], "first_messages": []}


def preset_opening(mode: str) -> list:
    """返回该模式的开场演出消息序列（含旁白与首条消息，均已写盘）。"""
    from modules.conversation_store import append_message as _app
    opening = _load_opening(mode)
    msgs = []
    for n in opening["narrations"]:
        m = {"type": "narration", "text": n["text"], "style": n.get("style", "scene")}
        _app("firefly", m, mode=mode)
        msgs.append(dict(m))
    for text in opening["first_messages"]:
        m = {"type": "text", "content": text}
        seq, t = _app("firefly", m, mode=mode)
        m["time"] = t
        msgs.append(m)
    return msgs


def _get_environment(mode: str = DEFAULT_MODE) -> str:
    now = datetime.now()
    h = now.hour
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    d = f"{now.month}月{now.day}日 {wd} "
    if 5 <= h < 8:       desc = "清晨，天刚亮。"
    elif 8 <= h < 12:    desc = "上午。"
    elif 12 <= h < 14:   desc = "中午。"
    elif 14 <= h < 18:   desc = "下午。"
    elif 18 <= h < 21:   desc = "傍晚。"
    elif 21 <= h < 24:   desc = "夜晚。"
    else:                desc = "深夜，万籁俱寂。"
    # 世界观后缀随预设包（prompts/env_suffix.md，如 haruno 的"黄金时刻永夜"规则）；无则无时
    from modules.llm_base import load_slot
    return f"{d}{desc}{load_slot('prompts/env_suffix', mode)}"


# ── 监控 ─────────────────────────────────────────
_lock = threading.Lock()
_CHAT_COUNT = 0
_DIRECT_COUNT = 0
_ORCH_ERRORS = 0

# ── 阶段进度（前端等待回复时轮询 /chat-stage 显示"正在做什么"）──
# 键 = id(session)（进程内唯一，服务器版多用户天然隔离，不泄露 session_id）
_STAGES: dict[str, str] = {}
# 阶段开始时间戳：前端据此显示"已等待 N 秒"，上游卡住时预警而不静默
_STAGE_START: dict[str, float] = {}
_STAGE_LABELS = {
    "retriever": "正在翻阅记忆与资料…",
    "analyzer": "正在理解你的话…",
    "polisher": "正在酝酿回复…",
    "organizer": "正在准备表情包…",
}


def _set_stage(session, stage: str | None) -> None:
    with _lock:
        key = str(id(session))
        if stage is None:
            _STAGES.pop(key, None)
            _STAGE_START.pop(key, None)
        else:
            _STAGES[key] = stage
            _STAGE_START[key] = time.time()


def get_chat_stage(session) -> str | None:
    """返回当前流水线阶段（None=空闲）。前端轮询用。"""
    with _lock:
        return _STAGES.get(str(id(session)))


def get_stage_waited(session) -> float:
    """当前阶段已等待秒数（无阶段或异常清除后返回 0）。前端超时预警用。"""
    with _lock:
        ts = _STAGE_START.get(str(id(session)))
        return max(0.0, time.time() - ts) if ts else 0.0


def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, "正在思考…")

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
    # 写入作用域标记（服务器版多用户隔离）：落盘文件本身已按用户分目录，
    # 此字段用于 get_pipeline_log 的读取侧复核，防旧格式记录跨界展示。
    try:
        from modules.app_config import user_scope_key
        entry["_scope"] = user_scope_key()
    except Exception:
        entry["_scope"] = ""
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
    """返回当前用户最近 limit 条流水线记录（/pipeline 调试面板）。

    只读本用户的落盘文件 {mode}/data/pipeline.jsonl（mode_data_dir 经用户上下文按账号
    隔离），**不读进程级全局 _PIPELINE_LOG**——该内存列表在服务器版是多账号共享的，
    直接切片返回会造成跨账号会话内容泄漏（2026-09-10 修复）。
    旧格式（无 _scope 字段）的记录一律跳过：宁可少显示，不跨界。"""
    try:
        from modules.app_config import user_scope_key
        _scope = user_scope_key()
    except Exception:
        _scope = ""
    out = []
    try:
        lines = _pipeline_file(mode).read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for ln in reversed(lines):
        if len(out) >= limit:
            break
        try:
            rec = _json.loads(ln)
        except Exception:
            continue
        # 服务器版多用户隔离：_PIPELINE_LOG 是进程级全局，落盘文件才是按用户分的。
        # 只返回属于当前用户作用域的条目（旧的无作用域记录一律跳过，宁可少显示不跨界）。
        if (rec.get("_scope") or "") != _scope:
            continue
        out.append(rec)
    return list(reversed(out))


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
    analyzer_model: str = "deepseek-v4-flash-vision-exp",
    organizer_model: str = "deepseek-v4-flash-vision-exp",
    polisher_model: str = "deepseek-v4-flash-vision-exp",
    retriever_model: str = "deepseek-v4-flash-vision-exp",
    retriever_effort: str = "none",
    analyzer_effort: str = "high",
    polisher_effort: str = "high",
    organizer_effort: str = "none",
    retriever_temperature: float = 0.0,
    polisher_temperature: float = 0.5,
    memory_head: str = "",
    hint: str = "",
    mode: str = DEFAULT_MODE,
    vision_images: list | None = None,   # A9：首轮识图 base64 data URL 列表（仅本地版直接链路）
) -> ChatResult:
    global _CHAT_COUNT, _DIRECT_COUNT, _ORCH_ERRORS
    with _lock:
        _CHAT_COUNT += 1
        turn = _CHAT_COUNT

    logger.info("[PIPELINE #%d] ====== 开始处理 chat 请求 ======", turn)
    logger.info("[PIPELINE #%d] user_input=%s hint=%s", turn,
                (user_input[:80] if user_input else "(empty)"), hint)

    ctx: ContextManager = session["context"]
    _set_stage(session, None)   # 清除旧阶段（异常/完成路径也会清）

    # A3：本轮总预算（前方链路的每阶段超时 + 本硬顶共同约束）
    deadline = time.time() + _TURN_BUDGET_SEC

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
    environment = _get_environment(mode)
    # LLM 子代理检索：知识库整体注入（system 稳定前缀，缓存高命中），
    # 输出压缩知识摘要。无本地模型依赖（安卓端可行），全局性覆盖。
    # 话题锚点只取上一条用户消息：指代消解；话题理解归 analyzer（20 轮历史），
    # 子代理不重复接收流萤自产回复（噪音 + miss 成本 + 话题漂移）。
    # 无知识库的包（未声明 knowledge_dirs 且包内无 knowledge/）：跳过，省一次 LLM 调用。
    try:
        if not has_knowledge(mode):
            retrieved_knowledge = ""
            _rt0 = _rt1 = time.perf_counter()
        else:
            _set_stage(session, "retriever")
            anchor = [m for m in ctx.get_recent(10) if m.get("role") == "user"][-1:]
            _rt0 = time.perf_counter()
            logger.info("[PIPELINE #%d] ⓪ Retriever 开始...", turn)
            _stage_timeout(client, retriever_effort, deadline, "retriever")
            r_out = LlmRetriever(client, model=retriever_model,
                                 temperature=retriever_temperature,
                                 effort=retriever_effort, mode=mode).retrieve(RetrieveInput(
                user_input=user_input,
                recent_history=anchor,
            ))
            _rt1 = time.perf_counter()
            retrieved_knowledge = r_out.knowledge
            logger.info("[PIPELINE #%d] ⓪ Retriever 完成 (%.1fs)", turn, _rt1 - _rt0)
    except Exception as e:
        from modules.api_client import ApiError
        # 预算耗尽：异常上抛（走外层 api:error 路径带 timeout 码），不静默吞掉
        if isinstance(e, ApiError) and e.code == "timeout":
            raise
        retrieved_knowledge = ""
        _rt0 = _rt1 = time.perf_counter()
        logger.warning("[PIPELINE #%d] ⓪ Retriever 失败: %s: %s", turn, type(e).__name__, e)

    try:
        # ── 1. 分析器 ──────────────────────────
        input_text = user_input
        if hint == "typing_long":
            input_text = f"（{user_name(mode)}正在输入了很久，但还没有发送。你可以先问：怎么了？有什么想和我说的吗？）"
        elif hint == "still_typing":
            input_text = f"{user_input}\n（注意：{user_name(mode)}还在输入第二条消息，可能还有下文）"

        _t0 = time.perf_counter()
        logger.info("[PIPELINE #%d] ① Analyzer 开始...", turn)
        _set_stage(session, "analyzer")
        _stage_timeout(client, analyzer_effort, deadline, "analyzer")
        analyzer = Analyzer(client, model=analyzer_model, effort=analyzer_effort, mode=mode)
        analysis = analyzer.analyze(AnalyzerInput(
            user_input=input_text,
            recent_history=ctx.get_recent(20),
            retrieved_knowledge=retrieved_knowledge,
            environment=environment,
        ))
        _t1 = time.perf_counter()
        logger.info("[PIPELINE #%d] ① Analyzer 完成 (%.1fs), intent=%s",
                    turn, _t1 - _t0, (analysis.intent[:60] if analysis.intent else "?"))

        # ── 2. 回复器（全权生成回复文本）────────
        logger.info("[PIPELINE #%d] ② Polisher 开始...", turn)
        _set_stage(session, "polisher")
        _stage_timeout(client, polisher_effort, deadline, "polisher")
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
            vision_images=(vision_images or []),
        ))
        _t2 = time.perf_counter()
        messages = list(polish_output.messages)
        logger.info("[PIPELINE #%d] ② Polisher 完成 (%.1fs), 生成 %d 条消息",
                    turn, _t2 - _t1, len(messages))

        # ── 3. 组织器（story=表情包调度；haruno=旁白演出）──
        # 失败只损失表情包/旁白，不影响文本回复
        logger.info("[PIPELINE #%d] ③ Organizer 开始...", turn)
        _set_stage(session, "organizer")
        org_output = None
        try:
            _stage_timeout(client, organizer_effort, deadline, "organizer")
            organizer = Organizer(client, model=organizer_model, effort=organizer_effort, mode=mode)
            org_output = organizer.organize(OrganizerInput(
                user_input=user_input,
                reply_texts=[m["content"] for m in messages if m.get("type") == "text"],
                recent_history=ctx.get_recent(5),
                mode=mode,
            ))
            if org_output.sticker_label:
                from domain.stickers.picker import pick_sticker_by_label
                entry = pick_sticker_by_label(org_output.sticker_label, mode)
                if entry:
                    messages.append({"type": "sticker", "path": entry.file, "label": entry.label})
            # haruno 旁白：after=-1 前置；after=n 插在第 n+1 条消息之后（视觉小说式穿插演出）
            if org_output.narrations:
                messages = _merge_narrations(messages, org_output.narrations)
        except Exception as e:
            logger.warning("工具调度失败（跳过表情包/旁白）: %s", e)
        _t3 = time.perf_counter()
        logger.info("[PIPELINE #%d] ③ Organizer 完成 (%.1fs), sticker=%s",
                    turn, _t3 - _t2, org_output.sticker_label if org_output else "无")

        _set_stage(session, None)
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
        import traceback
        _set_stage(session, None)
        logger.error("[PIPELINE #%d] 编排器异常 %s: %s\n%s",
                     turn, type(e).__name__, e, traceback.format_exc())
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
        return ChatResult(messages=messages, bubble=None, error_code=_err_code(e))

    # ── 4. 记录历史 ────────────────────────────
    _set_stage(session, None)
    total_elapsed = _t3 - _rt0 if _t3 > 0 else 0
    logger.info("[PIPELINE #%d] ====== 完成 (总耗时 %.1fs), 回复 %d 条 =====",
                turn, total_elapsed, len(messages))
    texts = [m["content"] for m in messages if m.get("type") == "text"]
    ctx.add_turn(user_input, " ".join(texts) if texts else "(表情包)")
    for m in messages:
        if m.get("type") == "sticker":
            ctx.add_action("表情包", m.get("label", "表情"))
        elif m.get("type") == "narration":
            # 旁白进上下文：回复器下轮能看到"她做了什么动作/环境如何"
            ctx.add_action("旁白", m.get("text", ""))

    return ChatResult(messages=messages, bubble=None, error_code=_first_degraded_code(
        locals().get("r_out"), analysis, polish_output, org_output))
