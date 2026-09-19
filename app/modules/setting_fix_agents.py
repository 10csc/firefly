# -*- coding: utf-8 -*-
"""设定纠错 Agent 层（阶段 2.8 自 setting_fix 拆出，纯移动无行为变化）

两个 LLM Agent：对齐 Agent（聊天+选择题，绝不写文件）与提案 Agent（生成不生效的修改清单）。
静态校验/文件落点在 setting_fix.py；存储在 setting_fix_store.py。
对 setting_fix 的依赖（load_current_files/file_label/validate_changes/常量）全部
**函数内 lazy import**——setting_fix 顶层 re-export 本模块，顶层互引会成环。
"""

import logging

from modules.app_config import char_name
from modules.llm_base import extract_json, parse_json, record_usage, record_error

logger = logging.getLogger(__name__)


def format_conversation(conversation: list[dict]) -> str:
    if not conversation:
        return "（尚无对话）"
    lines = []
    for m in conversation[-40:]:
        who = "用户" if m.get("who") == "user" else "设定助手"
        lines.append(f"[{who}] {m.get('text', '')}")
    return "\n".join(lines)


def _user_turns(conversation: list[dict]) -> int:
    return sum(1 for m in conversation if m.get("who") == "user")


# ── LLM 调用 ──────────────────────────────────────────
def _thinking_extra(effort: str) -> dict:
    if effort == "none":
        return {"thinking": {"type": "disabled"}}
    # 2026-08-13 起 low/high/max 三档平滑直通（旧 low→high 映射已过时，删除）
    eff = effort if effort in ("low", "high", "max") else "high"
    return {"thinking": {"type": "enabled"}, "reasoning_effort": eff}


def _call_json(client, system: str, user: str, model: str, effort: str,
               max_tokens: int = 6000) -> dict | None:
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            extra_body=_thinking_extra(effort),
        )
        record_usage("setting_fix", resp)
        raw = resp.choices[0].message.content.strip()
        rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
        if not raw and rc:
            raw = extract_json(rc) or rc
        return parse_json(raw)
    except Exception as e:
        record_error("setting_fix", model, str(e))
        raise


# ── 阶段一：对齐 Agent ────────────────────────────────
_ALIGN_SYSTEM = """你是「__CHAR__设定纠错助手」。你的任务不是扮演__CHAR__，而是和用户把“角色哪里说得不对”对齐清楚。

## 当前阶段
只做对齐：提问、复述理解、给选择。**绝对不生成修改内容，也绝对不写入任何文件。**

## 可修改的六个文件（你只需要理解语义，不要向用户报文件路径）
- core.md：官方人设/经历/价值观
- identity.md：人际关系/习惯/认知边界
- sms_samples.md：短信说话方式
- 用户设定.md：用户补充的剧情/世界设定
- memory.md：这段对话的过往摘要（头部 + 承诺/偏好/事件）
- 手账.md：__CHAR__第一人称的重要对话与约定

## 归因方向
- 官方设定错 → core / identity / sms_samples
- 世界知识、剧情事实、用户自己的剧情补充 → 用户设定
- 角色记错了真实发生过的对话/约定 → memory / 手账
- 拿不准就问，不要猜。

## 对话规则
1. 一次只问一个问题；需要选择时给 2-4 个选项，同时允许用户自由输入。
2. 不重复已问过的问题。
3. 信息足够、或用户说“没问题/对/就这样/可以/开始”时，立即 stage=ready，
   text 里用 2-4 句复述“我的理解”，然后告诉用户点「开始修改」。
4. 对话轮数快满时同样给“我的理解”并 stage=ready，不要无限追问。
5. 用户描述的问题不需要改设定时，直接说明原因并 stage=ready（开始修改会生成 no_fix）。

## 输出格式（一行 JSON，禁止其他文字）
{"stage":"aligning 或 ready","text":"回复正文（人话，不出现文件路径和内部规则）","options":["选项1","选项2"]}"""


