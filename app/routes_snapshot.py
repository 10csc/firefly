# -*- coding: utf-8 -*-
"""快照路由（服务器备份快照；routes.py 拆分）：

保存快照 = 把**所有角色包 + 互动产生的用户数据**打包 zip 存服务器
（本地版登录后上传；服务器版数据本在云端直接打包）。
每账号保留最近 5 份滚动（防冗余）。恢复快照 = 按包分发覆盖（恢复前本机自动备份）。

存储：{用户数据根}/snapshots/（服务器版经 _user_ctx 按 uid 隔离）。
快照 zip 结构：{mode}/... + stickers/（用户添加表情包，角色卡资源）+ _config.json（剥 Key，不自动恢复）。
"""
import json
import logging
import re
import time
from pathlib import Path

from modules import app_config as cfg
from routes_common import _read_json, _is_server
# 3.4：快照哈希口径与恢复端**共用同一实现**（别再各写一份）
from infra.sync.restore import _pack_digest, _sha256_file

logger = logging.getLogger(__name__)

_SNAPSHOT_KEEP = 5
_SNAP_NAME_RE = re.compile(r"^snapshot-\d{8}-\d{6}(_\d+)?\.zip$")
_EXPORT_EXCLUDE_DIRS = {".sync_backups", ".setting_fix", ".sync_conflicts"}


def _snapshots_dir() -> Path:
    """快照目录：{user_root}/snapshots/（user_root 经 _user_ctx 按用户隔离；本地版 = USER_DIR）。"""
    base = cfg._user_ctx_dir() or cfg.USER_DIR
    return Path(base) / "snapshots"


def _user_root() -> Path:
    return Path(cfg._user_ctx_dir() or cfg.USER_DIR)


def _snap_name_ok(name) -> str:
    n = str(name or "").strip()
    return n if _SNAP_NAME_RE.fullmatch(n) else ""


def build_full_snapshot_zip() -> bytes:
    """打包所有角色包 + 用户数据为 zip 字节（只读打包，不修改数据）。
    内容：全部注册模式 + 清单里**归档**的包（3.8：归档 ≠ 丢保险）+ stickers/ 用户添加表情包
    （角色卡资源）+ _config.json（剥离 API Key，恢复时不自动还原）+ `_manifest.json`（3.4）。

    白名单取 `cfg.backup_pack_ids()`（MODES ∪ packs.json 含 archived），并用
    `user_root/{id}` 直接定位包目录 —— 不走 `cfg.mode_root()`，因为后者对不在
    MODES 里的包会**静默回退默认包**，那样归档包就会把别的包的数据打进快照。"""
    import io
    import zipfile
    user_root = _user_root()
    buf = io.BytesIO()
    digests: dict[str, list] = {}          # pid → [(rel, sha256)]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for mode in cfg.backup_pack_ids():
            root = user_root / mode
            if not root.is_dir():
                continue
            for fp in sorted(root.rglob("*")):
                if not fp.is_file():
                    continue
                rel = fp.relative_to(user_root)
                if any(part in _EXPORT_EXCLUDE_DIRS for part in rel.parts):
                    continue
                zf.write(fp, rel.as_posix())
                digests.setdefault(mode, []).append(
                    (rel.relative_to(mode).as_posix(), _sha256_file(fp)))
        sdir = user_root / "stickers"
        sticker_digest = []
        if sdir.is_dir():
            for fp in sorted(sdir.rglob("*")):
                if fp.is_file():
                    zf.write(fp, fp.relative_to(user_root).as_posix())
                    sticker_digest.append((fp.relative_to(sdir).as_posix(), _sha256_file(fp)))
        try:
            import copy as _copy
            cfg_copy = _copy.deepcopy(cfg.config)
            cfg_copy.pop("api_key", None)
            for _p in cfg_copy.get("providers") or []:
                _p.pop("api_key", None)
            zf.writestr("_config.json", json.dumps(cfg_copy, ensure_ascii=False, indent=1))
        except Exception:
            pass
        try:
            zf.writestr("_manifest.json", json.dumps(
                _snapshot_manifest(digests, sticker_digest), ensure_ascii=False, indent=1))
        except Exception as e:
            logger.warning("快照 manifest 写入失败（快照本身仍可用）: %s", e)
    return buf.getvalue()


