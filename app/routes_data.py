# -*- coding: utf-8 -*-
"""数据导出/导入/本地备份/A1 增量同步路由（routes.py 拆分产物，纯重构无行为变化）。"""

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from modules import app_config as cfg
from modules.app_config import DEFAULT_MODE
from modules.multipart import parse_multipart

from routes_common import (_read_json, _body_mode, _query_mode, _is_server,
                           _image_used_bytes, _image_quota_bytes)
from routes_auth import _auth_server_base

logger = logging.getLogger(__name__)


# 导出/备份打包排除的内部目录（同步冲突备份/设定纠错中间态，不是用户数据）
_EXPORT_EXCLUDE_DIRS = {".sync_backups", ".setting_fix", ".sync_conflicts"}


def _build_backup_zip(root: Path, mode: str) -> bytes:
    """打包模式数据为 zip 字节：{mode} 根全量文件（排除内部目录；images/ 压缩图是
    正式消息数据，进包）+ _config.json（剥离 Key）+ stickers-meta.json（表情包
    文字元数据，图片本体不打包）。export_data 下载与 /backup/create 本地备份共用。
    只读打包，不修改数据。"""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(root.rglob("*")):
            if not fp.is_file():
                continue
            rel = fp.relative_to(root)
            if any(part in _EXPORT_EXCLUDE_DIRS for part in rel.parts):
                continue
            zf.write(fp, rel.as_posix())
        # config.json：全量配置但剥离 API Key（换机迁移带上供应商/base 信息）
        try:
            import copy as _copy
            cfg_copy = _copy.deepcopy(cfg.config)
            cfg_copy.pop("api_key", None)
            for _p in cfg_copy.get("providers") or []:
                _p.pop("api_key", None)
            zf.writestr("_config.json",
                        json.dumps(cfg_copy, ensure_ascii=False, indent=1))
        except Exception:
            pass
        # 表情包元数据：{file名: {label, category, enabled, sha256}}——图片本体不打包。
        # 用户表情包目录与注册表在 USER_DIR/{uid}/stickers/（无模式维度，见 sticker_picker）
        try:
            from tools.sticker_picker import _user_registry_file
            meta = {}
            reg = _user_registry_file()
            sdir = reg.parent if reg is not None else None
            if reg.exists() and sdir is not None:
                data = json.loads(reg.read_text(encoding="utf-8"))
                entries = data if isinstance(data, list) else data.get("stickers", [])
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    fname = str(e.get("file", "")).rsplit("/", 1)[-1]
                    fp = sdir / fname
                    if not fp.exists() or not fp.is_file():
                        continue
                    meta[fname] = {"label": e.get("label", ""), "category": e.get("category", ""),
                                   "enabled": bool(e.get("enabled", True)),
                                   "sha256": hashlib.sha256(fp.read_bytes()).hexdigest()}
            if meta:
                zf.writestr("stickers-meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        except Exception:
            pass
    return buf.getvalue()


def export_data(h):
    """导出当前模式数据为 zip 备份（对话/记忆/手账/设定/聊天图片 + config.json[剥离 Key] +
    表情包文字元数据——换机迁移用）。打包逻辑见 _build_backup_zip。
    带 name 参数时改为下载 backups/ 目录里已有的对应备份包（备份管理列表的「下载」）。
    Content-Disposition: attachment 触发浏览器/WebView 下载。只读打包，不修改数据。"""
    q = parse_qs(urlparse(h.path).query)
    mode = (q.get("mode") or [DEFAULT_MODE])[0]
    if mode not in cfg.MODES:
        mode = DEFAULT_MODE
    name = (q.get("name") or [""])[0]
    if name:
        # 下载既有备份包（名字校验与 /backup/* 同规则，防穿越）
        name = _backup_name_ok(name)
        fp = (_backup_dir(DEFAULT_MODE) / name) if name else None
        if not fp or not fp.is_file():
            h._json({"ok": False, "error": "非法或已不存在的备份名"}, 404)
            return
        data = fp.read_bytes()
        fname = name
    else:
        data = _build_backup_zip(cfg.mode_root(mode), mode)
        fname = f"firefly-backup-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f'attachment; filename="{fname}"')
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


# ══ 数据导入 / 本地备份 ════
# 导出复用 GET /export-data（zip 下载）；导入=multipart zip 覆盖（导入前自动备份 +
# zip slip 防御，见 _import_zip_to_mode）。备份本地化：/backup/* 端点管理
# {用户目录}/backups/（每模式留最近 _BACKUP_KEEP 份），手动云端备份（/sync/upload|download）已下线。
_IMPORT_MAX_BYTES = 60 * 1024 * 1024          # zip 上传上限（含 multipart 开销）


_IMPORT_MAX_FILE_BYTES = 20 * 1024 * 1024     # 包内单文件解压上限


_IMPORT_MAX_TOTAL_BYTES = 100 * 1024 * 1024   # 包内解压总量上限


_BACKUP_KEEP = 10                             # 每模式保留最近手动备份份数


def _zip_safe_entries(zf) -> list[tuple[str, object]]:
    """zip slip 防御：拒绝绝对路径与 .. 穿越，只收普通文件；返回 [(name, info)]。
    超限抛 ValueError（调用方转人话文案）。"""
    out = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        # zip slip 防御：拒绝绝对路径（/ 或 Windows 盘符 C:/）、.. 穿越，只收普通文件
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":") or ".." in name.split("/"):
            raise ValueError("压缩包内含非法路径，已拒绝")
        if info.file_size > _IMPORT_MAX_FILE_BYTES:
            raise ValueError("压缩包内单个文件过大，已拒绝")
        total += info.file_size
        if total > _IMPORT_MAX_TOTAL_BYTES:
            raise ValueError("压缩包解压总量过大，已拒绝")
        out.append((name, info))
    return out


