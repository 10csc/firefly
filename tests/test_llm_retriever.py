# -*- coding: utf-8 -*-
"""子代理检索白盒测试 — mock LLM 测审查/降级/输出"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.llm_retriever import LlmRetriever, RetrieveInput, InputRejected

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print("  V", desc)
    else: FAIL += 1; print("  X", desc)


class MockMessage:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = ""

class MockChoice:
    def __init__(self, content):
        self.message = MockMessage(content)

class MockResponse:
    def __init__(self, content):
        self.choices = [MockChoice(content)]

class MockCompletions:
    def __init__(self, responses):
        self._queue = list(responses)
    def create(self, *, model, messages, max_tokens=200, temperature=0.0, extra_body=None):
        self.last_kwargs = {"model": model, "messages": messages, "temperature": temperature, "extra_body": extra_body}
        if self._queue:
            return MockResponse(self._queue.pop(0))
        return MockResponse("")

class MockClient:
    def __init__(self, responses=None):
        self.chat = type("Chat", (), {"completions": MockCompletions(responses or [""])})()


# ============================================================
# 1. 审查：非法输入拒绝
# ============================================================
print("=== 审查 ===")
mm = LlmRetriever(MockClient(["知识摘要"]))
try:
    mm.retrieve("裸字符串")
    check("非 RetrieveInput 应拒绝", False)
except InputRejected:
    check("非 RetrieveInput 拒绝", True)
try:
    mm.retrieve(RetrieveInput(user_input="   "))
    check("空输入应拒绝", False)
except InputRejected:
    check("空输入拒绝", True)
try:
    mm.retrieve(RetrieveInput(user_input="hi", recent_history="not-a-list"))
    check("非法历史应拒绝", False)
except InputRejected:
    check("非法历史拒绝", True)


# ============================================================
# 2. 正常检索（mock LLM）
# ============================================================
print("\n=== 正常检索 ===")
client = MockClient(["开拓者问身体，相关：医疗舱设定、失熵症恢复、银狼态度。"])
mm = LlmRetriever(client, model="deepseek-v4-flash", temperature=0.2)
out = mm.retrieve(RetrieveInput(user_input="你还在医疗舱里吗", recent_history=[
    {"role": "user", "content": "晚上好"},
    {"role": "assistant", "content": "晚上好呀"},
]))
check("输出知识非空", len(out.knowledge) > 0)
check("输出原文保留", out.raw == out.knowledge)
kc = client.chat.completions.last_kwargs
check("Non-think 关闭思考", kc["extra_body"] == {"thinking": {"type": "disabled"}})
check("temperature 生效传参", kc["temperature"] == 0.2)
check("model 正确", kc["model"] == "deepseek-v4-flash")
check("system 含设定资料库", "设定资料库" in client.chat.completions.last_kwargs["messages"][0]["content"])
check("user 含用户输入", "你还在医疗舱里吗" in client.chat.completions.last_kwargs["messages"][1]["content"])


# ============================================================
# 3. 降级：LLM 异常/空输出 → 空知识
# ============================================================
print("\n=== 降级 ===")
class BoomCompletions:
    def create(self, **kw):
        raise RuntimeError("boom")
class BoomClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": BoomCompletions()})()
out2 = LlmRetriever(BoomClient()).retrieve(RetrieveInput(user_input="测试"))
check("LLM 异常降级为空", out2.knowledge == "")

out3 = LlmRetriever(MockClient([""])).retrieve(RetrieveInput(user_input="测试"))
check("空输出降级为空", out3.knowledge == "")


# ============================================================
# 汇总
# ============================================================
print("\n" + "=" * 50)
print(f"  通过: {PASS}  失败: {FAIL}")
print("=" * 50)
if FAIL > 0:
    sys.exit(1)
