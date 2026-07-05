# -*- coding: utf-8 -*-
"""记忆管理器白盒测试 — mock LLM 测 rest/wake/中断"""

import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.memory_manager import MemoryManager, MemoryError, get_counters, wake as memory_wake

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print("  V", desc)
    else: FAIL += 1; print("  X", desc)


# ---- Mock -------------------------------------------------
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
    def __init__(self, responses: list):
        self._queue = list(responses)

    def create(self, *, model, messages, max_tokens=200, temperature=0.0, extra_body=None):
        if self._queue:
            return MockResponse(self._queue.pop(0))
        return MockResponse("{}")

class MockClient:
    def __init__(self, responses: list = None):
        self.chat = type("Chat", (), {"completions": MockCompletions(responses or ["{}"])})()


# ---- 辅助：临时文件 ---------------------------------------
from pathlib import Path

def _make_mem_path():
    fd, path = tempfile.mkstemp(suffix="_test_memory.md")
    os.close(fd)
    os.unlink(path)  # 删除让 memory manager 自己创建
    return Path(path)

def _make_idx_path():
    fd, path = tempfile.mkstemp(suffix="_test_index.json")
    os.close(fd)
    return Path(path)


# ============================================================
# 1. 空历史 rest
# ============================================================
print("=== 空历史 rest ===")

mm = MemoryManager(
    MockClient([json.dumps({"new_head": "流萤和开拓者刚刚开始对话。", "resolved": [], "added": []})]),
    memory_file=_make_mem_path(),
    index_file=_make_idx_path(),
)
result = mm.rest([], 0)
check("空历史 rest 不崩溃", result is not None)


# ============================================================
# 2. 正常 rest（mock LLM）
# ============================================================
print("\n=== 正常 rest ===")

mem_file = _make_mem_path()
idx_file = _make_idx_path()

mm = MemoryManager(
    MockClient([json.dumps({"new_head": "流萤和开拓者关系逐渐亲密。", "resolved": [], "added": []})]),
    memory_file=mem_file, index_file=idx_file,
)
result = mm.rest([
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "晚上好呀"},
], 1)
check("正常 rest success", result.success is True)
check("新头部非空", len(result.new_head) > 0)
check("integrated_turn=1", result.integrated_turn == 1)
check("memory.md 已创建", os.path.exists(str(mem_file)))

content = open(str(mem_file), encoding="utf-8").read()
check("memory.md 含核心记忆头部", "核心记忆头部" in content)


# ============================================================
# 3. 中断检测
# ============================================================
print("\n=== 中断检测 ===")

# 只有 index 没有 memory = 中断
tmp_idx = _make_idx_path()
with open(tmp_idx, "w", encoding="utf-8") as f:
    f.write(json.dumps({"last_integrated_turn": 5}))
fake_mem = _make_mem_path()  # 文件已被 _make_mem_path 删除，模拟中断

mm2 = MemoryManager(MockClient(), memory_file=fake_mem, index_file=tmp_idx)
try:
    mm2.wake()
    check("中断应抛异常", False)
except MemoryError:
    check("中断抛 MemoryError", True)

# 正常情况：index + memory 都存在
mm3 = MemoryManager(MockClient(), memory_file=mem_file, index_file=tmp_idx)
head = mm3.wake()
check("正常 wake 头部非空", len(head) > 0)


# ============================================================
# 4. 计数器递增（rest 后计数器应该 +1）
# ============================================================
print("\n=== 计数器递增 ===")

before = get_counters().get("rest_count", 0)
mem_file_c = _make_mem_path()
idx_file_c = _make_idx_path()
mm_c = MemoryManager(
    MockClient([json.dumps({"new_head": "测试计数器。", "resolved": [], "added": []})]),
    memory_file=mem_file_c, index_file=idx_file_c,
)
mm_c.rest([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], 1)
after = get_counters().get("rest_count", 0)
check("rest() 后 rest_count +1", after == before + 1)


# ============================================================
# 5. resolved 标记落盘（核心 bug 修复验证）
# ============================================================
print("\n=== resolved 标记落盘 ===")

mem_file_r = _make_mem_path()
idx_file_r = _make_idx_path()

# 先写入一个带承诺的 memory.md
mem_file_r.parent.mkdir(parents=True, exist_ok=True)
with open(mem_file_r, "w", encoding="utf-8") as f:
    f.write("# 核心记忆头部\n\n旧头部。\n\n# 事实与任务（追加区）\n\n## 承诺\n- [2026-07-01] 下次带蛋糕\n\n")

# LLM 返回 resolved 标记这条承诺已完成
mm_r = MemoryManager(
    MockClient([json.dumps({
        "new_head": "新头部。",
        "resolved": [{"type": "承诺", "text": "下次带蛋糕"}],
        "added": [],
    })]),
    memory_file=mem_file_r, index_file=idx_file_r,
)
# index 设为 0 让新对话非空
with open(idx_file_r, "w", encoding="utf-8") as f:
    f.write(json.dumps({"last_integrated_turn": 0}))
mm_r.rest([{"role": "user", "content": "蛋糕吃到了"}, {"role": "assistant", "content": "嗯"}], 1)

content_r = open(str(mem_file_r), encoding="utf-8").read()
check("resolved 条目被标记（已完成）", "下次带蛋糕（已完成）" in content_r)
check("resolved 条目保留（未删除）", "下次带蛋糕" in content_r)


# ============================================================
# 6. 模块级 wake() 中断降级（不抛异常，返回空串）
# ============================================================
print("\n=== 模块级 wake 中断降级 ===")

# 用 monkey-patch 让模块级 _INDEX_FILE / _MEMORY_FILE 指向临时文件
import modules.memory_manager as mm_mod
orig_mem = mm_mod._MEMORY_FILE
orig_idx = mm_mod._INDEX_FILE

tmp_idx_w = _make_idx_path()
with open(tmp_idx_w, "w", encoding="utf-8") as f:
    f.write(json.dumps({"last_integrated_turn": 5}))
tmp_mem_w = _make_mem_path()  # 不存在，模拟中断

mm_mod._MEMORY_FILE = tmp_mem_w
mm_mod._INDEX_FILE = tmp_idx_w
try:
    head = memory_wake(MockClient())
    check("模块级 wake 中断降级返回空串", head == "")
finally:
    mm_mod._MEMORY_FILE = orig_mem
    mm_mod._INDEX_FILE = orig_idx


# ============================================================
# 汇总
# ============================================================
print("\n" + "=" * 50)
print(f"  通过: {PASS}  失败: {FAIL}")
print("=" * 50)
if FAIL > 0:
    sys.exit(1)