def _backup_dir(mode: str) -> Path:
    """本地备份目录：{用户数据根}/backups/（与模式目录平级，不进导出/同步循环）。
    服务器版经 _user_ctx 自动按用户目录隔离。"""
    return cfg.mode_root(mode).parent / "backups"


def _backup_current_mode(mode: str, prefix: str) -> None:
    """把当前模式数据打成 zip 存到 backups/（导入/恢复前自动备份，防误操作）。空目录跳过。"""
    root = cfg.mode_root(mode)
    if not any(root.rglob("*")):
        return
    import io as _io
    import zipfile as _zipfile
    _backup_dir(mode).mkdir(parents=True, exist_ok=True)
    fp = _backup_dir(mode) / f"{prefix}-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with _zipfile.ZipFile(fp, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(root).as_posix())


def _import_zip_to_mode(data: bytes, mode: str, backup_prefix: str = "auto") -> tuple[bool, str, int]:
    """zip 数据覆盖导入到指定模式（导入前把当前模式备份到 backups/，前缀 backup_prefix）。
    返回 (ok, error, 文件数)。"""
    import io as _io
    import zipfile as _zipfile
    import shutil as _sh
    if not data.startswith(b"PK"):
        return False, "不是有效的 zip 备份文件", 0
    try:
        zf = _zipfile.ZipFile(_io.BytesIO(data))
    except Exception:
        return False, "zip 解析失败（文件损坏？）", 0
    try:
        entries = _zip_safe_entries(zf)
    except ValueError as e:
        return False, str(e), 0

    # 覆盖式导入：先自动备份现有数据，再清空目标目录解压
    try:
        _backup_current_mode(mode, backup_prefix)
    except Exception:
        pass    # 备份失败不阻塞导入（导入包本身是用户拿来的数据源）
    root = cfg.mode_root(mode)
    try:
        if root.exists():
            _sh.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        for name, info in entries:
            dst = root / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dst, "wb") as out:
                _sh.copyfileobj(src, out)
    except Exception as e:
        return False, f"写入失败: {e}", 0
    # 清缓存：设定/手账/短信样本的内存缓存必须重载（旧内容会串进新数据）
    try:
        from modules.llm_base import clear_cache, reload_journal
        from modules.polisher import clear_samples_cache
        clear_cache()
        clear_samples_cache()
        reload_journal(mode)
    except Exception:
        pass
    return True, "", len(entries)


