# -*- coding: utf-8 -*-
"""主动性门控 — 信号量三件套（REPLY/ACTIVE/HIDDEN）+ 忽视计数 + proactive_log + 三个 gate

（自 proactive.py 拆分：纯代码门控层，零 LLM 成本。依赖单向不循环：
proactive_gate ← proactive_gen ← proactive（facade 编排入口）。

信号量：
- REPLY（回复通道锁，按 mode 隔离）：响应式/主动式/概率式共用
- ACTIVE（主动性互斥，按 mode）：主动式与概率式互斥，用户回应或超时恢复
- HIDDEN（隐藏式回复冷却，按 mode）：独立于 ACTIVE，后台低频触发专用

门控：
- gate_open（主动式）：硬约束轮次 + 软约束概率（响应感知降档并入有效概率）
- prob_gate_open（概率式）：ACTIVE + 静默窗 + 概率
- hidden_gate_open（隐藏式）：独立冷却 + 时段概率，不碰 ACTIVE
"""

import json
import logging
import random
import threading
import time
from datetime import datetime
from pathlib import Path

from modules.app_config import mode_data_dir, DEFAULT_MODE, user_scope_key

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _state_key(mode: str = DEFAULT_MODE):
    """状态作用域键（服务器版多用户隔离）：(mode, 用户作用域)。
    本地版无用户上下文，scope 恒为空串，行为与原来一致。"""
    return (mode, user_scope_key())

# ── 信号量 ──────────────────────────────────────────
# REPLY（回复通道锁，按 mode 隔离）：响应式/主动式/概率式共用。
#   响应式：阻塞获取（用户消息不可丢）；主动式/概率式：非阻塞（忙则放弃）。
#   按模式分锁：story 与 haruno 的回复通道互不阻塞（各自独立写盘文件），
#   避免"haruno 主动生成中 → story 用户发消息被拖住"的跨模式连锁阻塞。
#   进程内锁，重启自动空闲。
_REPLY_LOCK_GUARD = threading.Lock()
_REPLY_LOCKS: dict[str, threading.Lock] = {}


def _reply_lock_for(mode: str = DEFAULT_MODE) -> threading.Lock:
    # 按 (mode, 用户) 分锁：服务器版多用户并发下，A 的回复流程不阻塞 B 的
    with _REPLY_LOCK_GUARD:
        key = _state_key(mode)
        if key not in _REPLY_LOCKS:
            _REPLY_LOCKS[key] = threading.Lock()
        return _REPLY_LOCKS[key]


def reply_try_lock(mode: str = DEFAULT_MODE) -> bool:
    """主动式/概率式：非阻塞获取 REPLY。成功返回 True。"""
    return _reply_lock_for(mode).acquire(blocking=False)


def reply_lock(mode: str = DEFAULT_MODE) -> None:
    """响应式：阻塞获取 REPLY（用户消息不可丢）。"""
    _reply_lock_for(mode).acquire()


def reply_unlock(mode: str = DEFAULT_MODE) -> None:
    _reply_lock_for(mode).release()

# ACTIVE（主动性互斥，按 mode）：主动式与概率式互斥——触发后归 0，
#   用户回应（/chat 入口）或超 10 分钟自动恢复。重启从 proactive_log 重建。
_ACTIVE_RECOVER_MIN = 10  # 超时恢复阈值（分钟）
_ACTIVE: dict[str, int] = {}   # {mode: 0/1}，默认 1（空闲）

# HIDDEN（隐藏式回复冷却，按 mode）：独立于 ACTIVE——后台低频触发专用，
#   不参与主动式/概率式互斥，也不被其状态影响。重启从 proactive_log 重建。
_HIDDEN_COOLDOWN_MIN = 10   # 距上次隐藏式触发 ≥ 10 分钟（冷却兜底；主频控在 KeepAlive 定时器）
_HIDDEN: dict[str, float] = {}   # {mode: last_ts}，默认无记录（可触发）


def _active_get(mode: str = DEFAULT_MODE) -> int:
    return _ACTIVE.get(_state_key(mode), 1)


def _active_set(mode: str, val: int):
    with _lock:
        _ACTIVE[_state_key(mode)] = 1 if val else 0


