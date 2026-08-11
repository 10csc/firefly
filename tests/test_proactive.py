# -*- coding: utf-8 -*-
"""主动性模块白盒测试 — 门控(v2轮次制) / 动机决策 / 生成 / 冲突丢弃 / 主动轮"""

import sys, os, json, tempfile, random, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# 隔离 user_data：测试不碰真实用户数据
import modules.app_config as cfg
_tmp = tempfile.mkdtemp(prefix="firefly_test_proactive_")
cfg.USER_DIR = __import__("pathlib").Path(_tmp)
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"
from modules.context_manager import ContextManager
from modules import proactive as P

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")


# ── Mock client（与 test_orchestrator 同构）──────────
class MockMessage:
    def __init__(self, content, reasoning_content=""):
        self.content = content
        self.reasoning_content = reasoning_content

class MockChoice:
    def __init__(self, content):
        self.message = MockMessage(content)

class MockResponse:
    def __init__(self, content): self.choices = [MockChoice(content)]

class MockCompletions:
    def __init__(self, responses):
        self._queue = list(responses)
    def create(self, **kwargs):
        if self._queue:
            return MockResponse(self._queue.pop(0))
        return MockResponse("{}")

class MockClient:
    def __init__(self, responses=None):
        self.chat = type("Chat", (), {"completions": MockCompletions(responses or [])})()


def make_session():
    return {"context": ContextManager(), "memory_head": ""}


def reset_log():
    """清空 proactive_log + conversation，让硬约束从零开始（每次测试独立）。"""
    P._log_file("story").unlink(missing_ok=True)
    P._IGNORED.pop("story", None)
    try:
        from modules.conversation_store import conv_file
        fp = conv_file("story")
        if fp.exists():
            fp.write_text("", encoding="utf-8")
    except Exception:
        pass


def seed_turn(history: list):
    """构造带历史的 session：history 为 [("user"/"proactive", "内容"), ...]。"""
    s = make_session()
    for who, text in history:
        if who == "user":
            s["context"].add_turn(text, "嗯")
        else:
            s["context"].add_proactive_turn(text)
    return s


# ══════════════════════════════════════════════════
print("=== 门控 v2：硬约束（轮次制）===")
reset_log()
ok, reason, consume = P.gate_open(False, 4, 0.5, mode="story", turn_count=10)
check("关闭→拒绝且不消耗机会", not ok and reason == "已关闭" and consume is False)

# 从未判断过（空日志）→ 放行（新会话给机会）
reset_log()
ok, reason, consume = P.gate_open(True, 4, 1.0, mode="story", turn_count=10)
check("空日志(从未判断)→放行", ok and consume is True)

# 写入一次判断记录（turn=2）→ 距上次判断 < hard → 拒绝且不消耗
reset_log()
P._append_log({"turn": 2, "sent": False, "time": "2026-08-09 00:00:00"}, mode="story")
ok, reason, consume = P.gate_open(True, 4, 1.0, mode="story", turn_count=5)
check("距上次判断 3 轮 < hard(4)→拒绝", not ok and "未到 4 轮" in reason and consume is False)

# 距上次判断 >= hard → 放行
ok, reason, consume = P.gate_open(True, 4, 1.0, mode="story", turn_count=7)
check("距上次判断 5 轮 ≥ hard(4)→放行", ok and consume is True)

