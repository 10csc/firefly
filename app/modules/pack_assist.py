# -*- coding: utf-8 -*-
"""逐文件 AI 辅助（阶段 E，2026-09-15）——角色卡编辑页每个文件单元的 ✨ 助手

定位：AI 只提案不直写。流程：用户描述目标 → AI 出 diff 提案（replace/append 锚点）→
用户点「应用」→ 静态校验（含每文件边界）→ 备份 → 原子写 → 清缓存。

每个文件的提示词角色/允许操作/硬边界集中在 ASSIST_RULES（这是执行契约，改规则改这里）。
写入形式统一：backup（{pack}/data/.assist_backups/，每文件留 5 份）→ atomic_write_text。
"""

import logging
from modules import app_config as cfg
from modules.llm_base import extract_json, parse_json, record_usage

logger = logging.getLogger(__name__)

# ── 每文件规则表（执行契约）──────────────────────────────
# key：精确文件名 或 "knowledge/"（知识库通配）或 "memory/"（出厂记忆）
# role：AI 身份；ops：允许的操作；forbid：校验器硬边界（文案里的人类可读规则 + forbid_markers 为代码级禁令）
ASSIST_RULES = {
    "core.md": {
        "role": "人设编辑助手",
        "ops": ("replace", "append"),
        "boundary": "不删 [事实] 条目（除非用户明说）；保留时态标记体系（[事实]/[经历]/[若]/[原句]）；单处修改 ≤2000 字",
    },
    "identity.md": {
        "role": "关系设定助手",
        "ops": ("replace", "append"),
        "boundary": "不动角色名/用户称呼槽位（那是 preset.json 的字段，去详情页顶改）；其余按用户目标改",
    },
    "sms_samples.md": {
        "role": "语料编辑助手",
        "ops": ("replace", "append"),
        "boundary": "只增删改示例条目；不动格式骨架（「」分条结构）",
    },
    "用户设定.md": {
        "role": "设定整理助手",
        "ops": ("replace", "append"),
        "boundary": "新条目必须带 [事实]/[经历]/[若] 前缀；不动别的区段",
    },
    "prompts/polisher.md": {
        "role": "提示词工程助手",
        "ops": ("replace",),
        "boundary": "输出协议段（[MSG] 分条/格式约束）禁止改；只改人设口吻段",
        "must_contain": ["[MSG]"],   # 改后必须仍含这些标记（校验器强制）
    },
    "prompts/analyzer_extra.md": {
        "role": "提示词工程助手",
        "ops": ("replace", "append"),
        "boundary": "只追加核查口径，不动 JSON 输出协议",
    },
    "prompts/organizer_sticker.md": {
        "role": "提示词工程助手",
        "ops": ("replace",),
        "boundary": "保留调度输出格式约束（JSON 字段名不动）",
    },
    "prompts/organizer_narration.md": {
        "role": "提示词工程助手",
        "ops": ("replace",),
        "boundary": "保留旁白输出格式约束（JSON 字段名不动）",
    },
    "prompts/proactive_context.md": {"role": "情境文案助手", "ops": ("replace",), "boundary": "纯文案"},
    "prompts/env_suffix.md": {"role": "情境文案助手", "ops": ("replace",), "boundary": "纯文案"},
    "knowledge/": {
        "role": "知识编辑助手",
        "ops": ("replace", "append"),
        "boundary": "不动 `# ` 一级标题行与 `> 范围` 行；新增条目注明来源",
        "protect_prefixes": ["# ", "> 范围"],
    },
    "memory/": {
        "role": "记忆整理助手",
        "ops": ("replace", "append"),
        "boundary": "保持「## 核心记忆」「## 既定事实」两区结构；条目带 [事实]/[经历] 前缀",
        "must_contain": ["## 核心记忆", "## 既定事实"],
    },
}

_MAX_CHANGE_CHARS = 2000


def rule_for(file: str) -> dict:
    """按文件名取规则：精确匹配 → 目录前缀通配（knowledge/ / memory/）→ None（不可辅助）。"""
    if file in ASSIST_RULES:
        return ASSIST_RULES[file]
    for prefix in ("knowledge/", "memory/"):
        if file.startswith(prefix):
            return ASSIST_RULES[prefix]
    return None


def assistable(file: str) -> bool:
    return rule_for(file) is not None


