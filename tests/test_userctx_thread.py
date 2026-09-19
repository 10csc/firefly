# -*- coding: utf-8 -*-
"""用户上下文跨线程传播（数据隔离守卫）。

背景（2026-09-19 审查发现）：`core/userctx.py` 用 `contextvars`，而 **contextvars 在新建线程里
是空的、不继承**。`auto_rest.maybe_schedule` 起的是裸 `threading.Thread`，于是后台线程里
`_user_ctx_dir()` 返回 None ⇒ `core/paths.py::mode_root()` 回落到 **全局 USER_DIR**。
服务器版后果：用户的「自动整理」读写错目录 ⇒ 他自己的活跃窗口永不收缩（越聊 prompt 越长越慢），
全局根下还会堆无用目录。本地版看不出来（本地本来就没有用户上下文）——典型"本地全绿、服务器错"。

本测试就是在服务器形态下（设了 user_dir）验证：**后台 worker 解析到的路径必须在用户目录下**。
不加 `contextvars.copy_context()` 时，A3/A4 必然失败。
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

_TMP = Path(tempfile.mkdtemp(prefix="firefly_test_userctx_"))
os.environ["FIREFLY_DATA_DIR"] = str(_TMP)

import modules.app_config as cfg                    # noqa: E402
from core import paths                              # noqa: E402
from core import userctx                            # noqa: E402

cfg.USER_DIR = _TMP / "user_data"                   # 服务器形态的"全局根"
GLOBAL_ROOT = cfg.USER_DIR
USER_DIR = GLOBAL_ROOT / "31"                       # 某个用户的数据根
(USER_DIR / "story").mkdir(parents=True, exist_ok=True)

import modules.auto_rest as AR                      # noqa: E402
from modules import memory_manager as MM            # noqa: E402

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


print("=== A. 机制：copy_context 才能把用户上下文带进新线程 ===")
seen = {}


def _probe(key):
    seen[key] = userctx.user_scope_key()


_tok = userctx.set_user_context(user_dir=USER_DIR)          # 记下 token，C 段要 reset
# ⚠️ 只能 set 一次：set 返回的 token 是**栈式**的，reset 只回退一层。
#    早先这里手滑调了两次，C 段 reset 后仍留着第一层上下文 —— 测试自己把自己坑了。
t = threading.Thread(target=_probe, args=("bare",))          # 裸线程（反例）
t.start(); t.join()
seen_ctx = {}


def _probe_ctx():
    seen_ctx["v"] = userctx.user_scope_key()


import contextvars                                          # noqa: E402
t2 = threading.Thread(target=contextvars.copy_context().run, args=(_probe_ctx,))
t2.start(); t2.join()
check("A1 请求线程里能读到用户作用域", userctx.user_scope_key() == str(USER_DIR),
      userctx.user_scope_key())
check("A2 ★ 裸线程里读不到（这就是那个 bug 的成因）", seen["bare"] == "", repr(seen["bare"]))
check("A3 ★ copy_context 的线程里读得到", seen_ctx["v"] == str(USER_DIR), seen_ctx["v"])

print("\n=== B. 集成：auto_rest 后台 worker 解析到的路径 ===")
recorded = {}


class _FakeMM:
    """替掉 MemoryManager：只记录 rest() 执行时 mode_root 解析到哪。"""

    def __init__(self, client, model, mode=None):
        self.mode = mode

    def rest(self, full, turn, keep_turns=0):
        recorded["mode_root"] = str(paths.mode_root(self.mode))
        class R:
            success = True
            error = ""
            integrated_turn = 0
        return R()

    def load_head(self):
        return "head"

    def update_journal(self, tail):
        recorded["journal"] = True


_orig_mm = MM.MemoryManager
_orig_load = None
import modules.conversation_store as CS                      # noqa: E402
_orig_load = (CS.load_all_context, CS.count_user_turns)
# 这两个补丁要对 B、C 两段都生效：worker 里 `if not full: return` 会在"没有对话文件"时
# 提前返回（全局根下本来就没有对话），那样 C 段就测不到路径解析了。
CS.load_all_context = lambda mode: [{"role": "user", "content": "第1轮"}]
CS.count_user_turns = lambda mode=None: 1
try:
    MM.MemoryManager = _FakeMM
    # 让 maybe_schedule 一定排程：活跃窗口 ≥ 上限
    AR.active_turns = lambda mode: 999
    AR.window_max = lambda: 1

    AR._RUNNING.clear()
    AR.maybe_schedule("story")                 # 在"请求线程"里调用（已设 user_dir）
    for _ in range(50):
        if "mode_root" in recorded:
            break
        time.sleep(0.1)
finally:
    MM.MemoryManager = _orig_mm

root = recorded.get("mode_root", "")
print("    worker 里 mode_root() =", root)
check("B1 worker 真的跑到了 rest()", bool(root))
check("B2 ★ 路径落在**该用户**目录下（不是全局 USER_DIR）",
      root.startswith(str(USER_DIR)), root)
check("B3 ★ 明确不在全局根下", not root.startswith(str(GLOBAL_ROOT / "story")),
      str(GLOBAL_ROOT / "story"))
check("B4 手账也走到了", recorded.get("journal") is True)

print("\n=== C. 本地版（无用户上下文）行为不变：落到全局根 ===")
userctx.reset_user_context(_tok)
recorded.clear()
try:
    MM.MemoryManager = _FakeMM
    # 直接驱动 worker（传个假 client 越过 get_client 判空）——这里要测的是**路径解析**，
    # 不是排程条件；走 maybe_schedule 会先卡在 client 取不到而提前 return。
    contextvars.copy_context().run(AR._worker, "story", None, object())
finally:
    MM.MemoryManager = _orig_mm
root2 = recorded.get("mode_root", "")
print("    无上下文时 mode_root() =", root2)
check("C1 无用户上下文 → 落到全局 USER_DIR（本地版语义不变）",
      root2 == str(GLOBAL_ROOT / "story"), root2)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