def import_data(h):
    """导入 zip 备份（覆盖当前模式数据）。multipart：mode + file。
    换机/恢复用；导入前自动备份当前模式到 backups/（见 _import_zip_to_mode）。"""
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少 zip 文件"}); return
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    ok, err, n = _import_zip_to_mode(file_info["data"], mode)
    if not ok:
        h._json({"ok": False, "error": err}); return
    h._json({"ok": True, "files": n, "mode": mode})


# ── 本地备份管理（/backup/*）：{用户目录}/backups/，每模式留最近 _BACKUP_KEEP 份 ──
# 手动云端备份（/sync/upload|download）已下线：备份改本地目录管理，恢复复用导入链路。
_BACKUP_NAME_RE = re.compile(r"^[a-z0-9_\-\.]+\.zip$")


def _backup_name_ok(name) -> str:
    """备份文件名审查：白名单字符 + .zip 结尾，拒绝 .. 与路径分隔符。合法返回名字，否则 ""。"""
    name = str(name or "").strip()
    if not name or len(name) > 120:
        return ""
    if not _BACKUP_NAME_RE.match(name) or ".." in name or "/" in name or "\\" in name:
        return ""
    return name


def _backup_mode_of(name: str) -> str:
    """从备份名解析模式（{mode}-{时间戳}.zip）；不含合法模式返回 ""。"""
    m = name.split("-", 1)[0]
    return m if m in cfg.MODES else ""


