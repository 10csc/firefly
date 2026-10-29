# -*- coding: utf-8 -*-
"""A1 增量同步（阶段 2.2 自 routes_data 拆出）

服务器侧：GET /sync/manifest、POST /sync/import、POST /sync/export；
客户端编排：POST /sync/now（本地后端直连认证服务器：清单 → 对比 → 上传/拉取 → 本地落盘）。
同步范围与策略以 `modules.storage` 注册表为唯一真相源（见 modules.sync_engine）。
"""

import functools
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from modules import app_config as cfg
from modules import sync_state as sync_store
from modules.app_config import DEFAULT_MODE
from modules.multipart import parse_multipart
from modules.storage import atomic_write_bytes, atomic_write_text
from routes_common import _read_json
from routes_common import _is_server
from routes_common import _image_used_bytes
from routes_common import _image_quota_bytes
from routes_auth import _auth_server_base

from infra.sync.restore import _IMPORT_MAX_BYTES

logger = logging.getLogger(__name__)
_SYNC_EXPORT_BATCH = 200


def _sync_guard(operation: str):
    """同步端点的最后一道进程内防线：异常必须变成带错误号的 JSON，不能断连。

    `sync.js` 过去把所有 fetch 解析/断连都显示成“网络错误”，导致真实的后端
    `AttributeError` 被误诊成网络故障。trace_id 同时进日志和响应，便于用户直接把
    错误号发回来；这里不吞异常，只把它变成可诊断、可重试的失败。
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(h):
            try:
                return fn(h)
            except Exception as exc:
                trace_id = uuid.uuid4().hex[:16]
                logger.exception("[sync:%s] trace_id=%s", operation, trace_id)
                try:
                    h._json({
                        "ok": False,
                        "code": "SYNC_INTERNAL_ERROR",
                        "error": "同步服务内部错误，请重试",
                        "trace_id": trace_id,
                    }, 500)
                except Exception:
                    logger.exception("[sync:%s] 错误响应发送失败 trace_id=%s",
                                     operation, trace_id)
        return wrapped
    return decorate


def _requested_query_mode(h) -> str:
    qs = parse_qs(urlparse(h.path).query)
    return str(qs.get("mode", [DEFAULT_MODE])[0] or "")


def _addressable_mode(h, requested, *, code: str = "SYNC_MODE_NOT_ADDRESSABLE"):
    """Return an addressable mode, or send 400 before the caller touches data."""
    mode = str(requested or "").strip()
    if mode in cfg.backup_pack_ids():
        return mode
    logger.warning("同步拒绝不可寻址 mode=%r（可用=%s）",
                   mode, sorted(cfg.backup_pack_ids()))
    h._json({
        "ok": False,
        "code": code,
        "error": f"角色包 {mode or '<empty>'} 在当前服务端不可寻址",
        "requested_mode": mode,
    }, 400)
    return None


def _response_mode_error(response, expected: str) -> str:
    if not isinstance(response, dict):
        return "服务端返回了无法识别的同步响应"
    actual = str(response.get("mode") or "")
    if actual != expected:
        return (f"服务端把 {expected} 解析成 {actual or '<missing>'}，"
                f"已停止以免写坏数据")
    return ""


# ══ A1 增量同步（文件级双向：三端同一套 Python，按用户上下文隔离）══
# 协议：客户端（本地后端）直连认证服务器——
#   GET  /sync/manifest            ← 服务器侧清单（该用户 {mode} 目录）
#   POST /sync/import  (multipart) → 服务器合并写入（append 行合并 / 文档新者胜，冲突备份）
#   POST /sync/export  {"files"}   → 服务器侧差异文件 zip（客户端拉回合并）
#   POST /sync/now                 → 本地后端编排：manifest → 对比 → 上传/拉取 → 本地落盘
# images/ 压缩图是正式消息数据：blob 整份双向传输（不解码不合并，按首段目录判定）；
# stickers/ 表情包二进制仍本地策略不进清单（只同步文字元数据，见 sync_engine）。


def _sync_mode_root(mode: str):
    """同步数据根：服务器版=该用户目录（server_app 注入上下文）；本地版=USER_DIR/{mode}。

    3.8：白名单以 `cfg.backup_pack_ids()`（MODES ∪ packs.json 含 archived）为准。
    归档包不在 MODES 里，而 `cfg.mode_root()` 对白名单外的 mode 会**静默回退默认包** ——
    归档包若走回退，就会把默认包的数据当成自己的数据同步，双向写坏两边。
    白名单内的非 MODES 包直接定位 `{用户目录}/{id}`；白名单外必须失败关闭。
    """
    m = str(mode or "").strip()
    if m not in cfg.backup_pack_ids():
        raise ValueError(f"同步 mode 不可寻址: {m or '<empty>'}")
    if cfg.valid_mode(m):
        return cfg.mode_root(m), m
    return Path(cfg._user_ctx_dir() or cfg.USER_DIR) / m, m


@_sync_guard("manifest")
def sync_manifest(h):
    """服务器侧清单（A1）。GET /sync/manifest?mode=story  → {files: {path: {size,sha256,mtime}}}。"""
    from modules.sync_engine import scan_manifest
    from modules import sync_server_state
    m = _addressable_mode(h, _requested_query_mode(h))
    if m is None:
        return
    root, m = _sync_mode_root(m)
    with sync_server_state.mode_lock(root):
        state = sync_server_state.load(root)
        h._json({"ok": True, "mode": m, "state_rev": int(state.get("rev") or 0),
                 "files": scan_manifest(root)})


@_sync_guard("state")
def sync_state(h):
    """服务器权威同步状态（SPEC §6.1/§6.2）：rev、epoch、墓碑、memory rev。"""
    from modules import sync_server_state
    m = _addressable_mode(h, _requested_query_mode(h))
    if m is None:
        return
    root, m = _sync_mode_root(m)
    with sync_server_state.mode_lock(root):
        state = sync_server_state.load(root)
        h._json({"ok": True, "mode": m, "state": state,
                 "state_rev": int(state.get("rev") or 0)})


@_sync_guard("ops")
def sync_ops(h):
    """服务器应用客户端 ops（clear/retract/rest）；op_id 幂等。"""
    from modules import sync_server_state
    body = _read_json(h) or {}
    mode = _addressable_mode(h, body.get("mode", DEFAULT_MODE))
    if mode is None:
        return
    ops = body.get("ops") or []
    if not isinstance(ops, list):
        h._json({"ok": False, "error": "ops 必须为列表"})
        return
    if len(ops) > 500:
        h._json({"ok": False, "code": "SYNC_OPS_TOO_MANY",
                 "error": "单批最多 500 个操作"}, 413)
        return
    root, m = _sync_mode_root(mode)
    result = sync_server_state.apply_ops(root, ops)
    status = 200 if result.get("ok") else 409
    h._json({"mode": m, **result}, status)


@_sync_guard("import")
def sync_import(h):
    """服务器侧合并写入（A1）。POST /sync/import multipart：
    fields: mode + mtimes(json {path: mtime})；parts: 每个待写文件一个 part（名字 = 相对路径）。
    裁决：jsonl → 行合并；favorites.json → 按 seq/f-id 并集；文档类 → mtime 新者胜（服务器更新则跳过）。
    冲突/覆盖前备份到 {mode}/.sync_backups/（客户端同理）。"""
    from modules import sync_server_state
    content_type = h.headers.get("Content-Type", "") or ""
    if not content_type.startswith("multipart/form-data"):
        h._json({"ok": False, "code": "SYNC_MULTIPART_INVALID",
                 "error": "Content-Type 必须为 multipart/form-data"}, 400)
        return
    try:
        content_length = int(h.headers.get("Content-Length", "0") or "0")
    except (TypeError, ValueError):
        content_length = 0
    if content_length <= 0:
        h._json({"ok": False, "code": "SYNC_MULTIPART_INVALID",
                 "error": "multipart 上传长度无效"}, 400)
        return
    if content_length > _IMPORT_MAX_BYTES:
        h._json({"ok": False, "code": "SYNC_IMPORT_TOO_LARGE",
                 "error": "同步上传超过大小上限"}, 413)
        return
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    if not fields or not files:
        h._json({"ok": False, "code": "SYNC_MULTIPART_INVALID",
                 "error": "multipart 缺少字段或文件内容"}, 400)
        return
    mode = _addressable_mode(h, fields.get("mode", DEFAULT_MODE))
    if mode is None:
        return
    root, m = _sync_mode_root(mode)
    legacy_client = (h.headers.get("X-Firefly-Sync-Protocol", "") or "") != "2"
    try:
        mtimes = json.loads(fields.get("mtimes", "{}") or "{}")
        if not isinstance(mtimes, dict):
            mtimes = {}
    except Exception:
        mtimes = {}
    try:
        base_sha = json.loads(fields.get("base_sha", "{}") or "{}")
        if not isinstance(base_sha, dict):
            base_sha = {}
    except Exception:
        base_sha = {}
    expected_rev = fields.get("state_rev")
    with sync_server_state.mode_lock(root):
        srv_state = sync_server_state.load(root)
        if expected_rev not in (None, ""):
            try:
                expected_rev = int(expected_rev)
            except (TypeError, ValueError):
                h._json({"ok": False, "code": "SYNC_STATE_REV_INVALID",
                         "error": "state_rev 必须为整数"}, 400)
                return
            if expected_rev != int(srv_state.get("rev") or 0):
                h._json({"ok": False, "code": "SYNC_STATE_CHANGED",
                         "error": "服务器同步状态已变化，请重试"}, 409)
                return
        result = _import_files_locked(
            root, files, srv_state, mtimes, base_sha, legacy_client)
    h._json({"ok": True, "mode": m, **result})


def _import_files_locked(root: Path, files: dict, srv_state: dict, mtimes: dict,
                         base_sha: dict, legacy_client: bool) -> dict:
    from modules.sync_engine import (in_sync_scope, merge_jsonl, merge_favorites,
                                     backup_file)
    cleared_paths = ("data/memory.md", "data/.memory_index",
                     "journal/手账.md", "data/pipeline.jsonl", "data/proactive_log.jsonl")
    backups_dir = root / ".sync_backups"
    applied, skipped, conflicts = [], [], []
    for name, info in files.items():
        try:
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or not in_sync_scope(rel, root):
                skipped.append(f"{name}（非法路径/超范围）")
                continue
            if legacy_client and int(srv_state.get("history_epoch") or 0) > 0 \
                    and rel.as_posix() in cleared_paths:
                skipped.append(f"{name}（旧客户端不得覆盖新代际）")
                continue
            data = info["data"]
            if not isinstance(data, bytes):
                data = bytes(data)
            fp = root / rel
            if rel.parts[0] == "images":
                if _image_used_bytes() + len(data) > _image_quota_bytes():
                    skipped.append(f"{name}（图片空间已满）")
                    continue
                if not atomic_write_bytes(fp, data):
                    skipped.append(f"{name}（写入失败）")
                    continue
                applied.append(rel.as_posix())
                continue
            # 文档类 CAS：空串只允许匹配“服务器无此文件”，不再把缺失误判为放行。
            if rel.suffix != ".jsonl" and rel.name != "favorites.json" \
                    and rel.as_posix() in base_sha:
                want = str(base_sha.get(rel.as_posix()) or "")
                cur = ""
                if fp.is_file():
                    digest = hashlib.sha256()
                    with fp.open("rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            digest.update(chunk)
                    cur = digest.hexdigest()
                if want != cur:
                    conflicts.append(f"{name}（base_sha 不符，拒绝覆盖）")
                    continue
            local_text = fp.read_text(encoding="utf-8") if fp.exists() else ""
            remote_text = data.decode("utf-8", errors="replace")
            st = fp.stat() if fp.exists() else None
            remote_mtime = float(mtimes.get(rel.as_posix(), time.time()) or time.time())
            if rel.suffix == ".jsonl":
                merge_state = srv_state
                if not rel.as_posix().endswith("data/conversation.jsonl"):
                    merge_state = None
                merged, cf = merge_jsonl(local_text, remote_text, state=merge_state)
                if cf:
                    backup_file(fp, backups_dir, "conflict")
                if merged != local_text:
                    if not atomic_write_text(fp, merged):
                        skipped.append(f"{name}（写入失败）")
                        continue
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
                conflicts.extend(cf)
            elif rel.name == "favorites.json":
                merged, cf = merge_favorites(local_text, remote_text, state=srv_state)
                if merged != local_text:
                    if not atomic_write_text(fp, merged):
                        skipped.append(f"{name}（写入失败）")
                        continue
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
                conflicts.extend(cf)
            else:
                if st and remote_mtime < st.st_mtime:
                    skipped.append(rel.as_posix() + "（服务器更新，客户端将拉取）")
                elif remote_text != local_text:
                    if st:
                        backup_file(fp, backups_dir, "pre")
                    if not atomic_write_text(fp, remote_text):
                        skipped.append(f"{name}（写入失败）")
                        continue
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
        except Exception as e:
            logger.warning("同步导入失败 %s: %s", name, e)
            skipped.append(f"{name}（{e}）")
    return {"applied": applied, "skipped": skipped, "conflicts": len(conflicts)}


@_sync_guard("export")
def sync_export(h):
    """服务器侧差异文件 zip（A1）。POST /sync/export {"mode","files":[...]} → zip 流。"""
    import io
    import zipfile
    from modules import sync_server_state
    from modules.sync_engine import in_sync_scope
    body = _read_json(h)
    mode = _addressable_mode(h, (body or {}).get("mode", DEFAULT_MODE))
    if mode is None:
        return
    root, _ = _sync_mode_root(mode)
    files = body.get("files") or []
    if not isinstance(files, list):
        h._json({"ok": False, "error": "files 必须为列表"}); return
    if len(files) > 200:
        h._json({"ok": False, "code": "SYNC_EXPORT_TOO_MANY_FILES",
                 "error": "单次最多拉取 200 个文件，请分批请求"}, 413)
        return
    with sync_server_state.mode_lock(root):
        state = sync_server_state.load(root)
        expected_rev = body.get("state_rev")
        if expected_rev is not None:
            try:
                expected_rev = int(expected_rev)
            except (TypeError, ValueError):
                h._json({"ok": False, "code": "SYNC_STATE_REV_INVALID",
                         "error": "state_rev 必须为整数"}, 400)
                return
            if expected_rev != int(state.get("rev") or 0):
                h._json({"ok": False, "code": "SYNC_STATE_CHANGED",
                         "error": "服务器同步状态已变化，请重试"}, 409)
                return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in files:
                try:
                    rel = Path(str(name))
                    if rel.is_absolute() or ".." in rel.parts or not in_sync_scope(rel, root):
                        continue
                    fp = root / rel
                    if fp.is_file():
                        zf.write(fp, rel.as_posix())
                except OSError:
                    continue
        data = buf.getvalue()
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", 'attachment; filename="sync-diff.zip"')
    h.send_header("X-Firefly-Mode", str(mode))
    h.send_header("X-Firefly-State-Rev", str(int(state.get("rev") or 0)))
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


@_sync_guard("now")
def sync_now(h):
    """本地端编排（A1）：本地后端直连认证服务器完成一轮双向同步。
    mode=all/缺省 = 全部角色包逐包同步（全包化）；mode=单包 = 仅该包（兼容）。
    范围：对话/记忆/手账/设定/收藏/pipeline 文字类 + images/ 压缩图（blob 整份，
    上传 read_bytes / 拉取 write_bytes，不解码不合并）；stickers/ 表情包二进制不进清单。"""
    if _is_server():
        h._json({"error": "服务器版数据已在云端，无需本端点"}, 403); return
    import io
    import urllib.request
    import zipfile
    from modules.auth_store import get_token
    from modules.sync_engine import (scan_manifest, plan_sync, merge_jsonl,
                                     merge_favorites, backup_file)
    body = _read_json(h)
    mode = str(body.get("mode", "all") or "all")
    if mode != "all":
        mode = _addressable_mode(h, mode)
        if mode is None:
            return
    token = get_token()
    if not token:
        h._json({"ok": False, "error": "未登录（请先在首页登录账号）"}); return
    base = _auth_server_base()

    def call(path: str, payload: dict | None = None, binary: bool = False):
        headers = {"Authorization": f"Bearer {token}",
                   "X-Firefly-Sync-Protocol": "2"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            if binary:
                return raw, dict(r.headers)
        return json.loads(raw.decode("utf-8"))

    # 3.8：白名单切 `cfg.backup_pack_ids()`（MODES ∪ 清单含 archived）—— 归档包的数据仍要同步
    _allow = cfg.backup_pack_ids()
    modes = list(_allow) if mode == "all" else [mode]
    reports = {"uploaded": [], "downloaded": [], "merged": [], "skipped": [], "conflicts": 0, "modes": modes}
    for _m in modes:
        err = _sync_one_mode(call, _m, reports)
        if err:
            h._json({"ok": False, "error": f"[{_m}] {err}"}); return
    h._json({"ok": True, **reports})


def _sync_one_mode(call, mode: str, reports: dict) -> str | None:
    """同一本地模式的完整同步回合串行执行，避免线程/定时器并发叠写。"""
    root, _ = _sync_mode_root(mode)
    with sync_store.mode_lock(root):
        return _sync_one_mode_body(call, mode, reports)


def _sync_one_mode_body(call, mode: str, reports: dict) -> str | None:
    """单模式双向同步主体（sync_now 按包遍历调用）。出错返回错误串，成功返回 None。"""
    import io
    import urllib.error
    import urllib.request
    import zipfile
    from modules.auth_store import get_token
    from modules.sync_engine import (scan_manifest, plan_sync, merge_jsonl,
                                     merge_favorites, backup_file)
    root, m = _sync_mode_root(mode)
    try:
        recovered = sync_store.recover_txn(root)   # 未完成事务先恢复/提交，再允许上传
    except Exception as e:
        logger.exception("同步前恢复本地事务异常 mode=%s: %s", m, e)
        return "未完成同步事务恢复异常，已暂缓上传"
    if not recovered:
        return "未完成同步事务恢复失败，已保留现场并暂缓上传"

    # 0. 服务器权威状态 + 待发送 ops。必须先于文件同步，避免本地已清空却把旧文件拉回。
    try:
        rstate = call(f"/sync/state?mode={m}")
    except Exception as e:
        return f"读取服务器同步状态失败：{e}"
    mode_error = _response_mode_error(rstate, m)
    if mode_error:
        return mode_error
    remote_state = (rstate or {}).get("state")
    if not isinstance(remote_state, dict):
        return "服务器同步状态响应缺少 state 对象"
    try:
        sync_store.apply_remote_state(root, remote_state)
    except Exception as e:
        return f"应用服务器同步状态失败：{e}"
    pending_ops = sync_store.load_outbox(root)
    if pending_ops:
        try:
            rp = call("/sync/ops", {"mode": m, "ops": pending_ops})
            mode_error = _response_mode_error(rp, m)
            if mode_error:
                return mode_error
            if not (rp or {}).get("ok"):
                codes = []
                for item in (rp or {}).get("rejected") or []:
                    code = str(item.get("code") or "").strip()
                    if code and code not in codes:
                        codes.append(code)
                detail = "/".join(codes) or str((rp or {}).get("code") or "SYNC_OPS_REJECTED")
                return f"待发送变更被拒绝（{detail}），已暂缓文件同步"
            remote_state = (rp or {}).get("state")
            if not isinstance(remote_state, dict):
                return "服务器同步操作响应缺少 state 对象"
            try:
                sync_store.apply_remote_state(root, remote_state)
            except Exception as e:
                return f"已收到操作确认，但本地同步状态保存失败：{e}"
            if not sync_store.clear_outbox(root, [o.get("op_id") for o in pending_ops]):
                return "同步确认已收到，但本地待发送队列清理失败，请重试"
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err = json.loads(e.read().decode("utf-8"))
                detail = str(err.get("code") or err.get("error") or "")
                for item in err.get("rejected") or []:
                    code = str(item.get("code") or "").strip()
                    if code and code not in detail:
                        detail += ("/" if detail else "") + code
            except Exception:
                pass
            return f"待发送变更被拒绝（{detail or ('HTTP ' + str(e.code))}），已暂缓文件同步"
        except Exception as e:
            return f"上传待发送变更失败：{e}"

    try:
        # 1. 服务器清单
        man = call(f"/sync/manifest?mode={m}")
        mode_error = _response_mode_error(man, m)
        if mode_error:
            return mode_error
        remote = (man or {}).get("files", {}) or {}
        expected_state_rev = (man or {}).get("state_rev")
    except Exception as e:
        return f"连接服务器失败: {e}"

    local = scan_manifest(root)
    plan = plan_sync(local, remote)

    # 2. 上传（本地独有 + 需合并的文件；文档类新者胜在服务器侧裁决）
    upload_paths = plan["to_upload"] + plan["to_merge"]
    if upload_paths:
        # 构建 multipart: 每个文件一个 part（名字=相对路径）
        boundary = "ffsync" + time.strftime("%H%M%S") + "x"
        parts = []
        mtimes = {}
        for name in upload_paths:
            fp = root / name
            if not fp.is_file():
                continue
            try:
                data = fp.read_bytes()
            except OSError:
                continue
            mtimes[name] = round(fp.stat().st_mtime, 3)
            parts.append(
                b"--" + boundary.encode() + b"\r\n"
                + f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n'.encode("utf-8")
                + b"Content-Type: application/octet-stream\r\n\r\n" + data + b"\r\n")
        body_m = (b"--" + boundary.encode() + b"\r\n"
                  + b'Content-Disposition: form-data; name="mode"\r\n\r\n' + m.encode("utf-8") + b"\r\n")
        body_mt = (b"--" + boundary.encode() + b"\r\n"
                   + b'Content-Disposition: form-data; name="mtimes"\r\n\r\n'
                   + json.dumps(mtimes).encode("utf-8") + b"\r\n")
        # 文档类写前 CAS：只对非 jsonl/favorites 发 base_sha；旧服务器忽略。
        base_sha = {}
        for _n in upload_paths:
            if _n.endswith(".jsonl") or _n.endswith("favorites.json"):
                continue
            base_sha[_n] = str(((remote.get(_n) or {}).get("sha256")) or "")
        body_bs = (b"--" + boundary.encode() + b"\r\n"
                   + b'Content-Disposition: form-data; name="base_sha"\r\n\r\n'
                   + json.dumps(base_sha).encode("utf-8") + b"\r\n")
        body_rev = (b"--" + boundary.encode() + b"\r\n"
                    + b'Content-Disposition: form-data; name="state_rev"\r\n\r\n'
                    + str(expected_state_rev if expected_state_rev is not None else "").encode("ascii")
                    + b"\r\n")
        body_mp = b"".join(parts) + b"--" + boundary.encode() + b"--\r\n"
        req = urllib.request.Request(_auth_server_base() + "/sync/import",
                                     data=body_m + body_mt + body_bs + body_rev + body_mp,
                                     headers={"Authorization": f"Bearer {get_token()}",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}",
                                              "X-Firefly-Sync-Protocol": "2"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                rp = json.loads(r.read().decode("utf-8"))
            mode_error = _response_mode_error(rp, m)
            if mode_error:
                return mode_error
            reports["uploaded"].extend(rp.get("applied", []))
            reports["conflicts"] += int(rp.get("conflicts", 0))
            reports["skipped"].extend(rp.get("skipped", []))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err = json.loads(e.read().decode("utf-8"))
                detail = str(err.get("code") or err.get("error") or "")
            except Exception:
                pass
            return f"上传失败（{e.code}{(':' + detail) if detail else ''}）"

    # 服务器合并 jsonl/favorites 后，文件 sha 与上传前清单不同。拉取必须基于
    # “导入完成后的新清单”，否则会把合法合并误判成哈希不一致而整轮失败。
    if upload_paths:
        try:
            refreshed = call(f"/sync/manifest?mode={m}")
            mode_error = _response_mode_error(refreshed, m)
            if mode_error:
                return mode_error
            remote = (refreshed or {}).get("files", {}) or {}
            expected_state_rev = (refreshed or {}).get("state_rev")
        except Exception as e:
            return f"上传后刷新服务器清单失败: {e}"

    # 3. 拉取（服务器独有 + 服务器更新的合并项）→ 本地合并落盘
    local_after = scan_manifest(root)
    need = [p for p in plan["to_download"]]
    # 服务器侧判定"服务器更新"而跳过的上传项 → 拉回
    for p in plan["to_merge"]:
        if (local_after.get(p, {}).get("sha256") or "") != (remote.get(p, {}).get("sha256") or ""):
            need.append(p)
    need = sorted(set(need))
    backups_dir = root / ".sync_backups"
    for start in range(0, len(need), _SYNC_EXPORT_BATCH):
        batch = need[start:start + _SYNC_EXPORT_BATCH]
        try:
            zip_raw, zip_headers = call("/sync/export", {
                "mode": m,
                "files": batch,
                "state_rev": expected_state_rev,
            }, binary=True)
        except Exception as e:
            return f"拉取失败: {e}"
        remote_mode = str(zip_headers.get("X-Firefly-Mode") or "")
        if remote_mode != m:
            return (f"服务端把 {m} 解析成 {remote_mode or '<missing>'}，"
                    f"已停止拉取以免写坏数据")
        try:
            zf_obj = zipfile.ZipFile(io.BytesIO(zip_raw))
        except zipfile.BadZipFile as e:
            return f"拉取失败: 服务器返回的同步包损坏（{e}）"
        with zf_obj as zf:
            for info in zf.infolist():
                name = info.filename
                try:
                    rel = Path(name)
                    if rel.is_absolute() or ".." in rel.parts:
                        continue
                    raw = zf.read(info)
                    remote_meta = remote.get(rel.as_posix()) or {}
                    remote_sha = str(remote_meta.get("sha256") or "")
                    if remote_sha and hashlib.sha256(raw).hexdigest() != remote_sha:
                        return f"拉取失败: {rel.as_posix()} 与清单哈希不一致，请重试"
                    fp = root / rel
                    if rel.parts[0] == "images":
                        # blob 不合并。若拉取窗口内本地文件被改，保留本地并备份远端，
                        # 不能像旧逻辑那样静默覆盖并发写入。
                        if fp.exists():
                            local_sha = hashlib.sha256(fp.read_bytes()).hexdigest()
                            if local_sha != remote_sha:
                                backup_file(fp, backups_dir, "conflict")
                                reports["conflicts"] += 1
                                continue
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        if not atomic_write_bytes(fp, raw):
                            return f"拉取失败: {rel.as_posix()} 写入失败"
                        reports["downloaded"].append(name)
                        continue
                    remote_text = raw.decode("utf-8", errors="replace")
                    local_text = fp.read_text(encoding="utf-8") if fp.exists() else ""
                    if rel.suffix == ".jsonl":
                        st = None
                        if rel.as_posix().endswith("data/conversation.jsonl"):
                            st = sync_store.load(root)
                        merged, cf = merge_jsonl(local_text, remote_text, state=st)
                        if cf:
                            backup_file(fp, backups_dir, "conflict")
                        reports["conflicts"] += len(cf)
                        if merged != local_text:
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            if not atomic_write_text(fp, merged):
                                return f"拉取失败: {rel.as_posix()} 写入失败"
                            reports["merged"].append(name)
                    elif rel.name == "favorites.json":
                        merged, cf = merge_favorites(
                            local_text, remote_text, state=sync_store.load(root))
                        if merged != local_text:
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            if not atomic_write_text(fp, merged):
                                return f"拉取失败: {rel.as_posix()} 写入失败"
                            reports["merged"].append(name)
                    else:
                        # 文档类拉取使用清单时抓到的本地 sha 做 CAS，不再用 mtime：
                        # 拉取窗口内本地被人/其他进程改过 → 备份冲突并保留本地。
                        expected_local_sha = str((local.get(str(name)) or {}).get("sha256") or "")
                        current_local_sha = ""
                        if fp.exists():
                            current_local_sha = hashlib.sha256(fp.read_bytes()).hexdigest()
                        if current_local_sha != expected_local_sha:
                            if fp.exists():
                                backup_file(fp, backups_dir, "conflict")
                            reports["conflicts"] += 1
                            reports["skipped"].append(name + "（拉取窗口内本地已变化，保留）")
                            continue
                        if remote_text != local_text:
                            if fp.exists():
                                backup_file(fp, backups_dir, "pre")
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            if not atomic_write_text(fp, remote_text):
                                return f"拉取失败: {rel.as_posix()} 写入失败"
                            reports["downloaded"].append(name)
                except Exception as e:
                    logger.warning("同步拉取落盘失败 %s: %s", name, e)
                    reports["skipped"].append(f"{name}（{e}）")
    return None