# hard=1, soft=1.0：距上次判断 1 轮 → 放行（判断机会消耗语义下不会连续触发）
reset_log()
P._append_log({"turn": 3, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
ok, reason, consume = P.gate_open(True, 1, 1.0, mode="story", turn_count=4)
check("hard=1 距 1 轮→放行", ok)
ok, reason, consume = P.gate_open(True, 1, 1.0, mode="story", turn_count=4)
check("hard=1 同轮重复调用→不消耗不重复（无新判断记录）", ok)  # 机会消耗由 check_and_generate 写记录保证

# 越界裁剪
reset_log()
ok, reason, _ = P.gate_open(True, 99, 1.0, mode="story", turn_count=50)
check("hard 越界→裁剪到 10", ok and reason is None)
ok, reason, _ = P.gate_open(True, 0, 1.0, mode="story", turn_count=50)
check("hard=0→裁剪到 1", ok)

print("=== 门控 v2：软约束（独立概率）===")
# 用 random 补丁控制：soft=0 → 永不通过（概率 0）
reset_log()
_orig_random = random.random
random.random = lambda: 0.0
ok, reason, consume = P.gate_open(True, 4, 0.0, mode="story", turn_count=10)
random.random = _orig_random
check("soft=0→拒绝（概率为0）", not ok and "概率" in reason and consume is True)

# soft=1.0 → 必过
reset_log()
ok, reason, consume = P.gate_open(True, 4, 1.0, mode="story", turn_count=10)
check("soft=1.0→必过", ok and consume is True)

# soft=0.5 + random 0.4 → 过；random 0.6 → 拒
reset_log()
random.random = lambda: 0.4
ok, _, _ = P.gate_open(True, 4, 0.5, mode="story", turn_count=10)
check("soft=0.5 随机0.4<0.5→过", ok)
random.random = lambda: 0.6
ok, reason, consume = P.gate_open(True, 4, 0.5, mode="story", turn_count=10)
check("soft=0.5 随机0.6≥0.5→拒且消耗", not ok and consume is True)
random.random = _orig_random

print("=== 响应感知（忽视降档）===")
# 无主动记录 → 忽视计数不增
reset_log()
P._update_ignored("story")
check("无主动记录→忽视0", P._ignored_count("story") == 0)

# 上次主动 sent 之后用户回过消息 → 忽视清零
reset_log()
from modules.conversation_store import append_message
append_message("firefly", {"type": "text", "content": "在吗", "proactive": True}, mode="story")
P._append_log({"_ts": 1000, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
append_message("user", {"type": "text", "content": "在的"}, mode="story")
P._update_ignored("story")
check("主动后用户回应→忽视0", P._ignored_count("story") == 0)

# 上次主动之后无用户消息 → 忽视+1；连续3次 → 降档
reset_log()
append_message("firefly", {"type": "text", "content": "在吗", "proactive": True}, mode="story")
P._append_log({"_ts": 1000, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
for i in range(1, 4):
    P._update_ignored("story")
    if i < 3:
        check(f"忽视第{i}次→计数{i}（未降档）", P._ignored_count("story") == i)
check("忽视第3次→计数3（达降档阈值）", P._ignored_count("story") == 3)

# 降档生效：soft=1.0 但忽视≥3 → 有效概率 0.5
random.random = lambda: 0.75
ok, reason, consume = P.gate_open(True, 4, 1.0, mode="story", turn_count=10)
check("忽视≥3 时 soft=1.0 实际0.5→随机0.75拒绝", not ok)
random.random = _orig_random

# 用户又回应 → 忽视清零
append_message("user", {"type": "text", "content": "来了"}, mode="story")
P._update_ignored("story")
check("再次回应→忽视清零", P._ignored_count("story") == 0)

print("=== 动机决策 ===")
client = MockClient([
    '{"should_speak": false, "reason_type": "none", "topic_hint": "", "reason": ""}',
])
d = P._decide_motivation(client, "", "", "", "", mode="story")
check("无动机→不说话", d.get("should_speak") is False and d.get("reason_type") == "none")

client = MockClient([
    '{"should_speak": true, "reason_type": "memory", "topic_hint": "上次说好要看的星星", "reason": "手账里有约定"}',
])
d = P._decide_motivation(client, "", "", "", "", mode="story")
check("有动机→说话", d.get("should_speak") is True)
check("reason_type 白名单", d.get("reason_type") == "memory")

client = MockClient(['{"should_speak": true, "reason_type": "hack", "topic_hint": "x", "reason": "y"}'])
d = P._decide_motivation(client, "", "", "", "", mode="story")
check("非法 reason_type→归一 none", d.get("reason_type") == "none")

client = MockClient(["这不是JSON"])
d = P._decide_motivation(client, "", "", "", "", mode="story")
check("垃圾输出→降级不说话", d.get("should_speak") is False)

# 别名兼容：should_send → should_speak
client = MockClient(['{"should_send": true, "reason_type": "share", "topic_hint": "x", "reason": "y"}'])
d = P._decide_motivation(client, "", "", "", "", mode="story")
check("should_send 别名→归一 should_speak", d.get("should_speak") is True)

print("=== 生成流程 ===")
client = MockClient(['{"should_speak": false, "reason_type": "none", "topic_hint": "", "reason": ""}'])
r = P.generate_proactive(make_session(), client, mode="story")
check("无动机→messages 空", len(r.messages) == 0)

client = MockClient([
    '{"should_speak": true, "reason_type": "memory", "topic_hint": "看星星的约定", "reason": "手账里有约定"}',
    "[MSG]还记得上次说好一起看星星吗\n[MSG]今晚天气好像不错",
    '{"sticker_label": "无"}',
])
r = P.generate_proactive(make_session(), client, mode="story")
check("有动机→有消息", len(r.messages) >= 1)
check("reason 传递", r.reason_type == "memory")

print("=== 写盘 + 主动轮进 ctx + 机会消耗 ===")
reset_log()
from modules.conversation_store import conv_file
client = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "今天看到一朵云", "reason": "想分享"}',
    "[MSG]今天看到一朵很像你的云",
])
s = make_session()
r = P.check_and_generate(s, client, mode="story",
                         enabled=True, hard=4, soft=1.0)
check("写盘成功", len(r.messages) == 1 and not r.discarded)
check("写盘消息带 time", r.messages[0].get("time") is not None)

# 主动轮进 ctx：role=assistant + proactive 标记，不计用户轮
hist = s["context"].get_full()
has_proactive = any(m.get("proactive") for m in hist)
check("主动轮写入 ctx（proactive 标记）", has_proactive)
check("主动轮不计入用户轮", s["context"].turn_count == 0)
check("距上次主动的用户轮=0", s["context"].turns_since_last_proactive() == 0)

# 机会消耗：刚判断过（turn=0 记录）→ 下一轮调用拒绝（未到 hard）
from modules.conversation_store import append_message
append_message("user", {"type": "text", "content": "你好"}, mode="story")
s["context"].add_turn("你好", "嗯")
ok, reason, consume = P.gate_open(True, 4, 1.0, mode="story", turn_count=s["context"].turn_count)
check("主动后 1 轮 < hard(4)→机会未到", not ok and "未到 4 轮" in reason)
append_message("user", {"type": "text", "content": "再来"}, mode="story")
s["context"].add_turn("再来", "嗯")
ok, _, _ = P.gate_open(True, 4, 1.0, mode="story", turn_count=s["context"].turn_count)
check("主动后 2 轮 < hard(4)→机会未到", not ok)
append_message("user", {"type": "text", "content": "再来"}, mode="story")
s["context"].add_turn("再来", "嗯")
ok, _, _ = P.gate_open(True, 4, 1.0, mode="story", turn_count=s["context"].turn_count)
check("主动后 3 轮 < hard(4)→机会未到", not ok)
append_message("user", {"type": "text", "content": "再来"}, mode="story")
s["context"].add_turn("再来", "嗯")
ok, _, _ = P.gate_open(True, 4, 1.0, mode="story", turn_count=s["context"].turn_count)
check("主动后 4 轮 = hard(4)→机会到", ok)

# 防连续响应：hard=1 + soft=1.0 场景，判断机会消耗后同轮不再触发
reset_log()
s2 = make_session()
client2 = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "a", "reason": "b"}',
    "[MSG]第一条主动",
    '{"sticker_label": "无"}',
    '{"should_speak": true, "reason_type": "share", "topic_hint": "c", "reason": "d"}',
    "[MSG]第二条主动",
    '{"sticker_label": "无"}',
])
append_message("user", {"type": "text", "content": "在吗"}, mode="story")
s2["context"].add_turn("在吗", "嗯")
r1 = P.check_and_generate(s2, client2, mode="story", enabled=True, hard=1, soft=1.0)
check("hard=1 用户1轮→发出主动", len(r1.messages) == 1)
# 同轮（无新用户消息）再次调用 → 机会已消耗，拒绝
r2 = P.check_and_generate(s2, client2, mode="story", enabled=True, hard=1, soft=1.0)
check("同轮再调→机会消耗拒绝（防连续响应）", len(r2.messages) == 0)
# 用户再来一轮 → 又可以主动
append_message("user", {"type": "text", "content": "嗯"}, mode="story")
s2["context"].add_turn("嗯", "好")
r3 = P.check_and_generate(s2, client2, mode="story", enabled=True, hard=1, soft=1.0)
check("hard=1 用户新轮→再主动（每轮一次是设计语义）", len(r3.messages) == 1)

