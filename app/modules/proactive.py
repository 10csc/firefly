# -*- coding: utf-8 -*-
"""主动性模块 — 流萤在适合的时候主动找开拓者说话

模块铁律：接收输入 → 审查约束（门控）→ 模块处理（动机决策 + 生成）→ 验证结果 → 最终输出

设计原则（与项目三层思考法一致）：
- 主动必须有可追溯的动机（记忆/手账/环境/牵挂），无来源的问候直接拒绝
- 数值只做后台门控（频率/概率），不做前台驱动力——"为什么说话"由 LLM 语义判断
- 生成复用 polisher 完整角色 prompt，不走独立起草器，角色一致性 100%
- 与用户对话互斥：写盘前检查用户是否刚发消息，冲突则丢弃草稿（防顺序错乱）
- 门控全在代码层（零 LLM 成本），动机决策一次轻量 Flash 调用（约几百 token）

频率控制（v2：事件/轮次制，替代 v1 的时间制——应用无法后台运行，
主动只能发生在用户打开 App 时，时间预算/间隔失去意义）：
- hard（硬约束）：每 N 轮用户对话触发一次"判断机会"（1-10 轮）
  ——只数用户轮（ctx.turn_count），主动轮独立不计入，机会到来后无论
  结果如何都消耗（下一次机会需再过 N 轮），防"每轮都主动"连续响应
- soft（软约束）：判断机会到来时，以概率 soft（0.0-1.0）真正发起
  主动（独立概率，不做动态概率平衡）
- 响应感知：主动发出后用户连续 3 轮未回应（忽视）→ 软概率 ×0.5 降档；
  用户回应过（含回避话题）→ 恢复正常

配置（config.json，设置面板可调）：
- proactive_enabled: bool   开关
- proactive_hard: 1-10      硬约束（每 N 轮用户对话 1 次判断机会，默认 4）
- proactive_soft: 0.0-1.0   软约束（判断机会到来时的触发概率，默认 0.5）
"""

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
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


