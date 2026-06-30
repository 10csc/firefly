# -*- coding: utf-8 -*-
"""心情更新器 — Adder(新情绪) + Decayer(消退) 并行 + 代码合并

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出"""

import logging

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────
_VALID_LABELS = frozenset({"安心", "开心", "低落", "害羞", "焦虑", "困惑"})
_CONFLICT_PAIRS = frozenset({
    ("开心", "低落"), ("低落", "开心"),
    ("安心", "焦虑"), ("焦虑", "安心"),
    ("安心", "低落"), ("低落", "安心"),
    ("低落", "害羞"), ("害羞", "低落"),
})


# ── 异常 ──────────────────────────────────────────
class MoodUpdaterError(Exception): pass
class InputRejected(MoodUpdaterError): pass
class OutputInvalid(MoodUpdaterError): pass


# ── Adder 提示词 — 只输出一个 标签:强度 ──────────
_ADDER_PROMPT = """你是一个情绪分类器。根据输入判断应新增什么情绪。只输出 情绪:数字，无则输出 无。

可选: 安心 开心 低落 害羞 焦虑 困惑
强度1-5。例: 低落:5 安心:2 无

规则（按优先级，命中即停）:
1.辱骂/攻击→低落:4-5
2.紧急/危险→焦虑:4-5
3.行为矛盾(辱骂后立刻道歉、态度剧烈翻转、言行不一致)→困惑:3-5。注意：如果对话历史显示近期有辱骂/攻击，本轮却道歉/示好，输出困惑而非安心——她需要时间消化这种矛盾
4.表白/性暗示→害羞:3-5
5.技术请求/超出认知→困惑:1-3。仅限编程、代码、AI、系统操作等流萤完全不了解的领域。关于她自身经历/装备/感受的提问不算困惑
6.道歉/关心/安抚→安心:2-3
7.日常闲聊→安心:2-3
8.无特别触发→无

只输出一个词，不要任何解释。"""


# ── Decayer 提示词 — 调整已有情绪强度 ────────────
_DECAYER_PROMPT = """判断当前对话是否构成有效的情绪恢复。只输出一个词。

## 恢复等级
有效恢复: 真诚道歉(承认错误+表达歉意)、持续的关心安抚、多轮正面闲聊
部分恢复: 敷衍道歉、短暂关心、转移话题到轻松方向
无恢复: 继续攻击、冷处理、完全不涉及情绪修复

当前情绪: {current_moods}
最近对话: {context}

输出: 有效恢复/部分恢复/无恢复"""


# ── Adder ────────────────────────────────────────
class MoodAdder:
    """检测本轮新增的情绪"""

    def __init__(self, client, model: str = "deepseek-v4-flash"):
        if client is None: raise InputRejected("client 不能为 None")
        self._client = client
        self._model = model

    def add(self, user_input: str, stop_reason: str, recent_history: list | None = None) -> dict | None:
        """返回 {"label":str, "intensity":int} 或 None"""
        if not isinstance(user_input, str):
            raise InputRejected("user_input 必须为 str")

        try:
            result = self._call(user_input, stop_reason, recent_history or [])
        except Exception as e:
            logger.error("MoodAdder API 失败: %s", e)
            return None

        return self._parse(result)

    def _call(self, user_input: str, stop_reason: str, history: list) -> str:
        # 组装最近对话
        ctx_lines = []
        for m in history[-10:]:  # 最近 5 轮
            role = "开拓者" if m["role"] == "user" else "流萤"
            ctx_lines.append(f"{role}: {m['content']}")
        ctx = "\n".join(ctx_lines) if ctx_lines else "（无历史）"

        user_msg = f"判定: {stop_reason}\n{ctx}\n输入: {user_input}"
        for attempt in range(3):
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _ADDER_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=200, temperature=0.0,
            )
            if resp.choices:
                c = resp.choices[0].message.content
                if c and c.strip():
                    return c.strip()
            logger.warning("MoodAdder 第%d次返回空", attempt + 1)
        raise MoodUpdaterError("MoodAdder 连续3次返回空")

    def _parse(self, raw: str) -> dict | None:
        raw = raw.strip()
        if raw == "无" or not raw:
            return None
        if ":" not in raw:
            return None
        label, _, intensity_str = raw.partition(":")
        label = label.strip()
        if label not in _VALID_LABELS:
            return None
        try:
            intensity = int(intensity_str.strip())
        except (ValueError, TypeError):
            intensity = 3
        intensity = max(1, min(5, intensity))
        return {"label": label, "intensity": intensity}


