# -*- coding: utf-8 -*-
"""清除历史 → 主动性状态复位回归（阶段 1 · 任务 1.5 / C-6.1，P1）

缺陷：`routes.clear_history` 写的是 `_IGNORED.pop(mode)` / `_HIDDEN.pop(mode)`，
而 `proactive_gate` 里这些表的键是 `_state_key(mode)` = **(mode, 用户作用域) 元组**。
按字符串 pop 永远匹配不到 → 用户清空历史后，角色仍背着：
- 连续被忽视 3 次的**降档惩罚**（软概率 ×0.5）；
- 隐藏式回复的**冷却时间戳**。
表现为"清空历史后她依然不肯主动开口"，且刷新/重进都不会好。

本测试钉死三件事：
1. 清历史后 `_ignored_count(mode) == 0`、隐藏冷却键被删（含**多用户作用域**全清）；
2. 只清该 mode，别的 mode 的状态不受影响；
3. 频控闸门（_PROB_LAST_CHECK）与半互斥（_ACTIVE 复位为 1）语义保持——
   故意不把频控一起清掉，否则等于给"反复清历史催主动消息"开后门。

沙箱纪律：USER_DIR 指到临时目录，绝不触碰真实 user_data/。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_proactive_reset_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


import routes
from modules import proactive as P
from modules import proactive_gate as G


class FakeH:
    def __init__(self, body=None):
        self.data = None
        self.status = 200
        self.headers = {}
        self.wfile = __import__("io").BytesIO()
        raw = json.dumps(body or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = __import__("io").BytesIO(raw)

    def _json(self, data, status=200):
        self.data = data
        self.status = status


def _plant(mode: str) -> None:
    """造出"被连续忽视 + 隐藏式刚触发过 + 频控刚检查过 + 主动性锁定"的状态。"""
    P._IGNORED[P._state_key(mode)] = 3
    P._HIDDEN[P._state_key(mode)] = time.time()
    P._PROB_LAST_CHECK[P._state_key(mode)] = time.time()
    P._active_set(mode, 0)


print("=== A. 前置：状态确实被种下（旧实现删不掉的正是这些键） ===")
_plant("story")
check("A1 忽视计数 = 3（已达降档阈值）", P._ignored_count("story") == 3)
check("A2 键是 (mode, scope) 元组，按字符串 pop 匹配不到",
      P._state_key("story") in P._IGNORED and "story" not in P._IGNORED)
check("A3 隐藏冷却已记录", P._state_key("story") in P._HIDDEN)
check("A4 ACTIVE 处于锁定（0）", P._active_get("story") == 0)

print("=== B. 多用户作用域：清历史要清掉该 mode 的全部 scope ===")
# 服务器版：同一 mode 下每个账号一个作用域键（本地版 scope 为空串）
_tok = cfg.set_user_context(user_dir=_tmp / "u-1001")
try:
    P._IGNORED[P._state_key("story")] = 2
    P._HIDDEN[P._state_key("story")] = time.time()
    _scope2 = P._state_key("story")
finally:
    cfg.reset_user_context(_tok)
check("B1 第二个作用域的键与本地版不同", _scope2 != P._state_key("story"))
check("B2 两个作用域键都已种下",
      _scope2 in P._IGNORED and P._state_key("story") in P._IGNORED)

# 别的 mode 的状态（不该被误清）
P._IGNORED[P._state_key("haruno")] = 3
P._HIDDEN[P._state_key("haruno")] = time.time()

print("=== C. 清除历史（走真实路由 handler） ===")
h = FakeH({"mode": "story", "session_id": "default"})
routes.clear_history(h)
check("C1 路由返回 ok", h.data.get("ok") is True)

print("=== D. 状态复位断言 ===")
check("D1 本地 scope 的忽视计数归零", P._ignored_count("story") == 0)
check("D2 本地 scope 的隐藏冷却被删（键不存在，而非置 0）",
      P._state_key("story") not in P._HIDDEN)
check("D3 第二个作用域的忽视计数也归零（元组键全清）", P._IGNORED.get(_scope2, 0) == 0)
check("D4 第二个作用域的隐藏冷却也被删", _scope2 not in P._HIDDEN)
check("D5 ACTIVE 复位为空闲（既有语义）", P._active_get("story") == 1)
check("D6 别的 mode 不受影响（haruno 仍是 3）", P._ignored_count("haruno") == 3)
check("D7 别的 mode 的隐藏冷却不受影响", P._state_key("haruno") in P._HIDDEN)
check("D8 频控闸门 _PROB_LAST_CHECK 故意不清（防刷主动消息）",
      P._state_key("story") in P._PROB_LAST_CHECK)

print("=== E. 清历史顺带清掉主动判断日志（既有语义不被破坏） ===")
_log = P._log_file("story")
_log.parent.mkdir(parents=True, exist_ok=True)
_log.write_text('{"sent": true, "_ts": 1}\n', encoding="utf-8")
h = FakeH({"mode": "story"})
routes.clear_history(h)
check("E1 proactive_log 被删", not _log.exists())
check("E2 复位逻辑仍生效（再清一次仍为 0）", P._ignored_count("story") == 0)

print("=== F. reset_states 幂等/边界 ===")
G.reset_states("story")           # 空表上再清一次不应抛错
check("F1 空状态下重复 reset 不抛错", True)
G.reset_states("not-a-mode")
check("F2 未知 mode 不会误伤别的 mode", P._ignored_count("haruno") == 3)
P._IGNORED[P._state_key("story")] = 3
G.reset_states("story")
check("F3 单次 reset 即清零", P._ignored_count("story") == 0)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
