# -*- coding: utf-8 -*-
"""回复器 — 全权生成流萤的短信回复（说什么 + 怎么说一步到位）

架构调整（方案B）：原组织器的内容决策职责并入本模块，消除双重创作
导致的方向偏移与幻觉叠加。表情包决策移交组织器（工具调度器）。
模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
模型: Flash + Think High（可配置为 Pro）
"""

import logging, threading
from dataclasses import dataclass, field

from modules.llm_base import (
    load_slot, load_journal, format_history,
    record_usage, record_error, resolve_character_file, render_pack_prompt,
    ASSET_CORE, ASSET_IDENTITY, ASSET_SMS_SAMPLES, is_relay_client,
)
from modules.app_config import user_scope_key, char_name, user_name

logger = logging.getLogger(__name__)
_lock = threading.Lock()


# ── 异常 ──────────────────────────────────────────
class PolisherError(Exception): pass
class InputRejected(PolisherError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class PolisherInput:
    user_input: str                  # 开拓者刚才说的话
    analyzer_summary: str = ""       # 分析器摘要
    analyzer_intent: str = ""        # 分析器意图
    analyzer_fact_check: list = field(default_factory=list)
    recent_history: list = field(default_factory=list)
    memory_head: str = ""            # 这段对话的过往摘要（rest 整理，窗口外压缩存档）
    environment: str = ""            # 环境（时段描述等）
    proactive_context: str = ""      # 非空 = 主动发消息场景（流萤找开拓者，不是回复）
    vision_images: list = field(default_factory=list)   # A9：base64 data URL 列表（仅首轮）


@dataclass
class PolisherOutput:
    messages: list = field(default_factory=list)  # [{"type":"text","content":"..."}]
    raw: str = ""
    reasoning: str = ""              # 模型思考过程（调试观测用）
    degraded: bool = False           # A3：本次为降级输出（LLM 调用失败被吞）
    error_code: str = ""             # A3：被吞的 ApiError 分类（透传 /chat 提示）


# ── 默认输出（降级用，每次生成新实例避免跨请求污染）──
# 降级话术单一来源：orchestrator 顶层 api:error 与前端 chat.js 网络错误兜底均对齐此句
DEGRADED_TEXT = "嗯…信号不太好，等会儿再试试？"


def _default_message() -> list:
    return [{"type": "text", "content": DEGRADED_TEXT}]


# ── 短信样本加载（模块级缓存，按模式隔离）─────────
_SAMPLES_CACHE: dict[str, str] = {}
_SAMPLES_LOCK = threading.Lock()


def _load_samples(mode: str = "story") -> str:
    with _SAMPLES_LOCK:
        ck = f"{mode}::{user_scope_key()}"   # 用户维度：防服务器版多用户缓存串扰
        if ck in _SAMPLES_CACHE:
            return _SAMPLES_CACHE[ck]
        fp = resolve_character_file("sms_samples.md", mode)
        if fp.exists():
            _SAMPLES_CACHE[ck] = fp.read_text(encoding="utf-8").strip()
        else:
            _SAMPLES_CACHE[ck] = ""
        return _SAMPLES_CACHE[ck]


def clear_samples_cache():
    """清除短信样本缓存（前端编辑 sms_samples.md 后调用）。"""
    global _SAMPLES_CACHE
    with _SAMPLES_LOCK:
        _SAMPLES_CACHE.clear()


# ── 监控 ──────────────────────────────────────────
_POLISH_COUNT = 0
_LLM_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {
            "polish_count": _POLISH_COUNT,
            "llm_errors": _LLM_ERRORS,
        }


# ── Prompt（稳定层：设定 + 风格，跨请求缓存命中）──────
# 按模式拆分：story=剧情模式（匹诺康尼后日常），haruno=春日手信（流萤想象的普通学生生活）。
# 共享骨架：分条/省略号/语气词/表达感情/划边界/禁止短信腔（与剧情无关的通用部分）。

# ── 人设段（预设包 prompts/polisher.md，用户副本优先）+ 骨架（输出协议框架锁死）──
# 包文件是含数据槽位（{core}/{identity}/{user_setting}/{journal}/{sms_samples}）
# 与名字槽位（{char_name}/{user_name}）的模板；缺失/渲染失败回退应急人设（仅数据槽位骨架）。
_POLISHER_FRAME = """{persona}

## 输出格式（严格遵循）
每行一条消息，前缀 [MSG]，**不要输出 [sticker] 行或把 [sticker] 写进消息里**——表情包由调度器单独决定：
[MSG]第一条消息
[MSG]第二条消息"""

_EMERGENCY_PERSONA = """你是{char_name}。你正在用手机给{user_name}发消息。基于设定和当前对话，直接写出你要发的短信。

## 角色核心
{core}

## 人际关系与认知边界
{identity}

## 用户补充的设定（与核心设定同等权威）
{user_setting}

## 手账（重要对话记录与未完成的约定）
{journal}

## 照短信样本感受节奏

{sms_samples}"""


# ── 回复器类 ──────────────────────────────────────
class Polisher:
    def __init__(self, client, model: str = "deepseek-flash",
                 effort: str = "high", temperature: float = 0.5, mode: str = "story"):
        self._client = client
        self._model = model
        self._mode = mode
        try:
            self._temperature = max(0.0, min(2.0, float(temperature)))
        except (TypeError, ValueError):
            self._temperature = 0.5
        # 官方文档：思考模式不支持 temperature（静默无效）；thinking 默认 enabled。
        # effort=none → 显式关闭思考，此时 temperature 才真正生效。
        # 2026-08-13 起 low 是真实档位（直通，旧 low→high 映射已过时）
        self._thinking = effort != "none"
        self._effort = effort if effort in ("low", "high", "max") else "high"

    def polish(self, inp: PolisherInput) -> PolisherOutput:
        global _POLISH_COUNT, _LLM_ERRORS

        # 1. 审查
        _validate_input(inp)

        with _lock:
            _POLISH_COUNT += 1

        # 2. 构建 prompt：人设段从预设包加载（prompts/polisher.md），输出协议框架锁死
        # relay 模式：core/identity/sms_samples 为本地资产→占位符（APP 填充）；
        # 用户设定/手账（用户数据）在服务器。
        relay = is_relay_client(self._client)
        persona = render_pack_prompt(
            "polisher", self._mode, _EMERGENCY_PERSONA,
            core=ASSET_CORE if relay else load_slot("core", self._mode),
            identity=ASSET_IDENTITY if relay else load_slot("identity", self._mode),
            user_setting=load_slot("用户设定", self._mode),
            journal=load_journal(self._mode),
            sms_samples=ASSET_SMS_SAMPLES if relay else _load_samples(self._mode),
            char_name=char_name(self._mode), user_name=user_name(self._mode),
        )
        stable = _POLISHER_FRAME.format(persona=persona)

        history_section = format_history(inp.recent_history, self._mode)
        memory_section = f"## 这段对话的过往摘要\n{inp.memory_head}\n\n" if inp.memory_head else ""
        env_section = f"## 当前环境\n{inp.environment}\n\n" if inp.environment else ""

        # 防复读（数据注入）：提取流萤最近说过的话，显式列出"不要重复"。
        # 规则约束（"禁止复读"）依赖模型自觉，效果有限；把已说内容作为
        # 数据列给模型（与 proactive 的 recent_said 同原理），模型无需靠记性。
        said_lines = []
        for m in reversed(inp.recent_history):
            if m.get("role") == "assistant" and m.get("content"):
                said_lines.append(str(m["content"]).replace("\n", " ")[:60])
                if len(said_lines) >= 3:
                    break
        said_section = ""
        if said_lines:
            said_section = (
                "## 你最近说过的话（下面这些已经说过了——不要重复：同一句、同一梗、同一意象都算）\n"
                + "\n".join(f"- {c}" for c in reversed(said_lines)) + "\n\n"
            )

        # 主动场景：本条消息不是对用户的回复，而是角色主动发起
        if inp.proactive_context:
            input_section = (
                f"## 本条消息是你主动发给{user_name(self._mode)}的（不是回复）\n{inp.proactive_context}\n\n"
                f"请直接输出你主动发给{user_name(self._mode)}的短信序列（不需要[MSG]之外的内容）："
            )
        else:
            input_section = f"## {user_name(self._mode)}刚才说\n{inp.user_input}\n\n请输出短信序列："

        fact_lines = []
        for fc in inp.analyzer_fact_check:
            if isinstance(fc, dict):
                fact_lines.append(f"  - \"{fc.get('claim','')}\" → {fc.get('verdict','不确定')}: {fc.get('note','')}")
        fact_section = "\n".join(fact_lines) if fact_lines else "  （无）"

        # 有图时说明：直接看，勿脑补（user 动态段，不动 system 缓存）
        vision_note = ("## 你正在看开拓者刚发来的图片\n"
                       "先说清它是什么（照片/画/截图/表情包…），再回应内容。\n"
                       "看不清就如实说看不清。不要猜来历（谁拍的、谁画的…）。\n\n"
        ) if inp.vision_images else ""

        dynamic = (
            f"## 最近对话\n{history_section}\n\n"
            f"{said_section}{memory_section}{env_section}"
            "## 分析层权威解读（语义以此为准，不得自行改判；尤其时间指向：说'到时候/以后'=未来状态，回复须针对未来）\n"
            f"意图: {inp.analyzer_intent}\n"
            f"事实核查:\n{fact_section}\n"
            f"摘要: {inp.analyzer_summary}\n\n"
            f"{vision_note}{input_section}"
        )

        # 3. 调 LLM
        try:
            if self._thinking:
                extra = {"thinking": {"type": "enabled"}, "reasoning_effort": self._effort}
            else:
                extra = {"thinking": {"type": "disabled"}}
            # 原生识图：vision_images（base64 data URL）→ user 消息 content blocks；
            # 图片字节仅此一轮进入模型，不落任何日志
            if inp.vision_images:
                blocks = [{"type": "text", "text": dynamic}]
                for url in inp.vision_images:
                    blocks.append({"type": "image_url",
                                   "image_url": {"url": url}})
                user_content = blocks
            else:
                user_content = dynamic
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": stable},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=10000, temperature=self._temperature,
                extra_body=extra,
            )
            record_usage("polisher", resp)
            raw = resp.choices[0].message.content.strip()
            rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
            # DeepSeek 思考模式：极端情况下全部 token 进 reasoning，content 为空。
            # 此时从 reasoning 提取 [MSG] 行作为兜底（思考末尾常已写出消息）。
            if not raw and rc:
                msgs_from_rc = _extract_msg_lines(rc)
                if msgs_from_rc:
                    raw = msgs_from_rc
                else:
                    raw = rc  # 退化为直接解析（大概率仍失败，走降级）
        except Exception as e:
            import traceback
            logger.error("[POLISHER] LLM 调用失败 — model=%s effort=%s thinking=%s "
                         "exception=%s: %s\n%s",
                         self._model, self._effort, self._thinking,
                         type(e).__name__, e, traceback.format_exc())
            with _lock:
                _LLM_ERRORS += 1
            record_error("polisher", self._model, str(e))
            # A3：降级输出携带被吞的 error_code（透传 /chat → 前端人话提示）
            from modules.api_client import error_code_of
            # 带图失败不脑补内容，如实说看不清（2026-08-29）
            fallback = ([{"type": "text", "content": "图片好像看不清……我这边几乎看不见图呢。"}]
                        if inp.vision_images else _default_message())
            return PolisherOutput(messages=fallback, raw="", degraded=True,
                                  error_code=error_code_of(e))

        # 4. 解析输出
        messages = _parse_response(raw)

        return PolisherOutput(messages=messages, raw=raw, reasoning=rc)


