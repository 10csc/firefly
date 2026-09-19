# -*- coding: utf-8 -*-
"""AI 建卡向导（搜索 + 分步生成）——用户给一句话，AI 联网搜资料后一步一步搭建角色。

流程：start（搜索+生成 core 草案）→ next（确认当前步→生成下一步）→ finish（落包）。
步骤：core.md → identity.md → sms_samples.md → prompts/polisher.md（人设段）。
AI 只基于搜索到的原文与用户描述构建，不凭记忆编造（搜索为空时明确告知并走纯描述构建）。

会话态：内存 forge_sessions（30 分钟 TTL，服务器版按用户上下文天然隔离）。
模型：Flash + Think High（创作任务），走 llm_base 记账。
"""

import json
import logging
import threading
import time
import uuid

from modules import app_config as cfg
from modules.llm_base import record_usage, record_error, parse_json
from modules.web_search import search_character

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_TTL = 30 * 60

_STEPS = ("core", "identity", "sms_samples", "polisher")
_STEP_LABELS = {"core": "核心设定", "identity": "人际关系与认知边界",
                "sms_samples": "短信风格示例", "polisher": "人设文案"}


def _gc():
    now = time.time()
    with _lock:
        for k in [k for k, v in _sessions.items() if now - v.get("ts", 0) > _TTL]:
            _sessions.pop(k, None)


def _llm(client, system: str, user: str, model: str, max_tokens: int = 4000) -> str:
    """向导内部 LLM 调用（Flash + Think High）。失败抛异常（调用方转人话）。"""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=max_tokens,
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    )
    record_usage("pack_forge", resp)
    raw = (resp.choices[0].message.content or "").strip()
    rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
    return raw or rc


def _material(s: dict) -> str:
    query = s["query"]
    si = s.get("search") or {}
    parts = [f"用户想创建的角色：{query}"]
    if s.get("char_name"):
        parts.append(f"角色名：{s['char_name']}")
    if s.get("user_name"):
        parts.append(f"对方（用户）称呼：{s['user_name']}")
    if si.get("abstract"):
        parts.append(f"\n## 搜索到的资料（来源：{si.get('source') or '网络'}）\n{si['abstract']}")
        for i, t in enumerate((si.get("results") or [])[1:4], 2):
            parts.append(f"资料{i}：{t}")
    else:
        parts.append("\n（联网没有搜到足够资料——仅根据用户的描述构建，不确定的宁可少写不要编）")
    return "\n".join(parts)


_SYSTEM = """你是角色卡搭建助手。根据给定资料，为一个短信聊天式角色扮演应用构建角色设定文件。
要求：
- 只基于给定资料与用户描述写，不确定的事不编造（宁可写得少而准）
- 用中文，口语自然，符合"她在用手机和人聊天"的语境
- 只输出文件正文，不要任何解释、标题外说明或 markdown 围栏"""


def _gen_core(s: dict) -> str:
    model = cfg.config.get("polisher_model") or cfg.MODEL
    user = _material(s) + """

## 任务：写 core.md（角色核心设定）
结构：
# {角色名} · 核心设定

[事实] 她是谁（身份、来历、核心经历）——每条一行，以 [事实] 开头，6-12 条
[事实] 她的性格与说话方式（2-4 条）
[事实] 她现在的生活状态（1-3 条）
写她的"人"，不要写剧情剧透清单。"""
    return _llm(s["client"], _SYSTEM, user, model)


def _gen_identity(s: dict) -> str:
    model = cfg.config.get("polisher_model") or cfg.MODEL
    core = s["drafts"].get("core", "")
    user = _material(s) + f"""

## 已定的核心设定
{core[:1500]}

## 任务：写 identity.md（人际关系与认知边界）
结构：
# {{角色名}} · 人际关系与认知边界

## 她与对方的关系
（她怎么看待/称呼对方（用户称呼：{s.get('user_name') or '对方'}），关系阶段，2-5 条）
## 习惯与喜好
（3-6 条）
## 认知边界
（她知道什么、不知道什么——她不可能知道的事也不要让她知道，2-5 条）"""
    return _llm(s["client"], _SYSTEM, user, model)


