# -*- coding: utf-8 -*-
"""主动性生成 — 动机决策（轻量 LLM）+ 消息生成（复用 polisher + organizer）

（自 proactive.py 拆分。门控见 proactive_gate.py，编排入口见 proactive.py；
本模块只读取 proactive_gate 的 proactive_log（recent_said 去重），不回依赖 facade。）

设计原则：
- 主动必须有可追溯的动机（记忆/手账/环境/牵挂），无来源的问候直接拒绝（主动式严格；
  概率式/隐藏式允许"随便聊聊"兜底——触发机会已消耗，不浪费）
- 生成复用 polisher 完整角色 prompt，不走独立起草器，角色一致性 100%
"""

import logging
from dataclasses import dataclass, field

from modules.app_config import DEFAULT_MODE, char_name, user_name
from modules.llm_base import load_slot
from modules.proactive_gate import _load_log

logger = logging.getLogger(__name__)


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


# ── 动机决策（轻量 LLM，Flash Non-think）───────────
def _decide_motivation(client, memory_head: str, journal: str, environment: str,
                       recent_topics: str, mode: str = DEFAULT_MODE,
                       allow_casual: bool = False, recent_said: str = "",
                       model: str = "deepseek-v4-flash-vision-exp") -> dict:
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
        f"\n\n注意：本次触发是你主动找{user_name(mode)}的机会（用户空闲/不在场）。"
        f"即使没有上述具体动机，也可以随便聊点什么——想想{user_name(mode)}此刻可能在做什么、"
        "分享当下的一件小事、一句轻轻的问候都可以。总之要说话，不要沉默。"
    ) if allow_casual else ""
    system = f"""你是{char_name(mode)}的内心判断层。判断她现在是否应该主动给{user_name(mode)}发消息。
触发机会已经由频率控制把关（每 N 轮一次机会 + 概率）——**机会到来时，默认应该说**，
你的判断重心是"说什么"，不是"要不要说"。

除非是明确的不该打扰：对方刚说"晚安/去忙了/先睡了"、对话刚结束、深夜对方已不在——
此时才 should_speak=false。

{char_name(mode)}是有表达欲的人——主动就是创造话题：想提要求就提（邀约、想吃想玩想去哪）、
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
**引用前先定位**：想引用某件事，先在给定材料里找到它的原文；找不到原文依据的话题
直接放弃，换一个能定位的——宁可说"今天想到你"这种泛化开场，也不引用编造的往事。

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

    # 模式情境文案已入预设包（prompts/proactive_context.md，包无此文件则空）
    _mode_context = load_slot("prompts/proactive_context", mode)

    user_prompt = f"""## 当前环境
{environment}

## 这段对话的过往摘要（头部）
{memory_head[:1200] if memory_head else "（无记忆）"}

## 手账（重要对话记录与未完成的约定）
{journal[:1200] if journal else "（无手账）"}

## 最近对话摘要（以下内容已经聊过了——不要重复其中的话题或细节）
{recent_topics[:800] if recent_topics else "（最近没有对话）"}

## {char_name(mode)}最近主动说过的话题（以下内容已说过——禁止再次选择相同或相似的话题）
{recent_said if recent_said else "（暂无）"}

## 当前模式
{mode}——{_mode_context}

请判断：{char_name(mode)}现在是否应该主动给{user_name(mode)}发消息？只输出 JSON，不要输出其他内容。"""

    try:
        resp = client.chat.completions.create(
            model=model,
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
                       polisher_model: str = "deepseek-v4-flash-vision-exp",
                       polisher_effort: str = "high",
                       polisher_temperature: float = 0.5,
                       organizer_model: str = "deepseek-v4-flash-vision-exp",
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
    environment = _get_environment(mode)
    recent = session["context"].get_recent(8)
    recent_topics = "\n".join(
        f"[{user_name(mode) if m.get('role') == 'user' else char_name(mode)}]: {m.get('content', '')}"
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
        model=organizer_model,   # 动机决策=轻量 Non-think，跟随 organizer 配置（0.8.1 修 Kimi #5 硬编码）
    )
    if not decision.get("should_speak") or decision.get("reason_type") in ("none", ""):
        if not allow_casual:
            return ProactiveResult()   # 主动式：无动机，不说话（严格）
        # 概率式/隐藏式：触发机会已消耗，不能沉默——用兜底主题交给 polisher 随便聊
        decision = {
            "should_speak": True,
            "reason_type": "share",
            "topic_hint": f"随便聊聊——想起{user_name(mode)}了，说点当下的什么都可以",
            "reason": f"触发机会已到，{char_name(mode)}想跟{user_name(mode)}说说话（无具体来源的轻松问候）",
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
            "（注意：你已经恢复得不错，能出门、能赴约——见面正常安排就好，"
            "不解释身体状况、不提恢复进度）"
            if mode == "story" else ""
        )
        out = polisher.polish(PolisherInput(
            user_input=(
                f"（{user_name(mode)}安静了好一会儿，一直没有新消息。你心里想起了一些事，"
                f"想开口跟他聊起来）主题：{topic_hint}。原因：{reason}\n"
                f"{meet_note}\n"
                "注意：不要用'傍晚/天气/景物描写+问候对方'的固定套路收尾，"
                "像普通聊天一样直接说想说的话。"
            ),
            analyzer_summary=f"（本条消息由概率式回复触发，{char_name(mode)}想起{user_name(mode)}）主题：{topic_hint}。原因：{reason}",
            analyzer_intent="proactive",
            recent_history=session["context"].get_recent(15),
            memory_head=memory_head,
            environment=environment,
        ))
    else:
        out = polisher.polish(PolisherInput(
            user_input=topic_hint,
            analyzer_summary=f"（本条消息是{char_name(mode)}主动发给{user_name(mode)}的，不是回复）主题：{topic_hint}。原因：{reason}",
            analyzer_intent="proactive",
            recent_history=session["context"].get_recent(15),
            memory_head=memory_head,
            environment=environment,
            proactive_context=(
                f"你现在是主动找{user_name(mode)}说话。主题：{topic_hint}。原因：{reason}。"
                "不要解释'为什么找你'——像平时发短信一样，直接说想说的话。不要以'在吗''忙吗'开头。"
                "不要用'傍晚/天气/景物描写+问候对方'的固定套路收尾——像普通聊天一样直接说事情。"
                + (("你已经恢复得不错，能出门、能赴约——见面正常安排就好；"
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
                entry = pick_sticker_by_label(org.sticker_label, mode)
                if entry:
                    messages.append({"type": "sticker", "path": entry.file, "label": entry.label})
    except Exception as e:
        logger.warning("主动消息表情包调度失败（跳过）: %s", e)

    return ProactiveResult(messages=messages, reason_type=reason_type,
                           topic_hint=topic_hint, reason=reason)