# ── 异常 ──────────────────────────────────────────
class ProactiveError(Exception): pass
class InputRejected(ProactiveError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class ProactiveResult:
    messages: list = field(default_factory=list)   # [{"type":"text","content":...}, ...]
    reason_type: str = ""    # memory|concern|share|none
    topic_hint: str = ""
    reason: str = ""
    discarded: bool = False  # 生成了但写盘前被用户消息抢占 → 丢弃
    error: str = ""


# ── 响应感知（忽视计数，机会到来时重算）────────────
# 主动发出后用户若未再发消息（跨会话重开时最典型），视为"忽视"；
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


# ── 动机决策（轻量 LLM，Flash Non-think）───────────
def _decide_motivation(client, memory_head: str, journal: str, environment: str,
                       recent_topics: str, mode: str = DEFAULT_MODE,
                       allow_casual: bool = False, recent_said: str = "") -> dict:
    """判断流萤现在该不该主动说话、为什么。返回 JSON dict。

    allow_casual=False（主动式）：严格动机——无真实来源就拒绝（避免打扰）。
    allow_casual=True（概率式/隐藏式）：触发机会已经由概率+信号量把关，
    无具体动机时允许"随便聊聊"兜底（流萤想起开拓者了，随便说点什么都行），
    保证触发必然有回复——不浪费已消耗的触发机会与 token。

    recent_said：最近主动说过的话题（proactive_log 提取），用于去重——
    提示模型不要重复已说过的话题/意象（霓虹灯、天气问候模板等）。

    输出格式（严格）：
    {"should_speak": true/false, "reason_type": "memory|concern|share|none",
     "topic_hint": "...", "reason": "..."}
    - should_speak=false 或 reason_type=none → 拒绝（无动机不说话）
    - reason_type: memory=手账/记忆里的约定或想做的事；concern=牵挂（仅深夜/长时间未联系时允许）；
      share=有想分享的事
    """
    casual_note = (
        "\n\n注意：本次触发是你主动找开拓者的机会（用户空闲/不在场）。"
        "即使没有上述具体动机，也可以随便聊点什么——想想开拓者此刻可能在做什么、"
        "分享当下的一件小事、一句轻轻的问候都可以。总之要说话，不要沉默。"
    ) if allow_casual else ""
    system = """你是流萤的内心判断层。判断她现在是否应该主动给开拓者发消息。
触发机会已经由频率控制把关（每 N 轮一次机会 + 概率）——**机会到来时，默认应该说**，
你的判断重心是"说什么"，不是"要不要说"。

除非是明确的不该打扰：对方刚说"晚安/去忙了/先睡了"、对话刚结束、深夜对方已不在——
此时才 should_speak=false。

流萤是有表达欲的人——主动就是创造话题：想提要求就提（邀约、想吃想玩想去哪）、
想到什么就说（新话题、见闻、想法）、想分享就分享。

主动的方向（有具体内容即可，不必等什么契机）：
- 提要求/邀约：想见他就约（"明天有空吗""下次陪我去…"）、有想做的事就提（"我想去看…"）
- 开话题：自己正在想的事、刚发生的事、想问他的事
- 表达自己：想去的地方、想要的东西、最近的打算
- 跟进：对方刚分享的事 → 补充新角度（"拍照时教教我""想看你拍的照片"）

禁止的"假主动"：没有内容的客套（"在吗""忙吗""吃饭了吗"）不是主动——
主动必须有具体内容（要求/话题/分享/想法）。

## 话题必须来自上下文（事实约束，最高优先）
主动说的话题必须能溯源到上面的内容——最近对话里真实发生过的事/对方说过的话、
记忆/手账里的记录、当前环境（时间/情境）。**上下文里没有的人、事、物、约定——不编造**。
"上次说的XX"必须真实存在；对方没说过、没发生过的事，不假装发生过。

## 话题必须新鲜（防复读，最高优先）
禁止的是**重复提起**：同一件事说第二遍、确认已定细节、追问已问过的问题
（"星星""周六见""还作数吗"这类）。
**不禁止跟进**：刚聊过的话题是最好用的素材——对方说买了相机，
可以开新角度（"那拍照时教教我""想看你拍的照片"）；说好周末逛街，
可以准备具体的事（"我看了那家甜品店的营业时间"）。跟进 = 补充新东西，不是复读。
user 消息里列出的"已说过话题"——禁止原样重复，但允许开新角度。

**追问封存**：对方没回答的问题——只追问一次，之后这个话题**整体封存**，
不再提起（哪怕换说法、换角度也不行），等对方主动再说。

## 已定事项不再确认
约定已经定好的细节（时间、地点、交换内容）——**不再问第二遍**（"约在哪""还作数吗"问一次就够）。
约定本身还可以作为话题背景，但只能提新角度（盼着、准备、新想法），不能重复确认已定的事。

## 景物/天气不是话题（防意象复读）
"今天的天空/晚霞/天光/风/星星很好看""你那边天气怎么样"这类景物天气描述**不是新鲜事**——
它们每天都在，不是值得主动提起的内容（除非真特殊：流星、彩虹、极端天气）。
主动要说的是**具体的事**：最近在做什么、想到了什么、想问他的问题、具体的见闻或分享。
""" + casual_note + """
## 语气与时机的自然感
结合当前环境里的时间（时刻/时段/星期），像真人一样选择开口的方式：
清晨是轻轻的早安，白天分享具体鲜活的小事，傍晚适合聊今天发生的事，
深夜只轻声说一句关心就好。不要机械问候，也不要开场白式地解释自己为什么说话。
## 输出格式（严格，一行 JSON，禁止任何其他文字）
{"should_speak": true或false, "reason_type": "memory或concern或share或none",
 "topic_hint": "一句话提示想说的话", "reason": "一句话解释动机来源"}
- should_speak=false 或 reason_type=none → 不说话
- topic_hint/reason 为空字符串，不要编造"""

    _MODE_CONTEXT = {
        "story": "剧情模式：匹诺康尼的一切已经结束，流萤重伤在星核猎手飞船的医疗舱里，"
                 "日常只能发短信聊天（她不能出门、不能赴约，见面=开拓者来看她；"
                 "话题围绕生活与想念，不汇报身体状态）",
        "haruno": "春日手信模式：普通学生流萤正在黄金时刻旅行，刚认识开拓者，"
                  "日常聊天轻松明亮（她是健康普通的学生，正在旅行）",
    }

    user_prompt = f"""## 当前环境
{environment}

## 跨会话记忆（头部）
{memory_head[:1200] if memory_head else "（无记忆）"}

## 手账（重要对话记录与未完成的约定）
{journal[:1200] if journal else "（无手账）"}

## 最近对话摘要（以下内容已经聊过了——不要重复其中的话题或细节）
{recent_topics[:800] if recent_topics else "（最近没有对话）"}

## 流萤最近主动说过的话题（以下内容已说过——禁止再次选择相同或相似的话题）
{recent_said if recent_said else "（暂无）"}

## 当前模式
{mode}——{_MODE_CONTEXT.get(mode, "")}

请判断：流萤现在是否应该主动给开拓者发消息？只输出 JSON，不要输出其他内容。"""

    try:
        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=400, temperature=0.0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        from modules.llm_base import record_usage, parse_json
        record_usage("proactive", resp)
        raw = resp.choices[0].message.content.strip()
        obj = parse_json(raw)
        if not isinstance(obj, dict):
            return {"should_speak": False, "reason_type": "none"}
        # 防御：LLM 可能输出 should_send/should 等别名，统一归一（should_speak 为准）
        if "should_speak" not in obj:
            obj["should_speak"] = bool(obj.get("should_send", obj.get("should", False)))
        reason_type = str(obj.get("reason_type", "none")).strip()
        if reason_type not in ("memory", "concern", "share", "none"):
            reason_type = "none"
        obj["reason_type"] = reason_type
        return obj
    except Exception as e:
        logger.warning("主动性动机决策失败（跳过本轮）: %s", e)
        return {"should_speak": False, "reason_type": "none"}


# ── 生成（复用 polisher + organizer，跳过 analyzer）─
def generate_proactive(session: dict, client, mode: str = DEFAULT_MODE,
                       polisher_model: str = "deepseek-v4-flash",
                       polisher_effort: str = "high",
                       polisher_temperature: float = 0.5,
                       organizer_model: str = "deepseek-v4-flash",
                       organizer_effort: str = "none",
                       memory_head: str = "",
                       use_reply_flow: bool = False,
                       allow_casual: bool = False) -> ProactiveResult:
    """生成一条主动消息。返回 ProactiveResult（messages 可能为空）。

    流程：动机决策 → 门控通过 → polisher（proactive 标记）→ organizer 表情包
    不调用 analyzer（没有用户输入需要分析）；写盘由调用方（routes）负责，写盘前做冲突检查。

    use_reply_flow=False（主动式）：polisher 走"主动发起"分支（proactive_context），
    明确"本条消息是你主动发给开拓者的"。
    use_reply_flow=True（概率式）：polisher 走"回复流程"分支（无 proactive_context），
    以触发场景作为输入——语气更像在对话中接话/想起补充，而非刻意找话。
    """
    from modules.llm_base import load_journal
    from orchestrator import _get_environment
    environment = _get_environment()
    recent = session["context"].get_recent(8)
    recent_topics = "\n".join(
        f"[{'开拓者' if m.get('role') == 'user' else '流萤'}]: {m.get('content', '')}"
        for m in recent if m.get("content")
    )

    # 最近主动说过的话题（去重：不重复已说过的内容/意象）
    recent_said = ""
    try:
        rows = [r for r in _load_log(mode) if r.get("sent") and r.get("topic_hint")]
        recent_said = "；".join(f"[{r.get('time','')[:16]}] {str(r['topic_hint'])[:60]}"
                                for r in rows[-3:])
    except Exception:
        pass

    decision = _decide_motivation(
        client, memory_head=memory_head,
        journal=load_journal(mode), environment=environment,
        recent_topics=recent_topics, mode=mode,
        allow_casual=allow_casual, recent_said=recent_said,
    )
    if not decision.get("should_speak") or decision.get("reason_type") in ("none", ""):
        if not allow_casual:
            return ProactiveResult()   # 主动式：无动机，不说话（严格）
        # 概率式/隐藏式：触发机会已消耗，不能沉默——用兜底主题交给 polisher 随便聊
        decision = {
            "should_speak": True,
            "reason_type": "share",
            "topic_hint": "随便聊聊——想起开拓者了，说点当下的什么都可以",
            "reason": "触发机会已到，流萤想跟开拓者说说话（无具体来源的轻松问候）",
        }

    reason_type = decision.get("reason_type", "share")
    topic_hint = str(decision.get("topic_hint", "")).strip()[:200]
    reason = str(decision.get("reason", "")).strip()[:300]

    # 生成：polisher 全角色 prompt；跳过 analyzer（主动场景无输入可分析）
    from modules.polisher import Polisher, PolisherInput
    from modules.organizer import Organizer, OrganizerInput
    from modules.llm_base import format_history

    polisher = Polisher(client, model=polisher_model, effort=polisher_effort,
                        temperature=polisher_temperature, mode=mode)
    if use_reply_flow:
        # 概率式：调用回复流程（模拟开拓者安静后的接话，非"主动发起"分支）
        meet_note = (
            "（注意：你在医疗舱里不能出门赴约——'见面'是开拓者来看你，"
            "由他定地方带来，你等着就好；不解释身体状况）"
            if mode == "story" else ""
        )
        out = polisher.polish(PolisherInput(
            user_input=(
                "（开拓者安静了好一会儿，一直没有新消息。你心里想起了一些事，"
                f"想开口跟他聊起来）主题：{topic_hint}。原因：{reason}\n"
                f"{meet_note}\n"
                "注意：不要用'傍晚/天气/景物描写+问候对方'的固定套路收尾，"
                "像普通聊天一样直接说想说的话。"
            ),
            analyzer_summary=f"（本条消息由概率式回复触发，流萤想起开拓者）主题：{topic_hint}。原因：{reason}",
            analyzer_intent="proactive",
            recent_history=session["context"].get_recent(15),
            memory_head=memory_head,
            environment=environment,
        ))
    else:
        out = polisher.polish(PolisherInput(
            user_input=topic_hint,
            analyzer_summary=f"（本条消息是流萤主动发给开拓者的，不是回复）主题：{topic_hint}。原因：{reason}",
            analyzer_intent="proactive",
            recent_history=session["context"].get_recent(15),
            memory_head=memory_head,
            environment=environment,
            proactive_context=(
                f"你现在是主动找开拓者说话。主题：{topic_hint}。原因：{reason}。"
                "不要解释'为什么找你'——像平时发短信一样，直接说想说的话。不要以'在吗''忙吗'开头。"
                "不要用'傍晚/天气/景物描写+问候对方'的固定套路收尾——像普通聊天一样直接说事情。"
                + (("你在医疗舱里不能出门赴约——'见面'是开拓者来看你，由他定地方，你等着就好；"
                    "不解释身体状况、不提恢复进度。") if mode == "story" else "")
            ),
        ))
    messages = list(out.messages)
    if not messages:
        return ProactiveResult()

    # 表情包（story 模式；haruno 旁白不适合主动场景，跳过）
    try:
        if mode == "story":
            organizer = Organizer(client, model=organizer_model, effort=organizer_effort, mode=mode)
            org = organizer.organize(OrganizerInput(
                user_input=topic_hint,
                reply_texts=[m["content"] for m in messages if m.get("type") == "text"],
                recent_history=session["context"].get_recent(5),
                mode=mode,
            ))
            if org.sticker_label:
                from tools.sticker_picker import pick_sticker_by_label
                entry = pick_sticker_by_label(org.sticker_label)
                if entry:
                    messages.append({"type": "sticker", "path": entry.file, "label": entry.label})
    except Exception as e:
        logger.warning("主动消息表情包调度失败（跳过）: %s", e)

    return ProactiveResult(messages=messages, reason_type=reason_type,
                           topic_hint=topic_hint, reason=reason)


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


# ── 概率式回复门控（信号量 + 概率）──────────────────

def prob_gate_open(enabled: bool, prob_value: float, mode: str = DEFAULT_MODE) -> tuple:
    """概率式回复门控。返回 (通过, 拒绝原因或 None)。

    服务端掷概率（读配置值），前置条件只有：开关 + ACTIVE 信号量。
    """
    if not enabled:
        return False, "概率式已关闭"
    # ACTIVE 懒恢复：超 10 分钟未回应 → 自动复位（原子检查+恢复）
    _active_try_recover(mode)
    if _active_get(mode) <= 0:
        return False, "主动性信号量未恢复（等用户回应或超时）"
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
                     hour: int = None) -> tuple:
    """隐藏式回复门控。返回 (通过, 拒绝原因或 None)。

    前置条件：开关 + 冷却（距上次隐藏式 ≥ 冷却间隔）+ 时段概率。
    时段概率 = 时段权重 × 用户配置概率（整体缩放）。
    不检查 ACTIVE（前后台场景独立），不消耗主动式轮次机会。

    hour 参数仅供测试/模拟注入固定时段，缺省取当前时间。
    """
    if not enabled:
        return False, "隐藏式已关闭"
    last_ts = _HIDDEN.get(_state_key(mode), 0)
    if last_ts and time.time() - last_ts < _HIDDEN_COOLDOWN_MIN * 60:
        mins = int((_HIDDEN_COOLDOWN_MIN * 60 - (time.time() - last_ts)) / 60)
        return False, f"隐藏式冷却中（剩 {mins} 分钟）"
    if hour is None:
        hour = datetime.now().hour
    pv = max(0.0, min(1.0, float(prob_value))) * _hidden_hour_weight(hour)
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