def _gen_samples(s: dict) -> str:
    model = cfg.config.get("polisher_model") or cfg.MODEL
    core = s["drafts"].get("core", "")
    user = _material(s) + f"""

## 已定的核心设定
{core[:1500]}

## 任务：写 sms_samples.md（短信风格示例）
结构：
# {{角色名}} · 短信风格速查

（先 2-4 行总结她的短信习惯：长短、语气词、标点习惯、省略号用法）

然后给 8-12 条她会说的话的示例（每条一行，日常聊天场景：问候/关心/分享/约定/玩笑/想念），
要像真人在发短信，不要书面腔。"""
    return _llm(s["client"], _SYSTEM, user, model, max_tokens=3000)


def _gen_polisher(s: dict) -> str:
    model = cfg.config.get("polisher_model") or cfg.MODEL
    core = s["drafts"].get("core", "")
    samples = s["drafts"].get("sms_samples", "")
    user = _material(s) + f"""

## 已定的核心设定
{core[:1200]}

## 已定的短信风格
{samples[:800]}

## 任务：写 prompts/polisher.md（回复器人设文案，模型每次回复前都会读）
这是给大模型看的"怎么扮演她"的指令文档。结构：
开头一行：你是{{角色名}}。你正在用手机给{{对方称呼}}发消息。基于设定和当前对话，直接写出你要发的短信。

## 角色核心
{{core}}

## 人际关系与认知边界
{{identity}}

## 用户补充的设定（与核心设定同等权威）
{{user_setting}}

## 手账（重要对话记录与未完成的约定）
{{journal}}

## 她的底色（性格与表达原则，4-8 条，根据资料写）

## 她的短信是什么样子（分条/省略号/语气词，根据风格示例写）

## 她不做什么（根据性格写 4-8 条禁忌：不会说的话、不会做的事）

## 照短信样本感受节奏

{{sms_samples}}

注意：{{core}} 这类槽位标记必须原样保留（系统会注入内容），不要替换成实际文字。"""
    return _llm(s["client"], _SYSTEM, user, model, max_tokens=4000)


_GENERATORS = {"core": _gen_core, "identity": _gen_identity,
               "sms_samples": _gen_samples, "polisher": _gen_polisher}


def forge_start(client, body: dict) -> dict:
    """POST /pack-forge/start：搜索 + 生成第一步（core）草案。"""
    _gc()
    query = str(body.get("query") or "").strip()[:200]
    if not query:
        return {"ok": False, "error": "请描述想创建的角色（名字/作品/一句话）"}
    char_name = str(body.get("char_name") or "").strip()[:20]
    user_name = str(body.get("user_name") or "").strip()[:20]
    presentation = str(body.get("presentation") or "sticker").strip()
    if presentation not in ("sticker", "narration", "none"):
        presentation = "sticker"
    search = search_character(query)
    sid = uuid.uuid4().hex[:12]
    with _lock:
        _sessions[sid] = {
            "ts": time.time(), "client": client, "query": query,
            "char_name": char_name, "user_name": user_name,
            "presentation": presentation, "search": search, "drafts": {},
        }
    s = _sessions[sid]
    try:
        draft = _gen_core(s)
    except Exception as e:
        record_error("pack_forge", cfg.config.get("polisher_model") or cfg.MODEL, str(e))
        with _lock:
            _sessions.pop(sid, None)
        return {"ok": False, "error": f"生成失败（{type(e).__name__}），请稍后再试"}
    with _lock:
        s["drafts"]["core"] = draft
        s["ts"] = time.time()
    return {"ok": True, "session": sid, "step": "core", "step_label": _STEP_LABELS["core"],
            "draft": draft, "searched": bool(search.get("abstract")),
            "search_source": search.get("source") or ""}


