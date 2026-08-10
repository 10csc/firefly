# -*- coding: utf-8 -*-
"""上下文管理器 — 对话历史存储 + token 估算 + 精力计算

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
"""

import logging
from copy import deepcopy
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── 异常 ──────────────────────────────────────────
class ContextManagerError(Exception):
    """上下文管理器异常基类"""
    pass


class InputRejected(ContextManagerError):
    """审查阶段：输入不合法"""
    pass


class InternalError(ContextManagerError):
    """验证阶段：内部状态异常"""
    pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class ContextStats:
    """上下文统计快照"""
    total_tokens: int        # 估算的总 token 数
    energy: int              # 精力值
    energy_max: int = 300    # 精力上限
    fatigue_visible: bool = False  # 疲劳是否可见（energy < 100）


# ── Token 估算（内化实现，不引外部库）─────────────
def _estimate_tokens(text: str) -> int:
    """中英文混合 token 估算。
    DeepSeek 分词中文约 0.6 token/字，英文/符号约 4 字符折合 1 token。
    此为近似值，用于精力计算，不需要精确匹配 API 实际 token 数。
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - chinese
    return max(1, int(chinese * 0.6 + other * 0.25))


def _estimate_messages_tokens(messages: list) -> int:
    """估算消息列表的总 token 数"""
    total = 0
    for m in messages:
        total += _estimate_tokens(m.get("content", ""))
        total += 4  # 每条消息 ~4 tokens 格式开销
    return total


# ── 核心类 ────────────────────────────────────────
class ContextManager:
    """上下文管理器 — 管理对话历史 + token 监控 + 精力计算"""

    def __init__(self, energy_max: int = 300, token_capacity: int = 300000):
        # 审查构造参数
        if energy_max <= 0:
            raise ContextManagerError(f"energy_max 必须为正数，当前: {energy_max}")
        if token_capacity <= 0:
            raise ContextManagerError(f"token_capacity 必须为正数，当前: {token_capacity}")
        self._history: list[dict] = []
        self._energy_max = energy_max
        self._token_capacity = token_capacity

    # ── 行为记录 ────────────────────────────────
    def add_action(self, action_type: str, action_detail: str):
        """记录流萤的行为到对话历史（表情包/气泡切换等）。
        以 system 消息形式插入，规划器读取历史时自然感知。

        Args:
            action_type: 行为类型，如 "表情包"、"气泡切换"
            action_detail: 行为描述，如 "比心"、"光阴莫负"
        """
        if not isinstance(action_type, str) or not action_type.strip():
            raise InputRejected("action_type 为空")
        if not isinstance(action_detail, str) or not action_detail.strip():
            raise InputRejected("action_detail 为空")
        content = f"[行为: {action_type.strip()}] {action_detail.strip()}"
        self._history.append({"role": "system", "content": content})
        logger.debug("add_action: %s", content)

    # ── 主入口 ──────────────────────────────────
    def add_turn(self, user_msg: str, assistant_msg: str) -> ContextStats:
        """添加一轮对话，返回更新后的统计。
        流程：审查 → 处理 → 验证 → 输出
        """        # 1. 审查阶段 —— 不合法直接拒绝
        if not isinstance(user_msg, str):
            raise InputRejected(f"user_msg 必须为 str，实际: {type(user_msg).__name__}")
        if not isinstance(assistant_msg, str):
            raise InputRejected(f"assistant_msg 必须为 str，实际: {type(assistant_msg).__name__}")
        if not user_msg.strip():
            raise InputRejected("user_msg 为空字符串，拒绝存储")
        if not assistant_msg.strip():
            raise InputRejected("assistant_msg 为空字符串，拒绝存储")

        # 2. 模块处理
        self._history.append({"role": "user", "content": user_msg})
        self._history.append({"role": "assistant", "content": assistant_msg})
        logger.debug("add_turn: 轮次=%d, 累计消息=%d", self.turn_count, len(self._history))

        # 3. 验证阶段 —— 内部状态异常立即暴露
        stats = self._compute_stats()
        if not (0 <= stats.energy <= self._energy_max):
            raise InternalError(
                f"energy 溢出: {stats.energy}，范围 [0, {self._energy_max}]"
            )
        if stats.total_tokens < 0:
            raise InternalError(f"total_tokens 为负: {stats.total_tokens}")

        # 4. 最终输出
        return stats

    def add_proactive_turn(self, assistant_msg: str) -> ContextStats:
        """添加一轮主动消息（流萤主动找开拓者，非回复）。

        与 add_turn 的区别：只有 assistant 一方（无 user），且不计入
        turn_count（用户轮计数）——主动轮单独划分，不影响硬约束轮次预算。
        流程：审查 → 处理 → 验证 → 输出
        """
        if not isinstance(assistant_msg, str):
            raise InputRejected(f"assistant_msg 必须为 str，实际: {type(assistant_msg).__name__}")
        if not assistant_msg.strip():
            raise InputRejected("assistant_msg 为空字符串，拒绝存储")

        self._history.append({"role": "assistant", "content": assistant_msg, "proactive": True})
        logger.debug("add_proactive_turn: 累计消息=%d", len(self._history))

        stats = self._compute_stats()
        if not (0 <= stats.energy <= self._energy_max):
            raise InternalError(
                f"energy 溢出: {stats.energy}，范围 [0, {self._energy_max}]"
            )
        if stats.total_tokens < 0:
            raise InternalError(f"total_tokens 为负: {stats.total_tokens}")
        return stats

    def pop_last_turn(self) -> tuple[str, str] | None:
        """移除最后一轮对话（user + assistant + 中间的 system action 消息）。

        优先移除最后一段主动轮（assistant 且 proactive）——主动轮是独立轮次，
        用户撤回"上一轮"时应优先撤回最近一次主动消息（含其后无 user 跟随的情况）。
        否则走用户轮逻辑。

        Returns:
            (user_msg, assistant_msg) 或 ("__proactive__", assistant_msg) 或 None
        """
        # 末尾是主动消息（proactive 标记）→ 撤回最近一段连续主动轮
        if self._history and self._history[-1].get("proactive"):
            j = len(self._history) - 1
            while j > 0 and self._history[j - 1].get("proactive"):
                j -= 1
            last = self._history[-1]["content"]
            del self._history[j:]
            logger.debug("pop_last_turn: 移除主动轮, 剩余消息=%d", len(self._history))
            return "__proactive__", last

        # 从末尾向前找最后一个 role="user" 的消息索引
        i = None
        for idx in range(len(self._history) - 1, -1, -1):
            if self._history[idx]["role"] == "user":
                i = idx
                break
        if i is None:
            return None

        # 扩展到连续 user 块起点：一轮可能包含多条 user（分条发送），整轮撤回
        while i > 0 and self._history[i - 1]["role"] == "user":
            i -= 1

        # 从 i 往后找最后一个 role="assistant" 的消息索引
        j = None
        for idx in range(i + 1, len(self._history)):
            if self._history[idx]["role"] == "assistant":
                j = idx
        if j is None:
            return None

        user_msg = self._history[i]["content"]
        assistant_msg = self._history[j]["content"]

        # 删除从该 user 消息到末尾的所有消息
        del self._history[i:]

        logger.debug("pop_last_turn: 移除轮次, 剩余消息=%d", len(self._history))
        return user_msg, assistant_msg

    def turns_since_last_proactive(self) -> int | None:
        """距上次主动轮的用户轮数。从未主动过返回 None。

        硬约束轮次预算的数据源：主动轮不计入用户轮，因此直接按
        用户轮数（turn_count）与"最后一个主动轮后的用户消息数"求差。
        """
        last_p = None
        for idx in range(len(self._history) - 1, -1, -1):
            if self._history[idx].get("proactive"):
                last_p = idx
                break
        if last_p is None:
            return None
        return sum(1 for m in self._history[last_p + 1:] if m["role"] == "user")

    # ── 查询方法 ─────────────────────────────────
    def get_recent(self, n_turns: int = 10) -> list:
        """获取最近 n 轮对话，返回副本。
        按 user 消息数计轮次——历史中夹杂的 system 行为消息不挤占轮数。
        """
        if not isinstance(n_turns, int) or n_turns <= 0:
            raise InputRejected(f"n_turns 必须为正整数，当前: {n_turns}")
        seen = 0
        start = 0
        for idx in range(len(self._history) - 1, -1, -1):
            if self._history[idx]["role"] == "user":
                seen += 1
                if seen >= n_turns:
                    start = idx
                    break
        return deepcopy(self._history[start:])

    def get_full(self) -> list:
        """获取完整历史，返回副本"""
        return deepcopy(self._history)

    @property
    def stats(self) -> ContextStats:
        """当前统计快照"""
        st = self._compute_stats()
        # property 不能抛异常，静默修正边界
        if st.energy < 0:
            st.energy = 0
        if st.total_tokens < 0:
            st.total_tokens = 0
        return st

    @property
    def turn_count(self) -> int:
        """已存储的对话轮数（按 user 消息数计，system 行为消息不算轮）"""
        return sum(1 for m in self._history if m["role"] == "user")

    # ── 内部计算 ─────────────────────────────────
    def _compute_stats(self) -> ContextStats:
        total_tokens = _estimate_messages_tokens(self._history)
        energy = max(0, self._energy_max - total_tokens // 1000)
        return ContextStats(
            total_tokens=total_tokens,
            energy=energy,
            energy_max=self._energy_max,
            fatigue_visible=(energy < 100),
        )