# ── 对外主入口（routes / Android 后台调用）──────────
def check_and_generate(session: dict, client, mode: str = DEFAULT_MODE,
                       enabled: bool = True, hard: int = 4, soft: float = 0.5,
                       prob_enabled: bool = True, prob_value: float = 0.3,
                       hidden: bool = False,
                       polisher_model: str = "deepseek-v4-flash",
                       polisher_effort: str = "high",
                       polisher_temperature: float = 0.5,
                       organizer_model: str = "deepseek-v4-flash",
                       organizer_effort: str = "none",
                       memory_head: str = "") -> ProactiveResult:
    """主动式 + 概率式 + 隐藏式统一入口。

    流程（REPLY 由调用方获取；本函数内做状态检查 + 通道串联）：
    1. hidden=True（隐藏式）：只走 hidden_gate_open（独立冷却+概率，不碰 ACTIVE）
    2. 否则（前台）：主动式（gate_open：硬约束轮次 + 软约束概率）→ 触发则生成；
       主动式未触发 → 概率式（prob_gate_open：ACTIVE + 概率掷骰）→ 触发则生成
    3. 都不触发 → 返回空

    主动式/概率式共用 ACTIVE 信号量互斥：任一触发成功 → ACTIVE=0，
    直到用户回应（/chat 入口复位）或超 10 分钟自动恢复。
    隐藏式完全独立（HIDDEN 冷却），不影响前台状态。

    写盘前原子检查：生成期间用户若发了新消息（conversation 新增了行），
    主动消息丢弃——用户主动说话时流萤不需要再找话题（顺序永远正确）。

    主动轮写入 ctx（add_proactive_turn）：主动消息也是会话记忆的一部分，
    独立轮次不计入用户轮计数（硬约束轮次预算只数用户轮）。
    """
    global _PROACTIVE_SENT, _PROACTIVE_DISCARDED, _PROACTIVE_GATE_REJECT
    ctx = session["context"]
    turn_count = ctx.turn_count

    # ── 春日手信模式规则：概率式/隐藏式通道不参与（直接不触发）──
    # 该模式设定是"刚认识、还在旅行"的新鲜感，突兀的"想起你/后台消息"破坏氛围；
    # 只有主动式（跟随对话节奏的轮次制）参与。
    if mode == "haruno" and hidden:
        return ProactiveResult()   # 隐藏式：haruno 直接不触发

    # ── 隐藏式（独立通道：冷却 + 概率，不碰 ACTIVE）──
    if hidden:
        ok, reason = hidden_gate_open(prob_enabled, prob_value, mode)
        if not ok:
            with _lock:
                _PROACTIVE_GATE_REJECT += 1
            return ProactiveResult()
        result = _run_generation(session, client, mode, turn_count, prob=True, hidden=True,
                                 polisher_model=polisher_model,
                                 polisher_effort=polisher_effort,
                                 polisher_temperature=polisher_temperature,
                                 organizer_model=organizer_model,
                                 organizer_effort=organizer_effort,
                                 memory_head=memory_head)
        if result.messages and not result.discarded:
            _mark_hidden_sent(mode)   # 更新冷却（不碰 ACTIVE）
        return result

    # ACTIVE 懒恢复：超 10 分钟未回应 → 自动复位（原子检查+恢复）
    _active_try_recover(mode)

    # ── 主动式（硬约束 + 软约束概率）──
    ok, reason, consume = gate_open(enabled, hard, soft, mode, turn_count=turn_count)
    if ok:
        result = _run_generation(session, client, mode, turn_count, prob=False,
                                 polisher_model=polisher_model,
                                 polisher_effort=polisher_effort,
                                 polisher_temperature=polisher_temperature,
                                 organizer_model=organizer_model,
                                 organizer_effort=organizer_effort,
                                 memory_head=memory_head)
        if result.messages and not result.discarded:
            _active_set(mode, 0)   # 主动式触发 → 消耗 ACTIVE
            return result
        # 主动式门控通过但无动机/被丢弃 → 信号量未消耗，fallthrough 到概率式
    else:
        if consume:
            # 判断机会消耗：记录（turn=当前用户轮数，sent=False）
            _append_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "_ts": time.time(),
                "mode": mode,
                "turn": turn_count,
                "sent": False,
                "reason": reason,
            }, mode=mode)
        with _lock:
            _PROACTIVE_GATE_REJECT += 1

    # ── 概率式（主动式未触发才走；ACTIVE + 概率掷骰）──
    if mode == "haruno":
        return ProactiveResult()   # 春日手信：概率式通道不参与，直接不触发
    if _active_get(mode) <= 0:
        return ProactiveResult()   # 主动性互斥中，概率式不可触发
    ok2, reason2 = prob_gate_open(prob_enabled, prob_value, mode)
    if not ok2:
        return ProactiveResult()   # 概率式门控拒绝（概率未中/开关关）

    result = _run_generation(session, client, mode, turn_count, prob=True,
                             polisher_model=polisher_model,
                             polisher_effort=polisher_effort,
                             polisher_temperature=polisher_temperature,
                             organizer_model=organizer_model,
                             organizer_effort=organizer_effort,
                             memory_head=memory_head)
    if result.messages and not result.discarded:
        _active_set(mode, 0)   # 概率式触发 → 消耗 ACTIVE
    return result


