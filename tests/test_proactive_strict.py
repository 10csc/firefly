# -*- coding: utf-8 -*-
"""主动性模块强化测试（资深测试标准）：
覆盖本次改动的核心：REPLY 按模式分锁 / ACTIVE 原子恢复 / 隐藏式时段概率 /
最后活跃模式判定 / 后台入口 / prompt 时间语境。

铁律：正常路径(≥3例,断言具体内容) + 边界 + 错误路径 + 数据一致性 + 并发。
每个用例一行注释说明防什么 bug。
"""

import sys, os, json, tempfile, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import modules.app_config as cfg
_tmp = tempfile.mkdtemp(prefix="firefly_test_strict_")
cfg.USER_DIR = __import__("pathlib").Path(_tmp)
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"
from modules.context_manager import ContextManager
from modules import proactive as P

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")

# ── Mock（只 mock 外部依赖：LLM 网络、时间）──────────
class MockMessage:
    def __init__(self, content): self.content = content
class MockChoice:
    def __init__(self, content): self.message = MockMessage(content)
class MockResponse:
    def __init__(self, content): self.choices = [MockChoice(content)]
class MockCompletions:
    def __init__(self, responses):
        self._queue = list(responses)
        self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._queue:
            return MockResponse(self._queue.pop(0))
        return MockResponse("{}")
class MockClient:
    def __init__(self, responses=None):
        self.chat = type("Chat", (), {"completions": MockCompletions(responses or [])})()
    def completions_calls(self):
        return self.chat.completions.calls

def make_session():
    return {"context": ContextManager(), "memory_head": ""}

def reset_state(mode="story"):
    """清空所有模式相关状态 + 磁盘，测试隔离。"""
    P._ACTIVE.clear()
    P._HIDDEN.clear()
    P._IGNORED.clear()
    for m in ("story", "haruno"):
        try:
            P._log_file(m).unlink(missing_ok=True)
            from modules.conversation_store import conv_file
            fp = conv_file(m)
            if fp.exists():
                fp.write_text("", encoding="utf-8")
        except Exception:
            pass
    for key in list(P._REPLY_LOCKS):
        del P._REPLY_LOCKS[key]

# ══════════════════════════════════════════════════
print("=== A. REPLY 按模式分锁 ===")

# A1 正常路径：同模式互斥
reset_state()
ok1 = P.reply_try_lock("story")
ok2 = P.reply_try_lock("story")
check("A1 同模式第二次 try_lock 失败", ok1 is True and ok2 is False)
P.reply_unlock("story")

# A2 正常路径：不同模式互不阻塞
ok_a = P.reply_try_lock("story")
ok_b = P.reply_try_lock("haruno")
check("A2 story 占用时 haruno 仍可获取（分锁核心）", ok_a is True and ok_b is True)
P.reply_unlock("haruno")
P.reply_unlock("story")

# A3 正常路径：释放后可重获
P.reply_lock("story")
P.reply_unlock("story")
check("A3 释放后阻塞获取成功", P.reply_try_lock("story") is True)
P.reply_unlock("story")

# A4 边界：未知模式（未来多模式）也可独立分锁
ok_c = P.reply_try_lock("stella")
ok_d = P.reply_try_lock("stella")
check("A4 未知模式可独立上锁且互斥", ok_c is True and ok_d is False)
P.reply_unlock("stella")

# A5 边界：同一模式多次取锁返回同一把锁（幂等）
l1 = P._reply_lock_for("story")
l2 = P._reply_lock_for("story")
check("A5 _reply_lock_for 幂等（同模式同锁）", l1 is l2)

# A6 数据一致性：占用-释放循环 N 次无锁泄漏
leak = False
for _ in range(50):
    if not P.reply_try_lock("story"):
        leak = True
        break
    P.reply_unlock("story")
check("A6 50 次占用-释放循环无泄漏", not leak and P.reply_try_lock("story") is True)
P.reply_unlock("story")

