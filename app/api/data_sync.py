# -*- coding: utf-8 -*-
"""A1 增量同步（阶段 2.2 自 routes_data 拆出）

服务器侧：GET /sync/manifest、POST /sync/import、POST /sync/export；
客户端编排：POST /sync/now（本地后端直连认证服务器：清单 → 对比 → 上传/拉取 → 本地落盘）。
同步范围与策略以 `modules.storage` 注册表为唯一真相源（见 modules.sync_engine）。
"""

import json
import logging
import time
from pathlib import Path

from modules import app_config as cfg
from modules.app_config import DEFAULT_MODE
from modules.multipart import parse_multipart
from routes_common import _read_json
from routes_common import _body_mode
from routes_common import _query_mode
from routes_common import _is_server
from routes_common import _image_used_bytes
from routes_common import _image_quota_bytes
from routes_auth import _auth_server_base

from infra.sync.restore import _IMPORT_MAX_BYTES

logger = logging.getLogger(__name__)


# ══ A1 增量同步（文件级双向：三端同一套 Python，按用户上下文隔离）══
# 协议：客户端（本地后端）直连认证服务器——
#   GET  /sync/manifest            ← 服务器侧清单（该用户 {mode} 目录）
#   POST /sync/import  (multipart) → 服务器合并写入（append 行合并 / 文档新者胜，冲突备份）
#   POST /sync/export  {"files"}   → 服务器侧差异文件 zip（客户端拉回合并）
#   POST /sync/now                 → 本地后端编排：manifest → 对比 → 上传/拉取 → 本地落盘
# images/ 压缩图是正式消息数据：blob 整份双向传输（不解码不合并，按首段目录判定）；
# stickers/ 表情包二进制仍本地策略不进清单（只同步文字元数据，见 sync_engine）。


def _sync_mode_root(mode: str):
    """同步数据根：服务器版=该用户目录（server_app 注入上下文）；本地版=USER_DIR/{mode}。"""
    m = mode if mode in cfg.MODES else DEFAULT_MODE
    return cfg.mode_root(m), m


def sync_manifest(h):
    """服务器侧清单（A1）。GET /sync/manifest?mode=story  → {files: {path: {size,sha256,mtime}}}。"""
    from modules.sync_engine import scan_manifest
    root, m = _sync_mode_root(_query_mode(h))
    h._json({"ok": True, "mode": m, "files": scan_manifest(root)})


def sync_import(h):
    """服务器侧合并写入（A1）。POST /sync/import multipart：
    fields: mode + mtimes(json {path: mtime})；parts: 每个待写文件一个 part（名字 = 相对路径）。
    裁决：jsonl → 行合并；favorites.json → 按 seq/f-id 并集；文档类 → mtime 新者胜（服务器更新则跳过）。
    冲突/覆盖前备份到 {mode}/.sync_backups/（客户端同理）。"""
    from modules.sync_engine import (in_sync_scope, merge_jsonl, merge_favorites,
                                     apply_merge_file, backup_file)
    root, m = _sync_mode_root(DEFAULT_MODE)
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    root, m = _sync_mode_root(mode)
    try:
        mtimes = json.loads(fields.get("mtimes", "{}") or "{}")
        if not isinstance(mtimes, dict):
            mtimes = {}
    except Exception:
        mtimes = {}
    backups_dir = root / ".sync_backups"
    applied, skipped, conflicts = [], [], []
    for name, info in files.items():
        try:
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or not in_sync_scope(rel, root):
                skipped.append(f"{name}（非法路径/超范围）")
                continue
            data = info["data"]
            if not isinstance(data, bytes):
                data = bytes(data)
            fp = root / rel
            if rel.parts[0] == "images":
                # 图片二进制（blob，正式消息数据）：跳过 utf-8 解码与合并，
                # 配额检查（同 upload_image）后原子落盘
                if _image_used_bytes() + len(data) > _image_quota_bytes():
                    skipped.append(f"{name}（图片空间已满）")
                    continue
                from modules.storage import atomic_write_bytes
                if atomic_write_bytes(fp, data):
                    applied.append(rel.as_posix())
                else:
                    skipped.append(f"{name}（写入失败）")
                continue
            local_text = fp.read_text(encoding="utf-8") if fp.exists() else ""
            remote_text = data.decode("utf-8", errors="replace")
            st = fp.stat() if fp.exists() else None
            remote_mtime = float(mtimes.get(rel.as_posix(), time.time()) or time.time())
            if rel.suffix == ".jsonl":
                merged, cf = merge_jsonl(local_text, remote_text)
                if cf:
                    backup_file(fp, backups_dir, "conflict")
                if merged != local_text:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(merged, encoding="utf-8")
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
                conflicts.extend(cf)
            elif rel.name == "favorites.json":
                merged, cf = merge_favorites(local_text, remote_text)
                if merged != local_text:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(merged, encoding="utf-8")
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
                conflicts.extend(cf)
            else:
                # 文档类：新者胜。服务器已有且 mtime 更新 → 跳过（客户端应 pull 服务器版）
                if st and remote_mtime < st.st_mtime:
                    skipped.append(rel.as_posix() + "（服务器更新，客户端将拉取）")
                elif remote_text != local_text:
                    if st:
                        backup_file(fp, backups_dir, "pre")
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(remote_text, encoding="utf-8")
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
        except Exception as e:
            logger.warning("同步导入失败 %s: %s", name, e)
            skipped.append(f"{name}（{e}）")
    h._json({"ok": True, "applied": applied, "skipped": skipped,
             "conflicts": len(conflicts)})