# ── 辅助函数 ──────────────────────────────────────
def _validate_input(inp: PolisherInput):
    if not isinstance(inp, PolisherInput):
        raise InputRejected(f"inp 必须为 PolisherInput，实际: {type(inp).__name__}")
    if not isinstance(inp.user_input, str) or not inp.user_input.strip():
        raise InputRejected("user_input 为空")
    if not isinstance(inp.recent_history, list):
        raise InputRejected("recent_history 必须为 list")
    if not isinstance(inp.analyzer_fact_check, list):
        inp.analyzer_fact_check = []


_MAX_REPLY_MESSAGES = 6   # 回复条数硬上限：防回复器循环输出刷屏（实测出现过 46 条）


def _extract_msg_lines(text: str) -> str:
    """从思考内容中提取 [MSG] 行（content 为空时的兜底）。"""
    lines = [l.strip() for l in text.split("\n") if l.strip().startswith("[MSG]")]
    return "\n".join(lines) if lines else ""


def _parse_response(raw: str) -> list:
    """解析 [MSG] 格式为消息列表。表情包决策已移交组织器，[STICKER] 行忽略。"""
    messages = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line.startswith("[MSG]"):
            text = line[5:].strip()
            if not text:
                continue
            if messages and text == messages[-1]["content"]:
                continue  # 相邻完全重复跳过（循环输出的特征之一）
            messages.append({"type": "text", "content": text})
            if len(messages) >= _MAX_REPLY_MESSAGES:
                logger.warning("回复器输出超过 %d 条，已截断（疑似循环重复）", _MAX_REPLY_MESSAGES)
                break

    if not messages:
        logger.warning("回复器解析失败 raw='%s'", raw[:200] if raw else "(empty)")
        messages = _default_message()

    return messages
