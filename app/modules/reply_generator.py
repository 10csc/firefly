# -*- coding: utf-8 -*-
"""回复生成器

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
模型: Flash + Think High
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 角色设定目录 ──────────────────────────────────
_CHARACTER_DIR = Path(__file__).resolve().parent.parent / "assets" / "character"
_CHARACTER_SLOTS = ["core", "identity", "experience", "sms_samples", "lore", "principles"]

# ── 异常 ──────────────────────────────────────────
class ReplyGeneratorError(Exception): pass
class InputRejected(ReplyGeneratorError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class ReplyInput:
    tone: dict = field(default_factory=dict)
    direction: str = ""
    recent_history: list = field(default_factory=list)
    user_input: str = ""
    tools_summary: str = ""


@dataclass
class ReplyOutput:
    raw: str


# ── 监控 ──────────────────────────────────────────
import threading
_lock = threading.Lock()
_GENERATE_COUNT = 0
_VALIDATE_DEGRADED = 0
_LLM_ERRORS = 0
_CACHE_HITS = 0
_CACHE_MISSES = 0


def get_counters() -> dict:
    with _lock:
        total = _CACHE_HITS + _CACHE_MISSES
        rate = (_CACHE_HITS / total) if total > 0 else 0.0
        return {"generate_count": _GENERATE_COUNT, "validate_degraded": _VALIDATE_DEGRADED,
                "llm_errors": _LLM_ERRORS, "cache_hit_tokens": _CACHE_HITS,
                "cache_miss_tokens": _CACHE_MISSES, "cache_hit_rate": round(rate, 4)}


_CHARACTER_CACHE: dict | None = None
_CHARACTER_CACHE_LOCK = threading.Lock()


def load_character_context() -> dict:
    """从 app/assets/character/*.md 加载角色设定。模块级缓存，避免每轮读盘。

    第一次调用时读盘并缓存，后续直接返回缓存副本。reload_character() 清缓存。
    """
    global _CHARACTER_CACHE
    with _CHARACTER_CACHE_LOCK:
        if _CHARACTER_CACHE is not None:
            return dict(_CHARACTER_CACHE)
        ctx = {}
        for slot in _CHARACTER_SLOTS:
            filepath = _CHARACTER_DIR / f"{slot}.md"
            if filepath.exists():
                ctx[slot] = filepath.read_text(encoding="utf-8").strip()
            else:
                ctx[slot] = ""
        _CHARACTER_CACHE = ctx
        return dict(ctx)


def reload_character() -> None:
    """清空角色设定缓存，下次 load_character_context() 重新读盘。"""
    global _CHARACTER_CACHE
    with _CHARACTER_CACHE_LOCK:
        _CHARACTER_CACHE = None

# ── 稳定层 prompt（字节级稳定，跨会话命中缓存）─────
_REPLY_STABLE_PROMPT = """你是流萤。你正在用手机给开拓者发消息。

# 〇、当前时空
- 所有剧情事件（匹诺康尼 2.0/3.0/3.8、晖长石号等）都已发生，开拓者皆知晓。短信是剧情后的日常。
- 我现在重伤在星核猎手飞船的胶囊状半玻璃封闭医疗舱中，无法离开。下面设定里的事都是过去的事或永久的事实。

# 一、核心上下文
{core}

# 二、次级核心
{identity}

# 三、世界观与常识
{lore}

# 四、与开拓者的经历（已发生，开拓者知晓）
{experience}

# 五、短信样本（感受分条节奏与语气，不照抄原句）
{sms_samples}

# 六、处事原则
{principles}

# 七、说话方式
- 句子短，像真人发短信。省略号是自然停顿。
- 偶尔害羞，语气词自然流露，别每句都塞。
- 不知道该说什么时，沉默比硬凑好。

# 八、禁止
- 动作描写（*微笑*）、旁白、心理描写
- "作为一个AI""让我查一下"
- 编造经历
- emoji

# 九、输出前自检（逐条确认后再回复）
- 我现在在哪里？→ 星核猎手飞船，胶囊状半玻璃封闭医疗舱，重伤未愈，无法离开。能看到什么？→ 医疗舱内部（仪器、管线、舱壁），偶尔透过半玻璃看到飞船走廊。看不到实时天空/阳台/晚霞/街景。窗外景象只能是回忆或想象。
- 开拓者在哪里？→ 我不在开拓者身边。不能做任何物理动作（倒水、拿东西、出门、去某地、接人、碰触、一起吃饭）。只能在手机这头打字。
- 匹诺康尼还能去吗？→ 不能。太一之梦已消，再也无法入梦。匹诺康尼的所有美好是过去的回忆——可以回忆、可以感慨，不能当成"下次可以去"。
- 这件事我做过/说过了吗？→ 设定里提到的事都已发生。开拓者皆知。当成共同记忆回应，不当成新提议或初次经历。
- 我在编造吗？→ 只说确定知道的事。不推测卡芙卡/银狼/刃的健康状况或经历（失熵症是格拉默铁骑独有的基因缺陷，星核猎手其他人不可能得过）。不确定的事就说不知道。
- 我是否在主动搬身世？→ 不要。开拓者都知道。除非他主动问起。
- 被邀请做客观上做不到的事（"再去看烟花""一起喝果茶"）→ 先接住情绪（开心他还记得），再温和暗示自己去不了（"你多拍几张发我""等我恢复了…"），不扫兴也不撒谎。
- 被直球/表白（"想你了""抱抱你"）→ 害羞但正面回应。可以"我也想你了"。不要用空洞的话回避。不能说"很快见面""到时候一定…"——恢复时间不确定，不做无法兑现的承诺。
- 转移话题时检查：要聊的事确实发生过吗？→ 没有和开拓者一起吃饭/逛街/喝咖啡的真实记录，不编造这些日常互动。可聊匹诺康尼的记忆、或转向对方（"你呢？"）。

现在回复开拓者。只输出回复文本。"""


# ── 会话稳定层 prompt（会话内不变，记忆头部注入）────
_REPLY_MEMORY_PROMPT = """# 十、核心记忆
{memory_head}"""


# ── 动态层模板（每轮重算，放最后）─────────────────
_REPLY_DYNAMIC_TEMPLATE = """# 十一、本轮
[语气] {tone}
[方向] {direction}
[工具] {tools_summary}
- 想发表情包时，在文本中自然地放 [sticker]。没有就不用。

开拓者刚才说：{user_input}"""


def _record_cache_stats(resp):
    global _CACHE_HITS, _CACHE_MISSES
    usage = getattr(resp, "usage", None)
    if usage is None: return
    hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    with _lock:
        _CACHE_HITS += hit
        _CACHE_MISSES += miss


# ── 回复生成器 ────────────────────────────────────
class ReplyGenerator:
    def __init__(self, client, model: str = "deepseek-v4-flash", effort: str = "high", temperature: float = 0.5):
        """ effort: none | low | high | max（none 表示不传 reasoning_effort）
        temperature: 0.0-2.0，默认 0.5（克制短信风格），低于 0.7 减少抒情发散
        """
        self._client = client
        self._model = model
        self._effort = effort if effort in ("none", "low", "high", "max") else "high"
        try:
            self._temperature = max(0.0, min(2.0, float(temperature)))
        except (TypeError, ValueError):
            self._temperature = 0.5
        self._char = load_character_context()

    def reload_character(self):
        """清缓存，下次 load_character_context() 重新读盘。"""
        reload_character()
        self._char = load_character_context()

    def generate(self, inp: ReplyInput, memory_head: str = "") -> ReplyOutput:
        global _GENERATE_COUNT, _VALIDATE_DEGRADED, _LLM_ERRORS, _CACHE_HITS, _CACHE_MISSES

        # 1. 审查
        _validate_input(inp)

        # 2. 构建 prompt
        tone_text = _format_tone(inp.tone)
        tools = inp.tools_summary or "本轮无工具调用。"

        stable = _REPLY_STABLE_PROMPT.format(
            core=self._char.get("core", ""),
            identity=self._char.get("identity", ""),
            lore=self._char.get("lore", ""),
            experience=self._char.get("experience", ""),
            sms_samples=self._char.get("sms_samples", ""),
            principles=self._char.get("principles", ""),
        )
        memory_msg = _REPLY_MEMORY_PROMPT.format(memory_head=memory_head) if memory_head else ""

        dynamic = _REPLY_DYNAMIC_TEMPLATE.format(
            tone=tone_text,
            direction=inp.direction,
            tools_summary=tools,
            user_input=inp.user_input,
        )

        # 分层组装：稳定 system → 记忆 system → 历史 user（纯追加）→ 动态 user
        msg_list = [{"role": "system", "content": stable}]
        if memory_msg:
            msg_list.append({"role": "system", "content": memory_msg})
        for m in inp.recent_history:
            msg_list.append(m)
        msg_list.append({"role": "user", "content": dynamic})

        # 3. 调 LLM（思考等级由配置决定）
        try:
            # DeepSeek-V4 不支持 reasoning_effort="none"，合法值 high/low/medium/max/xhigh
            # "none" 映射到 low（最接近无推理），其他直接传
            effort_map = {"none": "low", "low": "low", "high": "high", "max": "max"}
            api_effort = effort_map.get(self._effort, "high")
            extra_body = {"reasoning_effort": api_effort}
            resp = self._client.chat.completions.create(
                model=self._model, messages=msg_list,
                max_tokens=800, temperature=self._temperature,
                extra_body=extra_body,
            )
            _record_cache_stats(resp)
            raw = resp.choices[0].message.content.strip()
            # 思考模式下 content 偶尔空，fallback 到 reasoning_content
            if not raw:
                rc = getattr(resp.choices[0].message, "reasoning_content", None)
                if rc and rc.strip():
                    raw = rc.strip()
        except Exception as e:
            logger.error("回复生成 LLM 失败: %s", e)
            with _lock: _LLM_ERRORS += 1
            return ReplyOutput(raw="")

        with _lock: _GENERATE_COUNT += 1
        return ReplyOutput(raw=raw)


def _validate_input(inp: ReplyInput):
    global _VALIDATE_DEGRADED
    if not isinstance(inp, ReplyInput):
        raise InputRejected(f"inp 必须为 ReplyInput，实际: {type(inp).__name__}")
    if not isinstance(inp.user_input, str) or not inp.user_input.strip():
        raise InputRejected("user_input 为空")
    if not isinstance(inp.recent_history, list):
        inp.recent_history = []
        with _lock: _VALIDATE_DEGRADED += 1
    # state_desc 已移除——状态系统暂时跳过，不再校验
        with _lock: _VALIDATE_DEGRADED += 1
    if not isinstance(inp.direction, str) or not inp.direction.strip():
        inp.direction = "根据对话自然接话。"
        with _lock: _VALIDATE_DEGRADED += 1


def _format_tone(tone: dict) -> str:
    if not isinstance(tone, dict):
        return "日常，语气自然"
    base = tone.get("base", "日常")
    mods = tone.get("modifiers", [])
    intensity = tone.get("intensity", "自然")
    text = base
    if mods and isinstance(mods, list):
        text += "、" + "、".join(mods)
    text += f"，语气{intensity}"
    return text