print("=== 冲突丢弃 ===")
reset_log()
client = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "x", "reason": "y"}',
    "[MSG]测试",
])
s3 = make_session()
# 模拟生成期间用户发消息：先注册一个会写盘的并发（直接预置 n_before 相同即可，
# 这里用 check_and_generate 内部逻辑验证：生成前无新消息→正常写盘）
r = P.check_and_generate(s3, client, mode="story", enabled=True, hard=4, soft=1.0)
check("正常场景→写盘成功", len(r.messages) == 1 and not r.discarded)

print("=== 概率式回复门控（信号量 + 概率）===")
reset_log()
from modules.conversation_store import conv_file
# ACTIVE 默认 1（空闲）
P._active_set("story", 1)
# 无信号量问题 → 概率 1.0 必过
ok, reason = P.prob_gate_open(True, 1.0, mode="story")
check("prob: 开启+概率1.0→放行", ok and reason is None)
ok, reason = P.prob_gate_open(False, 1.0, mode="story")
check("prob: 关闭→拒绝", not ok and "已关闭" in reason)

# 概率 0 → 必拒
ok, reason = P.prob_gate_open(True, 0.0, mode="story")
check("prob: 概率0→拒绝", not ok and "概率" in reason)

# ACTIVE=0（主动性互斥中）→ 拒绝（即使概率 1.0）
P._append_log({"_ts": time.time() - 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
P._active_set("story", 0)
ok, reason = P.prob_gate_open(True, 1.0, mode="story")
check("prob: ACTIVE=0→拒绝", not ok and "信号量" in reason)
P._active_set("story", 1)

# 完整链路：概率式走 check_and_generate（主动式未触发 → 串联概率式）
reset_log()
P._active_set("story", 1)
P._IGNORED.pop("story", None)
append_message("user", {"type": "text", "content": "在吗"}, mode="story")
# 制造"主动式机会已消耗"记录（turn=0 判断记录）→ 主动式 gate_open 拒绝（距上次判断 0 轮 < hard）
P._append_log({"turn": 0, "sent": False, "time": "2026-08-09 00:00:00"}, mode="story")
s_prob = make_session()
# 主动式硬约束不会过（距上次判断 0 轮 < hard），概率式应触发
client_prob = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "想起一件事", "reason": "忽然想到"}',
    "[MSG]忽然想起，上次你说想看的那颗星",
    '{"sticker_label": "无"}',
])
r = P.check_and_generate(s_prob, client_prob, mode="story",
                         enabled=True, hard=4, soft=0.5,
                         prob_enabled=True, prob_value=1.0)