def sync_export(h):
    """服务器侧差异文件 zip（A1）。POST /sync/export {"mode","files":[...]} → zip 流。"""
    import io
    import zipfile
    from modules.sync_engine import in_sync_scope
    body = _read_json(h)
    mode = _body_mode(body)
    root, _ = _sync_mode_root(mode)
    files = body.get("files") or []
    if not isinstance(files, list):
        h._json({"ok": False, "error": "files 必须为列表"}); return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files[:200]:   # 上限防超大响应
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
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


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
    token = get_token()
    if not token:
        h._json({"ok": False, "error": "未登录（请先在首页登录账号）"}); return
    base = _auth_server_base()

    def call(path: str, payload: dict | None = None, binary: bool = False):
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        return raw if binary else json.loads(raw.decode("utf-8"))

    modes = list(cfg.MODES) if mode in ("all", "") else ([mode] if mode in cfg.MODES else [DEFAULT_MODE])
    reports = {"uploaded": [], "downloaded": [], "merged": [], "skipped": [], "conflicts": 0, "modes": modes}
    for _m in modes:
        err = _sync_one_mode(call, _m, reports)
        if err:
            h._json({"ok": False, "error": f"[{_m}] {err}"}); return
    h._json({"ok": True, **reports})


def _sync_one_mode(call, mode: str, reports: dict) -> str | None:
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
        # 1. 服务器清单
        remote = call(f"/sync/manifest?mode={m}").get("files", {})
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
        body_mp = b"".join(parts) + b"--" + boundary.encode() + b"--\r\n"
        req = urllib.request.Request(_auth_server_base() + "/sync/import",
                                     data=body_m + body_mt + body_mp,
                                     headers={"Authorization": f"Bearer {get_token()}",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                rp = json.loads(r.read().decode("utf-8"))
            reports["uploaded"].extend(rp.get("applied", []))
            reports["conflicts"] += int(rp.get("conflicts", 0))
            reports["skipped"].extend(rp.get("skipped", []))
        except urllib.error.HTTPError as e:
            return f"上传失败（{e.code}）"

    # 3. 拉取（服务器独有 + 服务器更新的合并项）→ 本地合并落盘
    local_after = scan_manifest(root)
    need = [p for p in plan["to_download"]]
    # 服务器侧判定"服务器更新"而跳过的上传项 → 拉回
    for p in plan["to_merge"]:
        if (local_after.get(p, {}).get("sha256") or "") != (remote.get(p, {}).get("sha256") or ""):
            need.append(p)
    need = sorted(set(need))
    if need:
        try:
            zip_raw = call("/sync/export", {"mode": m, "files": need}, binary=True)
        except Exception as e:
            return f"拉取失败: {e}"
        with zipfile.ZipFile(io.BytesIO(zip_raw)) as zf:
            backups_dir = root / ".sync_backups"
            for info in zf.infolist():
                name = info.filename
                try:
                    rel = Path(name)
                    if rel.is_absolute() or ".." in rel.parts:
                        continue
                    fp = root / rel
                    if rel.parts[0] == "images":
                        # 图片二进制（blob，正式消息数据）：不解码不合并，整份落盘
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        fp.write_bytes(zf.read(info))
                        reports["downloaded"].append(name)
                        continue
                    remote_text = zf.read(info).decode("utf-8", errors="replace")
                    local_text = fp.read_text(encoding="utf-8") if fp.exists() else ""
                    if rel.suffix == ".jsonl":
                        merged, cf = merge_jsonl(local_text, remote_text)
                        if cf:
                            backup_file(fp, backups_dir, "conflict")
                        reports["conflicts"] += len(cf)
                        if merged != local_text:
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(merged, encoding="utf-8")
                            reports["merged"].append(name)
                    elif rel.name == "favorites.json":
                        merged, cf = merge_favorites(local_text, remote_text)
                        if merged != local_text:
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(merged, encoding="utf-8")
                            reports["merged"].append(name)
                    else:
                        # 文档类：第 1 步抓的远端清单 mtime 与当前文件 mtime 比——
                        # 远端更新才覆盖，否则本地更新保留（防旧远端回滚覆盖本地新内容）
                        st = fp.stat().st_mtime if fp.exists() else 0.0
                        remote_mtime = float((remote.get(str(name)) or {}).get("mtime", 0) or 0)
                        if fp.exists() and remote_mtime <= st + 0.05:
                            reports["skipped"].append(name + "（本地更新，保留）")
                        elif remote_text != local_text:
                            if fp.exists():
                                backup_file(fp, backups_dir, "pre")
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(remote_text, encoding="utf-8")
                            reports["downloaded"].append(name)
                except Exception as e:
                    logger.warning("同步拉取落盘失败 %s: %s", name, e)
                    reports["skipped"].append(f"{name}（{e}）")
    return None
