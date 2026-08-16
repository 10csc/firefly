# -*- coding: utf-8 -*-
"""设定纠错助手的持久化与生效层 — 对话 / pending / 备份 / 回滚 / 审计

目录（服务器版按用户上下文隔离）：
  user_data/{mode}/.setting_fix/
    conversation.jsonl   对齐对话（重启可恢复）
    pending.json         待批准修改方案
    manifest.json        版本清单
    audit.jsonl          操作留痕
    backups/v{N}/        每次应用前的文件快照（相对 mode 根目录）
"""
import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from modules.app_config import mode_root, DEFAULT_MODE, user_scope_key

logger = logging.getLogger(__name__)

_FIX_DIR_NAME = ".setting_fix"
_LOCKS: dict[tuple, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def locked(mode: str = DEFAULT_MODE):
    """同一 (mode, 用户作用域) 串行化：LLM 对话与 apply 之间不并发。"""
    key = (mode or DEFAULT_MODE, user_scope_key())
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


def _dir(mode: str) -> Path:
    d = mode_root(mode) / _FIX_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backups_dir(mode: str) -> Path:
    d = _dir(mode) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(mode: str) -> Path:
    return _dir(mode) / "manifest.json"


def _audit_path(mode: str) -> Path:
    return _dir(mode) / "audit.jsonl"


def _conv_path(mode: str) -> Path:
    return _dir(mode) / "conversation.jsonl"


def _pending_path(mode: str) -> Path:
    return _dir(mode) / "pending.json"


def _atomic_write(fp: Path, text: str) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(fp)


# ── 对话（阶段一）──────────────────────────────────────
def load_conversation(mode: str = DEFAULT_MODE) -> list[dict]:
    fp = _conv_path(mode)
    if not fp.exists():
        return []
    out = []
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("text"):
                try:
                    obj["seq"] = int(obj.get("seq", 0))
                except (TypeError, ValueError):
                    obj["seq"] = 0
                out.append(obj)
    except OSError:
        pass
    return out


def append_conversation(mode: str, who: str, text: str,
                        options: list | None = None, stage: str = "aligning") -> dict:
    msgs = load_conversation(mode)
    seq = (msgs[-1].get("seq", 0) + 1) if msgs else 1
    record = {
        "seq": seq,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "who": who,
        "text": text,
        "options": [str(o) for o in (options or [])],
        "stage": stage,
    }
    with open(_conv_path(mode), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def clear_conversation(mode: str = DEFAULT_MODE) -> None:
    fp = _conv_path(mode)
    try:
        if fp.exists():
            fp.unlink()
    except OSError:
        pass


def _last_stage(conversation: list[dict]) -> str:
    for m in reversed(conversation):
        if m.get("who") == "assistant" and m.get("stage"):
            return str(m["stage"])
    return "aligning" if conversation else "idle"


# ── pending（阶段二）────────────────────────────────────
def save_pending(mode: str, proposal: dict) -> None:
    _atomic_write(_pending_path(mode),
                  json.dumps(proposal, ensure_ascii=False, indent=2))


def load_pending(mode: str = DEFAULT_MODE) -> dict | None:
    fp = _pending_path(mode)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("pending.json 损坏，按无 pending 处理")
        return None


def clear_pending(mode: str = DEFAULT_MODE) -> None:
    fp = _pending_path(mode)
    try:
        if fp.exists():
            fp.unlink()
    except OSError:
        pass


# ── manifest / audit ────────────────────────────────────
def _load_manifest(mode: str) -> dict:
    fp = _manifest_path(mode)
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            logger.warning("manifest 损坏，重建")
    return {"version": 0, "active": 0, "history": []}


def _save_manifest(mode: str, m: dict) -> None:
    _atomic_write(_manifest_path(mode), json.dumps(m, ensure_ascii=False, indent=2))


def _audit(mode: str, action: str, detail: dict) -> None:
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **detail}
    with open(_audit_path(mode), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def history(mode: str = DEFAULT_MODE, limit: int = 10) -> list[dict]:
    m = _load_manifest(mode)
    return list(reversed(m.get("history", [])[-limit:]))


# ── 状态汇总 ────────────────────────────────────────────
def get_status(mode: str = DEFAULT_MODE) -> dict:
    from modules.setting_fix import cleanup_legacy
    cleanup_legacy(mode)   # 旧 feedback/harness 数据一次性清理（幂等）
    conv = load_conversation(mode)
    pending = load_pending(mode)
    if pending is not None:
        stage = "proposal"
    else:
        stage = _last_stage(conv)
    m = _load_manifest(mode)
    return {
        "ok": True,
        "stage": stage,
        "messages": conv,
        "proposal": pending,
        "active_version": m.get("active", 0),
        "latest_version": m.get("version", 0),
        "history": history(mode),
        "can_start": stage == "ready" or pending is not None,
    }


# ── 应用 / 回滚 ─────────────────────────────────────────
def _clear_caches(mode: str) -> None:
    from modules.llm_base import clear_cache, reload_journal
    clear_cache()
    reload_journal(mode)
    try:
        from modules.polisher import clear_samples_cache
        clear_samples_cache()
    except Exception:
        pass


def _backup_files(mode: str, version: int, paths: list[Path]) -> None:
    """把当前文件快照到 backups/v{version}/（相对 mode 根目录）。"""
    root = mode_root(mode)
    dst_root = _backups_dir(mode) / f"v{version}"
    for fp in paths:
        rel = fp.relative_to(root).as_posix()
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if fp.exists():
            dst.write_bytes(fp.read_bytes())
        else:
            dst.write_text("", encoding="utf-8")


def _prune_backups(mode: str, keep: int = 10) -> None:
    """备份最多保留最近 keep 个版本（防止导出/账号同步体积无限增长）。"""
    root = _backups_dir(mode)
    try:
        dirs = []
        for d in root.iterdir():
            if d.is_dir() and d.name.startswith("v"):
                try:
                    dirs.append((int(d.name[1:]), d))
                except ValueError:
                    continue
        for _, d in sorted(dirs, reverse=True)[keep:]:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _restore_backup(mode: str, version: int) -> list[str]:
    """把 backups/v{version}/ 下的文件恢复回原位。返回恢复的相对路径列表。"""
    root = mode_root(mode)
    src_root = _backups_dir(mode) / f"v{version}"
    if not src_root.exists():
        return []
    restored = []
    for fp in src_root.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(src_root)
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(dst, fp.read_text(encoding="utf-8"))
        restored.append(rel.as_posix())
    return restored


def apply_pending(mode: str = DEFAULT_MODE) -> tuple[bool, str, list[str], int]:
    """应用 pending（用户点「应用」后调用）。返回 (ok, error, applied, version)。"""
    from modules.setting_fix import (validate_changes, apply_change,
                                     load_current_files, editable_path)

    pending = load_pending(mode)
    if not pending:
        return False, "没有待应用的修改方案", [], 0
    changes = pending.get("changes")
    if not isinstance(changes, list) or not changes:
        return False, "修改方案为空", [], 0

    files = load_current_files(mode)
    ok, errors = validate_changes(changes, mode, files)
    if not ok:
        return False, "应用前校验未通过: " + "；".join(errors[:4]), [], 0

    touched = []
    seen = set()
    for ch in changes:
        name = ch.get("file")
        if name in seen:
            continue
        seen.add(name)
        touched.append(editable_path(name, mode))

    m = _load_manifest(mode)
    new_version = int(m.get("version", 0)) + 1
    _backup_files(mode, int(m.get("version", 0)), touched)

    # 先在内存中生成全部新文本
    from modules.setting_fix import _seed_empty
    working = dict(files)
    try:
        for ch in changes:
            name = ch["file"]
            working[name] = _seed_empty(name, working[name])
            working[name] = apply_change(working[name], ch)
    except ValueError as e:
        return False, str(e), [], 0

    # 全部写 tmp 后再逐个 replace；中途失败用刚备份的快照尽力回滚
    applied = []
    try:
        for name in seen:
            fp = editable_path(name, mode)
            _atomic_write(fp, working[name])
            applied.append(name)
    except Exception as e:
        for name in seen:
            try:
                _restore_backup(mode, int(m.get("version", 0)))
            except Exception:
                pass
        return False, f"写入失败已尝试回滚: {e}", [], 0

    m["version"] = new_version
    m["active"] = new_version
    m.setdefault("history", []).append({
        "v": new_version,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": "apply",
        "files": sorted(seen),
        "diagnosis": str(pending.get("diagnosis") or "")[:120],
    })
    _save_manifest(mode, m)
    _audit(mode, "apply", {"version": new_version, "files": sorted(seen)})
    clear_pending(mode)
    clear_conversation(mode)
    _clear_caches(mode)
    _prune_backups(mode)
    return True, "", applied, new_version


def rollback(mode: str = DEFAULT_MODE) -> tuple[bool, str, int]:
    m = _load_manifest(mode)
    cur = int(m.get("active", 0))
    if cur <= 0:
        return False, "当前没有可回滚的修改", 0
    target = cur - 1
    restored = _restore_backup(mode, target)
    if not restored and not (_backups_dir(mode) / f"v{target}").exists():
        return False, f"缺少 v{target} 备份，无法回滚", 0
    m["active"] = target
    m.setdefault("history", []).append({
        "v": target,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": "rollback",
        "files": restored,
    })
    _save_manifest(mode, m)
    _audit(mode, "rollback", {"version": target, "files": restored})
    clear_pending(mode)
    _clear_caches(mode)
    return True, "", target


def dismiss(mode: str = DEFAULT_MODE) -> tuple[bool, str]:
    if load_pending(mode) is None:
        return False, "没有待处理的修改方案"
    clear_pending(mode)
    clear_conversation(mode)
    _audit(mode, "dismiss", {})
    return True, ""


def reset(mode: str = DEFAULT_MODE) -> tuple[bool, str]:
    clear_pending(mode)
    clear_conversation(mode)
    _audit(mode, "reset", {})
    return True, ""
