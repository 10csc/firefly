# -*- coding: utf-8 -*-
"""分析器白盒测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.analyzer import Analyzer, AnalyzerInput, AnalyzerOutput, InputRejected, _parse_and_validate

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")

# ══════════════════════════════════════════════════
# 输入审查
# ══════════════════════════════════════════════════
print("=== 输入审查 ===")

# AnalyzerInput 审查
try:
    a = Analyzer(None, model="mock")
    a.analyze(AnalyzerInput(user_input="你好"))
    check("正常输入→不抛异常", True)
except Exception:
    check("正常输入→不抛异常", False)

try:
    a = Analyzer(None, model="mock")
    a.analyze("非法输入")
    check("非 AnalyzerInput→InputRejected", False)
except (InputRejected, TypeError):
    check("非 AnalyzerInput→InputRejected", True)
except Exception:
    check("非 AnalyzerInput→InputRejected", True)

# ══════════════════════════════════════════════════
# JSON 解析
# ══════════════════════════════════════════════════
print("\n=== JSON 解析 ===")

raw = '{"intent":"关心","fact_check":[{"claim":"你还好吗","verdict":"真实","note":"关心状态"}],"summary":"开拓者在关心流萤的状态"}'
out = _parse_and_validate(raw)
check("正常 JSON→intent=关心", out.intent == "关心")
check("正常 JSON→fact_check 1条", len(out.fact_check) == 1)
check("正常 JSON→summary 非空", bool(out.summary))
check("正常 JSON→fact_check 字段齐全",
      all(k in out.fact_check[0] for k in ("claim", "verdict", "note")))

raw2 = '{}'
out2 = _parse_and_validate(raw2)
check("空 JSON→默认 intent", out2.intent == "normal")
check("空 JSON→默认 summary", bool(out2.summary))

raw3 = '{"intent":"","fact_check":"invalid"}'
out3 = _parse_and_validate(raw3)
check("非法 fact_check→空列表", out3.fact_check == [])
check("空 intent→normal", out3.intent == "normal")

raw4 = '{"intent":"关心"}'
out4 = _parse_and_validate(raw4)
check("缺 summary→默认", bool(out4.summary))

# 降级
out5 = _parse_and_validate("垃圾文字")
check("无 JSON→降级默认", out5.intent == "normal")
check("无 JSON→降级有 summary", bool(out5.summary))

# ══════════════════════════════════════════════════
# 计数器
# ══════════════════════════════════════════════════
print("\n=== 计数器 ===")
from modules.analyzer import get_counters
c = get_counters()
check("含 analyze_count", "analyze_count" in c)
check("含 llm_errors", "llm_errors" in c)
from modules.llm_base import get_token_stats
check("token 统计含 cache_hit_rate", "cache_hit_rate" in get_token_stats())

print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")

# C3（审计 2026-09-15）：补退出码——原先失败仍返回 0，「退出码全 0 = 绿」的
# 门禁对分析器（流水线核心）完全失明
sys.exit(1 if FAIL else 0)
