# -*- coding: utf-8 -*-
"""模式级同步状态（同步架构 SPEC §3/§4 的第一步）。

当前只落地本地最小状态：消息代际、撤回墓碑、历史版本号。
它不是最终 ops 协议；先把“删除不是文件更新”这件事变成可持久化的数据，
再逐步接服务器 rev/op 账本。

文件：{mode}/data/sync_state.json（本阶段不参与服务器同步）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = 1
_STATE_REL = "data/sync_state.json"
_OUTBOX_REL = "data/.sync_outbox.jsonl"
_TXN_REL = "data/.sync_txn.json"
_lock = threading.RLock()
_mode_locks: dict[str, threading.RLock] = {}
_mode_locks_guard = threading.Lock()


class SyncRecoveryPending(OSError):
    """已有事务无法完成恢复；必须保留现场，禁止覆盖或继续破坏性操作。"""


def mode_lock(root: Path) -> threading.RLock:
    """同一模式在本进程内串行；供完整同步回合与状态提交共用。"""
    key = str(Path(root).resolve()).lower()
    with _mode_locks_guard:
        lock = _mode_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _mode_locks[key] = lock
        return lock


def _atomic_json(fp: Path, value: dict) -> bool:
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_name(fp.name + ".tmp." + uuid.uuid4().hex)
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(fp)
        return True
    except OSError as e:
        logger.warning("同步状态原子写失败 %s: %s", fp, e)
        return False


def txn_path(root: Path) -> Path:
    return Path(root) / _TXN_REL


def begin_txn(root: Path, op_type: str, payload: dict | None = None) -> dict:
    """写 PREPARED 事务；数据改动完成后再 commit_txn。"""
    with _lock:
        # 上一个事务若因进程崩溃残留，先按类型恢复；否则绝不能覆盖它的 op_id。
        if txn_path(root).exists():
            if not recover_txn(root):
                raise SyncRecoveryPending(
                    "上一笔同步事务恢复失败，已保留现场；请稍后重试")
        st = load(root)
        txn = {
            "txn_id": new_mid(),
            "op_id": new_mid(),
            "type": str(op_type),
            "base_rev": int(st.get("history_rev") or 0),
            "base_epoch": int(st.get("history_epoch") or 0),
            "payload": payload or {},
            "state": "PREPARED",
        }
        p = txn_path(root)
        if not _atomic_json(p, txn):
            raise OSError(f"事务日志写入失败: {p}")
        return txn


def abort_txn(root: Path) -> bool:
    try:
        txn_path(root).unlink()
        return True
    except OSError:
        return not txn_path(root).exists()


def commit_txn(root: Path, txn: dict) -> dict:
    """把 PREPARED 事务变成 outbox 记录；outbox 只接收已提交操作。"""
    with _lock:
        op = {
            "op_id": str(txn.get("op_id") or new_mid()),
            "type": str(txn.get("type") or ""),
            "base_rev": int(txn.get("base_rev") or 0),
            "payload": txn.get("payload") or {},
        }
        committed = dict(txn)
        committed["op"] = op
        committed["state"] = "COMMITTED"
        # 先把 COMMITTED 意图写进事务：若随后写 outbox 时崩溃，recover_txn 能补写。
        if not _atomic_json(txn_path(root), committed):
            raise OSError("事务 COMMITTED 标记写入失败")
        if not _append_outbox_unique(root, op):
            raise OSError("待发送同步队列写入失败")
        abort_txn(root)
        return op


def recover_txn(root: Path) -> bool:
    """完成或回滚上次中断的本地事务。

    clear/retract/favorite/unfavorite 一旦进入 PREPARED，就代表用户动作已获准执行：
    重启后必须补完数据效果并提交 op，否则本地已删除、服务器却永远收不到墓碑。
    rest 的 PREPARED 只表示“准备开始调模型”；失败/崩溃时必须丢弃，不能伪造成整理成功。

    返回 True 表示事务已清空（完成、丢弃或隔离）；False 表示恢复失败，
    原事务仍保留，调用方不得覆盖它。
    """
    try:
        raw = json.loads(txn_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except Exception as e:
        logger.warning("同步事务损坏，已隔离 %s: %s", txn_path(root), e)
        try:
            txn_path(root).replace(txn_path(root).with_suffix(".bad"))
            return True
        except OSError:
            return False
    if not isinstance(raw, dict):
        return abort_txn(root)

    with _lock:
        if raw.get("state") == "COMMITTED":
            op = raw.get("op")
            if isinstance(op, dict) and op.get("op_id") and op.get("type"):
                if _append_outbox_unique(root, op):
                    return abort_txn(root)
                return False
            logger.warning("同步事务 COMMITTED 缺少 op，隔离现场: %s", txn_path(root))
            try:
                txn_path(root).replace(txn_path(root).with_suffix(".bad"))
                return True
            except OSError:
                return False

        typ = str(raw.get("type") or "")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        should_commit = False
        actionable = True
        if typ == "clear":
            if _clear_local_history_files(root):
                current = load(root)
                base_epoch = raw.get("base_epoch")
                if base_epoch is None:
                    # 兼容 hot5 生成的旧事务：若 state 已前进到下一历史版本，
                    # 说明上次已保存 epoch，只是 COMMITTED/outbox 没写完。
                    current_rev = int(current.get("history_rev") or 0)
                    base_rev = int(raw.get("base_rev") or 0)
                    base_epoch = (int(current.get("history_epoch") or 0) - 1
                                  if current_rev > base_rev
                                  else int(current.get("history_epoch") or 0))
                target_epoch = int(base_epoch or 0) + 1
                if int(current.get("history_epoch") or 0) < target_epoch:
                    try:
                        clear_history(root)
                    except OSError as e:
                        logger.error("clear 状态落盘失败（保留待重试）: %s", e)
                current = load(root)
                should_commit = int(current.get("history_epoch") or 0) >= target_epoch
        elif typ == "retract":
            mids = [str(x) for x in (payload.get("mids") or []) if str(x)]
            actionable = bool(mids)
            if mids:
                removed = _remove_mids_from_conversation(root, mids)
                persisted = _set_tombstones(root, mids) if removed else False
                should_commit = removed and persisted
        elif typ == "favorite":
            fav_id = str(payload.get("id") or "")
            actionable = bool(fav_id)
            if fav_id:
                item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
                actionable = str(item.get("id") or "") == fav_id
                if str(item.get("id") or "") == fav_id:
                    restored = _restore_favorite_item(root, item)
                    untouched = (_set_favorite_tombstone(root, fav_id, keep=True)
                                 if restored else False)
                    should_commit = restored and untouched
        elif typ == "unfavorite":
            fav_id = str(payload.get("id") or "")
            actionable = bool(fav_id)
            if fav_id:
                removed = _remove_favorite_item(root, fav_id)
                persisted = (_set_favorite_tombstone(root, fav_id, keep=False)
                             if removed else False)
                should_commit = removed and persisted
        if should_commit:
            try:
                commit_txn(root, raw)
            except OSError as e:
                logger.error("同步事务恢复提交失败（保留待下次重试）: %s", e)
                return False
            return not txn_path(root).exists()
        else:
            # PREPARED 的破坏性操作已获用户批准；副作用失败时不能丢弃事务，
            # 否则本地文件可能已删一半而服务器永远收不到对应墓碑。
            if actionable and typ in ("clear", "retract", "favorite", "unfavorite"):
                logger.error("同步事务副作用恢复失败（保留待下次重试）: %s", txn_path(root))
                return False
            return abort_txn(root)


def _remove_mids_from_conversation(root: Path, mids) -> bool:
    wanted = {str(x) for x in (mids or []) if str(x)}
    if not wanted:
        return True
    fp = Path(root) / "data" / "conversation.jsonl"
    if not fp.exists():
        return True
    kept = []
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                kept.append(line)
                continue
            if isinstance(rec, dict) and message_mid(rec) in wanted:
                continue
            kept.append(line)
        from modules.storage import atomic_write_text
        text = "\n".join(kept) + ("\n" if kept else "")
        return atomic_write_text(fp, text)
    except OSError as e:
        logger.warning("事务恢复删除消息失败 %s: %s", fp, e)
        return False


def _clear_local_history_files(root: Path) -> bool:
    """服务器 epoch 高于本机时，按 clear 语义清掉本地聊天产物。

    不改 sync_state 自身；调用方随后合并服务器状态。
    """
    import shutil

    from modules.storage import atomic_write_text

    root = Path(root)
    ok = True
    conv = root / "data" / "conversation.jsonl"
    try:
        if conv.parent.is_dir():
            ok = atomic_write_text(conv, "") and ok
    except OSError as e:
        logger.warning("事务恢复清空对话失败 %s: %s", conv, e)
        ok = False
    for rel in ("data/memory.md", "data/.memory_index", "data/pipeline.jsonl",
                "data/proactive_log.jsonl", "journal/手账.md"):
        try:
            p = root / rel
            if p.is_file():
                p.unlink()
        except OSError as e:
            logger.warning("事务恢复删除 %s 失败: %s", rel, e)
            ok = False
    try:
        archive = root / "data" / "archive"
        if archive.exists():
            shutil.rmtree(archive)
    except Exception as e:
        logger.warning("事务恢复删除归档失败: %s", e)
        ok = False
    # 语音是历史消息的附属产物；清历史后对应 seq 已不存在，不能留下
    # “看不见也播不了”的孤儿文件。这里只删 store 管理的 v<seq>.wav，
    # 不碰与 user_data 平级的语音模型/语气库。
    voice_dir = root / "data" / "voice"
    try:
        if voice_dir.is_dir():
            for fp in voice_dir.iterdir():
                if (fp.is_file() and fp.suffix.lower() == ".wav"
                        and fp.stem.startswith("v") and fp.stem[1:].isdigit()):
                    try:
                        fp.unlink()
                    except OSError as e:
                        logger.warning("事务恢复删除语音 %s 失败: %s", fp, e)
                        ok = False
            try:
                voice_dir.rmdir()
            except OSError:
                # 非 store 管理的文件或并发写入不算清理失败；只处理可安全识别的音频。
                pass
    except OSError as e:
        logger.warning("事务恢复扫描语音目录失败 %s: %s", voice_dir, e)
        ok = False
    return ok


def _read_favorites(root: Path) -> list:
    fp = Path(root) / "data" / "favorites.json"
    try:
        arr = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    except Exception:
        arr = []
    return arr if isinstance(arr, list) else []


def _write_favorites(root: Path, items: list) -> bool:
    from modules.storage import atomic_write_text
    fp = Path(root) / "data" / "favorites.json"
    return atomic_write_text(fp, json.dumps(items, ensure_ascii=False, indent=1))


def _restore_favorite_item(root: Path, item: dict) -> bool:
    """PREPARED 收藏事务的数据副作用重放：按 id/source 幂等 upsert。"""
    fav_id = str(item.get("id") or "")
    source = str(item.get("source") or "")
    items = _read_favorites(root)
    kept = [
        x for x in items
        if not (isinstance(x, dict) and (
            str(x.get("id") or "") == fav_id
            or (source and str(x.get("source") or "") == source)))
    ]
    kept.append(item)
    return _write_favorites(root, kept)


def _remove_favorite_item(root: Path, fav_id: str) -> bool:
    items = _read_favorites(root)
    kept = [
        x for x in items
        if not (isinstance(x, dict) and str(x.get("id") or "") == fav_id)
    ]
    return _write_favorites(root, kept)


def apply_remote_state(root: Path, remote: dict | None) -> dict:
    """合并服务器状态；epoch 前进时先清本地旧历史产物。"""
    if not isinstance(remote, dict):
        return load(root)
    with _lock:
        local = load(root)
        if int(remote.get("history_epoch") or 0) > int(local.get("history_epoch") or 0):
            if not _clear_local_history_files(root):
                raise OSError("应用服务器新 epoch 前清理本地历史失败")
        return merge_server_state(root, remote)


def outbox_path(root: Path) -> Path:
    return Path(root) / _OUTBOX_REL


def load_outbox(root: Path) -> list:
    p = outbox_path(root)
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("op_id") and rec.get("type"):
                    out.append(rec)
            except Exception:
                continue
    except OSError:
        pass
    return out


def append_op(root: Path, op_type: str, payload: dict | None = None) -> dict:
    with _lock:
        st = load(root)
        op = {
            "op_id": new_mid(),
            "type": str(op_type),
            "base_rev": int(st.get("history_rev") or 0),
            "payload": payload or {},
        }
        if not _append_outbox_unique(root, op):
            raise OSError("待发送同步队列写入失败")
        return op


def _append_outbox_unique(root: Path, op: dict) -> bool:
    """原子重写 outbox，并按 op_id 去重。

    崩溃恢复可能把同一 COMMITTED 事务重放两次；同 op_id 写两遍虽然服务端幂等，
    但会让客户端多传一轮，因此入口就消除重复。
    """
    op_id = str((op or {}).get("op_id") or "")
    if not op_id:
        return False
    current = load_outbox(root)
    if any(str(x.get("op_id")) == op_id for x in current):
        return True
    current.append(op)
    p = outbox_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp." + uuid.uuid4().hex)
        tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in current),
                       encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError as e:
        logger.warning("同步 outbox 写入失败 %s: %s", p, e)
        return False


def clear_outbox(root: Path, op_ids) -> bool:
    ids = {str(x) for x in (op_ids or [])}
    if not ids:
        return True
    # append_op/commit_txn 与同步确认都可能并发。若这里无锁地读-改-写，
    # 会覆盖掉确认期间新追加的 op，表现为“本地明明点过收藏，重启后消失”。
    with _lock:
        kept = [op for op in load_outbox(root) if str(op.get("op_id")) not in ids]
        p = outbox_path(root)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp." + uuid.uuid4().hex)
            tmp.write_text("".join(json.dumps(op, ensure_ascii=False) + "\n" for op in kept),
                           encoding="utf-8")
            tmp.replace(p)
            return True
        except OSError:
            return False


def merge_server_state(root: Path, remote: dict | None) -> dict:
    """把服务器权威 epoch/墓碑合并到本机；只增不回退。"""
    if not isinstance(remote, dict):
        return load(root)
    with _lock:
        st = load(root)
        try:
            st["history_epoch"] = max(int(st.get("history_epoch") or 0),
                                      int(remote.get("history_epoch") or 0))
            st["history_rev"] = max(int(st.get("history_rev") or 0),
                                    int(remote.get("history_rev") or 0))
            st["memory_rev"] = max(int(st.get("memory_rev") or 0),
                                   int(remote.get("memory_rev") or 0))
        except (TypeError, ValueError):
            pass
        tombs = set(str(x) for x in (st.get("tombstones") or []))
        tombs.update(str(x) for x in (remote.get("tombstones") or []))
        st["tombstones"] = sorted(tombs)[-20000:]
        fav_tombs = set(str(x) for x in (st.get("favorite_tombstones") or []))
        fav_tombs.update(str(x) for x in (remote.get("favorite_tombstones") or []))
        st["favorite_tombstones"] = sorted(fav_tombs)[-5000:]
        if not save(root, st):
            raise OSError("同步状态写入失败")
        return st


def state_path(root: Path) -> Path:
    return Path(root) / _STATE_REL


def default_state() -> dict:
    return {
        "schema": SCHEMA,
        "rev": 0,
        "history_epoch": 0,
        "history_rev": 0,
        "memory_rev": 0,
        "rest_base_rev": 0,
        "tombstones": [],
        "favorite_tombstones": [],
    }


def load(root: Path) -> dict:
    p = state_path(root)
    out = default_state()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in out:
                    out[k] = v
    except Exception:
        pass
    try:
        if not isinstance(out.get("favorite_tombstones"), list):
            out["favorite_tombstones"] = []
        out["history_epoch"] = int(out.get("history_epoch") or 0)
        out["history_rev"] = int(out.get("history_rev") or 0)
        out["memory_rev"] = int(out.get("memory_rev") or 0)
        out["rest_base_rev"] = int(out.get("rest_base_rev") or 0)
        if not isinstance(out.get("tombstones"), list):
            out["tombstones"] = []
    except (TypeError, ValueError):
        out = default_state()
    return out


def save(root: Path, state: dict) -> bool:
    p = state_path(root)
    return _atomic_json(p, state)


def current_epoch(root: Path) -> int:
    return int(load(root).get("history_epoch") or 0)


def bump_history(root: Path) -> dict:
    with _lock:
        st = load(root)
        st["history_rev"] = int(st.get("history_rev") or 0) + 1
        save(root, st)
        return st


def clear_history(root: Path) -> dict:
    """清理历史：进入新代际；旧 epoch 的消息不再有效。"""
    with _lock:
        st = load(root)
        st["history_epoch"] = int(st.get("history_epoch") or 0) + 1
        st["history_rev"] = int(st.get("history_rev") or 0) + 1
        st["rest_base_rev"] = 0
        st["tombstones"] = []
        if not save(root, st):
            raise OSError("清理历史的同步状态写入失败")
        return st


def record_rest(root: Path, base_rev: int) -> dict:
    with _lock:
        st = load(root)
        st["memory_rev"] = int(st.get("memory_rev") or 0) + 1
        st["rest_base_rev"] = int(base_rev or 0)
        save(root, st)
        return st


def _set_favorite_tombstone(root: Path, fav_id, keep: bool) -> bool:
    with _lock:
        st = load(root)
        ids = [str(x) for x in (st.get("favorite_tombstones") or [])]
        key = str(fav_id or "").strip()
        if key and not keep and key not in ids:
            ids.append(key)
        if key and keep:
            ids = [x for x in ids if x != key]
        st["favorite_tombstones"] = ids[-5000:]
        return save(root, st)


def add_favorite_tombstone(root: Path, fav_id, keep: bool = False) -> dict:
    _set_favorite_tombstone(root, fav_id, keep)
    return load(root)


def _set_tombstones(root: Path, mids) -> bool:
    with _lock:
        st = load(root)
        old = [str(x) for x in (st.get("tombstones") or [])]
        for m in mids or []:
            m = str(m or "").strip()
            if m and m not in old:
                old.append(m)
        st["tombstones"] = old[-20000:]
        return save(root, st)


def add_tombstones(root: Path, mids) -> dict:
    _set_tombstones(root, mids)
    return load(root)


def new_mid() -> str:
    return uuid.uuid4().hex


def message_mid(rec: dict) -> str:
    """消息稳定身份：新消息用 mid；旧消息按 seq + 内容 sha 生成 legacy id。"""
    if not isinstance(rec, dict):
        return ""
    mid = str(rec.get("mid") or "").strip()
    if mid:
        return mid
    try:
        seq = int(rec.get("seq"))
    except (TypeError, ValueError):
        seq = 0
    body = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"legacy:{seq}:{sha}"


def filter_favorites_text(text: str, state: dict | None = None) -> str:
    st = state or default_state()
    dead = set(str(x) for x in (st.get("favorite_tombstones") or []))
    if not dead:
        return text
    try:
        arr = json.loads(text or "[]")
    except Exception:
        return text
    if not isinstance(arr, list):
        return text
    kept = [x for x in arr
            if not (isinstance(x, dict) and str(x.get("id", "")) in dead)]
    return json.dumps(kept, ensure_ascii=False, indent=1)


def filter_conversation_text(text: str, state: dict | None = None) -> str:
    """过滤旧代际/已撤回的行。无状态或无需过滤时原样返回，保持字节稳定。"""
    st = state or default_state()
    epoch = int(st.get("history_epoch") or 0)
    tombstones = set(str(x) for x in (st.get("tombstones") or []))
    if epoch <= 0 and not tombstones:
        return text
    kept = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
        except Exception:
            kept.append(s)
            continue
        if not isinstance(rec, dict):
            kept.append(s)
            continue
        rec_epoch = int(rec.get("epoch") or 0)
        if rec_epoch < epoch:
            continue
        if message_mid(rec) in tombstones:
            continue
        kept.append(s)
    return "\n".join(kept) + ("\n" if kept else "")