def run_alignment(client, mode: str, conversation: list[dict], user_text: str,
                  model: str, effort: str) -> dict:
    from modules.setting_fix import (MAX_ALIGN_USER_TURNS, MAX_TEXT_LEN,
                                     file_label, load_current_files)
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("描述不能为空")
    user_text = user_text.strip()[:MAX_TEXT_LEN]
    files = load_current_files(mode)
    bundle = "\n\n".join(
        f"===== {name}（{file_label(name, mode)}）=====\n{text or '（空）'}"
        for name, text in files.items()
    )
    turns = _user_turns(conversation) + 1
    conv_text = format_conversation(conversation)
    user_prompt = (
        f"## 该模式当前设定文件\n{bundle}\n\n"
        f"## 对齐对话\n{conv_text}\n\n"
        f"## 用户刚才说\n{user_text}\n\n"
        f"## 约束\n当前是第 {turns} 轮用户输入（上限 {MAX_ALIGN_USER_TURNS} 轮）。"
        "只输出 JSON。"
    )
    data = _call_json(client, _ALIGN_SYSTEM.replace("__CHAR__", char_name(mode)),
                      user_prompt, model, effort, max_tokens=3000)

    if not isinstance(data, dict):
        data = {}
    stage = data.get("stage")
    text = str(data.get("text") or "").strip()
    options = data.get("options")
    if stage not in ("ready", "aligning"):
        stage = "aligning"
    if not text:
        text = "我大致理解了。你可以在下方补充，或点「开始修改」生成方案。"
    if turns >= MAX_ALIGN_USER_TURNS:
        stage = "ready"
    # 明确的确认语直接 ready（用户说没问题/对/就这样 等）
    low = user_text.strip().lower()
    if any(k in low for k in ("没问题", "可以了", "就这样", "开始修改", "对，", "对的", "确认")):
        stage = "ready"
    clean_options = []
    if isinstance(options, list):
        for o in options[:4]:
            s = str(o).strip()
            if s and len(s) <= 60:
                clean_options.append(s)
    if stage == "ready" and not clean_options:
        text = (text + "\n\n我的理解如上。点下方「开始修改」生成修改清单，"
                       "或者继续补充细节。").strip()
    return {"stage": stage, "text": text, "options": clean_options}


# ── 阶段二：提案 Agent ────────────────────────────────
_PROPOSE_SYSTEM = """你是「__CHAR__设定纠错助手」的修改提案层。基于与用户的对齐对话，生成一份**不生效**的修改清单。

## 铁律
1. 你只提案，永远不直接修改文件；生效必须由用户点「应用」。
2. 官方设定错 → 改 core.md / identity.md / sms_samples.md；
   世界知识、剧情事实、用户自己的剧情补充 → 用户设定.md；
   角色记错了真实发生的对话/约定 → memory.md / 手账.md。
3. core / identity / sms_samples 只允许 op="replace"（原位纠正，禁止追加）。
4. 用户设定 / memory / 手账 允许 replace 或 append。
5. old 必须从下面文件内容里**逐字复制**，且保证唯一；不唯一就多带上下文。
6. 一次最多 4 处修改，同一文件最多 2 处。
7. 拿不准或不需要改：kind="no_fix"，diagnosis 说明原因。
8. 不要为“显得有产出”改风格或个人偏好。

## 输出格式（一行 JSON，禁止其他文字）
{"kind":"proposal","diagnosis":"人话总结","changes":[{"file":"core.md","op":"replace","old":"原文","new":"新文","reason":"为什么改"}]}
或 {"kind":"no_fix","diagnosis":"说明为什么不用改","changes":[]}"""


def run_proposal(client, mode: str, conversation: list[dict],
                 model: str, effort: str) -> dict:
    from modules.setting_fix import file_label, load_current_files, validate_changes
    files = load_current_files(mode)
    bundle = "\n\n".join(
        f"===== {name}（{file_label(name, mode)}）=====\n{text or '（空）'}"
        for name, text in files.items()
    )
    conv_text = format_conversation(conversation)
    base_user = (
        f"## 该模式当前设定文件\n{bundle}\n\n"
        f"## 与用户的对齐对话\n{conv_text}\n\n"
        "请只输出 JSON。"
    )
    propose_system = _PROPOSE_SYSTEM.replace("__CHAR__", char_name(mode))
    data = _call_json(client, propose_system, base_user, model, effort, max_tokens=6000)
    if not isinstance(data, dict):
        raise ValueError("提案模型返回格式非法")

    kind = data.get("kind")
    diagnosis = str(data.get("diagnosis") or "").strip()
    changes = data.get("changes") if isinstance(data.get("changes"), list) else []

    if kind == "no_fix":
        return {"ok": True, "kind": "no_fix", "diagnosis": diagnosis or "这次不需要修改设定。",
                "changes": []}
    if kind != "proposal":
        raise ValueError("提案模型未给出可识别的结论")

    if not changes:
        return {"ok": True, "kind": "no_fix",
                "diagnosis": diagnosis or "对齐后判断：这次不需要修改设定。", "changes": []}

    ok, errors = validate_changes(changes, mode, files)
    if not ok:
        # 把校验错误回喂模型重试一次（只允许一次）
        retry_user = (base_user + "\n\n## 上次方案被校验器拒绝，请修正后重新输出 JSON\n"
                      + "；".join(errors[:6]))
        data2 = _call_json(client, propose_system, retry_user, model, effort, max_tokens=6000)
        if isinstance(data2, dict) and data2.get("kind") == "proposal":
            changes2 = data2.get("changes") if isinstance(data2.get("changes"), list) else []
            ok, errors = validate_changes(changes2, mode, files)
            if ok:
                changes = changes2
                diagnosis = str(data2.get("diagnosis") or diagnosis).strip() or diagnosis
        if not ok:
            return {"ok": False, "error": "生成的修改方案未通过校验，请补充描述后重试："
                                          + "；".join(errors[:4])}
    return {"ok": True, "kind": "proposal",
            "diagnosis": diagnosis or "已按你的描述生成修改清单。",
            "changes": changes}
