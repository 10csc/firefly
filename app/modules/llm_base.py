# -*- coding: utf-8 -*-
"""LLM 基础设施 — 三个模块共享的调用/解析/缓存/加载逻辑

提取理由：analyzer/organizer/polisher 各自复制了 _load_slot、_record_cache_stats、
JSON 提取逻辑。 locality 破裂——JSON 解析 bug 要改三处。
此模块提供共享基础设施，各模块只保留 prompt + dataclass + 验证。
"""

import json, logging, threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 运行时数据根目录 ─────────────────────────────────
# 唯一来源：app_config。前端所有"编辑保存"类接口都写 user_data/，
# 因此读取必须 user_data 优先，否则出现"前端显示已改、模型仍用旧文件"的静默分裂。
from modules.app_config import USER_DIR, ROOT as _ROOT, mode_character_dir, bundled_character_dir, mode_journal_dir, DEFAULT_MODE, user_scope_key

# ── 角色设定加载（共享缓存，key 含模式，跨模式不串）──
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def resolve_character_file(name: str, mode: str = DEFAULT_MODE) -> Path:
    """设定文件路径：{mode} 数据目录优先，退回 bundled {mode} 默认。"""
    u = mode_character_dir(mode) / name
    return u if u.exists() else bundled_character_dir(mode) / name


def load_slot(slot: str, mode: str = DEFAULT_MODE) -> str:
    """按 slot 名读取角色设定（{mode} 优先），模块级缓存（key 含用户维度防服务器版串扰）。"""
    key = (slot, mode, user_scope_key())
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
        fp = resolve_character_file(f"{slot}.md", mode)
        if fp.exists():
            _CACHE[key] = fp.read_text(encoding="utf-8").strip()
        else:
            _CACHE[key] = ""
        return _CACHE[key]