def _active_reset(mode: str = DEFAULT_MODE):
    """用户回应（/chat 入口）→ 复位。"""
    _active_set(mode, 1)


def _restore_active_semaphore(mode: str = DEFAULT_MODE) -> int:
    """重启/初始化时从持久化记录重建 ACTIVE。

    依据 proactive_log 最后一条 sent 记录（跳过隐藏式——用户不在场的触发
    不参与前台主动性状态）+ conversation 最后 user 消息：
    - 用户回应过（user 消息在主动之后）→ 1
    - 未回应但距上次主动 ≥ 10 分钟 → 1
    - 未回应且 < 10 分钟 → 0（防"退出重进刷主动"）
    """
    rows = [r for r in _load_log(mode) if r.get("sent") and not r.get("hidden")]
    if not rows:
        _ACTIVE[_state_key(mode)] = 1
        return 1
    last_ts = rows[-1].get("_ts", 0)
    last_user_ts = _last_user_msg_ts(mode)
    if last_user_ts > last_ts:
        _ACTIVE[_state_key(mode)] = 1   # 用户回应过
    elif time.time() - last_ts >= _ACTIVE_RECOVER_MIN * 60:
        _ACTIVE[_state_key(mode)] = 1   # 超时恢复
    else:
        _ACTIVE[_state_key(mode)] = 0   # 锁定中
    return _ACTIVE[_state_key(mode)]


def _active_try_recover(mode: str = DEFAULT_MODE) -> bool:
    """原子恢复：锁定中（ACTIVE=0）且距上次主动 ≥ 10 分钟 → 复位为 1。

    替代 _active_expired + _active_set 的分离读写（两者间存在竞态窗口）。
    返回恢复后是否空闲（=1）。调用方无需再读 ACTIVE 判断恢复。
    """
    if _ACTIVE.get(_state_key(mode), 1) == 1:
        return True
    # 锁外读日志（_load_log 自带锁，避免嵌套死锁），只把置位放锁内
    rows = [r for r in _load_log(mode) if r.get("sent") and not r.get("hidden")]
    expired = not rows or time.time() - rows[-1].get("_ts", 0) >= _ACTIVE_RECOVER_MIN * 60
    with _lock:
        if expired:
            _ACTIVE[_state_key(mode)] = 1
            return True
        return _ACTIVE.get(_state_key(mode), 1) == 1


# ── 监控 ──────────────────────────────────────────
_PROACTIVE_SENT = 0       # 主动消息成功发出
_PROACTIVE_DISCARDED = 0  # 生成后被用户消息抢占丢弃
_PROACTIVE_GATE_REJECT = 0  # 门控拒绝次数
_PROACTIVE_SOFT_REJECT = 0  # 软约束（概率）拒绝次数


def get_counters() -> dict:
    with _lock:
        return {
            "proactive_sent": _PROACTIVE_SENT,
            "proactive_discarded": _PROACTIVE_DISCARDED,
            "proactive_gate_reject": _PROACTIVE_GATE_REJECT,
            "proactive_soft_reject": _PROACTIVE_SOFT_REJECT,
        }


# 计数器记账助手：门控拒绝/发送成功/丢弃发生在编排层（proactive.py），
# 计数器本体在本模块（gate 内软拒绝也记这里），经助手保持锁纪律一致。
def _count_gate_reject():
    global _PROACTIVE_GATE_REJECT
    with _lock:
        _PROACTIVE_GATE_REJECT += 1


def _count_sent():
    global _PROACTIVE_SENT
    with _lock:
        _PROACTIVE_SENT += 1


def _count_discarded():
    global _PROACTIVE_DISCARDED
    with _lock:
        _PROACTIVE_DISCARDED += 1


# ── 响应感知（忽视计数，机会到来时重算）────────────
# 主动发出后用户若未再发消息（长时间没再发消息时最典型），视为"忽视"；
# 连续忽视 3 次判断机会 → 软概率降档。用户回应过（发了消息，含回避话题）
# 即恢复正常——"回应时回避也算回应"。
_IGNORED_LIMIT = 3   # 连续忽视 3 次 → 降档
_SOFT_PENALTY = 0.5  # 降档系数：软概率 × 0.5
_IGNORED: dict[str, int] = {}


