# -*- coding: utf-8 -*-
"""harness 槽位版本管理（批准制落地的文件层）

目录布局（全部在 user_data 下，天然按用户隔离）：
  user_data/{mode}/character/harness_rules.md        生效版（active）
  user_data/{mode}/character/.harness/pending.md     候选（L1 唯一写入区）
  user_data/{mode}/character/.harness/report.json    候选评审报告
  user_data/{mode}/character/.harness/v{N}.md        历史版本（回滚源）
  user_data/{mode}/character/.harness/manifest.json  版本清单
  user_data/{mode}/character/.harness/audit.jsonl    生效/回滚审计

权限模型：优化器只能调 stage_candidate（写 pending）；apply 必须由批准 API 调用；
模板层（Python 常量）永不经过本模块。
"""
import json
import logging
import threading
import time
from pathlib import Path

from modules.app_config import mode_character_dir
from modules.llm_base import clear_cache
from modules.prompt_gate import validate

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()

ACTIVE_NAME = "harness_rules.md"


def _dir(mode: str) -> Path:
    d = mode_character_dir(mode) / ".harness"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _active(mode: str) -> Path:
    return mode_character_dir(mode) / ACTIVE_NAME


def _manifest_path(mode: str) -> Path:
    return _dir(mode) / "manifest.json"


def _audit_path(mode: str) -> Path:
    return _dir(mode) / "audit.jsonl"


def _load_manifest(mode: str) -> dict:
    fp = _manifest_path(mode)
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            logger.warning("manifest 损坏，重建空清单")
    return {"version": 0, "active": 0, "history": []}


def _save_manifest(mode: str, m: dict):
    fp = _manifest_path(mode)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)


def _audit(mode: str, action: str, detail: dict):
    fp = _audit_path(mode)
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **detail}
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _atomic_write(fp: Path, text: str):
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(fp)


def _entries(text: str) -> list:
    """提取条目（- 开头行）供 diff 使用；无条目时回退为整段。"""
    items = [ln.strip() for ln in text.split("\n")
             if ln.strip().startswith("-")]
    return items or ([text.strip()] if text.strip() else [])


def diff_entries(old: str, new: str) -> str:
    """条目级纯文本 diff（给人看的批准凭据，不引第三方库）。"""
    o, n = _entries(old), _entries(new)
    if not o and not n:
        return "（新旧均为空）"
    if len(o) <= 1 and len(n) <= 1:
        return f"- 旧：{o[0] if o else '（空）'}\n+ 新：{n[0] if n else '（空）'}"
    so, sn = set(o), set(n)
    added = [x for x in n if x not in so]
    removed = [x for x in o if x not in sn]
    lines = []
    for x in added:
        lines.append(f"+ 新增：{x}")
    for x in removed:
        lines.append(f"− 移除：{x}")
    if not lines:
        lines.append("（条目无变化，仅结构/措辞调整，请查看全文）")
    return "\n".join(lines)


def stage_candidate(mode: str, text: str, report: dict | None = None) -> dict:
    """L1 写入：候选落 pending（不生效）。返回 {ok, error?, version?}。"""
    with _LOCK:
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "候选内容为空"}
        gate = validate(text, mode)
        if not gate.ok:
            return {"ok": False, "error": "静态校验未通过: " + "；".join(gate.errors[:3])}
        m = _load_manifest(mode)
        _atomic_write(_dir(mode) / "pending.md", text)
        rep = dict(report or {})
        rep["gate"] = {"ok": True, "warnings": gate.warnings}
        rep["target_version"] = m.get("version", 0) + 1
        rep["staged_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _atomic_write(_dir(mode) / "report.json",
                      json.dumps(rep, ensure_ascii=False, indent=2))
        _audit(mode, "stage", {"version": rep["target_version"]})
        return {"ok": True, "version": rep["target_version"]}


def get_status(mode: str) -> dict:
    """读候选与生效状态（批准 UI 用，只读）。"""
    with _LOCK:
        m = _load_manifest(mode)
        pending = _dir(mode) / "pending.md"
        report = _dir(mode) / "report.json"
        active = _active(mode)
        status = {
            "has_pending": pending.exists(),
            "active_version": m.get("active", 0),
            "latest_version": m.get("version", 0),
            "active": active.read_text(encoding="utf-8") if active.exists() else "",
            "pending": pending.read_text(encoding="utf-8") if pending.exists() else "",
            "report": None,
        }
        if report.exists():
            try:
                status["report"] = json.loads(report.read_text(encoding="utf-8"))
            except Exception:
                status["report"] = None
        if status["has_pending"] and status["active"]:
            status["diff"] = diff_entries(status["active"], status["pending"])
        else:
            status["diff"] = "（当前无生效规则，候选将作为第一条生效）"
        return status


def dismiss(mode: str) -> dict:
    """忽略候选：删除 pending（不生效，不进历史）。"""
    with _LOCK:
        pending = _dir(mode) / "pending.md"
        report = _dir(mode) / "report.json"
        if not pending.exists() and not report.exists():
            return {"ok": False, "error": "没有待处理的候选"}
        try:
            pending.unlink()
        except FileNotFoundError:
            pass
        try:
            report.unlink()
        except FileNotFoundError:
            pass
        _audit(mode, "dismiss", {"note": "用户忽略候选"})
        return {"ok": True}


def apply(mode: str) -> dict:
    """L3 生效：候选 → 原子替换 active → 归档 → 清缓存 → 审计。需 L2 批准后调用。"""
    with _LOCK:
        pending = _dir(mode) / "pending.md"
        if not pending.exists():
            return {"ok": False, "error": "没有待应用的候选"}
        text = pending.read_text(encoding="utf-8")
        gate = validate(text, mode)
        if not gate.ok:
            return {"ok": False, "error": "应用前静态校验未通过: " + "；".join(gate.errors[:3])}

        m = _load_manifest(mode)
        active = _active(mode)
        # 首次应用：把当前生效内容（可能为空）归档为 v0，供回滚
        if m.get("version", 0) == 0 and active.exists():
            _atomic_write(_dir(mode) / "v0.md", active.read_text(encoding="utf-8"))

        new_v = m.get("version", 0) + 1
        _atomic_write(_dir(mode) / f"v{new_v}.md", text)   # 归档新版（正向审计）
        _atomic_write(active, text)                        # 生效
        m["version"] = new_v
        m["active"] = new_v
        m["history"].append({"v": new_v, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "action": "apply"})
        _save_manifest(mode, m)
        # 候选已生效：清空 pending（report 信息已进审计与归档，不再展示）
        try:
            pending.unlink()
        except FileNotFoundError:
            pass
        try:
            (_dir(mode) / "report.json").unlink()
        except FileNotFoundError:
            pass
        _audit(mode, "apply", {"version": new_v})
        clear_cache()
        return {"ok": True, "version": new_v}


def rollback(mode: str) -> dict:
    """回滚到上一生效版本（v0 = 清空/恢复首版）。"""
    with _LOCK:
        m = _load_manifest(mode)
        cur = m.get("active", 0)
        if cur <= 0:
            return {"ok": False, "error": "当前没有可回滚的生效版本"}
        target = cur - 1
        active = _active(mode)
        src = _dir(mode) / f"v{target}.md"
        if src.exists():
            _atomic_write(active, src.read_text(encoding="utf-8"))
        else:
            try:
                active.unlink()
            except FileNotFoundError:
                pass
        m["active"] = target
        m["history"].append({"v": target, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "action": "rollback"})
        _save_manifest(mode, m)
        _audit(mode, "rollback", {"version": target})
        clear_cache()
        return {"ok": True, "version": target}