def clear_cache():
    """清除角色设定缓存（前端编辑设定文件后调用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ── 手账加载（按模式隔离）───────────────────────────
# 统一写入点：{mode}/journal/手账.md（前端保存、记忆管理器更新都写这里）。
# 读取 fallback 旧位置 knowledge/story/手账.md，兼容历史数据。
def _journal_file(mode: str = DEFAULT_MODE) -> Path:
    return mode_journal_dir(mode) / "手账.md"


_JOURNAL_LEGACY = _ROOT / "knowledge" / "story" / "手账.md"
_JOURNAL_CACHE: dict[str, str | None] = {}
_JOURNAL_LOCK = threading.Lock()


def load_journal(mode: str = DEFAULT_MODE) -> str:
    """加载流萤的手账，模块级缓存（key 含用户维度防服务器版串扰）。legacy 位置仅 story 模式兼容。"""
    with _JOURNAL_LOCK:
        ck = f"{mode}::{user_scope_key()}"
        if ck in _JOURNAL_CACHE:
            return _JOURNAL_CACHE[ck] or ""
        fp = _journal_file(mode)
        if not fp.exists() and mode == DEFAULT_MODE:
            fp = _JOURNAL_LEGACY
        if fp.exists():
            _JOURNAL_CACHE[ck] = fp.read_text(encoding="utf-8").strip()
        else:
            _JOURNAL_CACHE[ck] = ""
        return _JOURNAL_CACHE[ck] or ""


def reload_journal(mode: str = DEFAULT_MODE):
    """强制重新加载手账（休息后调用）。"""
    with _JOURNAL_LOCK:
        _JOURNAL_CACHE.pop(f"{mode}::{user_scope_key()}", None)


# ── 历史格式化（各模块共享）──────────────────────────
def format_history(messages: list) -> str:
    """把 context 历史格式化为带轮次标注的文本。system 行为消息原样保留。"""
    lines = []
    turn = 0
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            turn += 1
            lines.append(f"[第{turn}轮] 开拓者: {content}")
        elif role == "assistant":
            if m.get("proactive"):
                lines.append(f"      流萤(主动): {content}")
            else:
                lines.append(f"      流萤: {content}")
        elif role == "system":
            lines.append(f"      {content}")
    return "\n".join(lines) if lines else "（无历史）"


# ── Token统计 ────────────────────────────────────────
_stats_lock = threading.Lock()
_cache_hits = 0
_cache_misses = 0
_prompt_tokens = 0
_completion_tokens = 0
_reasoning_tokens = 0
_total_tokens = 0
_model_prompt: dict[str, int] = {}
_model_completion: dict[str, int] = {}


# ── 请求日志 ─────────────────────────────────────────
_REQUEST_LOG: list[dict] = []
_REQUEST_LOG_MAX = 500
_REQUEST_LOG_LOCK = threading.Lock()
from datetime import datetime


def _log_request(module: str, model: str, success: bool,
                 pt: int, ct: int, tt: int, hit: int, miss: int, cost_cny: float, error: str = ""):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "module": module, "model": model, "success": success,
        "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt,
        "cache_hit": hit, "cache_miss": miss,
        "cost_cny": round(cost_cny, 6),
    }
    if error:
        entry["error"] = error
    with _REQUEST_LOG_LOCK:
        _REQUEST_LOG.append(entry)
        if len(_REQUEST_LOG) > _REQUEST_LOG_MAX:
            _REQUEST_LOG.pop(0)


# 定价（人民币/百万tokens，来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing）
_PRICING = {
    "deepseek-v4-flash":  {"hit": 0.02, "miss": 1, "output": 2},
    "deepseek-v4-pro":    {"hit": 0.025, "miss": 3, "output": 6},
}


def _calc_cost(model: str, hit: int, miss: int, ct: int) -> float:
    pr = _PRICING.get(model, _PRICING["deepseek-v4-flash"])
    return hit / 1e6 * pr["hit"] + miss / 1e6 * pr["miss"] + ct / 1e6 * pr["output"]


def record_usage(module: str, resp):
    """提取 token 统计并记录到累计计数器和请求日志。"""
    global _cache_hits, _cache_misses, _prompt_tokens, _completion_tokens, _reasoning_tokens, _total_tokens
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    model = getattr(resp, "model", "unknown") or "unknown"

    hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    tt = getattr(usage, "total_tokens", 0) or 0
    rt = 0
    details = getattr(usage, "completion_tokens_details", None)
    if details:
        rt = getattr(details, "reasoning_tokens", 0) or 0

    with _stats_lock:
        _cache_hits += hit
        _cache_misses += miss
        _prompt_tokens += pt
        _completion_tokens += ct
        _reasoning_tokens += rt
        _total_tokens += tt
        _model_prompt[model] = _model_prompt.get(model, 0) + pt
        _model_completion[model] = _model_completion.get(model, 0) + ct

    cost = _calc_cost(model, hit, miss, ct)
    _log_request(module, model, True, pt, ct, tt, hit, miss, cost)


def record_error(module: str, model: str, error: str):
    """记录失败的 LLM 请求。"""
    cost = _calc_cost(model, 0, 0, 0)
    _log_request(module, model, False, 0, 0, 0, 0, 0, cost, error)


def get_token_stats() -> dict:
    """返回 token 统计及估算费用（人民币）。"""
    with _stats_lock:
        total_input = _cache_hits + _cache_misses
        rate = (_cache_hits / total_input) if total_input > 0 else 0.0
        cost_breakdown = {}
        total_cost = 0.0
        for model, pt in _model_prompt.items():
            ct = _model_completion.get(model, 0)
            pr = _PRICING.get(model, _PRICING["deepseek-v4-flash"])
            est_hit = int(pt * rate) if total_input > 0 else 0
            est_miss = pt - est_hit
            cost = est_hit / 1e6 * pr["hit"] + est_miss / 1e6 * pr["miss"] + ct / 1e6 * pr["output"]
            cost_breakdown[model] = {"prompt_tokens": pt, "completion_tokens": ct, "cache_hit": est_hit, "cache_miss": est_miss, "cost_cny": round(cost, 6)}
            total_cost += cost
        return {"cache_hit_tokens": _cache_hits, "cache_miss_tokens": _cache_misses, "cache_hit_rate": round(rate, 4), "prompt_tokens": _prompt_tokens, "completion_tokens": _completion_tokens, "reasoning_tokens": _reasoning_tokens, "total_tokens": _total_tokens, "by_model": cost_breakdown, "total_cost_cny": round(total_cost, 6)}


def get_request_log(limit: int = 200) -> list[dict]:
    """返回最近的请求日志。"""
    with _REQUEST_LOG_LOCK:
        return list(_REQUEST_LOG[-limit:])


# ── JSON 提取（从 LLM 输出中提取第一个完整 JSON 对象）──
def extract_json(raw: str) -> str:
    """从 LLM 原始输出中提取第一个完整的 JSON 对象字符串。

    处理：去 markdown fence、去前后噪音、按括号深度提取。
    解析失败时返回原始字符串（让调用方 json.loads 抛异常）。
    """
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:]) if len(lines) > 1 else raw
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    start = raw.find("{")
    if start < 0:
        return raw

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]

    return raw[start:]


def parse_json(raw: str) -> dict | None:
    """提取并解析 JSON，失败返回 None。"""
    extracted = extract_json(raw)
    try:
        return json.loads(extracted)
    except json.JSONDecodeError as e:
        logger.warning("JSON 解析失败: %s", e)
        return None