check("prob: 完整链路写盘成功", len(r.messages) == 1 and not r.discarded)
# 概率式触发后 ACTIVE 消耗
check("prob: 触发后 ACTIVE=0", P._active_get("story") == 0)
# 记录无 turn 字段（不消耗主动式轮次机会）
log = P._load_log("story")
prob_entries = [l for l in log if l.get("prob")]
print("  (debug) prob 记录:", [json.dumps(l, ensure_ascii=False) for l in prob_entries])
check("prob: 记录带 prob 标记且无 turn", len(prob_entries) >= 1 and "turn" not in prob_entries[0])

# ACTIVE=0 时概率式不能触发（信号量互斥）
client_prob2 = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "x", "reason": "y"}',
    "[MSG]测试",
    '{"sticker_label": "无"}',
])
r2 = P.check_and_generate(s_prob, client_prob2, mode="story",
                          enabled=True, hard=4, soft=0.5,
                          prob_enabled=True, prob_value=1.0)
check("prob: ACTIVE=0 时概率式被锁", len(r2.messages) == 0)

# 用户回应复位 ACTIVE
P._active_reset("story")
check("用户回应→ACTIVE 复位=1", P._active_get("story") == 1)

# 重启重建：主动记录在 10 分钟内且无回应 → ACTIVE=0
reset_log()
append_message("firefly", {"type": "text", "content": "主动", "proactive": True}, mode="story")
P._append_log({"_ts": time.time() - 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
v = P._restore_active_semaphore("story")
check("重启重建: 主动<10min无回应→ACTIVE=0", v == 0)
# 超 10 分钟 → ACTIVE=1
P._append_log({"_ts": time.time() - 11 * 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
v = P._restore_active_semaphore("story")
check("重启重建: 主动>10min→ACTIVE=1", v == 1)
# 用户回应过（conversation 有 user 消息在主动后）→ ACTIVE=1
reset_log()
append_message("firefly", {"type": "text", "content": "主动", "proactive": True}, mode="story")
P._append_log({"_ts": time.time() - 60, "sent": True, "time": "2026-08-09 00:00:00"}, mode="story")
append_message("user", {"type": "text", "content": "回应"}, mode="story")
v = P._restore_active_semaphore("story")
check("重启重建: 用户回应过→ACTIVE=1", v == 1)

# REPLY 锁测试（按模式隔离）
P.reply_lock("story")
check("REPLY[story] 占用后非阻塞获取失败", P.reply_try_lock("story") is False)
check("REPLY[story] 占用不阻塞 REPLY[haruno]", P.reply_try_lock("haruno") is True)
P.reply_unlock("haruno")
P.reply_unlock("story")
check("REPLY 释放后可获取", P.reply_try_lock("story") is True)
P.reply_unlock("story")

print("=== 隐藏式回复（独立通道）===")
reset_log()
P._active_set("story", 1)
P._HIDDEN.pop(P._state_key("story"), None)
# 0. 时段权重表：傍晚高峰 > 上午 > 深夜
H18 = P._hidden_hour_weight(18)
H10 = P._hidden_hour_weight(10)
H3 = P._hidden_hour_weight(3)
check("hidden: 时段权重 傍晚>上午>深夜", H18 > H10 > H3)
check("hidden: 深夜权重近零", H3 <= 0.02)
check("hidden: 非法小时回退低权重", P._hidden_hour_weight(99) == 0.02)
# 1. 隐藏式门控：开启+概率1.0（注入傍晚）→ 放行（固定随机 0 保证必过）
_orig_rand_fn = P.random.random
P.random.random = lambda: 0.0
ok, reason = P.hidden_gate_open(True, 1.0, mode="story", hour=18)
P.random.random = _orig_rand_fn
check("hidden: 开启+概率1.0(傍晚)→放行", ok and reason is None)
ok, reason = P.hidden_gate_open(False, 1.0, mode="story", hour=18)
check("hidden: 关闭→拒绝", not ok and "已关闭" in reason)
ok, reason = P.hidden_gate_open(True, 0.0, mode="story", hour=18)
check("hidden: 概率0→拒绝", not ok and "概率" in reason)

# 1.5 时段缩放：深夜 1.0 概率 × 低权重 ≈ 0.02 → 基本必拒（掷中概率 2%）
#     用多轮采样验证统计倾向（不依赖单次随机）
_rej_deep = sum(1 for _ in range(200)
                if not P.hidden_gate_open(True, 1.0, mode="story", hour=3)[0])
check("hidden: 深夜 1.0 概率 200 次采样拒绝率 > 90%", _rej_deep > 180)

# 2. 冷却：标记发送后 → 冷却中拒绝
P._mark_hidden_sent("story")
ok, reason = P.hidden_gate_open(True, 1.0, mode="story", hour=18)
check("hidden: 冷却中→拒绝", not ok and "冷却" in reason)

# 3. 冷却期间 ACTIVE 不受影响（隐藏式不碰 ACTIVE）
check("hidden: 冷却中 ACTIVE 仍=1", P._active_get("story") == 1)

# 4. 冷却超时恢复（伪造旧时间 + 固定随机）
P._HIDDEN[P._state_key("story")] = time.time() - 11 * 60
P.random.random = lambda: 0.0
ok, reason = P.hidden_gate_open(True, 1.0, mode="story", hour=18)
P.random.random = _orig_rand_fn
check("hidden: 冷却超时→放行", ok and reason is None)

# 5. 完整链路：hidden=True 走独立通道（不检查 ACTIVE，不消耗）
reset_log()
P._active_set("story", 0)   # 故意锁死 ACTIVE（前台互斥中）
append_message("user", {"type": "text", "content": "在吗"}, mode="story")
s_hidden = make_session()
client_hidden = MockClient([
    '{"should_speak": true, "reason_type": "share", "topic_hint": "想分享", "reason": "忽然想到"}',
    "[MSG]忽然想起一件事",
    '{"sticker_label": "无"}',
])
r = None
_orig_weight = P._hidden_hour_weight
P._hidden_hour_weight = lambda hour: 1.0   # 固定权重：测试不依赖当前时段
try:
    r = P.check_and_generate(s_hidden, client_hidden, mode="story",
                             enabled=True, hard=4, soft=0.5,
                             prob_enabled=True, prob_value=1.0,
                             hidden=True)
finally:
    P._hidden_hour_weight = _orig_weight
check("hidden: ACTIVE=0 仍可触发（独立通道）", r and len(r.messages) == 1 and not r.discarded)
check("hidden: 触发后 ACTIVE 仍=0（不消耗前台状态）", P._active_get("story") == 0)
# 记录带 hidden 标记
log = P._load_log("story")
hidden_entries = [l for l in log if l.get("hidden")]
check("hidden: 记录带 hidden 标记", len(hidden_entries) >= 1)
check("hidden: 记录无 turn/prob 字段", "turn" not in hidden_entries[0] and "prob" not in hidden_entries[0])
check("hidden: 冷却已记录", P._HIDDEN.get(P._state_key("story"), 0) > 0)

# 6. 重启重建：hidden 记录 → _restore_hidden_state 恢复冷却
v = P._restore_hidden_state("story")
check("hidden: 重启重建冷却（>0）", v > 0)
# 但 ACTIVE 重建跳过 hidden 记录（最后 sent 是 hidden → 不参与 ACTIVE 判断）
v2 = P._restore_active_semaphore("story")
check("hidden: ACTIVE 重建跳过 hidden 记录", v2 == 1)

# 7. 隐藏式不影响主动式轮次（_last_judge_turn 跳过 hidden）
v3 = P._last_judge_turn("story")
check("hidden: 不消耗主动式轮次机会", v3 is None)

# 8. 清理 _HIDDEN（clear-history 语义）
P._HIDDEN.pop(P._state_key("story"), None)
check("hidden: 清理后冷却重置", P._HIDDEN.get("story") is None)

print("=== 监控计数器 ===")
counters = P.get_counters()
check("计数器含 proactive_sent", "proactive_sent" in counters)
check("计数器含 proactive_discarded", "proactive_discarded" in counters)
check("计数器含 proactive_gate_reject", "proactive_gate_reject" in counters)
check("计数器含 proactive_soft_reject", "proactive_soft_reject" in counters)
check("sent>0（前面成功过）", counters["proactive_sent"] > 0)
check("gate_reject>0（关闭经入口触发）", counters["proactive_gate_reject"] > 0)

print("=== 统计 ===")
print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