# ── LLM 调用（只产提案，不写盘）──────────────────────────
def assist_turn(client, mode: str, file: str, message: str,
                history: list, model: str, effort: str) -> dict:
    """一轮辅助对话。返回 {reply, changes?}。changes 为提案（不落盘）。"""
    rule = rule_for(file)
    if rule is None:
        raise ValueError("该文件不支持 AI 辅助")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("描述不能为空")
    from modules.llm_base import resolve_character_file
    fp = resolve_character_file(file, mode)
    # 知识库/出厂记忆不走 resolve_character_file 的槽位（它们在子目录），单独解析
    if file.startswith(("knowledge/", "memory/")):
        from api.pack_paths import resolve_pack_text  # 见下（路径统一口）
        content = resolve_pack_text(mode, file)
    else:
        content = fp.read_text(encoding="utf-8") if fp.exists() else ""
    cname = cfg.char_name(mode)
    uname = cfg.user_name(mode)
    system = f"""你是「{cname}角色卡」的{rule["role"]}。用户是卡的主人（{uname}），正在编辑文件 {file}。

## 边界（违反即无效提案）
{rule["boundary"]}

## 输出协议（一行 JSON，禁止其他文字）
{{"reply":"人话解释你打算怎么改","changes":[{{"op":"replace|append","old":"原文锚点（replace 必填，必须在当前文件里逐字唯一出现）","new":"新内容","reason":"为什么"}}]}}

只允许这些操作：{"/".join(rule["ops"])}。changes 最多 4 处。"""
    hist = "\n".join(f"[{'用户' if m.get('who') == 'user' else 'AI'}] {m.get('text', '')}"
                     for m in (history or [])[-12:])
    user = f"## 文件当前内容\n{content or '（空）'}\n\n## 对话\n{hist}\n\n## 用户刚才说\n{message.strip()[:1000]}\n\n只输出 JSON。"

    # 知识库文件 → 官方内置联网搜索（Responses API web_search，实测 2026-09-15 可用）；
    # 仅本地直连（_CompatClient）+ DeepSeek 官方端点启用；relay/托管链路不支持 tools，优雅降级为不联网。
    use_search = file.startswith("knowledge/")
    if use_search:
        try:
            from modules.llm_base import is_relay_client
            prov = cfg.active_provider()
            is_official = "deepseek.com" in (prov.get("base_url") or "")
            if (not is_relay_client(client)) and type(client).__name__ == "_CompatClient" and is_official:
                from modules.api_client import responses_chat
                raw = responses_chat(cfg.get_api_key(), prov["base_url"], model, system, user,
                                     web_search=True)
                data = parse_json(extract_json(raw) or raw)
                if isinstance(data, dict) and data:
                    reply = str(data.get("reply") or "").strip() or "（无说明）"
                    changes = data.get("changes")
                    return {"reply": reply, "changes": changes if isinstance(changes, list) and changes else None,
                            "searched": True}
                # 搜索链返回无法解析 → 回落普通链路
        except Exception as e:
            logger.warning("联网搜索链失败（回落普通链路）: %s", e)
    extra = {"thinking": {"type": "disabled"}} if effort == "none" else \
            {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=4000, extra_body=extra)
    record_usage("pack_assist", resp)
    raw = resp.choices[0].message.content.strip()
    rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
    if not raw and rc:
        raw = extract_json(rc) or rc
    data = parse_json(raw)
    if not isinstance(data, dict):
        return {"reply": "我没理解到位，能换个说法再说一遍吗？", "changes": None}
    reply = str(data.get("reply") or "").strip() or "（无说明）"
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        changes = None
    return {"reply": reply, "changes": changes}


# ── 提案校验 + 应用（唯一写入口）──────────────────────────
def validate_proposal(content: str, changes: list, file: str) -> tuple[bool, list[str]]:
    """静态校验：操作白名单 / replace 锚点逐字唯一 / 长度上限 / 文件专属硬边界。
    返回 (ok, errors)。在**当前内容**上逐条模拟应用后检查 must_contain。"""
    rule = rule_for(file)
    if rule is None:
        return False, ["该文件不支持 AI 辅助"]
    errors = []
    if not isinstance(changes, list) or not changes:
        return False, ["提案为空"]
    if len(changes) > 4:
        return False, ["一次最多 4 处修改"]
    cur = content
    for i, ch in enumerate(changes):
        if not isinstance(ch, dict):
            errors.append(f"#{i + 1} 不是合法修改项"); continue
        op = ch.get("op")
        if op not in rule["ops"]:
            errors.append(f"#{i + 1} 操作 {op} 不被该文件允许"); continue
        new = str(ch.get("new") or "")
        if len(new) > _MAX_CHANGE_CHARS:
            errors.append(f"#{i + 1} 新内容超过 {_MAX_CHANGE_CHARS} 字")
        if op == "replace":
            old = str(ch.get("old") or "")
            if not old:
                errors.append(f"#{i + 1} replace 缺 old 锚点")
            elif cur.count(old) != 1:
                errors.append(f"#{i + 1} 锚点在文件中不是逐字唯一（出现 {cur.count(old)} 次）")
            else:
                cur = cur.replace(old, new, 1)
        else:
            cur = cur + ("\n" if cur and not cur.endswith("\n") else "") + new + "\n"
    # 结构硬边界：改后必须仍含的标记（输出协议段/记忆区结构）
    for marker in rule.get("must_contain", []):
        if marker not in cur:
            errors.append(f"改后内容缺失必要结构标记 {marker}（该段禁止修改）")
    return (not errors), errors


def apply_proposal(mode: str, file: str, changes: list) -> tuple[bool, str]:
    """应用提案：读当前内容 → 校验 → 备份 → 原子写 → 清缓存。返回 (ok, err)。"""
    from api.pack_paths import read_pack_text_for_write, write_pack_text
    cur = read_pack_text_for_write(mode, file)
    ok, errors = validate_proposal(cur, changes, file)
    if not ok:
        return False, "；".join(errors[:4])
    new = cur
    for ch in changes:
        if ch["op"] == "replace":
            new = new.replace(str(ch["old"]), str(ch["new"]), 1)
        else:
            new = new + ("\n" if new and not new.endswith("\n") else "") + str(ch["new"]) + "\n"
    err = write_pack_text(mode, file, new, backup_tag="assist")
    if err:
        return False, err
    # 清缓存（写哪类清哪类）
    try:
        if file.startswith("knowledge/"):
            from modules.llm_retriever import clear_knowledge_cache
            clear_knowledge_cache(mode)
        else:
            from modules.llm_base import clear_cache
            from modules.polisher import clear_samples_cache
            clear_cache()
            clear_samples_cache()
    except Exception:
        pass
    logger.info("AI 辅助已应用: %s/%s（%d 处）", mode, file, len(changes))
    return True, ""
