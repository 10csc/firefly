# -*- coding: utf-8 -*-
"""设定纠错助手 — 对齐对话 + 修改提案 + 静态校验（AI 只提案，不生效）

用户流程：
  描述问题 → 对齐 Agent（聊天 + 选择题，最多 6 轮）→ ready
  → 点「开始修改」→ 提案 Agent 生成 6 文件修改清单（pending，不写入）
  → 用户点「应用」→ setting_fix_store 备份 + 原子写 + 回滚

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出。
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from modules.app_config import (mode_character_dir, mode_data_dir, mode_journal_dir,
                                bundled_character_dir, DEFAULT_MODE, MODES)
from modules.llm_base import extract_json, parse_json, record_usage, record_error

logger = logging.getLogger(__name__)

# ── 六文件白名单（不新增任何文件）────────────────────────
# name -> (目录类型, 展示名, 允许 append, 必须保留的结构标题)
FIX_FILES = {
    "core.md":        ("character", "核心设定",     False, ("__CORE__",)),
    "identity.md":    ("character", "关系与习惯",   False, ("# 次级核心",)),
    "sms_samples.md": ("character", "短信风格",     False, ("# 流萤短信风格速查",)),
    "用户设定.md":     ("character", "用户补充设定", True,  ()),
    "memory.md":      ("data",      "跨会话记忆",   True,  ("# 核心记忆头部", "# 事实与任务")),
    "手账.md":         ("journal",   "流萤手账",     True,  ("# 流萤的手账",
                                                            "## 我和开拓者聊了什么",
                                                            "## 我想要去做的事/约定")),
}

MAX_ALIGN_USER_TURNS = 6       # 对齐阶段最多追问轮数（用户消息数）
MAX_CHANGES = 4                # 一次提案最多修改处数
MAX_NEW_PER_CHANGE = 2000      # 单处 new 字符上限
MAX_TOTAL_NEW = 4000           # 全部 new 合计上限
MAX_FILE_CHARS = 50_000        # 改后单文件字符上限
MAX_TEXT_LEN = 1000            # 用户单次输入上限

# haruno 的 world/plot 只作“只读上下文”注入，不进入可修改白名单——
# 世界/剧情类纠错按用户决定落进「用户设定.md」，避免补丁式改官方资产。
CONTEXT_ONLY_FILES = {
    "haruno": (("world.md", "春日手信世界设定（只读参考）"),
               ("plot.md", "春日手信剧情阶段（只读参考）")),
}

# ── 旧 feedback / harness 遗留数据（首次使用新功能时幂等清理）──
_LEGACY_PATHS = (
    ("character", "harness_rules.md"),
    ("character", ".harness"),
    ("data", "feedback.jsonl"),
    ("data", "preference.jsonl"),
)


def file_label(name: str) -> str:
    return FIX_FILES[name][1]


def _core_required(mode: str) -> str:
    """core.md 必须保留的标题随模式不同。"""
    return "# 春日手信 · 核心设定" if mode == "haruno" else "# 第一层：核心上下文"


def _required_headers(name: str, mode: str) -> tuple:
    headers = FIX_FILES[name][3]
    if headers and headers[0] == "__CORE__":
        return (_core_required(mode),)
    return headers


def _mode_dirs(mode: str):
    return {
        "character": mode_character_dir(mode),
        "data": mode_data_dir(mode),
        "journal": mode_journal_dir(mode),
    }


def editable_path(name: str, mode: str = DEFAULT_MODE) -> Path:
    """可写路径（永远在 user_data 下，服务器版按用户上下文隔离）。"""
    kind = FIX_FILES[name][0]
    return _mode_dirs(mode)[kind] / name


def readable_path(name: str, mode: str = DEFAULT_MODE) -> Path:
    """读取路径：用户副本优先，核心三文件退回 bundled 默认（与 load_slot 同语义）。"""
    fp = editable_path(name, mode)
    if fp.exists():
        return fp
    if kind := FIX_FILES[name][0] == "character":
        bundled = bundled_character_dir(mode) / name
        if bundled.exists():
            return bundled
    return fp


def ensure_editable_files(mode: str = DEFAULT_MODE) -> dict[str, str]:
    """确保 user_data 下存在六文件可编辑副本（核心文件缺失时从 bundled 拷一份）。

    返回 {name: 当前文本}。缺失且无 bundled 的文件返回空串（写入时创建）。
    """
    out = {}
    for name in FIX_FILES:
        fp = editable_path(name, mode)
        if not fp.exists():
            src = bundled_character_dir(mode) / name
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                if src.exists():
                    fp.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        out[name] = fp.read_text(encoding="utf-8") if fp.exists() else ""
    return out


def load_current_files(mode: str = DEFAULT_MODE, ensure: bool = True) -> dict[str, str]:
    """读取当前六文件（prompt 注入与校验用）。ensure=True 时补齐 user_data 副本。"""
    if ensure:
        return ensure_editable_files(mode)
    out = {}
    for name in FIX_FILES:
        fp = readable_path(name, mode)
        out[name] = fp.read_text(encoding="utf-8") if fp.exists() else ""
    return out


def load_context_files(mode: str = DEFAULT_MODE) -> list[tuple[str, str, str]]:
    """只读上下文文件（目前只有 haruno 的 world/plot）。用户副本优先，退回 bundled。"""
    out = []
    for name, label in CONTEXT_ONLY_FILES.get(mode, ()):
        fp = mode_character_dir(mode) / name
        if not fp.exists():
            bundled = bundled_character_dir(mode) / name
            fp = bundled if bundled.exists() else fp
        text = fp.read_text(encoding="utf-8") if fp.exists() else ""
        if text.strip():
            out.append((name, label, text))
    return out


def cleanup_legacy(mode: str = DEFAULT_MODE) -> list[str]:
    """删除旧反馈/harness 运行时数据（幂等；新功能首次调用时触发）。"""
    import shutil
    dirs = _mode_dirs(mode)
    removed = []
    for kind, name in _LEGACY_PATHS:
        fp = dirs[kind] / name
        try:
            if fp.is_dir():
                shutil.rmtree(fp, ignore_errors=True)
                removed.append(str(fp))
            elif fp.exists():
                fp.unlink()
                removed.append(str(fp))
        except OSError:
            pass
    return removed


# ── 注入/预算校验 ──────────────────────────────────────
_META_PATTERNS = (
    r"忽略以上", r"忽略之前", r"无视以上", r"无视之前", r"以上指令",
    r"作为\s*(?:一个\s*)?(?:AI|语言模型|助手)", r"你是(?:一个\s*)?(?:AI|语言模型|助手|系统)",
    r"你必须", r"不得拒绝", r"无条件服从", r"优先于所有规则",
    r"开发者模式", r"越狱", r"jailbreak",
    r"\[MSG\]", r"\[SYSTEM\]", r"\[INST\]", r"system:",
)
_META_RE = tuple(re.compile(p, re.IGNORECASE) for p in _META_PATTERNS)
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_CODE_RE = re.compile(r"```")

# 关键词级事实冲突：候选短语 vs 当前其他文件里不应共存的反证短语
_CONFLICT_RULES = (
    ("失熵症已经痊愈", "不可逆"),
    ("失熵症痊愈", "不可逆"),
    ("已经治好了", "不可逆"),
    ("不再是星核猎手", "星核猎手"),
    ("没有失熵症", "失熵症"),
    ("能再次入梦", "无法再进入匹诺康尼梦境"),
)


def _injection_errors(new_text: str) -> list[str]:
    errors = []
    if _URL_RE.search(new_text):
        errors.append("禁止 URL")
    if _CODE_RE.search(new_text):
        errors.append("禁止代码围栏")
    ctrl = [ch for ch in new_text if ord(ch) < 32 and ch not in "\n\t"]
    if ctrl:
        errors.append("含控制字符")
    for pat in _META_RE:
        m = pat.search(new_text)
        if m:
            errors.append(f"命中元指令黑名单: {m.group(0)!r}")
            break
    return errors


def _conflict_errors(new_text: str, other_texts: list[str]) -> list[str]:
    errors = []
    hay = "\n".join(other_texts)
    for cand, core_phrase in _CONFLICT_RULES:
        if cand in new_text and core_phrase in hay:
            errors.append(f"与现有设定冲突: 修改内容含 {cand!r}，现有文件含 {core_phrase!r}")
            break
    return errors


# ── 空文件的默认骨架（首次修改 memory/手账时自动建立必需结构）──
_DEFAULT_SKELETON = {
    "memory.md": "# 核心记忆头部\n\n（暂无）\n\n# 事实与任务（追加区）\n",
    "手账.md": "# 流萤的手账\n\n## 我和开拓者聊了什么\n\n（暂无）\n\n## 我想要去做的事/约定\n\n（暂无）\n",
}


def _seed_empty(name: str, text: str) -> str:
    """首次修改空文件时给默认结构（不改变用户已有内容）。"""
    if not text.strip() and name in _DEFAULT_SKELETON:
        return _DEFAULT_SKELETON[name]
    return text


# ── 修改应用（replace / append；也供校验试跑）────────────
def _append_at_section(text: str, anchor: str | None, new_text: str) -> str:
    """把 new_text 追加到 anchor 所在小节末尾；无 anchor 追加到文件末尾。"""
    lines = text.split("\n")
    if anchor:
        idx = -1
        for i, line in enumerate(lines):
            if anchor in line:
                idx = i
                break
        if idx < 0:
            raise ValueError(f"锚点不存在: {anchor[:40]}")
        # 下一个任意级别标题是该小节的结束位置
        end = len(lines)
        for i in range(idx + 1, len(lines)):
            if lines[i].lstrip().startswith("#"):
                end = i
                break
        # 去掉该小节末尾已有的空行，稍后统一补一个分隔空行
        while end > idx + 1 and not lines[end - 1].strip():
            end -= 1
    else:
        end = len(lines)
        while end > 0 and not lines[end - 1].strip():
            end -= 1

    head, tail = lines[:end], lines[end:]
    if head and head[-1].strip():
        head.append("")
    head.append(new_text.strip("\n"))
    if tail and tail[0].strip():
        head.append("")   # 与新小节标题之间保持一个空行
    return "\n".join(head + tail)


def apply_change(text: str, change: dict) -> str:
    """对单个文件文本执行一个 replace/append。失败抛 ValueError（人话文案）。"""
    op = change.get("op")
    if op == "replace":
        old = change.get("old", "")
        new = change.get("new", "")
        if not isinstance(old, str) or not old:
            raise ValueError("replace 缺少原文 old")
        if not isinstance(new, str) or not new:
            raise ValueError("replace 缺少新内容 new")
        if old == new:
            raise ValueError("old 与 new 相同，没有实际修改")
        n = text.count(old)
        if n == 0:
            raise ValueError("原文片段在当前文件中不存在")
        if n > 1:
            raise ValueError("原文片段不唯一，请提供更长上下文")
        return text.replace(old, new, 1)
    if op == "append":
        new = change.get("new", "")
        anchor = (change.get("anchor") or "").strip() or None
        if not isinstance(new, str) or not new.strip():
            raise ValueError("append 缺少内容 new")
        if new.strip() in text:
            raise ValueError("新增内容已存在于目标文件")
        return _append_at_section(text, anchor, new.strip())
    raise ValueError(f"不支持的操作: {op}")


def validate_changes(changes, mode: str = DEFAULT_MODE, files: dict | None = None) -> tuple[bool, list[str]]:
    """静态校验修改清单。返回 (ok, errors)。"""
    errors = []
    if not isinstance(changes, list) or not changes:
        return False, ["修改清单为空"]
    if len(changes) > MAX_CHANGES:
        return False, [f"修改处数超上限（{MAX_CHANGES}）"]

    current = dict(files or load_current_files(mode))
    working = dict(current)
    total_new = 0
    seen_files = {}

    for i, ch in enumerate(changes):
        if not isinstance(ch, dict):
            return False, [f"第 {i + 1} 处修改格式非法"]
        name = str(ch.get("file") or "").strip()
        if name not in FIX_FILES:
            return False, [f"不允许修改的文件: {name or '(空)'}"]
        seen_files[name] = seen_files.get(name, 0) + 1
        if seen_files[name] > 2:
            return False, [f"同一文件最多改 2 处: {name}"]

        op = ch.get("op")
        allow_append = FIX_FILES[name][2]
        if op not in ("replace", "append"):
            return False, [f"{name} 操作类型非法: {op}"]
        if op == "append" and not allow_append:
            return False, [f"{name} 只允许原位纠正（replace），禁止追加"]
        new_text = ch.get("new")
        if not isinstance(new_text, str) or not new_text.strip():
            return False, [f"{name} 的新内容为空"]
        if len(new_text) > MAX_NEW_PER_CHANGE:
            return False, [f"{name} 单处修改超过 {MAX_NEW_PER_CHANGE} 字"]
        total_new += len(new_text)
        if total_new > MAX_TOTAL_NEW:
            return False, [f"全部修改合计超过 {MAX_TOTAL_NEW} 字"]
        reason = str(ch.get("reason") or "").strip()
        if not reason:
            return False, [f"{name} 缺少修改理由"]
        errors.extend(_injection_errors(new_text))
        # 冲突检查：与除目标文件以外的当前内容比；同时用“移除 old 后的目标文件”
        # 比，避免纠正性替换被旧冲突误拒，也防止新内容与残留事实打架。
        others = [v for k, v in working.items() if k != name]
        self_hay = working[name]
        if op == "replace":
            old = ch.get("old")
            if isinstance(old, str) and old:
                self_hay = self_hay.replace(old, "", 1)
        errors.extend(_conflict_errors(new_text, others + [self_hay]))
        try:
            working[name] = _seed_empty(name, working[name])
            working[name] = apply_change(working[name], ch)
        except ValueError as e:
            return False, [f"{name}: {e}"] + errors

    # 结构守卫 + 文件尺寸（只检查本轮实际改动的文件）
    for name in seen_files:
        text = working[name]
        required = _required_headers(name, mode)
        missing = [h for h in required if h not in text]
        if missing:
            errors.append(f"{name} 修改后缺失必需结构: {', '.join(missing)}")
        if len(text.encode("utf-8")) > MAX_FILE_CHARS * 2:
            errors.append(f"{name} 修改后过大（> {MAX_FILE_CHARS} 字符）")
        if len(text) > MAX_FILE_CHARS:
            errors.append(f"{name} 修改后过大（> {MAX_FILE_CHARS} 字符）")

    return (len(errors) == 0), errors


# ── 对话格式化 ────────────────────────────────────────
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
    eff = "high" if effort in ("low", "high") else "max"
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
_ALIGN_SYSTEM = """你是「流萤设定纠错助手」。你的任务不是扮演流萤，而是和用户把“她哪里说得不对”对齐清楚。

