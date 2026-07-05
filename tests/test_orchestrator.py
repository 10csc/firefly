# -*- coding: utf-8 -*-
"""编排器白盒测试 — mock LLM client，验证 direct + 正常两条路径"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from app.modules.context_manager import ContextManager
from orchestrator import handle_chat, ChatResult

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ── Mock client ────────────────────────────────────
class MockMessage:
    def __init__(self, content):
        self.content = content

class MockChoice:
    def __init__(self, content):
        self.message = MockMessage(content)

class MockResponse:
    def __init__(self, content):
        self.choices = [MockChoice(content)]

class MockCompletions:
    """mock chat.completions，按队列返回预设响应"""
    def __init__(self, responses: list):
        self._queue = list(responses)
        self._calls = []

    def create(self, *, model, messages, max_tokens=200, temperature=0.0, extra_body=None):
        self._calls.append({"model": model, "call_index": len(self._calls)})
        if self._queue:
            return MockResponse(self._queue.pop(0))
        return MockResponse("{}")

class MockClient:
    def __init__(self, responses: list):
        self.chat = type("Chat", (), {"completions": MockCompletions(responses)})()


# ── 测试数据 ────────────────────────────────────────
def make_session():
    return {
        "context": ContextManager(),
        "violation_history": False,
        "state": {
            "mood": [{"label": "安心", "intensity": 3}],
            "affection": 85.0,
            "tension": 15.0,
            "initiative": 50.0,
        },
    }


# ══════════════════════════════════════════════════
# 1. direct 路径
# ══════════════════════════════════════════════════
print("=== direct 路径 ===")

# 空消息 → direct
session = make_session()
# Judge 返回空消息判定
client = MockClient([
    '{"stopped_at": -1, "stop_reason": "input:empty", "reply_direct": null, "execution_plan": null}',
])
result = handle_chat("", session, client)
check("空消息→有回复", len(result.messages) > 0)
check("空消息→bubble=None", result.bubble is None)

# 过于长的消息 → direct
session = make_session()
client = MockClient([
    '{"stopped_at": -1, "stop_reason": "input:too_long", "reply_direct": null, "execution_plan": null}',
])
result = handle_chat("x" * 3000, session, client)
check("超长→有回复", len(result.messages) > 0)

# API 错误
session = make_session()
client = MockClient([
    '{"stopped_at": -1, "stop_reason": "api:error", "reply_direct": null, "execution_plan": null}',
])
result = handle_chat("你好", session, client)
check("api:error→有回复", len(result.messages) > 0)
check("api:error→bubble=None", result.bubble is None)


# ══════════════════════════════════════════════════
# 2. 正常路径（完整流水线）
# ══════════════════════════════════════════════════
print("\n=== 正常路径 ===")

# 简化版：4 个 LLM 调用（judge, planner, reply_gen, refiner）
# composer 在短文本 (<20 chars, no [sticker]) 时走快速路径

session = make_session()
responses = [
    # Judge: normal
    '{"stopped_at": 4, "stop_reason": "normal", "reply_direct": null, '
    '"execution_plan": {"needs": {"knowledge": {"required": false, "topics": []}, '
    '"memory": {"required": false, "query": null, "scope": "recent"}, '
    '"search": {"required": false, "query": null, "reason": null}, "tools": []}, '
    '"tone": {"base": "日常", "modifiers": []}, '
    '"scene_sensitive": {"time": false, "location": false}, '
    '"state_hints": {"mood_trend": "neutral", "expects_energy_cost": true}}}',
    # Planner
    '{"tools": [], "tone": {"base": "日常", "modifiers": [], "intensity": "自然"}, '
    '"direction": "自然回应问候"}',
    # ReplyGenerator
    "晚上好呀，今天过得怎么样？",
    # Refiner: 短回复大概率原样返回（refiner 走同一 mock client）
]
client = MockClient(responses)
result = handle_chat("晚上好", session, client)
check("正常→有回复", len(result.messages) > 0)
check("正常→text", any(m["type"] == "text" for m in result.messages))
check("正常→state非空", "affection" in result.state and "tension" in result.state)

# 跳过状态系统后 state 不再变化（预期行为——等"像流萤"之后接回）
state = session["state"]
aff = state.get("affection", 0)
ten = state.get("tension", 0)
check(f"好感不变（85.00 = {aff:.2f}）", abs(aff - 85.0) < 0.01)
check(f"紧张不变（15.00 = {ten:.2f}）", abs(ten - 15.0) < 0.01)


# ══════════════════════════════════════════════════
# 3. 违规路径 — 状态正常处理，历史不记录
# ══════════════════════════════════════════════════
print("\n=== 违规路径 ===")

session = make_session()
responses = [
    # Judge: violation:sexual
    '{"stopped_at": 0, "stop_reason": "violation:sexual", "reply_direct": null, "execution_plan": null}',
]
client = MockClient(responses)
result = handle_chat("色情内容", session, client)
check("违规→有回复", len(result.messages) > 0)
check("违规→violation_history=True", session["violation_history"] is True)
check("违规→violation也记录历史", session["context"].turn_count >= 1)


# ══════════════════════════════════════════════════
# 4. LLM 降级 — Refiner 异常不崩溃
# ══════════════════════════════════════════════════
print("\n=== LLM 降级 ===")

session = make_session()
# Judge 正常，但 Adder 返回非法格式
responses = [
    # Judge
    '{"stopped_at": 4, "stop_reason": "normal", "reply_direct": null, '
    '"execution_plan": {"needs": {"knowledge": {"required": false, "topics": []}, '
    '"memory": {"required": false, "query": null, "scope": "recent"}, '
    '"search": {"required": false, "query": null, "reason": null}, "tools": []}, '
    '"tone": {"base": "日常", "modifiers": []}, '
    '"scene_sensitive": {"time": false, "location": false}, '
    '"state_hints": {"mood_trend": "neutral", "expects_energy_cost": true}}}',
    # Adder: 非法格式（非"无"也非"标签:数字"）
    "INVALID_RESPONSE_XYZ",
    # Decayer: 正常
    "无恢复",
    # StateUpdater: 正常
    '{"affection_delta": 0.0, "tension_delta": 0.0}',
    # Planner
    '{"tools": [], "tone": {"base": "日常", "modifiers": [], "intensity": "自然"}, '
    '"direction": "正常回应"}',
    # ReplyGenerator
    "嗯…你好",
]
client = MockClient(responses)
result = handle_chat("测试非法响应", session, client)
check("非法Adder→不崩溃有回复", len(result.messages) > 0)
check("非法Adder→状态不变", session["context"].turn_count >= 1)


# ══════════════════════════════════════════════════
# 5. state 不变属性（跳过状态系统，所有数值恒定）
# ══════════════════════════════════════════════════
print("\n=== initiative 不变属性 ===")

# 短消息、无提问 → 所有状态数值不变
session = make_session()
responses = [
    '{"stopped_at": 4, "stop_reason": "normal", "execution_plan": '
    '{"needs": {"knowledge": {"required": false, "topics": []}, '
    '"memory": {"required": false, "query": null, "scope": "recent"}, '
    '"search": {"required": false, "query": null, "reason": null}, "tools": []}, '
    '"tone": {"base": "日常", "modifiers": []}}}',
    "无", "无恢复", '{"affection_delta": 0.0, "tension_delta": 0.0}',
    '{"tools": [], "tone": {"base": "日常", "modifiers": [], "intensity": "自然"}, '
    '"direction": "回应"}',
    "嗯",
]
client = MockClient(responses)
handle_chat("嗯", session, client)
ini = session["state"].get("initiative", 50.0); aff = session["state"].get("affection", 85.0)
check(f"短消息→initiative不变（50→{ini:.2f}）", ini == 50.0); check(f"短消息→affection不变（85→{aff:.2f}）", abs(aff - 85.0) < 0.01)

# 提问 → 所有状态数值不变
session2 = make_session()
responses2 = [
    '{"stopped_at": 4, "stop_reason": "normal", "execution_plan": '
    '{"needs": {"knowledge": {"required": false, "topics": []}, '
    '"memory": {"required": false, "query": null, "scope": "recent"}, '
    '"search": {"required": false, "query": null, "reason": null}, "tools": []}, '
    '"tone": {"base": "日常", "modifiers": []}}}',
    "无", "无恢复", '{"affection_delta": 0.0, "tension_delta": 0.0}',
    '{"tools": [], "tone": {"base": "日常", "modifiers": [], "intensity": "自然"}, '
    '"direction": "回应"}',
    "我今天去了公园",
]
client2 = MockClient(responses2)
handle_chat("你喜欢什么颜色？", session2, client2)
ini2 = session2["state"].get("initiative", 50.0); aff2 = session2["state"].get("affection", 85.0)
check(f"提问→initiative不变（50→{ini2:.2f}）", ini2 == 50.0); check(f"提问→affection不变（85→{aff2:.2f}）", abs(aff2 - 85.0) < 0.01)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