def backup_create(h):
    """POST /backup/create {mode}：打包该模式（与 /export-data 同逻辑，见 _build_backup_zip）
    落 backups/{mode}-{yyyymmdd-HHMMSS}.zip，每模式保留最近 _BACKUP_KEEP 份（按名排序删旧）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    try:
        data = _build_backup_zip(cfg.mode_root(mode), mode)
    except Exception as e:
        logger.warning("备份打包失败: %s", e)
        h._json({"ok": False, "error": f"备份打包失败: {e}"}); return
    bdir = _backup_dir(mode)
    fp = None
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        # 秒级时间戳同名碰撞（同一秒内重复创建）→ 追加 _N 后缀保证唯一
        stem = f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}"
        name = f"{stem}.zip"
        i = 1
        while (bdir / name).exists():
            i += 1
            name = f"{stem}_{i}.zip"
        fp = bdir / name
        fp.write_bytes(data)
        # 每模式只留最近 _BACKUP_KEEP 份（文件名 = 模式-时间戳，按名排序即时间序，删旧）。
        # 刚写入的一份不参与淘汰：同秒连建时基底名会被淘汰再复用，不排除会自删
        olds = sorted(p.name for p in bdir.glob(f"{mode}-*.zip") if p.name != name)
        for old in olds[:max(len(olds) + 1 - _BACKUP_KEEP, 0)]:
            try:
                (bdir / old).unlink()
            except OSError:
                pass
    except OSError as e:
        logger.warning("备份写入失败: %s", e)
        h._json({"ok": False, "error": f"备份写入失败: {e}"}); return
    # 输出验证：落盘内容与打包字节一致才算成功
    try:
        if not fp.is_file() or fp.stat().st_size != len(data):
            h._json({"ok": False, "error": "备份写入校验失败"}); return
    except OSError:
        h._json({"ok": False, "error": "备份写入校验失败"}); return
    h._json({"ok": True, "name": name, "mode": mode, "size": len(data)})


def backups_list(h):
    """GET /backups → {ok, backups:[{name,mode,size,time}]}（新→旧）。
    只列手动备份（{mode}-*.zip）；auto-/pre-restore- 自动备份不列入。"""
    out = []
    try:
        bdir = _backup_dir(DEFAULT_MODE)
        if bdir.exists():
            for fp in sorted(bdir.glob("*.zip"), key=lambda p: p.name, reverse=True):
                m = _backup_mode_of(fp.name)
                if not m or not fp.name.startswith(f"{m}-"):
                    continue
                try:
                    st = fp.stat()
                except OSError:
                    continue
                out.append({"name": fp.name, "mode": m, "size": st.st_size,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    except OSError as e:
        logger.warning("备份列表读取失败: %s", e)
    h._json({"ok": True, "backups": out})


def backup_restore(h):
    """POST /backup/restore {name}：恢复指定备份（覆盖该备份所属模式的数据）。
    恢复前先对当前模式打一份 pre-restore 自动备份；解压校验复用 import-data 链路
    （zip slip 防御/单文件与总量上限/覆盖后清缓存，见 _import_zip_to_mode）。"""
    body = _read_json(h)
    name = _backup_name_ok(body.get("name"))
    if not name:
        h._json({"ok": False, "error": "非法备份名"}); return
    mode = _backup_mode_of(name)
    if not mode:
        h._json({"ok": False, "error": "备份名不含合法模式"}); return
    fp = _backup_dir(mode) / name
    if not fp.is_file():
        h._json({"ok": False, "error": "备份不存在"}); return
    try:
        data = fp.read_bytes()
    except OSError as e:
        h._json({"ok": False, "error": f"备份读取失败: {e}"}); return
    ok, err, n = _import_zip_to_mode(data, mode, backup_prefix="pre-restore")
    if not ok:
        h._json({"ok": False, "error": err}); return
    h._json({"ok": True, "restored": name, "mode": mode, "files": n})


def backup_delete(h):
    """POST /backup/delete {name}：删除一份本地备份（含自动备份，按名审查后删除）。"""
    body = _read_json(h)
    name = _backup_name_ok(body.get("name"))
    if not name:
        h._json({"ok": False, "error": "非法备份名"}); return
    fp = _backup_dir(DEFAULT_MODE) / name
    if not fp.is_file():
        h._json({"ok": False, "error": "备份不存在"}); return
    try:
        fp.unlink()
    except OSError as e:
        h._json({"ok": False, "error": f"删除失败: {e}"}); return
    h._json({"ok": True, "deleted": name})


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
    触发时机：登录态冷启动 / 聊天页回前台 / 设置页手动按钮（W4 前端接线在 W7）。
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
    mode = _body_mode(body)
    token = get_token()
    if not token:
        h._json({"ok": False, "error": "未登录（请先在首页登录账号）"}); return
    root, m = _sync_mode_root(mode)
    reports = {"uploaded": [], "downloaded": [], "merged": [], "skipped": [], "conflicts": 0}
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

    try:
        # 1. 服务器清单
        remote = call(f"/sync/manifest?mode={m}").get("files", {})
    except Exception as e:
        h._json({"ok": False, "error": f"连接服务器失败: {e}"}); return

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
        req = urllib.request.Request(base + "/sync/import",
                                     data=body_m + body_mt + body_mp,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                rp = json.loads(r.read().decode("utf-8"))
            reports["uploaded"].extend(rp.get("applied", []))
            reports["conflicts"] += int(rp.get("conflicts", 0))
            reports["skipped"].extend(rp.get("skipped", []))
        except urllib.error.HTTPError as e:
            h._json({"ok": False, "error": f"上传失败（{e.code}）"}); return

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
            h._json({"ok": False, "error": f"拉取失败: {e}"}); return
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
    h._json({"ok": True, **reports})