def _snapshot_manifest(digests: dict, sticker_digest: list) -> dict:
    """快照清单（3.4）：让"这份 zip 里有什么、是不是完整"变成可核对的事实，而不是靠解压猜。

    字段：`snapshot_version`（清单格式版本）/ `app_version` / `created_at` /
    `packs[{id,name,source,schema,files,sha256}]` / `stickers{files,sha256}`。
    `source`/`name`/`schema` 取自包注册表（用户包）或预设表（内置包）。"""
    packs = []
    for pid in sorted(digests):
        entries = digests[pid]
        meta = {}
        try:
            meta = cfg.pack_registry().get(pid) or {}
        except Exception:
            meta = {}
        preset = (cfg.PRESETS.get(pid) or {}) if hasattr(cfg, "PRESETS") else {}
        packs.append({
            "id": pid,
            "name": meta.get("name") or preset.get("name") or pid,
            "source": meta.get("source") or ("custom" if preset.get("custom") else "bundled"),
            "schema": meta.get("schema", preset.get("schema", 1)),
            "files": len(entries),
            "sha256": _pack_digest(entries),
        })
    return {
        "snapshot_version": 1,
        "app_version": getattr(cfg, "APP_VERSION", "") or "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "packs": packs,
        "stickers": {"files": len(sticker_digest), "sha256": _pack_digest(sticker_digest)},
    }


def _prune_old(d: Path) -> None:
    """滚动保留最近 _SNAPSHOT_KEEP 份（按名排序即时间序，删旧；刚写入的不参与淘汰）。"""
    names = sorted(p.name for p in d.glob("snapshot-*.zip"))
    for old in names[:max(len(names) - _SNAPSHOT_KEEP, 0)]:
        try:
            (d / old).unlink()
        except OSError:
            pass


def _validate_snapshot_zip(data: bytes) -> str:
    """上传快照的内容校验（C-6.14，2026-09-13）。返回 "" 通过，否则人话错误文案。

    为什么必须校验：上传分支原来只判"非空"，非 zip / 坏 zip 也会被 atomic_write 落盘成
    合法的 `snapshot-*.zip`；等到用户真的去恢复时才发现打不开——而那时滚动配额可能已经把
    旧的好快照淘汰掉了（等于用一份坏文件挤掉了真备份）。
    这里做三件事：PK 魔数 → zip 可解析 → 条目安全（_zip_safe_entries：zip slip + 解压炸弹）。
    格式层面的"像不像全包快照"（顶层 {mode}/ 或 stickers/）**故意不在这里判**——
    那属于阶段 3 的包 manifest 范围，阶段 1 不收紧可接受的输入集合。"""
    import io
    import zipfile
    from routes_data import _zip_safe_entries     # 延迟导入：routes_data ↔ routes_snapshot 互引
    if not data.startswith(b"PK"):
        return "不是有效的 zip 快照（缺少 PK 魔数）"
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return "zip 解析失败（文件损坏？）"
    try:
        entries = _zip_safe_entries(zf)
    except ValueError as e:
        return str(e)
    if not entries:
        return "快照里没有任何文件"
    return ""


def snapshot_create(h):
    """POST /snapshot/create（multipart: 可选 file=zip 上传）。
    服务器版/同机：无 file 时本端直接打包全量。
    本地版：本端打包存本地 snapshots/ 后，已登录则自动转发认证服务器（换机可恢复）。"""
    from routes import parse_multipart
    from routes_data import _IMPORT_MAX_BYTES
    # C-6.14：上传上限与恢复侧对齐（原来 120MB > 恢复侧 60MB，等于允许上传一份
    # 永远恢复不了的文件）；并做内容校验，坏数据不进快照库。
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    fi = files.get("file")
    if fi is not None:
        data = fi["data"]
        if not isinstance(data, bytes) or not data:
            h._json({"ok": False, "error": "快照内容为空"}); return
        _err = _validate_snapshot_zip(data)
        if _err:
            logger.warning("拒绝上传的快照（内容校验未过）: %s", _err)
            h._json({"ok": False, "error": _err}); return
    else:
        try:
            data = build_full_snapshot_zip()
        except Exception as e:
            logger.warning("快照打包失败: %s", e)
            h._json({"ok": False, "error": f"快照打包失败: {e}"}); return
    d = _snapshots_dir()
    d.mkdir(parents=True, exist_ok=True)
    stem = f"snapshot-{time.strftime('%Y%m%d-%H%M%S')}"
    name = f"{stem}.zip"
    i = 1
    while (d / name).exists():
        i += 1
        name = f"{stem}_{i}.zip"
    from modules.storage import atomic_write_bytes
    if not atomic_write_bytes(d / name, data):
        h._json({"ok": False, "error": "快照写入失败"}); return
    _prune_old(d)
    pushed = False
    push_error = ""
    # 本地版：已登录则自动转发认证服务器（快照上云，换机可恢复；转发失败不阻断本地快照）
    if not _is_server() and fi is None:
        pushed, push_error = _push_to_auth_server(name, data)
    h._json({"ok": True, "name": name, "size": len(data),
             "pushed": pushed, "push_error": push_error})


