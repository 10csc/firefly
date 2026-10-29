# -*- coding: utf-8 -*-
"""服务器权威同步状态与 op 账本（SPEC §5/§6 的服务器侧最小实现）。

- 状态文件：{mode}/data/.server_sync_state.json（普通文件同步排除）；
- op 账本：{mode}/data/.server_sync_ops.jsonl（幂等 + 审计）；
- 每进程一把锁；最终上线时按 user+mode 串行（当前服务器单进程共享锁已足够先跑通）。
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = 2
_STATE_REL = "data/.server_sync_state.json"
_LEDGER_REL = "data/.server_sync_ops.jsonl"
_locks_guard = threading.Lock()
_mode_locks: dict[str, threading.RLock] = {}


def mode_lock(root: Path) -> threading.RLock:
    """同一用户模式下串行 manifest/import/export/state/ops，避免同 rev 双分配。"""
    key = str(Path(root).resolve()).lower()
    with _locks_guard:
        lock = _mode_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _mode_locks[key] = lock
        return lock


def state_path(root: Path) -> Path:
    return Path(root) / _STATE_REL


def ledger_path(root: Path) -> Path:
    return Path(root) / _LEDGER_REL


def default_state() -> dict:
    return {"schema": SCHEMA, "rev": 0, "history_epoch": 0,
            "history_rev": 0, "memory_rev": 0, "tombstones": [],
            "favorite_tombstones": [], "ops": [], "pending_op": None}


def _normalise(st: dict) -> dict:
    out = default_state()
    if isinstance(st, dict):
        for key in out:
            if key in st:
                out[key] = st[key]
    try:
        for key in ("rev", "history_epoch", "history_rev", "memory_rev"):
            out[key] = int(out.get(key) or 0)
    except (TypeError, ValueError):
        return default_state()
    for key in ("tombstones", "favorite_tombstones", "ops"):
        if not isinstance(out.get(key), list):
            out[key] = []
    if not isinstance(out.get("pending_op"), dict):
        out["pending_op"] = None
    return out


def load(root: Path) -> dict:
    out = default_state()
    try:
        raw = json.loads(state_path(root).read_text(encoding="utf-8"))
        out = _normalise(raw)
    except Exception:
        out = default_state()
    if out.get("pending_op"):
        with mode_lock(root):
            out = _recover_pending(root, out)
    return out


def save(root: Path, st: dict) -> bool:
    p = state_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp." + uuid.uuid4().hex)
        tmp.write_text(json.dumps(_normalise(st), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError as e:
        logger.warning("服务器同步状态写入失败 %s: %s", p, e)
        return False


def _clear_server_history_files(root: Path) -> bool:
    """服务器侧执行 clear：聊天产物物理清掉（favorites/角色资产不动）。"""
    import shutil
    from modules.storage import atomic_write_text

    root = Path(root)
    try:
        conv = root / "data" / "conversation.jsonl"
        if not atomic_write_text(conv, ""):
            return False
    except OSError as e:
        logger.warning("服务器 clear 写对话失败: %s", e)
        return False
    for rel in ("data/memory.md", "data/.memory_index", "data/pipeline.jsonl",
                "data/proactive_log.jsonl", "journal/手账.md"):
        try:
            p = root / rel
            if p.is_file():
                p.unlink()
        except OSError as e:
            logger.warning("服务器 clear 删除 %s 失败: %s", rel, e)
            return False
    try:
        archive = root / "data" / "archive"
        if archive.exists():
            shutil.rmtree(archive)
    except Exception as e:
        logger.warning("服务器 clear 删除归档失败: %s", e)
        return False
    return True


def _filter_file(root: Path, rel: str, candidates: list, state: dict) -> bool:
    from modules.storage import atomic_write_text
    from modules import sync_state

    fp = Path(root) / rel
    if not fp.is_file():
        return True
    try:
        text = fp.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("服务器同步过滤读取失败 %s: %s", fp, e)
        return False
    out = text
    for fn in candidates:
        out = fn(out, state)
    if out != text:
        return atomic_write_text(fp, out)
    return True


def _apply_side_effect(root: Path, typ: str, state: dict, payload: dict) -> bool:
    from modules import sync_state

    if typ == "clear":
        return _clear_server_history_files(root)
    if typ == "retract":
        return _filter_file(root, "data/conversation.jsonl",
                            [sync_state.filter_conversation_text], state)
    if typ == "unfavorite":
        return _filter_file(root, "data/favorites.json",
                            [sync_state.filter_favorites_text], state)
    return True


def _recover_pending(root: Path, st: dict) -> dict:
    pending = st.get("pending_op") if isinstance(st.get("pending_op"), dict) else None
    if not pending:
        return st
    previous = _normalise(pending.get("previous_state"))
    final = _normalise(pending.get("final_state"))
    typ = str((pending.get("op") or {}).get("type") or "")
    payload = (pending.get("op") or {}).get("payload") or {}
    if _apply_side_effect(root, typ, final, payload):
        final["pending_op"] = None
        if save(root, final):
            return final
    # 恢复失败时绝不能伪装回 previous 并继续接收新 op。持久化文件里的 pending
    # 才是重试真相；把它带回内存状态，让 apply_ops 显式拒绝整批新操作。
    previous = copy.deepcopy(previous)
    previous["pending_op"] = copy.deepcopy(pending)
    return previous


def _write_ledger(root: Path, rec: dict) -> None:
    """JSONL 只作审计镜像；幂等真相在 state.ops，单文件原子提交。"""
    try:
        ledger_path(root).parent.mkdir(parents=True, exist_ok=True)
        with ledger_path(root).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("服务器同步 op 审计写失败: %s", e)


def _ledger_seen(root: Path) -> dict:
    seen = {}
    try:
        for line in ledger_path(root).read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("op_id"):
                    seen[str(rec["op_id"])] = rec
            except Exception:
                continue
    except OSError:
        pass
    return seen


def _seen_ops(st: dict) -> dict:
    # 以 state.ops 为唯一幂等权威；JSONL 只是审计镜像。
    seen = {}
    for rec in st.get("ops") or []:
        if isinstance(rec, dict) and rec.get("op_id"):
            seen[str(rec["op_id"])] = rec
    return seen


def _prepare_final(st: dict, raw: dict) -> tuple[dict, dict]:
    typ = str(raw.get("type") or "").strip()
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    final = copy.deepcopy(st)
    if typ == "clear":
        final["history_epoch"] = int(final.get("history_epoch") or 0) + 1
        final["history_rev"] = int(final.get("history_rev") or 0) + 1
        final["tombstones"] = []
    elif typ == "retract":
        mids = [str(x) for x in (payload.get("mids") or []) if str(x)]
        cur = set(str(x) for x in (final.get("tombstones") or []))
        cur.update(mids)
        final["tombstones"] = sorted(cur)[-20000:]
        final["history_rev"] = int(final.get("history_rev") or 0) + 1
    elif typ == "rest":
        final["memory_rev"] = int(final.get("memory_rev") or 0) + 1
    elif typ == "favorite":
        fav_id = str(payload.get("id") or "").strip()
        final["favorite_tombstones"] = [
            x for x in (final.get("favorite_tombstones") or []) if str(x) != fav_id]
    elif typ == "unfavorite":
        fav_id = str(payload.get("id") or "").strip()
        ids = [str(x) for x in (final.get("favorite_tombstones") or [])]
        if fav_id and fav_id not in ids:
            ids.append(fav_id)
        final["favorite_tombstones"] = ids[-5000:]
    final["rev"] = int(final.get("rev") or 0) + 1
    rec = {"op_id": str(raw.get("op_id")), "type": typ,
           "rev": final["rev"], "base_rev": int(raw.get("base_rev") or 0),
           "at": int(time.time()), "ok": True}
    final.setdefault("ops", []).append(rec)
    final["ops"] = final["ops"][-20000:]
    return final, rec


def _semantic_error(raw: dict, st: dict) -> dict | None:
    typ = str(raw.get("type") or "").strip()
    if typ not in ("clear", "retract", "rest", "favorite", "unfavorite"):
        return {"error": "unsupported type " + typ, "code": "SYNC_OP_UNSUPPORTED"}
    if typ in ("clear", "retract", "rest"):
        try:
            base = int(raw.get("base_rev") or 0)
        except (TypeError, ValueError):
            return {"error": "base_rev 非法", "code": "SYNC_OP_BASE_REV_INVALID"}
        current = int(st.get("history_rev") or 0)
        if base != current:
            return {
                "error": f"操作基于 history_rev={base}，服务器当前为 {current}",
                "code": "SYNC_OP_STALE_BASE_REV",
                "base_rev": base,
                "current_rev": current,
            }
    if typ in ("favorite", "unfavorite"):
        if not str((raw.get("payload") or {}).get("id") or "").strip():
            return {"error": "收藏 op 缺少 id", "code": "SYNC_OP_FAVORITE_ID_MISSING"}
    return None


def apply_ops(root: Path, ops: list) -> dict:
    """幂等应用客户端 ops；返回新 state + 已应用/跳过列表。"""
    with mode_lock(root):
        st = load(root)
        if st.get("pending_op"):
            rejected = [{
                "op_id": str((raw or {}).get("op_id") or "") if isinstance(raw, dict) else "",
                "code": "SYNC_RECOVERY_PENDING",
                "error": "上一批服务器同步操作尚未恢复完成，拒绝接收新操作",
            } for raw in (ops or [])[:1]]
            return {"ok": False, "state": st, "applied": [], "skipped": [],
                    "rejected": rejected, "code": "SYNC_RECOVERY_PENDING"}
        seen = _seen_ops(st)
        applied, skipped, rejected = [], [], []
        for raw in ops or []:
            if not isinstance(raw, dict):
                rejected.append({"op_id": "", "code": "SYNC_OP_INVALID",
                                 "error": "op 必须为对象"})
                continue
            op_id = str(raw.get("op_id") or "").strip()
            typ = str(raw.get("type") or "").strip()
            if not op_id or not typ:
                rejected.append({"op_id": op_id, "code": "SYNC_OP_INVALID",
                                 "error": "missing op_id/type"})
                continue
            if op_id in seen:
                old = seen[op_id]
                skipped.append({"op_id": op_id, "duplicate": True,
                                "type": old.get("type"), "rev": old.get("rev")})
                continue
            problem = _semantic_error(raw, st)
            if problem:
                rejected.append({"op_id": op_id, **problem})
                continue
            final, rec = _prepare_final(st, raw)
            # 单文件状态作为提交点：pending 先落盘，崩溃后 load() 会补完副作用。
            prepared = copy.deepcopy(final)
            prepared["pending_op"] = {
                "op": raw,
                "final_state": final,
                "previous_state": st,
            }
            if not save(root, prepared):
                rejected.append({"op_id": op_id, "code": "SYNC_STATE_WRITE_FAILED",
                                 "error": "服务器同步状态写入失败"})
                break
            if not _apply_side_effect(root, typ, final, raw.get("payload") or {}):
                rejected.append({"op_id": op_id,
                                 "code": "SYNC_FILE_COMMIT_FAILED",
                                 "error": "数据文件提交失败，已保留恢复事务"})
                break
            final["pending_op"] = None
            if not save(root, final):
                rejected.append({"op_id": op_id, "code": "SYNC_STATE_WRITE_FAILED",
                                 "error": "服务器同步状态最终提交失败"})
                break
            st = final
            _write_ledger(root, rec)
            seen[op_id] = rec
            applied.append({"op_id": op_id, "type": typ, "rev": rec["rev"]})
        return {"ok": not rejected, "state": st, "applied": applied,
                "skipped": skipped, "rejected": rejected,
                "code": (rejected[0].get("code") if rejected else "")}
