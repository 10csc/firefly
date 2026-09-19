# -*- coding: utf-8 -*-
"""设定纠错助手 — 对齐对话 + 修改提案 + 静态校验（AI 只提案，不生效）

用户流程：
  描述问题 → 对齐 Agent（聊天 + 选择题，最多 6 轮）→ ready
  → 点「开始修改」→ 提案 Agent 生成 6 文件修改清单（pending，不写入）
  → 用户点「应用」→ setting_fix_store 备份 + 原子写 + 回滚

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出。
"""
import logging
import re
from pathlib import Path

from modules.app_config import (mode_character_dir, mode_data_dir, mode_journal_dir,
                                bundled_character_dir, DEFAULT_MODE,
                                char_name, user_name)

logger = logging.getLogger(__name__)

# ── 六文件白名单（不新增任何文件）────────────────────────
# name -> (目录类型, 展示名, 允许 append, 必须保留的结构标题)
# 锚点标记：__FIRST_HEADER__ = bundled 包文件首个 # 标题行（随包自动正确）；
#           __JOURNAL__ = 按包角色名/用户称呼拼手账三标题。
FIX_FILES = {
    "core.md":        ("character", "核心设定",     False, ("__FIRST_HEADER__",)),
    "identity.md":    ("character", "关系与习惯",   False, ("__FIRST_HEADER__",)),
    "sms_samples.md": ("character", "短信风格",     False, ("__FIRST_HEADER__",)),
    "用户设定.md":     ("character", "用户补充设定", True,  ()),
    "memory.md":      ("data",      "过往摘要",     True,  ("# 核心记忆头部", "# 事实与任务")),
    "手账.md":         ("journal",   "手账",        True,  ("__JOURNAL__",)),
}

MAX_ALIGN_USER_TURNS = 6       # 对齐阶段最多追问轮数（用户消息数）
MAX_CHANGES = 4                # 一次提案最多修改处数
MAX_NEW_PER_CHANGE = 2000      # 单处 new 字符上限
MAX_TOTAL_NEW = 4000           # 全部 new 合计上限
MAX_FILE_CHARS = 50_000        # 改后单文件字符上限
MAX_TEXT_LEN = 1000            # 用户单次输入上限

# 世界/剧情类纠错按用户决定落进「用户设定.md」，避免补丁式改官方资产。
# （2026-09-04：haruno world/plot.md 从未产物化，只读上下文机制随引用一并删除；
#  若日后资料卡建卡，恢复见 git 历史）

# ── 旧 feedback / harness 遗留数据（首次使用新功能时幂等清理）──
_LEGACY_PATHS = (
    ("character", "harness_rules.md"),
    ("character", ".harness"),
    ("data", "feedback.jsonl"),
    ("data", "preference.jsonl"),
)


def file_label(name: str, mode: str = DEFAULT_MODE) -> str:
    label = FIX_FILES[name][1]
    if label == "手账":
        return f"{char_name(mode)}手账"
    return label


def _bundled_first_header(name: str, mode: str) -> str:
    """bundled 包文件的首个 # 标题行（结构锚点，随包自动正确）。"""
    fp = bundled_character_dir(mode) / name
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


def _required_headers(name: str, mode: str) -> tuple:
    headers = FIX_FILES[name][3]
    if headers and headers[0] == "__FIRST_HEADER__":
        h = _bundled_first_header(name, mode)
        return (h,) if h else ()
    if headers and headers[0] == "__JOURNAL__":
        return (f"# {char_name(mode)}的手账",
                f"## 我和{user_name(mode)}聊了什么",
                "## 我想要去做的事/约定")
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
}


def _default_skeleton(name: str, mode: str) -> str:
    """手账骨架按包角色名/用户称呼拼；memory 骨架通用。"""
    if name == "手账.md":
        return (f"# {char_name(mode)}的手账\n\n## 我和{user_name(mode)}聊了什么\n\n（暂无）\n\n"
                "## 我想要去做的事/约定\n\n（暂无）\n")
    return _DEFAULT_SKELETON.get(name, "")


def _seed_empty(name: str, text: str, mode: str = DEFAULT_MODE) -> str:
    """首次修改空文件时给默认结构（不改变用户已有内容）。"""
    if not text.strip():
        skeleton = _default_skeleton(name, mode)
        if skeleton:
            return skeleton
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
            working[name] = _seed_empty(name, working[name], mode)
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


# ── Agent 层（阶段 2.8 抽至 modules/setting_fix_agents.py；此处 re-export，
# routes_fix 的 `from modules.setting_fix import run_alignment, run_proposal` 不变）──
from modules.setting_fix_agents import (   # noqa: F401,E402
    format_conversation, run_alignment, run_proposal,
)

__all__ = [
    "FIX_FILES", "MAX_ALIGN_USER_TURNS", "MAX_CHANGES", "MAX_NEW_PER_CHANGE",
    "MAX_TOTAL_NEW", "MAX_FILE_CHARS", "MAX_TEXT_LEN",
    "file_label", "editable_path", "readable_path", "ensure_editable_files",
    "load_current_files", "cleanup_legacy", "apply_change", "validate_changes",
    "format_conversation", "run_alignment", "run_proposal",
]
