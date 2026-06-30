# -*- coding: utf-8 -*-
"""倍率变化器 — 所有属性联动关系的中央枢纽

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
计算方式：加算叠加，基线=1.0，各条规则叠加后钳位到 [0.5, 1.5]。

联动来源（6条）：
  A. 心情 → 好感倍率
  B. 心情 → 紧张倍率
  C. 心情 → 主动性倍率
  D. 好感度 → 紧张倍率（双向联动）
  E. 紧张度 → 好感倍率（双向联动）
  F. 主动性 → 好感/紧张倍率（单向 U 形）
"""

import logging

logger = logging.getLogger(__name__)


# ── 异常 ──────────────────────────────────────────
class RateModifierError(Exception): pass
class InputRejected(RateModifierError): pass
class OutputInvalid(RateModifierError): pass


# ── 心情倍率系数 ───────────────────────────────────
# 格式: {label: (好感系数, 紧张系数, 主动性系数)}
_MOOD_COEFFICIENTS = {
    "安心": (0.01, -0.02, 0.0),
    "开心": (0.03, -0.03, 0.01),
    "低落": (-0.02, 0.02, -0.02),
    "焦虑": (-0.01, 0.03, 0.0),
    "害羞": (0.0, 0.03, 0.01),
    "困惑": (0.0, 0.0, 0.0),
}


def compute_rates(prev_moods: list, prev_affection: float, prev_tension: float, prev_initiative: float) -> dict:
    """主入口。根据上一轮状态计算本轮倍率。

    返回 {"affection": float, "tension": float, "initiative": float}
    """
    # 1. 审查
    if not isinstance(prev_moods, list):
        raise InputRejected(f"prev_moods 必须为 list，实际: {type(prev_moods).__name__}")
    for i, m in enumerate(prev_moods):
        if not isinstance(m, dict):
            raise InputRejected(f"prev_moods[{i}] 必须为 dict，实际: {type(m).__name__}")
        if "label" not in m or "intensity" not in m:
            raise InputRejected(f"prev_moods[{i}] 缺少 label/intensity: {m}")
        if not isinstance(m["intensity"], (int, float)) or not (1 <= m["intensity"] <= 5):
            raise InputRejected(f"prev_moods[{i}] intensity 必须在 1-5: {m['intensity']}")
    for key, val, lo, hi in [
        ("affection", prev_affection, 0, 100),
        ("tension", prev_tension, 0, 200),
        ("initiative", prev_initiative, 0, 100),
    ]:
        if not isinstance(val, (int, float)):
            raise InputRejected(f"prev_{key} 必须为数字，实际: {type(val).__name__}")
        if not (lo <= val <= hi):
            raise InputRejected(f"prev_{key} 超出范围 [{lo},{hi}]: {val}")

    # 2. 计算
    aff_mod = _mood_to_affection(prev_moods) + _tension_to_affection(prev_tension) + _initiative_to_affection(prev_initiative)
    ten_mod = _mood_to_tension(prev_moods) + _affection_to_tension(prev_affection) + _initiative_to_tension(prev_initiative)
    ini_mod = _mood_to_initiative(prev_moods)

    # 2.5. 困惑缓冲——困惑越高，所有倍率趋近 1.0（情绪反应被压制）
    confusion = _get_confusion_intensity(prev_moods)
    if confusion > 0:
        dampen = confusion * 0.06  # 每级困惑缓冲 6%，社交困惑 3-5 → 缓冲 18%-30%
        aff_mod *= (1.0 - dampen)
        ten_mod *= (1.0 - dampen)
        ini_mod *= (1.0 - dampen)

    # 3. 加算基线 + 钳位（紧张不受倍率钳位限制）
    result = {
        "affection": _clamp(1.0 + aff_mod),
        "tension": 1.0 + ten_mod,       # 不钳位——紧张是短期数值，允许剧烈波动
        "initiative": _clamp(1.0 + ini_mod),
    }

    # 4. 验证
    for k, v in result.items():
        if k == "tension":
            continue  # 紧张不验证钳位范围
        if not (0.5 <= v <= 1.5):
            logger.error("倍率变化器输出异常: %s=%s 超出 [0.5,1.5]", k, v)
            raise OutputInvalid(f"{k} 倍率 {v} 超出钳位范围")

    return result


# ── A/B/C: 心情 → 倍率 ────────────────────────────

def _mood_to_affection(moods: list) -> float:
    total = 0.0
    for m in moods:
        coeff = _MOOD_COEFFICIENTS.get(m["label"], (0, 0, 0))
        total += coeff[0] * m["intensity"]
    return total


def _mood_to_tension(moods: list) -> float:
    total = 0.0
    for m in moods:
        coeff = _MOOD_COEFFICIENTS.get(m["label"], (0, 0, 0))
        total += coeff[1] * m["intensity"]
    return total


def _mood_to_initiative(moods: list) -> float:
    total = 0.0
    for m in moods:
        coeff = _MOOD_COEFFICIENTS.get(m["label"], (0, 0, 0))
        total += coeff[2] * m["intensity"]
    return total


# ── D: 好感度 → 紧张倍率 ──────────────────────────

def _affection_to_tension(affection: float) -> float:
    """低好感→社交紧张，高好感→放松。多段线性递减。"""
    if affection <= 40:
        return 0.45 - affection * 0.0025      # 0→0.45, 40→0.35
    elif affection <= 70:
        return 0.49 - affection * 0.00345     # 41→0.35, 70→0.25
    elif affection <= 80:
        return 1.44 - affection * 0.0167      # 71→0.25, 80→0.10
    elif affection <= 85:
        return 0.0                             # 舒适区
    else:
        return 1.09 - affection * 0.0129      # 86→-0.02, 100→-0.20


# ── E: 紧张度 → 好感倍率 ──────────────────────────

def _tension_to_affection(tension: float) -> float:
    """高紧张→好感表达受阻。多段线性递增（负向）。"""
    if tension <= 30:
        return 0.0
    elif tension <= 60:
        return -(0.011 + tension * 0.00103)    # 31→-0.02, 60→-0.05
    elif tension <= 80:
        return -(tension * 0.00789 - 0.431)    # 61→-0.05, 80→-0.20
    elif tension <= 90:
        return -(tension * 0.0111 - 0.699)     # 81→-0.20, 90→-0.30
    else:
        return -(tension * 0.0222 - 1.722)     # 91→-0.30, 100→-0.50


# ── F: 主动性 → 好感/紧张倍率（U 形）─────────────

def _initiative_to_affection(initiative: float) -> float:
    """以 50 为对称轴的 V 形。偏离越远，好感倍率加成越大。"""
    if initiative <= 50:
        return 0.02 + (50 - initiative) * 0.0016   # 0→0.10, 50→0.02
    else:
        return 0.02 + (initiative - 50) * 0.002    # 50→0.02, 100→0.12


def _initiative_to_tension(initiative: float) -> float:
    """以 50 为对称轴的倒 V 形。偏离越远，紧张倍率越低（越从容）。"""
    if initiative <= 50:
        return -(0.02 + (50 - initiative) * 0.002)    # 0→-0.12, 50→-0.02
    else:
        return -(0.02 + (initiative - 50) * 0.0024)   # 50→-0.02, 100→-0.14


# ── 工具 ──────────────────────────────────────────

def _get_confusion_intensity(moods: list) -> float:
    """从心情列表中提取困惑强度"""
    for m in moods:
        if m["label"] == "困惑":
            return float(m["intensity"])
    return 0.0


def _clamp(value: float, low: float = 0.5, high: float = 1.5) -> float:
    return max(low, min(high, value))