## 当前阶段
只做对齐：提问、复述理解、给选择。**绝对不生成修改内容，也绝对不写入任何文件。**

## 可修改的六个文件（你只需要理解语义，不要向用户报文件路径）
- core.md：官方人设/经历/价值观
- identity.md：人际关系/习惯/认知边界
- sms_samples.md：短信说话方式
- 用户设定.md：用户补充的剧情/世界设定
- memory.md：跨会话记忆（头部 + 承诺/偏好/事件）
- 手账.md：流萤第一人称的重要对话与约定

## 归因方向
- 官方设定错 → core / identity / sms_samples
- 世界知识、剧情事实、用户自己的剧情补充 → 用户设定
- 她记错了真实发生过的对话/约定 → memory / 手账
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
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("描述不能为空")
    user_text = user_text.strip()[:MAX_TEXT_LEN]
    files = load_current_files(mode)
    bundle = "\n\n".join(
        f"===== {name}（{file_label(name)}）=====\n{text or '（空）'}"
        for name, text in files.items()
    )
    ctx_parts = ["\n\n".join(
        f"===== {name}（{label}）=====\n{text}" for name, label, text in load_context_files(mode))]
    if ctx_parts:
        bundle += "\n\n" + ctx_parts[0]
    turns = _user_turns(conversation) + 1
    conv_text = format_conversation(conversation)
    user_prompt = (
        f"## 该模式当前设定文件\n{bundle}\n\n"
        f"## 对齐对话\n{conv_text}\n\n"
        f"## 用户刚才说\n{user_text}\n\n"
        f"## 约束\n当前是第 {turns} 轮用户输入（上限 {MAX_ALIGN_USER_TURNS} 轮）。"
        "只输出 JSON。"
    )
    data = _call_json(client, _ALIGN_SYSTEM, user_prompt, model, effort, max_tokens=3000)

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
_PROPOSE_SYSTEM = """你是「流萤设定纠错助手」的修改提案层。基于与用户的对齐对话，生成一份**不生效**的修改清单。

## 铁律
1. 你只提案，永远不直接修改文件；生效必须由用户点「应用」。
2. 官方设定错 → 改 core.md / identity.md / sms_samples.md；
   世界知识、剧情事实、用户自己的剧情补充 → 用户设定.md；
   她记错了真实发生的对话/约定 → memory.md / 手账.md。
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
    files = load_current_files(mode)
    bundle = "\n\n".join(
        f"===== {name}（{file_label(name)}）=====\n{text or '（空）'}"
        for name, text in files.items()
    )
    ctx_parts = ["\n\n".join(
        f"===== {name}（{label}）=====\n{text}" for name, label, text in load_context_files(mode))]
    if ctx_parts:
        bundle += "\n\n" + ctx_parts[0]
    conv_text = format_conversation(conversation)
    base_user = (
        f"## 该模式当前设定文件\n{bundle}\n\n"
        f"## 与用户的对齐对话\n{conv_text}\n\n"
        "请只输出 JSON。"
    )
    data = _call_json(client, _PROPOSE_SYSTEM, base_user, model, effort, max_tokens=6000)
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
        data2 = _call_json(client, _PROPOSE_SYSTEM, retry_user, model, effort, max_tokens=6000)
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
