# -*- coding: utf-8 -*-
"""主动性模块 — 流萤在适合的时候主动找开拓者说话
（公共 API + 三通道编排。门控实现拆至 proactive_gate.py，动机决策与生成拆至
proactive_gen.py；本文件 re-export 全部对外名字，modules.proactive.<名字> 口径不变。）

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

配置（config.json，设置面板可调；默认值以 app_config._load_config 为准）：
- proactive_enabled: bool   开关（默认关）
- proactive_hard: 1-10      硬约束（每 N 轮用户对话 1 次判断机会，默认 6）
- proactive_soft: 0.0-1.0   软约束（判断机会到来时的触发概率，默认 0.35）
- prob_reply_value: 0.0-1.0 概率式/隐藏式回复触发概率（默认 0.10，默认开）
"""

import logging
import random  # noqa: F401  打桩锚点：测试经 modules.proactive.random 补丁 random.random
import time
from datetime import datetime

from modules.app_config import DEFAULT_MODE
from modules import proactive_gate as _gate
# re-export：门控层对外名字（routes 函数内 from-import / 测试 P.<名字> / metrics 动态加载，
# 全部继续以 modules.proactive.<名字> 可用；dict 对象为同一引用，原地修改双向可见）
from modules.proactive_gate import (  # noqa: F401
    _ACTIVE, _HIDDEN, _IGNORED, _PROB_LAST_CHECK, _REPLY_LOCKS,
    _active_get, _active_reset, _active_set, _active_try_recover,
    _append_log, _hidden_hour_weight, _ignored_count, _last_judge_turn,
    _last_user_msg_ts, _load_log, _log_file, _mark_hidden_sent,
    _reply_lock_for, _restore_active_semaphore, _restore_hidden_state,
    _state_key, _update_ignored,
    gate_open, get_counters, prob_gate_open,
    reply_lock, reply_try_lock, reply_unlock, reset_states,
)
from modules.proactive_gen import (  # noqa: F401
    InputRejected, ProactiveError, ProactiveResult,
    _decide_motivation, generate_proactive,
)

logger = logging.getLogger(__name__)


# ── 门控 facade（权重函数经本模块命名空间解析）────────────────
def hidden_gate_open(enabled: bool, prob_value: float, mode: str = DEFAULT_MODE,
                     hour: int = None) -> tuple:
    """隐藏式回复门控。返回 (通过, 拒绝原因或 None)。逻辑在 proactive_gate。

    单独包一层的原因：时段权重函数经本模块命名空间解析（上面的
    _hidden_hour_weight re-export）——测试打桩 P._hidden_hour_weight 后，
    直连 P.hidden_gate_open 与 check_and_generate 全链路都吃到桩。
    """
    return _gate.hidden_gate_open(enabled, prob_value, mode=mode, hour=hour,
                                  _weight_fn=_hidden_hour_weight)


# ── 对外主入口（routes / Android 后台调用）──────────
def check_and_generate(session: dict, client, mode: str = DEFAULT_MODE,
                       enabled: bool = True, hard: int = 4, soft: float = 0.5,
                       prob_enabled: bool = True, prob_value: float = 0.3,
                       hidden: bool = False,
                       polisher_model: str = "deepseek-v4-flash-vision-exp",
                       polisher_effort: str = "high",
                       polisher_temperature: float = 0.5,
                       organizer_model: str = "deepseek-v4-flash-vision-exp",
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
            _gate._count_gate_reject()
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
        _gate._count_gate_reject()

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
        _gate._count_discarded()
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
    _gate._count_sent()

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
            enabled=bool(cfg.pack_cfg(mode, "proactive_enabled", True)),
            hard=cfg.pack_cfg(mode, "proactive_hard", 4),
            soft=cfg.pack_cfg(mode, "proactive_soft", 0.5),
            prob_enabled=bool(cfg.pack_cfg(mode, "hidden_reply_enabled", True)),
            prob_value=cfg.pack_cfg(mode, "prob_reply_value", 0.3),
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
