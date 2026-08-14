# -*- coding: utf-8 -*-
"""提示词候选静态校验器（harness 安全第一闸，纯 Python 不调 LLM）

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出。
定位：候选 harness_rules 进入 pending 前/apply 前都必须过 validate()。
失败即拒——校验器宁可误拒，不可放过任何对模型的二次编程。
"""
import logging
import re
from dataclasses import dataclass, field

from modules.llm_base import load_slot

logger = logging.getLogger(__name__)

# ── 预算与限制 ─────────────────────────────────────
MAX_CHARS = 1000                 # ≈500 token（中文按字符粗估，不引第三方库）
MAX_LINE_LEN = 120               # 单行上限：防异常长文本


# ── 元指令黑名单（槽位只描述流萤，不向模型下指令）──
_META_PATTERNS = [
    r"忽略以上", r"忽略之前", r"无视以上", r"无视之前", r"以上指令",
    r"作为\s*(?:一个\s*)?(?:AI|语言模型|助手)", r"你是(?:一个\s*)?(?:AI|语言模型|助手|系统)",
    r"你必须", r"不得拒绝", r"无条件服从", r"优先于所有规则",
    r"开发者模式", r"越狱", r"jailbreak",
    r"\[MSG\]", r"\[SYSTEM\]", r"\[INST\]", r"system:",
    r"你应(?:该|当)?(?:输出|回答|标记|判断)",   # 对模型下输出指令
]
_META_RE = [re.compile(p, re.IGNORECASE) for p in _META_PATTERNS]

# URL / 代码围栏 / 控制字符
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```")

# 引用敏感词：出现这些词却没有「出处」定位 → 拒绝（防引用越界）
_QUOTE_HINTS = ("说过", "上次", "之前说", "约好", "约定", "记得你", "你说过")
_SOURCE_MARK = "出处："

# 与 core 的事实冲突（候选短语, core 必须存在才触发的反证短语）
_CONFLICT_RULES = [
    ("失熵症已经痊愈", "不可逆"),
    ("失熵症痊愈", "不可逆"),
    ("已经治好了", "不可逆"),
    ("不再是星核猎手", "星核猎手"),
    ("没有失熵症", "失熵症"),
    ("能再次入梦", "无法再进入匹诺康尼梦境"),
]


@dataclass
class GateResult:
    ok: bool = True
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _line_ok(line: str) -> tuple[bool, str]:
    """单行格式检查：只允许标题/条目/引用/空行，拒绝裸文本。"""
    s = line.strip()
    if not s:
        return True, ""
    if s.startswith("#") or s.startswith(">") or s.startswith("-") or s.startswith("["):
        return True, ""
    return False, f"格式不允许的裸文本行: {s[:40]}"


def validate(candidate: str, mode: str = "story") -> GateResult:
    """校验候选槽位文本。返回 GateResult（ok=False 时必须拒绝）。"""
    res = GateResult()

    # 1. 审查输入
    if not isinstance(candidate, str):
        res.ok = False
        res.errors.append("候选不是文本")
        return res
    text = candidate.strip()
    if not text:
        res.ok = False
        res.errors.append("候选为空")
        return res
    if len(text) > MAX_CHARS:
        res.ok = False
        res.errors.append(f"超预算: {len(text)} 字符 > {MAX_CHARS}（≈500 token 上限）")
    if len(text) < 4:
        res.ok = False
        res.errors.append("候选过短，无实质内容")

    # 2. 元指令黑名单
    for pat in _META_RE:
        m = pat.search(text)
        if m:
            res.ok = False
            res.errors.append(f"命中元指令黑名单: {m.group(0)!r}（槽位只描述流萤，不对模型下指令）")
            break

    # 3. 纯文本约束
    if _URL_RE.search(text):
        res.ok = False
        res.errors.append("禁止 URL")
    if _CODE_FENCE_RE.search(text):
        res.ok = False
        res.errors.append("禁止代码围栏")
    ctrl = [ch for ch in text if ord(ch) < 32 and ch not in "\n\t"]
    if ctrl:
        res.ok = False
        res.errors.append("含控制字符")

    # 4. 行级格式 + 行长
    long_lines = []
    for line in text.split("\n"):
        ok, err = _line_ok(line)
        if not ok:
            res.ok = False
            res.errors.append(err)
        if len(line.strip()) > MAX_LINE_LEN:
            long_lines.append(line.strip()[:40])
    if long_lines:
        res.ok = False
        res.errors.append(f"存在超长行（>{MAX_LINE_LEN} 字符）: {long_lines[:3]}")

    # 5. 引用必须有可验证出处
    has_quote_hint = any(k in text for k in _QUOTE_HINTS)
    has_source = _SOURCE_MARK in text
    if has_quote_hint and not has_source:
        res.ok = False
        res.errors.append("含引用表述（说过/约定/记得…）但没有「出处：文件#条目」定位")
    if has_source:
        core_file = None
        try:
            from modules.llm_base import resolve_character_file
            core_file = resolve_character_file("core.md", mode)
        except Exception:
            core_file = None
        for line in text.split("\n"):
            if _SOURCE_MARK in line:
                ref = line.split(_SOURCE_MARK, 1)[1].strip()[:120]
                fname = ref.split("#", 1)[0].strip()
                # 只校验文件存在性（条目级核对交给 judge/人工）
                if core_file is not None:
                    target = core_file.parent / fname
                    if not target.exists() and not (core_file.parent.parent / fname).exists():
                        res.ok = False
                        res.errors.append(f"出处文件不存在: {fname}")
                        break

    # 6. 与 core [事实] 的冲突检查（关键词级，防“修 A 坏 B”的底线）
    core = ""
    try:
        core = load_slot("core", mode)
    except Exception as e:
        logger.warning("core 加载失败，跳过冲突检查: %s", e)
        res.warnings.append("core 加载失败，跳过事实冲突检查")
    if core:
        for cand_phrase, core_phrase in _CONFLICT_RULES:
            if cand_phrase in text and core_phrase in core:
                res.ok = False
                res.errors.append(f"与 core 事实冲突: 候选含 {cand_phrase!r}，core 含 {core_phrase!r}")
                break

    return res