def _ignored_count(mode: str = DEFAULT_MODE) -> int:
    return _IGNORED.get(_state_key(mode), 0)


def _update_ignored(mode: str = DEFAULT_MODE):
    """判断机会到来时重算忽视计数：上次主动之后用户是否回过消息。

    只统计主动式/概率式（sent 且非 hidden）——隐藏式是用户不在场时触发，
    不存在"回应"概念，不参与响应感知。
    """
    rows = [r for r in _load_log(mode) if r.get("sent") and not r.get("hidden")]   # 先取数据（_load_log 自带锁）
    with _lock:
        if not rows:
            _IGNORED[_state_key(mode)] = 0
            return
        last_ts = rows[-1].get("_ts", 0)
        responded = False
        try:
            from modules.conversation_store import load_recent
            for m in reversed(load_recent(limit=50, mode=mode)):
                if m.get("who") == "user" and m.get("time"):
                    ts = datetime.strptime(m["time"], "%Y-%m-%d %H:%M:%S").timestamp()
                    if ts > last_ts:
                        responded = True
                    break
        except Exception:
            pass
        _IGNORED[_state_key(mode)] = 0 if responded else _IGNORED.get(_state_key(mode), 0) + 1


# ── 判断机会记录（proactive_log.jsonl）─────────────
# 每条判断（硬约束到点后）写一条记录，含当时用户轮数 turn 与 sent 结果。
# 机会消耗语义：无论判断结果如何（概率没中/无动机/发出），机会用掉，
# 下一次机会需再过 hard 轮用户对话——防止 hard=1 时每轮都触发。
def _log_file(mode: str = DEFAULT_MODE) -> Path:
    return mode_data_dir(mode) / "proactive_log.jsonl"


def _append_log(entry: dict, mode: str = DEFAULT_MODE):
    """追加一条判断记录。文件锁保护，失败静默（不影响主流程）。"""
    try:
        fp = _log_file(mode)
        fp.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with fp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_log(mode: str = DEFAULT_MODE) -> list:
    """读全部判断记录。与 _append_log 同锁。

    字段净化（审查约束）：磁盘文件可能被用户手改/损坏——JSON 解析失败的行跳过，
    解析成功但值为 null/错类型的字段在此强转或剔除，防止下游 int()/时间比较
    抛 TypeError 击穿调用链（_restore_active_semaphore / _last_judge_turn 等）。
    """
    fp = _log_file(mode)
    if not fp.exists():
        return []
    rows = []
    try:
        with _lock:
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                    except Exception:
                        continue
                    # _ts 强转 float（失败置 0：视为超时，宽松恢复不崩）
                    try:
                        row["_ts"] = float(row.get("_ts") or 0)
                    except (TypeError, ValueError):
                        row["_ts"] = 0.0
                    # turn 必须为 int（排除 bool，json true 会被 isinstance(int) 误收）
                    if "turn" in row and not (isinstance(row.get("turn"), int) and not isinstance(row.get("turn"), bool)):
                        row.pop("turn", None)
                    # sent/hidden/prob 强转真布尔（字符串 "false" 的 truthy 会误判为真）；
                    # 只归一已存在的键——不存在的键不新增（下游用 in 判断通道类型）
                    for _k in ("sent", "hidden", "prob"):
                        if _k in row:
                            row[_k] = row.get(_k) in (True, 1)
                    rows.append(row)
    except Exception:
        pass
    return rows


def _last_judge_turn(mode: str = DEFAULT_MODE) -> int | None:
    """上次主动式判断机会消耗时的用户轮数。从未判断过返回 None。

    跳过概率式（prob=True）与隐藏式（hidden=True）记录——它们无 turn 字段
    且不消耗主动式轮次机会。
    """
    for r in reversed(_load_log(mode)):
        if r.get("prob") or r.get("hidden"):
            continue
        if "turn" in r:
            return int(r.get("turn", 0))
    return None