# A7 错误路径：无配对 unlock 抛 RuntimeError（立即暴露调用方配对 bug）
try:
    P.reply_unlock("story")
    check("A7 无配对 unlock 抛异常", False)
except RuntimeError:
    check("A7 无配对 unlock 抛异常", True)

# A8 并发：线程1 持锁期间线程2 try 必须失败（用 Event 同步时序，防竞态测试本身不确定）
reset_state()
held = threading.Event()
released = threading.Event()
t1_result = {}
t2_result = {}
def holder():
    P.reply_lock("story")
    held.set()
    released.wait(5)
    P.reply_unlock("story")
def contender():
    held.wait(5)
    t2_result["got"] = P.reply_try_lock("story")
    if t2_result["got"]:
        P.reply_unlock("story")
t1 = threading.Thread(target=holder)
t2 = threading.Thread(target=contender)
t1.start(); t2.start()
t1.join(5); t2.join(5)
released.set()
check("A8 持锁期间并发 try 失败", t2_result.get("got") is False)

# ══════════════════════════════════════════════════
print("=== B. ACTIVE 原子恢复（_active_try_recover）===")

# B1 正常：空闲(1) → 返回 True 且不修改
reset_state()
P._active_set("story", 1)
check("B1 空闲时返回 True", P._active_try_recover("story") is True)
check("B1 空闲时状态不变", P._active_get("story") == 1)

# B2 正常：锁定且无记录 → 恢复为 1
reset_state()
P._active_set("story", 0)
check("B2 锁定无记录→恢复 True", P._active_try_recover("story") is True)
check("B2 恢复后 ACTIVE=1", P._active_get("story") == 1)

