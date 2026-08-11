# -*- coding: utf-8 -*-
"""编排器白盒测试 — direct 路径 + mock 三模块（分析器→回复器→工具调度）"""

import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# 隔离 user_data：handle_chat 会写 pipeline.jsonl 到真实 user_data（实测污染过），
# 重定向必须在导入 orchestrator 之前（sticker_picker 的模块级迁移同理）
import modules.app_config as cfg
_tmp = tempfile.mkdtemp(prefix="firefly_test_orch_")
cfg.USER_DIR = __import__("pathlib").Path(_tmp)
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from modules.context_manager import ContextManager
from orchestrator import handle_chat, ChatResult, get_pipeline_log

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")


# ── Mock client（全模块共享）──────────────────────────
class MockMessage:
    def __init__(self, content): self.content = content

class MockChoice:
    def __init__(self, content): self.message = MockMessage(content)

class MockResponse:
    def __init__(self, content): self.choices = [MockChoice(content)]

class MockCompletions:
    def __init__(self, responses: list):
        self._queue = list(responses)
    def create(self, **kwargs):
        if self._queue:
            return MockResponse(self._queue.pop(0))
        return MockResponse("{}")

class MockClient:
    def __init__(self, responses: list = None):
        self.chat = type("Chat", (), {"completions": MockCompletions(responses or [])})()


def make_session():
    return {
        "context": ContextManager(),
        "memory_head": "",
    }


# ══════════════════════════════════════════════════
# direct 路径
# ══════════════════════════════════════════════════
print("=== direct 路径 ===")

result = handle_chat("", make_session(), MockClient())
check("空消息→有回复", len(result.messages) > 0)
check("空消息→bubble=None", result.bubble is None)

result = handle_chat("a" * 2001, make_session(), MockClient())
check("超长→有回复", len(result.messages) > 0)
check("超长→bubble=None", result.bubble is None)

# 全部降级路径（三个模块连续返回空 JSON / 垃圾）
noop_responses = ["{}", "{}", "{}"]
result = handle_chat("你好", make_session(), MockClient(noop_responses))
check("LLM 全部降级→有回复", len(result.messages) > 0)
check("LLM 全部降级→text", any(m.get("type") == "text" for m in result.messages))

# ══════════════════════════════════════════════════
# 正常路径（分析器→回复器→工具调度）
# ══════════════════════════════════════════════════
print("\n=== 正常路径 ===")

responses = [
    "知识摘要：开拓者在关心流萤的身体状况",                # llm_retriever
    '{"intent":"关心","fact_check":[],"summary":"开拓者在关心流萤"}',  # analyzer
    """[MSG]我没事的
[MSG]你不用担心啦""",                                                 # polisher（全权生成）
    '{"sticker":"比心"}',                                              # organizer（工具调度）
]
result = handle_chat("你还好吗", make_session(), MockClient(responses))
check("正常→有回复", len(result.messages) > 0)
check("正常→含 text", any(m.get("type") == "text" for m in result.messages))
check("调度选图→含 sticker", any(m.get("type") == "sticker" for m in result.messages))

responses2 = [
    "知识摘要：今天天气不错",                          # llm_retriever
    '{"intent":"正常","fact_check":[],"summary":"正常聊天"}',
    "[MSG]嗯…今天天气不错呢",
    '{"sticker":"无"}',
]
result2 = handle_chat("今天天气真好", make_session(), MockClient(responses2))
check("正常2→有回复", len(result2.messages) > 0)
check("调度选无→不含 sticker", not any(m.get("type") == "sticker" for m in result2.messages))

# 工具调度失败不影响文本
responses3 = [
    '{"intent":"正常","fact_check":[],"summary":"正常聊天"}',
    "[MSG]晚安啦",
    "垃圾输出不是JSON",
]
result3 = handle_chat("晚安", make_session(), MockClient(responses3))
check("调度解析失败→文本仍在", any(m.get("type") == "text" for m in result3.messages))
check("调度解析失败→无 sticker", not any(m.get("type") == "sticker" for m in result3.messages))

# ══════════════════════════════════════════════════
# 流水线观测
# ══════════════════════════════════════════════════
print("\n=== 流水线观测 ===")
log = get_pipeline_log(10)
check("pipeline 有记录", len(log) > 0)
last = log[-1]
check("记录含 analyzer", "analyzer" in last)
check("记录含 polisher", "polisher" in last)
check("记录含 organizer", "organizer" in last)

print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
sys.exit(1 if FAIL else 0)