# ── Decayer: LLM判诚意 + 代码执行强度变化 ──────────
# 代码层消退规则
_DECAY_RULES = {
    "有效恢复": {"低落": -3, "焦虑": -3, "害羞": -1, "困惑": -1},
    "部分恢复": {"低落": -1, "焦虑": -1, "害羞": -1, "困惑": 0},
    "无恢复":  {},  # 自然微退: 强度>1的负面情绪 -1
}


class MoodDecayer:
    """LLM 判断恢复诚意，代码执行强度调整"""

    def __init__(self, client, model: str = "deepseek-v4-flash"):
        if client is None: raise InputRejected("client 不能为 None")
        self._client = client
        self._model = model

    def decay(
        self, user_input: str, current_moods: list, recent_history: list
    ) -> list[dict]:
        """返回调整后的 moods。负面情绪最低降到1（除非有效恢复可降到0删除）。"""
        if not current_moods:
            return []
        if not isinstance(current_moods, list):
            raise InputRejected("current_moods 必须为 list")

        # LLM 判断诚意
        level = "无恢复"  # 默认
        if current_moods:
            try:
                level = self._call(user_input, current_moods, recent_history or [])
            except Exception as e:
                logger.error("MoodDecayer API 失败，默认无恢复: %s", e)

        # 代码执行强度变化
        return self._apply(current_moods, level)

    def _call(self, user_input: str, moods: list, history: list) -> str:
        prev_text = "|".join(f"{m['label']}:{m['intensity']}" for m in moods)
        context_lines = []
        for m in history[-6:]:
            role = "开拓者" if m["role"] == "user" else "流萤"
            context_lines.append(f"{role}: {m['content']}")
        ctx = "\n".join(context_lines) if context_lines else "（无历史）"

        prompt = _DECAYER_PROMPT.format(current_moods=prev_text, context=ctx)

        for attempt in range(3):
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_input},
                ],
                max_tokens=200, temperature=0.0,
            )
            if resp.choices:
                c = resp.choices[0].message.content
                if c and c.strip():
                    return c.strip()
            logger.warning("MoodDecayer 第%d次返回空", attempt + 1)
        raise MoodUpdaterError("MoodDecayer 连续3次返回空")

    def _apply(self, moods: list, level: str) -> list[dict]:
        """根据诚意等级 + 修复后删除逻辑"""

        deltas = _DECAY_RULES.get(level.strip(), {})
        result = []
        for m in moods:
            label = m["label"]
            intensity = m["intensity"]

            if label in deltas:
                intensity += deltas[label]
            elif label in ("低落", "焦虑", "害羞", "困惑"):
                # 无恢复: 自然微退 -1（但不低于 1，明确行为转变前不归零）
                if intensity > 1:
                    intensity -= 1
            # 安心/开心: 不受消退影响

            intensity = max(0, min(5, intensity))
            if intensity > 0:
                result.append({"label": label, "intensity": intensity})
        return result if result else [{"label": "安心", "intensity": 2}]


# ── 合并逻辑（代码层）────────────────────────────
def merge_moods(decayed: list, added: dict | None) -> list[dict]:
    """合并消退后的情绪 + 新增情绪，消解冲突。
    流程：审查 → 合并 → 验证 → 输出
    """
    # 1. 审查
    if not isinstance(decayed, list):
        raise InputRejected(f"decayed 必须为 list，实际: {type(decayed).__name__}")
    for m in decayed:
        if not isinstance(m, dict) or "label" not in m or "intensity" not in m:
            raise InputRejected(f"decayed 元素格式错误: {m}")
    if added is not None:
        if not isinstance(added, dict) or "label" not in added or "intensity" not in added:
            raise InputRejected(f"added 格式错误: {added}")

    # 2. 合并
    moods = {m["label"]: m["intensity"] for m in decayed}

    if added:
        label, intensity = added["label"], added["intensity"]
        if label in moods:
            moods[label] = max(moods[label], intensity)
        else:
            moods[label] = intensity

    # 冲突消解
    for a, b in _CONFLICT_PAIRS:
        if a in moods and b in moods:
            if moods[a] >= moods[b]:
                del moods[b]
            else:
                del moods[a]

    result = [{"label": k, "intensity": v} for k, v in moods.items()]

    # 3. 验证
    if not result:
        raise OutputInvalid("merge 结果为空列表，不符合预期")
    for m in result:
        if m["label"] not in _VALID_LABELS:
            raise OutputInvalid(f"merge 产出非法标签: {m['label']}")
        if not (1 <= m["intensity"] <= 5):
            raise OutputInvalid(f"merge 产出非法强度: {m['intensity']}")

    return result
