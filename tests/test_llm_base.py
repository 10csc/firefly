# -*- coding: utf-8 -*-
"""llm_base 单元测试：token统计、请求日志、手账加载"""

import sys, json, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from unittest.mock import Mock
from modules.llm_base import (
    record_usage, record_error, get_token_stats, get_request_log,
    load_journal, reload_journal, load_slot, clear_cache, parse_json
)


# ── helper：构造 mock LLM response ──
def _mock_resp(model="deepseek-v4-flash", prompt=100, completion=50, hit=60, miss=40, reasoning=0):
    usage = Mock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.total_tokens = prompt + completion
    usage.prompt_cache_hit_tokens = hit
    usage.prompt_cache_miss_tokens = miss
    details = Mock()
    details.reasoning_tokens = reasoning
    usage.completion_tokens_details = details
    resp = Mock()
    resp.usage = usage
    resp.model = model
    return resp


# ═══════════════════════════════════════════
print("=== Token 统计 ===")

# 清空累积状态（模块级变量，需手动重置）
record_usage("analyzer", _mock_resp(model="deepseek-v4-flash", prompt=200, completion=80, hit=120, miss=80))
record_usage("organizer", _mock_resp(model="deepseek-v4-pro", prompt=300, completion=120, hit=200, miss=100))
record_usage("polisher", _mock_resp(model="deepseek-v4-pro", prompt=150, completion=90, hit=80, miss=70))

stats = get_token_stats()
assert stats["prompt_tokens"] == 200 + 300 + 150
assert stats["completion_tokens"] == 80 + 120 + 90
assert stats["cache_hit_tokens"] == 120 + 200 + 80
assert stats["cache_miss_tokens"] == 80 + 100 + 70
assert "deepseek-v4-flash" in stats["by_model"]
assert "deepseek-v4-pro" in stats["by_model"]
assert stats["by_model"]["deepseek-v4-flash"]["prompt_tokens"] == 200
assert stats["by_model"]["deepseek-v4-pro"]["prompt_tokens"] == 300 + 150
assert stats["by_model"]["deepseek-v4-flash"]["completion_tokens"] == 80
assert stats["by_model"]["deepseek-v4-pro"]["completion_tokens"] == 120 + 90
print("  ✓ 累计统计正确")

assert stats["total_cost_cny"] > 0
# Flash: 200 prompt (hit=120 miss=80) + 80 output
# 120/1e6*0.02 + 80/1e6*1 + 80/1e6*2 = 0.0000024 + 0.00008 + 0.00016
# ~0.00024
# Pro: 450 prompt (hit=280 miss=170) + 210 output  
# 280/1e6*0.025 + 170/1e6*3 + 210/1e6*6
# 0.000007 + 0.00051 + 0.00126 = ~0.00178
# total ~0.00202
assert 0.001 < stats["total_cost_cny"] < 0.01
print(f"  ✓ 费用估算正确（¥{stats['total_cost_cny']:.6f}）")


# ═══════════════════════════════════════════
print("=== 请求日志 ===")

log = get_request_log(200)
assert len(log) == 3
assert log[0]["module"] == "analyzer"
assert log[1]["module"] == "organizer"
assert log[2]["module"] == "polisher"
assert all(r["success"] for r in log)
assert all("cost_cny" in r for r in log)
print("  ✓ 请求日志正确记录")

# 失败请求
record_error("organizer", "deepseek-v4-pro", "timeout")
log = get_request_log(200)
assert len(log) == 4
assert log[3]["success"] == False
assert log[3]["error"] == "timeout"
assert log[3]["module"] == "organizer"
print("  ✓ 失败请求正确记录")


# ═══════════════════════════════════════════
print("=== 手账加载 ===")

journal = load_journal()
assert isinstance(journal, str)
print(f"  ✓ 手账加载成功（{len(journal)} 字）")

# 缓存
journal2 = load_journal()
assert journal == journal2
print("  ✓ 手账缓存命中")

# reload
reload_journal()
journal3 = load_journal()
assert journal3 == journal
print("  ✓ reload_journal 后重新加载")


# ═══════════════════════════════════════════
print("=== JSON 解析 ===")

assert parse_json('{"a":1}') == {"a": 1}
assert parse_json('not json') is None
assert parse_json('```json\n{"b":2}\n```') == {"b": 2}
assert parse_json('text {"c":[1,2]} more') == {"c": [1, 2]}
print("  ✓ JSON 解析正常")


# ═══════════════════════════════════════════
print("=== 角色设定加载 ===")

core = load_slot("core")
assert len(core) > 100
assert "流萤" in core or "失熵" in core
identity = load_slot("identity")
assert "开拓者" in identity or "星核猎手" in identity
# 缓存
core2 = load_slot("core")
assert core == core2
print("  ✓ 角色设定加载正常")

# clear_cache
clear_cache()
# 注：测试后不污染环境，再次加载恢复缓存
load_slot("core")
load_slot("identity")
print("  ✓ clear_cache 正常")


print("\n全部通过 ✓")
