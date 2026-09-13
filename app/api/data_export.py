# -*- coding: utf-8 -*-
"""数据导出/导入/本地备份端点（阶段 2.2 自 routes_data 拆出）

- GET  /export-data：单包 zip 导出（排除内部目录）
- POST /import-data：multipart zip 覆盖导入（识别全包快照 → 交恢复引擎按包分发）
- /backup/*：本地备份管理（创建/列表/恢复/删除，每模式留最近 _BACKUP_KEEP 份）

导入的原子换目录、zip slip 防御、快照分发都在 infra.sync.restore（本模块只做参数审查与响应）。
"""

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
from routes_common import _read_json
from routes_common import _body_mode

from infra.sync.restore import (
    _BACKUP_KEEP, _EXPORT_EXCLUDE_DIRS, _IMPORT_MAX_BYTES, _backup_dir,
    _import_zip_to_mode, _is_snapshot_zip, _restore_full_snapshot,
)

logger = logging.getLogger(__name__)


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
            from domain.stickers.picker import _user_registry_file
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


def import_data(h):
    """导入 zip 备份（覆盖当前模式数据）。multipart：mode + file。
    两种格式自动识别：全包快照（顶层含 {mode}/ 或 stickers/）→ 按包分发恢复
    （忽略 mode 字段）；单包导出（条目相对模式根）→ 覆盖该模式。导入前自动备份。
    换机/恢复用；打包逻辑见 _build_backup_zip / routes_snapshot.build_full_snapshot_zip。"""
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少 zip 文件"}); return
    data = file_info["data"]
    if not isinstance(data, bytes):
        data = bytes(data)
    # 全包快照：走按包分发恢复（否则会被当成单包塞进当前模式，产生 {mode}/{mode}/ 嵌套垃圾）
    try:
        import io as _io
        import zipfile as _zipfile
        _is_snap = (data.startswith(b"PK")
                    and _is_snapshot_zip(_zipfile.ZipFile(_io.BytesIO(data))))
    except Exception:
        _is_snap = False
    if _is_snap:
        ok, err, n = _restore_full_snapshot(data)
        if not ok:
            h._json({"ok": False, "error": err}); return
        h._json({"ok": True, "files": n, "snapshot": True})
        return
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    ok, err, n = _import_zip_to_mode(data, mode)
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