def forge_next(body: dict) -> dict:
    """POST /pack-forge/next：确认当前步（可带用户编辑后的草案）→ 生成下一步。"""
    _gc()
    sid = str(body.get("session") or "").strip()
    with _lock:
        s = _sessions.get(sid)
    if not s:
        return {"ok": False, "error": "向导会话已过期，请重新开始"}
    cur = str(body.get("step") or "").strip()
    if cur not in _STEPS:
        return {"ok": False, "error": "非法步骤"}
    edited = str(body.get("draft") or "").strip()
    with _lock:
        if edited:
            s["drafts"][cur] = edited
        s["ts"] = time.time()
    idx = _STEPS.index(cur)
    if idx >= len(_STEPS) - 1:
        return {"ok": False, "error": "已是最后一步，请点「完成创建」"}
    nxt = _STEPS[idx + 1]
    try:
        draft = _GENERATORS[nxt](s)
    except Exception as e:
        record_error("pack_forge", cfg.config.get("polisher_model") or cfg.MODEL, str(e))
        return {"ok": False, "error": f"生成失败（{type(e).__name__}），请稍后再试"}
    with _lock:
        s["drafts"][nxt] = draft
        s["ts"] = time.time()
    return {"ok": True, "session": sid, "step": nxt, "step_label": _STEP_LABELS[nxt],
            "draft": draft, "is_last": nxt == _STEPS[-1]}


def forge_finish(body: dict) -> dict:
    """POST /pack-forge/finish：确认全部草案 → 创建包（复用 routes_pack.create_pack 逻辑）。"""
    sid = str(body.get("session") or "").strip()
    with _lock:
        s = _sessions.pop(sid, None)
    if not s:
        return {"ok": False, "error": "向导会话已过期"}
    files = body.get("files") or {}
    name = str(body.get("name") or s["query"]).strip()[:30] or s["query"][:30]
    char_name = str(body.get("char_name") or s.get("char_name") or name).strip()[:20]
    user_name = str(body.get("user_name") or s.get("user_name") or "你").strip()[:20]
    presentation = s.get("presentation") or "sticker"
    import uuid as _uuid
    pid = "custom_" + _uuid.uuid4().hex[:8]
    cdir = cfg.USER_DIR / pid / "character"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "preset.json").write_text(json.dumps({
        "id": pid, "name": name, "char_name": char_name, "user_name": user_name,
        "presentation": presentation, "desc": "AI 协助搭建", "tagline": "",
        "schema": cfg.PRESET_SCHEMA,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    from modules.polisher import _EMERGENCY_PERSONA
    (cdir / "prompts").mkdir(exist_ok=True)
    core = str(files.get("core") or s["drafts"].get("core") or "").strip()
    identity = str(files.get("identity") or s["drafts"].get("identity") or "").strip()
    samples = str(files.get("sms_samples") or s["drafts"].get("sms_samples") or "").strip()
    polisher = str(files.get("polisher") or s["drafts"].get("polisher") or "").strip()
    (cdir / "core.md").write_text(core or f"# {char_name} · 核心设定\n", encoding="utf-8")
    (cdir / "identity.md").write_text(identity or f"# {char_name} · 人际关系与认知边界\n", encoding="utf-8")
    (cdir / "sms_samples.md").write_text(samples or f"# {char_name} · 短信风格示例\n", encoding="utf-8")
    (cdir / "prompts" / "polisher.md").write_text(polisher or _EMERGENCY_PERSONA, encoding="utf-8")
    # 阶段 3.2：登记进 packs.json 再重扫（清单是包存在性的权威）
    cfg.pack_registry().register(pid, source="custom")
    cfg.reload_presets()
    logger.info("AI 建卡完成: %s（%s，搜索=%s）", pid, name, bool((s.get("search") or {}).get("abstract")))
    return {"ok": True, "id": pid, "name": name}