def _last_user_msg_ts(mode: str = DEFAULT_MODE) -> float:
    """conversation 最后一条 user 消息的时间戳（浮点秒）。无则 0。

    ACTIVE 重建/回应判定用：用户回应过（user 消息在主动之后）→ ACTIVE 复位。
    """
    from modules.conversation_store import load_recent
    for m in reversed(load_recent(limit=100, mode=mode)):
        if m.get("who") == "user" and m.get("time"):
            try:
                return datetime.strptime(m["time"], "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                continue
    return 0.0


# ── 门控（纯代码，零 LLM 成本；不通过直接返回）──────
def gate_open(enabled: bool, hard: int, soft: float, mode: str = DEFAULT_MODE,
              turn_count: int = 0) -> tuple:
    """主动门控总入口。返回 (通过, 拒绝原因或 None, 是否消耗机会)。

    防线：
    1. 开关关闭 → 拒绝（不消耗机会）
    2. 硬约束：距上次判断的用户轮数 < hard → 拒绝（机会未到，不消耗）
       ——判断机会消耗：无论上次判断结果如何，都要再过 hard 轮用户对话
    3. 软约束：随机数 >= 有效概率 → 拒绝（机会消耗：概率没中）
       ——独立概率，无动态概率平衡；响应感知降档已并入有效概率
    """
    if not enabled:
        return False, "已关闭", False
    hard = max(1, min(10, int(hard)))
    soft = max(0.0, min(1.0, float(soft)))

    # 硬约束：距上次判断的用户轮数
    last_judge = _last_judge_turn(mode)
    if last_judge is not None:
        delta = turn_count - last_judge
        # delta<0：上次判断在回灌裁掉的历史里 → 视为机会已恢复（放行）
        if 0 <= delta < hard:
            return False, f"距上次判断 {delta} 轮，未到 {hard} 轮", False

    # 响应感知：机会到来，重算忽视计数并降档
    _update_ignored(mode)
    eff = soft
    if _ignored_count(mode) >= _IGNORED_LIMIT:
        eff = soft * _SOFT_PENALTY

    # 软约束：独立概率（机会消耗：无论结果，本次机会用掉）
    if random.random() >= eff:
        with _lock:
            global _PROACTIVE_SOFT_REJECT
            _PROACTIVE_SOFT_REJECT += 1
        return False, "概率未通过", True
    return True, None, True


# ── 概率式回复门控（信号量 + 静默窗 + 概率）──────────────────
# 静默窗：同一次“空闲机会”只掷一次骰。否则前端 10s 轮询 × 10% 概率会在
# 1-2 分钟内几乎必然触发，与“偶尔想起你”的产品语义不符。
# 每次概率式检查后记录时间戳；10 分钟内不再给第二次机会。
_PROB_QUIET_SEC = 600.0
_PROB_LAST_CHECK: dict[str, float] = {}


def prob_gate_open(enabled: bool, prob_value: float, mode: str = DEFAULT_MODE,
                   quiet_seconds: float = _PROB_QUIET_SEC) -> tuple:
    """概率式回复门控。返回 (通过, 拒绝原因或 None)。

    前置条件：开关 + ACTIVE 信号量 + 静默窗 + 概率。
    quiet_seconds=0 供测试跳过静默窗。
    """
    if not enabled:
        return False, "概率式已关闭"
    # ACTIVE 懒恢复：超 10 分钟未回应 → 自动复位（原子检查+恢复）
    _active_try_recover(mode)
    if _active_get(mode) <= 0:
        return False, "主动性信号量未恢复（等用户回应或超时）"
    now = time.time()
    key = _state_key(mode)
    last = _PROB_LAST_CHECK.get(key, 0.0)
    if quiet_seconds > 0 and last and now - last < quiet_seconds:
        mins = int((quiet_seconds - (now - last)) / 60) + 1
        return False, f"概率式静默中（约 {mins} 分钟后再检查）"
    _PROB_LAST_CHECK[key] = now
    pv = max(0.0, min(1.0, float(prob_value)))
    if random.random() >= pv:
        with _lock:
            global _PROACTIVE_SOFT_REJECT
            _PROACTIVE_SOFT_REJECT += 1
        return False, "概率未通过"
    return True, None


# ── 隐藏式回复门控（独立冷却 + 时段概率，不碰 ACTIVE）────
# 隐藏式回复：Android 后台定时器触发（用户不在场时），与前台三种回复
# 完全独立——不检查/不消耗 ACTIVE，只受自身冷却 + 概率控制。
# 冷却状态持久化在 proactive_log（hidden 标记记录），重启重建。
#
# 时段概率分布（真人作息代理）：用户空闲时段高概率，忙碌/深夜低概率。
# 最终触发概率 = 时段权重 × 用户配置概率（prob_reply_value 整体缩放）——
# 用户调高/调低滑条，所有时段相对分布不变，整体同向变化。
_HIDDEN_HOUR_WEIGHTS = (
    #  0-1  深夜沉睡，几乎不打扰
    0.02, 0.02, 0.02, 0.02, 0.02, 0.02,
    #  6-7  清晨刚醒，轻问候
    0.10, 0.15,
    #  8-11 上午（通勤/上班/上课），中低
    0.20, 0.15, 0.15, 0.15,
    # 12-13 午休，空闲小峰
    0.30, 0.25,
    # 14-16 下午工作/学习，低
    0.15, 0.15, 0.15,
    # 17-19 傍晚黄金时段（下班放学），最高
    0.40, 0.40, 0.40,
    # 20-21 晚间放松，较高
    0.35, 0.30,
    # 22-23 夜渐深，回落
    0.15, 0.05,
)


def _hidden_hour_weight(hour: int) -> float:
    """24h 时段概率权重（0.0-1.0）。非法 hour 回退深夜低权重（审查约束）。"""
    if not isinstance(hour, int) or hour < 0 or hour > 23:
        return 0.02
    return _HIDDEN_HOUR_WEIGHTS[hour]


def hidden_gate_open(enabled: bool, prob_value: float, mode: str = DEFAULT_MODE,
                     hour: int = None, _weight_fn=None) -> tuple:
    """隐藏式回复门控。返回 (通过, 拒绝原因或 None)。

    前置条件：开关 + 冷却（距上次隐藏式 ≥ 冷却间隔）+ 时段概率。
    时段概率 = 时段权重 × 用户配置概率（整体缩放）。
    不检查 ACTIVE（前后台场景独立），不消耗主动式轮次机会。

    hour 参数仅供测试/模拟注入固定时段，缺省取当前时间。
    _weight_fn：时段权重函数注入——facade（modules.proactive）经此传入自己
    命名空间的 _hidden_hour_weight，使测试在 facade 层打桩即可全链路生效；
    缺省用本模块的 _hidden_hour_weight（行为不变）。
    """
    if not enabled:
        return False, "隐藏式已关闭"
    last_ts = _HIDDEN.get(_state_key(mode), 0)
    if last_ts and time.time() - last_ts < _HIDDEN_COOLDOWN_MIN * 60:
        mins = int((_HIDDEN_COOLDOWN_MIN * 60 - (time.time() - last_ts)) / 60)
        return False, f"隐藏式冷却中（剩 {mins} 分钟）"
    if hour is None:
        hour = datetime.now().hour
    weight_fn = _weight_fn if _weight_fn is not None else _hidden_hour_weight
    pv = max(0.0, min(1.0, float(prob_value))) * weight_fn(hour)
    if random.random() >= pv:
        with _lock:
            global _PROACTIVE_SOFT_REJECT
            _PROACTIVE_SOFT_REJECT += 1
        return False, "概率未通过"
    return True, None


def _mark_hidden_sent(mode: str = DEFAULT_MODE):
    """隐藏式触发成功 → 记录冷却时间（内存 + 持久化）。"""
    _HIDDEN[_state_key(mode)] = time.time()


def _restore_hidden_state(mode: str = DEFAULT_MODE) -> float:
    """重启/初始化时从 proactive_log 重建 HIDDEN 冷却时间（最后一条 hidden sent 记录）。"""
    for r in reversed(_load_log(mode)):
        if r.get("hidden") and r.get("sent"):
            _HIDDEN[_state_key(mode)] = r.get("_ts", 0)
            return _HIDDEN[_state_key(mode)]
    _HIDDEN.pop(_state_key(mode), None)
    return 0.0