def _run_generation(session: dict, client, mode: str, turn_count: int, prob: bool,
                    polisher_model: str, polisher_effort: str,
                    polisher_temperature: float, organizer_model: str,
                    organizer_effort: str, memory_head: str,
                    hidden: bool = False) -> ProactiveResult:
    """动机决策 → 生成 → 冲突检查 → 写盘 → 主动轮写 ctx（共享生成骨架）。"""
    global _PROACTIVE_SENT, _PROACTIVE_DISCARDED
    ctx = session["context"]
    from modules.conversation_store import get_total_count
    n_before = get_total_count(mode=mode)

    result = generate_proactive(session, client, mode=mode,
                                polisher_model=polisher_model,
                                polisher_effort=polisher_effort,
                                polisher_temperature=polisher_temperature,
                                organizer_model=organizer_model,
                                organizer_effort=organizer_effort,
                                memory_head=memory_head,
                                use_reply_flow=prob,
                                allow_casual=prob or hidden)
    if not result.messages:
        if not prob:
            # 主动式：机会已消耗（动机决策无动机 → 不说话，记录）
            _append_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "_ts": time.time(),
                "mode": mode,
                "turn": turn_count,
                "sent": False,
                "reason": "无动机",
            }, mode=mode)
        return result

    # 冲突检查：生成期间用户发了新消息 → 丢弃
    n_after = get_total_count(mode=mode)
    if n_after > n_before:
        result.discarded = True
        with _lock:
            _PROACTIVE_DISCARDED += 1
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "_ts": time.time(),
            "mode": mode,
            "sent": False,
            "reason_type": result.reason_type,
            "topic_hint": result.topic_hint,
            "discarded": True,
            "conflict": "user_message_arrived",
        }
        if hidden:
            entry["hidden"] = True
        elif prob:
            entry["prob"] = True
        else:
            entry["turn"] = turn_count
        _append_log(entry, mode=mode)
        return result

    # 写盘：流萤消息（proactive 标记），回填 time 供前端渲染
    from modules.conversation_store import append_message
    for m in result.messages:
        record = {"type": m.get("type"), "proactive": True}
        if m.get("type") == "text":
            record["content"] = m.get("content", "")
        elif m.get("type") == "sticker":
            record["path"] = m.get("path", "")
            record["label"] = m.get("label", "")
        seq, t = append_message("firefly", record, mode=mode)
        m["time"] = t
    with _lock:
        _PROACTIVE_SENT += 1

    # 主动轮写入会话内存（独立轮次，不计用户轮）——流萤知道自己主动说过什么
    try:
        texts = [m.get("content", "") for m in result.messages if m.get("type") == "text"]
        if texts:
            ctx.add_proactive_turn(" ".join(texts))
        for m in result.messages:
            if m.get("type") == "sticker":
                ctx.add_action("表情包", m.get("label", "表情"))
    except Exception as e:
        logger.warning("主动轮写 ctx 失败: %s", e)

    # 记录（sent=True）
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_ts": time.time(),
        "mode": mode,
        "sent": True,
        "reason_type": result.reason_type,
        "topic_hint": result.topic_hint,
        "reason": result.reason,
        "discarded": False,
    }
    if hidden:
        entry["hidden"] = True
    elif prob:
        entry["prob"] = True
    else:
        entry["turn"] = turn_count
    _append_log(entry, mode=mode)
    return result