def _push_to_auth_server(name: str, data: bytes) -> tuple[bool, str]:
    """把本地快照 zip 转发到认证服务器 /snapshot/create（multipart file）。"""
    try:
        from modules.auth_store import get_token
        token = get_token()
        if not token:
            return False, "未登录（快照仅保存在本机）"
        import urllib.request
        from routes_auth import _auth_server_base
        boundary = "ffsnap" + time.strftime("%H%M%S") + "x"
        body = (b"--" + boundary.encode() + b"\r\n"
                + f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode()
                + b"Content-Type: application/zip\r\n\r\n" + data + b"\r\n"
                + b"--" + boundary.encode() + b"--\r\n")
        req = urllib.request.Request(_auth_server_base() + "/snapshot/create",
                                     data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            rp = json.loads(r.read().decode("utf-8"))
        if rp.get("ok"):
            return True, ""
        return False, str(rp.get("error") or "服务器拒绝")
    except Exception as e:
        logger.warning("快照转发服务器失败: %s", e)
        return False, f"转发服务器失败（本地快照已保留）: {e}"


def snapshot_list(h):
    """GET /snapshot/list → {ok, snapshots:[{name,size,time}]}（新→旧）。"""
    out = []
    d = _snapshots_dir()
    try:
        if d.is_dir():
            for fp in sorted(d.glob("snapshot-*.zip"), key=lambda p: p.name, reverse=True):
                try:
                    st = fp.stat()
                except OSError:
                    continue
                out.append({"name": fp.name, "size": st.st_size,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    except OSError as e:
        logger.warning("快照列表读取失败: %s", e)
    h._json({"ok": True, "snapshots": out})


def snapshot_download(h):
    """GET /snapshot/download?name=：下载指定快照 zip（恢复用）。"""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(h.path).query)
    name = _snap_name_ok((qs.get("name") or [""])[0])
    if not name:
        h._json({"ok": False, "error": "非法快照名"}); return
    fp = _snapshots_dir() / name
    if not fp.is_file():
        h._json({"ok": False, "error": "快照不存在"}, 404); return
    data = fp.read_bytes()
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f'attachment; filename="{name}"')
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def snapshot_delete(h):
    """POST /snapshot/delete {name}。"""
    body = _read_json(h)
    name = _snap_name_ok(body.get("name"))
    if not name:
        h._json({"ok": False, "error": "非法快照名"}); return
    fp = _snapshots_dir() / name
    if not fp.is_file():
        h._json({"ok": False, "error": "快照不存在"}); return
    try:
        fp.unlink()
    except OSError as e:
        h._json({"ok": False, "error": f"删除失败: {e}"}); return
    h._json({"ok": True, "deleted": name})


def snapshot_restore(h):
    """POST /snapshot/restore {name}：恢复指定快照（按包分发覆盖）。
    恢复前对当前全部包打一份 pre-restore 自动快照；主体复用 routes_data._restore_full_snapshot。"""
    body = _read_json(h)
    name = _snap_name_ok(body.get("name"))
    if not name:
        h._json({"ok": False, "error": "非法快照名"}); return
    fp = _snapshots_dir() / name
    if not fp.is_file():
        h._json({"ok": False, "error": "快照不存在"}); return
    data = fp.read_bytes()
    from routes_data import _IMPORT_MAX_BYTES, _restore_full_snapshot
    if len(data) > _IMPORT_MAX_BYTES:
        h._json({"ok": False, "error": "快照过大"}); return
    # 恢复前自动快照当前（pre-restore- 前缀：不进列表/滚动配额，单独留 3 份）
    # 2026-09-10 修复：原来恢复前快照「失败不阻断」，却仍以 backup=False 调用恢复主体，
    # 等于在"没有任何备份"的情况下执行破坏性覆盖（磁盘满/目录不可写时即触发）。
    # 现在改为：pre-restore 快照成功才跳过逐包备份；失败则回落到逐包备份。
    _sum: dict = {}
    pre_ok = False
    try:
        pre = build_full_snapshot_zip()
        d = _snapshots_dir()
        pre_name = f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        (d / pre_name).write_bytes(pre)
        olds = sorted(p.name for p in d.glob("pre-restore-*.zip"))
        for old in olds[:max(len(olds) - 3, 0)]:
            try: (d / old).unlink()
            except OSError: pass
        pre_ok = True
    except Exception as e:
        logger.warning("恢复前自动快照失败，改为逐包备份: %s", e)
    ok, err, n = _restore_full_snapshot(data, backup=not pre_ok, summary=_sum)
    if not ok:
        h._json({"ok": False, "error": err, "backup_ok": pre_ok}); return
    resp = {"ok": True, "restored": name, "files": n, "backup_ok": pre_ok}
    if _sum.get("manifest"):
        resp["manifest"] = _sum["manifest"]          # 3.4：把清单摘要回给前端
    h._json(resp)
