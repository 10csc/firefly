# -*- coding: utf-8 -*-
"""记忆游标自愈（阶段 3 · 任务 3.6）

问题：`.memory_index` 的 `last_integrated_turn` 是"整理到第几轮"的唯一依据。一旦偏大
（撤回 / 清历史 / 换机恢复 / 将来任何新的写对话入口忘了同步回写），后续整理会判定
"没有新对话"而**永久跳过** —— 用户表现为"记忆再也不更新"，全程零报错。
routes 的 undo / clear-history 各有一处即时回写，但每加一个写对话入口就多一个补偿点；
本卡在 `wake()` / `rest()` 入口做**兜底自愈**，两处即时回写保留（快，但不再是唯一防线）。

守四件事：
1. 游标偏大 → 自愈压低并落盘；
2. 游标正常/偏小 → **不动盘**（读路径不该改盘，延续阶段 2.6 的口径）；
3. 无 index 文件 → 不自愈、**不创建**任何文件（读路径零副作用）；
4. `wake()` 入口确实调用自愈（模块级入口，client=None 也能跑）。

沙箱纪律：USER_DIR 指到临时目录，绝不触碰真实 user_data。
"""
import json
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_index_heal_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

from modules import memory_manager as mm            # noqa: E402
from modules.conversation_store import append_message, count_user_turns   # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


IDX = mm._index_file("story")


def _write_idx(v):
    IDX.parent.mkdir(parents=True, exist_ok=True)
    IDX.write_text(json.dumps({"last_integrated_turn": v}), encoding="utf-8")


def _read_idx():
    return int(json.loads(IDX.read_text(encoding="utf-8"))["last_integrated_turn"])


print("=== A. 前置：造 3 轮用户对话 ===")
for i in range(3):
    append_message("user", {"type": "text", "content": f"第{i+1}句"}, mode="story")
check("A1 真实用户轮数 = 3", count_user_turns(mode="story") == 3)

print("=== B. 游标偏大 → 自愈压低 ===")
_write_idx(10)
_ret = mm.verify_index("story")
check("B1 返回压低后的游标（= 真实轮数）", _ret == 3)
check("B2 落盘已压低", _read_idx() == 3)
check("B3 再次自愈为 no-op（幂等）", mm.verify_index("story") == 3 and _read_idx() == 3)

print("=== C. 游标正常/偏小 → 不动盘（读路径零副作用） ===")
_write_idx(1)
_mtime = IDX.stat().st_mtime_ns
check("C1 偏小不修正且原样返回", mm.verify_index("story") == 1)
check("C2 未重写文件（mtime 不变）", IDX.stat().st_mtime_ns == _mtime)
_write_idx(3)
check("C3 相等同样不动", mm.verify_index("story") == 3)

print("=== D. 无 index → 不创建文件 ===")
IDX.unlink()
_before = {p.name for p in IDX.parent.iterdir()} if IDX.parent.exists() else set()
check("D1 无 index 返回 0", mm.verify_index("story") == 0)
_after = {p.name for p in IDX.parent.iterdir()} if IDX.parent.exists() else set()
check("D2 没有创建 .memory_index（读路径零副作用）", ".memory_index" not in _after
      and _after == _before)

print("=== E. wake() 入口调用自愈（模块级入口，client=None 也可） ===")
_write_idx(99)
try:
    mm.wake(mode="story")     # client=None：内部构造不走 __init__，只为触发自愈
except Exception as e:
    check(f"E0 wake 不应抛异常（{type(e).__name__}: {e}）", False)
check("E1 wake 后游标被压低", _read_idx() == 3)

print("=== F. rest() 入口在切片前自愈（用桩 client 走到切片判定） ===")


class _StubResp:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text,
                                                                 "reasoning_content": ""})()})()]


class _StubCompletions:
    def create(self, **kw):
        return _StubResp("{}")


class _StubClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _StubCompletions()})()


_write_idx(50)
_manager = mm.MemoryManager(_StubClient(), mode="story")
try:
    _res = _manager.rest([{"who": "user", "type": "text", "content": "x"}], 3)
except Exception as e:
    _res = None
    print("    rest 抛错（可接受，只要游标已自愈）:", type(e).__name__, e)
check("F1 rest 入口触发了自愈（游标 ≤ 真实轮数）", _read_idx() <= 3)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