# ── Android 后台入口（KeepAliveService 直调）────────
def _last_active_mode() -> str:
    """最后活跃模式：各模式 conversation 最后 user 消息时间戳取较新。

    隐藏式回复归属判定：用户最后在哪个模式聊天，流萤就"从那个世界"想起开拓者。
    无任何 user 消息（全新安装）→ 默认模式。
    """
    best, best_ts = DEFAULT_MODE, 0.0
    from modules import app_config as cfg
    for m in cfg.MODES:
        ts = _last_user_msg_ts(m)
        if ts > best_ts:
            best, best_ts = m, ts
    return best


def backdoor_proactive_check(mode: str = None) -> list:
    """Android 后台定时器调用：直接执行隐藏式主动检查，返回消息文本列表。

    无 session（后台可能没有聊天会话）→ 内部创建独立 context 并 hydrate 回灌。
    REPLY 非阻塞获取（与前台轮询互斥）；隐藏式独立通道（HIDDEN 冷却，不碰 ACTIVE）。
    mode 缺省/非法 → 自动判定最后活跃模式（Android 不再硬编码 story）。
    """
    from modules import app_config as cfg
    if not mode or mode not in cfg.MODES:
        mode = _last_active_mode()
    if not reply_try_lock(mode):
        return []
    try:
        from modules.context_manager import ContextManager
        from modules.conversation_store import hydrate_context
        client = cfg.get_client()
        if not client:
            return []
        # 重启兜底：HIDDEN 冷却从 proactive_log 重建（独立于 ACTIVE）
        _restore_hidden_state(mode)
        session = {"context": ContextManager(), "memory_head": ""}
        hydrate_context(session["context"], max_turns=20, mode=mode)
        result = check_and_generate(
            session, client, mode=mode,
            enabled=bool(cfg.config.get("proactive_enabled", True)),
            hard=cfg.config.get("proactive_hard", 4),
            soft=cfg.config.get("proactive_soft", 0.5),
            prob_enabled=bool(cfg.config.get("hidden_reply_enabled", True)),
            prob_value=cfg.config.get("prob_reply_value", 0.3),
            hidden=True,   # 隐藏式独立通道：HIDDEN 冷却，不碰 ACTIVE
            polisher_model=cfg.config["polisher_model"],
            polisher_effort=cfg.config["polisher_effort"],
            polisher_temperature=cfg.config["polisher_temperature"],
            organizer_model=cfg.config["organizer_model"],
            organizer_effort=cfg.config["organizer_effort"],
            memory_head=session.get("memory_head", ""),
        )
        return [m.get("content", "") for m in result.messages if m.get("type") == "text"]
    except Exception as e:
        logger.warning("后台主动检查失败: %s", e)
        return []
    finally:
        reply_unlock(mode)