# B3 正常：锁定 + 记录 ≥10min → 恢复
reset_state()
P._append_log({"_ts": time.time() - 11 * 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
P._active_set("story", 0)
check("B3 锁定>10min→恢复 True", P._active_try_recover("story") is True)

# B4 正常：锁定 + 记录 <10min → 不恢复
reset_state()
P._append_log({"_ts": time.time() - 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
P._active_set("story", 0)
check("B4 锁定<10min→不恢复", P._active_try_recover("story") is False)
check("B4 恢复后仍 ACTIVE=0", P._active_get("story") == 0)

# B5 边界：仅 hidden 记录 → 不参与前台恢复（视为无记录可恢复）
reset_state()
P._append_log({"_ts": time.time() - 60, "sent": True, "hidden": True, "time": "2026-08-09 00:00:00"}, mode="story")
P._active_set("story", 0)
check("B5 仅 hidden 记录→可恢复", P._active_try_recover("story") is True)

# B6 边界：日志文件不存在 → 恢复
reset_state()
P._active_set("story", 0)
try:
    P._log_file("story").unlink()
except Exception:
    pass
check("B6 日志文件不存在→恢复", P._active_try_recover("story") is True)

# B7 边界：日志含损坏行（非 JSON）→ 不崩溃，按可读行判断
reset_state()
fp = P._log_file("story")
fp.parent.mkdir(parents=True, exist_ok=True)
fp.write_text("{broken json line\n", encoding="utf-8")
P._active_set("story", 0)
check("B7 损坏日志→不抛异常", P._active_try_recover("story") in (True, False))

# B8 数据一致性：恢复为幂等（连续两次结果一致）
reset_state()
P._append_log({"_ts": time.time() - 11 * 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
P._active_set("story", 0)
r1 = P._active_try_recover("story")
r2 = P._active_try_recover("story")
check("B8 恢复幂等（两次结果一致）", r1 == r2 is True)

# B9 数据一致性：恢复只影响指定 mode，不串扰其他模式
reset_state()
P._append_log({"_ts": time.time() - 11 * 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
P._active_set("story", 0)
P._active_set("haruno", 0)
P._active_try_recover("story")
check("B9 恢复 story 不影响 haruno", P._active_get("story") == 1 and P._active_get("haruno") == 0)

# B10 并发：多线程同时恢复，最终状态一致（无撕裂）
reset_state()
P._append_log({"_ts": time.time() - 11 * 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
P._active_set("story", 0)
barrier2 = threading.Barrier(5)
def recover_thread():
    barrier2.wait()
    P._active_try_recover("story")
ts = [threading.Thread(target=recover_thread) for _ in range(5)]
for t in ts: t.start()
for t in ts: t.join()
check("B10 并发恢复后 ACTIVE=1", P._active_get("story") == 1)

# B11 错误路径：非法 mode 不抛异常
try:
    P._active_try_recover("bad_mode")
    check("B11 非法 mode 不抛异常", True)
except Exception:
    check("B11 非法 mode 不抛异常", False)

# ══════════════════════════════════════════════════
print("=== C. 隐藏式时段概率 ===")

# C1 正常：权重表 24 小时全部在 (0,1] 且傍晚>午休>上午>深夜
reset_state()
w = [P._hidden_hour_weight(h) for h in range(24)]
check("C1 24h 权重全部在 (0,1]", all(0 < x <= 1 for x in w))
check("C1 傍晚(17-19)权重最高", max(w) == P._hidden_hour_weight(18) and w[17] == w[18] == w[19] == 0.40)
check("C1 权重分布 傍晚>午休>上午>深夜", w[18] > w[12] > w[8] > w[3])

# C2 正常：有效概率 = 用户概率 × 时段权重（傍晚 0.4）
P.random.random = lambda: 0.0
ok, reason = P.hidden_gate_open(True, 1.0, mode="story", hour=18)
P.random.random = lambda: 0.99
ok2, reason2 = P.hidden_gate_open(True, 1.0, mode="story", hour=18)
P.random.random = lambda: 0.0
check("C2 傍晚概率1.0：随机0过 / 随机0.99拒", ok is True and ok2 is False)

# C3 正常：用户概率 0.3 缩放（傍晚有效 0.12 → 随机 0.1 过 / 0.13 拒）
P.random.random = lambda: 0.10
ok3, _ = P.hidden_gate_open(True, 0.3, mode="story", hour=18)
P.random.random = lambda: 0.13
ok4, _ = P.hidden_gate_open(True, 0.3, mode="story", hour=18)
check("C3 概率0.3×权重0.4=0.12 边界精确", ok3 is True and ok4 is False)

# C4 边界：深夜权重 0.02，用户概率 1.0 → 有效 0.02（极少触发但不为零）
P.random.random = lambda: 0.019
ok5, _ = P.hidden_gate_open(True, 1.0, mode="story", hour=3)
P.random.random = lambda: 0.021
ok6, _ = P.hidden_gate_open(True, 1.0, mode="story", hour=3)
check("C4 深夜概率1.0 有效0.02：0.019过/0.021拒", ok5 is True and ok6 is False)

# C5 边界：非法 hour → 回退低权重 0.02（防越界索引崩溃）
for bad in (-1, 24, 99):
    try:
        w_bad = P._hidden_hour_weight(bad)
        check(f"C5 非法 hour={bad} 回退低权重", w_bad == 0.02)
    except Exception:
        check(f"C5 非法 hour={bad} 回退低权重", False)

# C6 边界：非法 prob_value（负数/超1/字符串）→ clamp 不崩溃
P.random.random = lambda: 0.0
try:
    ok7, _ = P.hidden_gate_open(True, -1.0, mode="story", hour=18)
    ok8, _ = P.hidden_gate_open(True, 2.0, mode="story", hour=18)
    check("C6 负数概率 clamp 到 0 → 拒绝", ok7 is False and ok8 is True)
except Exception:
    check("C6 负数概率 clamp 到 0 → 拒绝", False)

# C7 错误路径：关闭 → 明确拒绝原因
ok9, reason9 = P.hidden_gate_open(False, 1.0, mode="story", hour=18)
check("C7 关闭→拒绝且原因='隐藏式已关闭'", ok9 is False and reason9 == "隐藏式已关闭")

# C8 错误路径：冷却中 → 拒绝且原因含"冷却"和剩余分钟
P._HIDDEN["story"] = time.time()
ok10, reason10 = P.hidden_gate_open(True, 1.0, mode="story", hour=18)
check("C8 冷却中→拒绝含'冷却'", ok10 is False and "冷却" in reason10 and "剩" in reason10)

# C9 数据一致性：触发成功不碰 ACTIVE（前后台场景独立）
reset_state()
P._active_set("story", 0)
P._HIDDEN.pop("story", None)
P.random.random = lambda: 0.0
ok11, _ = P.hidden_gate_open(True, 1.0, mode="story", hour=18)
check("C9 隐藏式放行不要求 ACTIVE=1", ok11 is True and P._active_get("story") == 0)

# C10 数据一致性：冷却记录按 mode 隔离
reset_state()
P._HIDDEN["story"] = time.time()
ok12, _ = P.hidden_gate_open(True, 1.0, mode="haruno", hour=18)
check("C10 story 冷却不影响 haruno", ok12 is True)

# C11 数据一致性：概率判定无副作用（拒绝不改状态）
reset_state()
P._HIDDEN.pop("story", None)
snap = dict(P._HIDDEN), dict(P._ACTIVE)
P.random.random = lambda: 0.99
P.hidden_gate_open(True, 1.0, mode="story", hour=18)
P.random.random = lambda: 0.0
check("C11 概率拒绝无副作用", snap == (dict(P._HIDDEN), dict(P._ACTIVE)))

# ══════════════════════════════════════════════════
print("=== D. 最后活跃模式判定（_last_active_mode）===")

# D1 正常：仅 story 有 user 消息 → story
reset_state()
from modules.conversation_store import append_message
append_message("user", {"type": "text", "content": "你好"}, mode="story")
check("D1 仅 story 有消息→story", P._last_active_mode() == "story")

# D2 正常：仅 haruno 有 → haruno
reset_state()
append_message("user", {"type": "text", "content": "你好"}, mode="haruno")
check("D2 仅 haruno 有消息→haruno", P._last_active_mode() == "haruno")

# D3 正常：两模式都有，haruno 更新 → haruno
reset_state()
append_message("user", {"type": "text", "content": "早"}, mode="story")
time.sleep(1.2)   # 秒精度时间戳：确保 haruno 严格更新
append_message("user", {"type": "text", "content": "早"}, mode="haruno")
check("D3 两模式都有且 haruno 更新→haruno", P._last_active_mode() == "haruno")

# D4 边界：两模式都无消息 → 默认 story
reset_state()
check("D4 全空→默认 story", P._last_active_mode() == "story")

# D5 边界：conversation 文件不存在 → 默认 story
reset_state()
check("D5 无文件→默认 story", P._last_active_mode() == "story")

# D6 数据一致性：判定是只读操作（不改任何状态）
reset_state()
before = (dict(P._ACTIVE), dict(P._HIDDEN), dict(P._IGNORED))
P._last_active_mode()
check("D6 判定只读无副作用", before == (dict(P._ACTIVE), dict(P._HIDDEN), dict(P._IGNORED)))

# D7 错误路径：损坏 conversation 文件 → 不崩溃，回退默认
reset_state()
from modules.conversation_store import conv_file
fp = conv_file("story")
fp.parent.mkdir(parents=True, exist_ok=True)
fp.write_text("{broken\n", encoding="utf-8")
try:
    m = P._last_active_mode()
    check("D7 损坏文件→不崩溃", m in ("story", "haruno"))
except Exception:
    check("D7 损坏文件→不崩溃", False)

# ══════════════════════════════════════════════════
print("=== E. 后台入口（backdoor_proactive_check）===")

# E1 正常：mode=None → 自动判定 + 生成（mock LLM）
reset_state()
append_message("user", {"type": "text", "content": "你好"}, mode="story")
cfg.config["api_key"] = "test-key"
_orig_client = cfg.get_client
def fake_client():
    return MockClient([
        '{"should_speak": true, "reason_type": "share", "topic_hint": "今天的天", "reason": "想分享"}',
        "[MSG]今天天气真不错",
        '{"sticker_label": "无"}',
    ])
cfg.get_client = fake_client
try:
    msgs = P.backdoor_proactive_check()   # 不传 mode → 自动判定 story
    check("E1 mode=None 自动判定并生成", msgs == ["今天天气真不错"])
finally:
    cfg.get_client = _orig_client

# E2 正常：显式合法 mode → 用之
reset_state()
append_message("user", {"type": "text", "content": "你好"}, mode="haruno")
cfg.config["api_key"] = "test-key"
cfg.get_client = fake_client
try:
    msgs = P.backdoor_proactive_check("haruno")
    check("E2 显式 haruno 生效", msgs == ["今天天气真不错"])
finally:
    cfg.get_client = _orig_client

# E3 边界：非法 mode 字符串 → 回退自动判定，不抛异常
reset_state()
append_message("user", {"type": "text", "content": "你好"}, mode="story")
cfg.config["api_key"] = "test-key"
cfg.get_client = fake_client
try:
    msgs = P.backdoor_proactive_check("not_a_mode")
    check("E3 非法 mode→自动判定不抛", msgs == ["今天天气真不错"])
finally:
    cfg.get_client = _orig_client

# E4 错误路径：REPLY 忙（同模式已占用）→ 返回空
reset_state()
P.reply_lock("story")
cfg.config["api_key"] = "test-key"
cfg.get_client = fake_client
try:
    msgs = P.backdoor_proactive_check("story")
    check("E4 REPLY 忙→返回空", msgs == [])
finally:
    cfg.get_client = _orig_client
    P.reply_unlock("story")

# E5 错误路径：无 API key → 返回空（不崩）
# 防 bug：get_api_key 回退环境变量，直接置 config 空可能仍有 key → 必须 patch 函数本身
reset_state()
cfg.config["api_key"] = ""
_orig_getkey = cfg.get_api_key
cfg.get_api_key = lambda: ""
try:
    msgs = P.backdoor_proactive_check("story")
    check("E5 无 key→返回空", msgs == [])
finally:
    cfg.get_api_key = _orig_getkey
    cfg.config["api_key"] = "test-key"

# E6 错误路径：LLM 全程抛异常 → backdoor 不向上抛异常，返回 list（防 Android 线程崩溃）
# 防 bug：后台入口必须吞掉一切 LLM 异常，否则 Android 后台线程未捕获异常会崩
reset_state()
P._HIDDEN.pop("story", None)
cfg.config["api_key"] = "test-key"
class BoomClient:
    def __init__(self): self.chat = type("C", (), {"completions": type("CC", (), {
        "create": lambda self, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    })()})()
cfg.get_client = lambda: BoomClient()
_orig_w3 = P._hidden_hour_weight
P._hidden_hour_weight = lambda h: 1.0
try:
    thrown = False
    try:
        msgs = P.backdoor_proactive_check("story")
    except Exception:
        thrown = True
    check("E6 LLM 异常→不向上抛且返回 list", (not thrown) and isinstance(msgs, list))
finally:
    P._hidden_hour_weight = _orig_w3
    cfg.get_client = _orig_client

# E7 数据一致性：finally 释放 REPLY 锁（异常后锁可重获）
P.reply_lock("story")
P.reply_unlock("story")
check("E7 后台调用后 REPLY 锁已释放", P.reply_try_lock("story") is True)
P.reply_unlock("story")

# E8 数据一致性：触发成功 → HIDDEN 冷却已记录（写盘防重复轰炸）
reset_state()
append_message("user", {"type": "text", "content": "你好"}, mode="story")
cfg.config["api_key"] = "test-key"
cfg.get_client = fake_client
P._HIDDEN.pop("story", None)
try:
    P.backdoor_proactive_check("story")
    check("E8 触发后冷却已记录", P._HIDDEN.get("story", 0) > 0)
finally:
    cfg.get_client = _orig_client

# ══════════════════════════════════════════════════
print("=== F. prompt 时间语境（_decide_motivation 构造）===")

# F1 正常：system prompt 含时段语气引导（防后续重构删掉时间语境）
client = MockClient(['{"should_speak": false, "reason_type": "none", "topic_hint": "", "reason": ""}'])
P._decide_motivation(client, "", "", "", "", mode="story")
calls = client.completions_calls()
sys_msg = calls[0]["messages"][0]["content"]
check("F1 prompt 含'语气与时机的自然感'", "语气与时机的自然感" in sys_msg)
check("F1 prompt 含时段关键词", "时间" in sys_msg and "深夜" in sys_msg)

# F2 正常：allow_casual=True 时追加兜底引导
client = MockClient(['{"should_speak": false, "reason_type": "none", "topic_hint": "", "reason": ""}'])
P._decide_motivation(client, "", "", "", "", mode="story", allow_casual=True)
sys_msg2 = client.completions_calls()[0]["messages"][0]["content"]
check("F2 allow_casual 含'随便聊'兜底", "随便聊" in sys_msg2)

# F3 边界：记忆/手账超长截断（>1200 截断，防 prompt 超限）
client = MockClient(['{"should_speak": false, "reason_type": "none", "topic_hint": "", "reason": ""}'])
long_text = "甲" * 5000
P._decide_motivation(client, long_text, long_text, "", "", mode="story")
usr_msg = client.completions_calls()[0]["messages"][1]["content"]
check("F3 超长记忆被截断（不含完整 5000 字）", ("甲" * 5000) not in usr_msg and "（无记忆）" not in usr_msg)

# F4 边界：空记忆 → 显示'（无记忆）'占位
client = MockClient(['{"should_speak": false, "reason_type": "none", "topic_hint": "", "reason": ""}'])
P._decide_motivation(client, "", "", "", "", mode="story")
usr_msg2 = client.completions_calls()[0]["messages"][1]["content"]
check("F4 空记忆→'（无记忆）'占位", "（无记忆）" in usr_msg2)

# F5 错误路径：LLM 抛异常 → 返回拒绝 dict 不抛（防动机决策崩溃拖垮生成）
client = MockClient([])   # 空队列会返回 "{}"（无害）
client.chat.completions.create = lambda **k: (_ for _ in ()).throw(OSError("net down"))
d = P._decide_motivation(client, "", "", "", "", mode="story")
check("F5 LLM 异常→拒绝 dict 不抛", d.get("should_speak") is False and d.get("reason_type") == "none")

# F6 数据一致性：reason_type 白名单归一（非法值→none 防下游误判）
client = MockClient(['{"should_speak": true, "reason_type": "weird", "topic_hint": "x", "reason": "y"}'])
d2 = P._decide_motivation(client, "", "", "", "", mode="story")
check("F6 非法 reason_type 归一 none", d2.get("reason_type") == "none")

# F7 数据一致性：should_speak 缺失但 should_send 存在 → 归一（LLM 输出兼容）
client = MockClient(['{"should_send": false, "reason_type": "share", "topic_hint": "x", "reason": "y"}'])
d3 = P._decide_motivation(client, "", "", "", "", mode="story")
check("F7 should_send 别名归一", d3.get("should_speak") is False)

# ══════════════════════════════════════════════════
print("=== G. check_and_generate 隐藏式独立通道 ===")

# G1 正常：hidden=True 时 ACTIVE=0 也触发（独立通道）
reset_state()
P._active_set("story", 0)
P._HIDDEN.pop("story", None)
s = make_session()
client = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "云", "reason": "看到一朵云"}',
    "[MSG]你看那朵云",
    '{"sticker_label": "无"}',
])
_orig_w = P._hidden_hour_weight
P._hidden_hour_weight = lambda h: 1.0
try:
    r = P.check_and_generate(s, client, mode="story",
                             enabled=True, hard=4, soft=0.5,
                             prob_enabled=True, prob_value=1.0,
                             hidden=True)
finally:
    P._hidden_hour_weight = _orig_w
check("G1 ACTIVE=0 隐藏式仍触发", len(r.messages) == 1 and not r.discarded)
check("G1 触发后 ACTIVE 仍=0", P._active_get("story") == 0)

# G2 正常：隐藏式记录带 hidden 标记且无 turn/prob（不消耗主动轮次）
log = P._load_log("story")
hidden_entries = [l for l in log if l.get("hidden") and l.get("sent")]
check("G2 记录带 hidden 且无 turn/prob", len(hidden_entries) >= 1
      and "turn" not in hidden_entries[-1] and "prob" not in hidden_entries[-1])

# G3 边界：隐藏式冷却中 → 不触发（即使概率 1.0）
reset_state()
P._HIDDEN["story"] = time.time()
s2 = make_session()
client2 = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "x", "reason": "y"}',
    "[MSG]测试",
    '{"sticker_label": "无"}',
])
_orig_w2 = P._hidden_hour_weight
P._hidden_hour_weight = lambda h: 1.0
try:
    r2 = P.check_and_generate(s2, client2, mode="story",
                              enabled=True, hard=4, soft=0.5,
                              prob_enabled=True, prob_value=1.0,
                              hidden=True)
finally:
    P._hidden_hour_weight = _orig_w2
check("G3 冷却中→不触发", len(r2.messages) == 0)

# G4 错误路径：隐藏式开关关 → 不触发
reset_state()
P._HIDDEN.pop("story", None)
s3 = make_session()
client3 = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "x", "reason": "y"}',
    "[MSG]测试",
    '{"sticker_label": "无"}',
])
r3 = P.check_and_generate(s3, client3, mode="story",
                          enabled=True, hard=4, soft=0.5,
                          prob_enabled=False, prob_value=1.0,
                          hidden=True)
check("G4 隐藏式关闭→不触发", len(r3.messages) == 0)

# G5 数据一致性：隐藏式触发成功 → HIDDEN 冷却记录（独立场景，防测试顺序耦合）
reset_state()
P._active_set("story", 1)
P._HIDDEN.pop("story", None)
s5 = make_session()
client5 = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "x", "reason": "y"}',
    "[MSG]独立场景触发",
    '{"sticker_label": "无"}',
])
_orig_w5 = P._hidden_hour_weight
P._hidden_hour_weight = lambda h: 1.0
try:
    r5 = P.check_and_generate(s5, client5, mode="story",
                              enabled=True, hard=4, soft=0.5,
                              prob_enabled=True, prob_value=1.0,
                              hidden=True)
finally:
    P._hidden_hour_weight = _orig_w5
check("G5 隐藏式触发成功", len(r5.messages) == 1)
check("G5 隐藏式触发后冷却已记录", P._HIDDEN.get("story", 0) > 0)

# G6 数据一致性：隐藏式写盘 conversation 且带 proactive 标记（独立场景）
from modules.conversation_store import load_recent
recent = load_recent(limit=10, mode="story")
pro = [m for m in recent if m.get("proactive") and m.get("content") == "独立场景触发"]
check("G6 隐藏式消息写盘带 proactive 标记", len(pro) >= 1)

# ══════════════════════════════════════════════════
print("=== 统计 ===")
print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
