# -*- coding: utf-8 -*-
"""部署清单 dry-run（J6，2026-10-27 T3）。

把"单文件挑传后口头说完成"变成可核验的三件套：
  manifest（本批文件 + 本地 sha256/size/目标路径）
  verify-local（部署前重新哈希，任何一字节变化即失败）
  rollback-plan（同批备份/恢复清单，只打印不执行）

本工具**绝不**执行 scp/ssh/rsync/systemctl，也绝不读写生产；远端动作必须由用户逐批批准后另走一步。
证据目录 `_harden/deploy/<change_id>/` 是 gitignore 的运行期证据，不是权威台账（权威见
`docs/治理/03_状态地图.md`）。

用法：
    python tools/deploy_manifest.py build --change-id hotfix-1 --files server/app.py,server/update.json
    python tools/deploy_manifest.py verify-local --manifest _harden/deploy/hotfix-1/manifest.json
    python tools/deploy_manifest.py rollback-plan --manifest _harden/deploy/hotfix-1/manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = "/opt/firefly/"
DENY_PARTS = {"user_data", ".git", "__pycache__", ".ssh"}
DENY_SUFFIX = {".pem", ".key", ".p12", ".pfx", ".env"}
DENY_NAMES = {"id_rsa", "id_ed25519", "config.json"}
CHANGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _sha256(fp: Path) -> str:
    h = hashlib.sha256()
    with fp.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> tuple[dict | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _validate_rel(raw: str) -> tuple[str, str]:
    """把 --files 的路径规范成相对根 posix；拒绝绝对/穿越/敏感文件。"""
    s = str(raw or "").strip().replace("\\", "/")
    if not s:
        return "", "空路径"
    p = Path(s)
    if p.is_absolute() or s.startswith("/"):
        return "", f"不允许绝对路径: {s}"
    parts = [x for x in p.parts if x not in ("", ".")]
    if any(x == ".." for x in parts):
        return "", f"不允许 .. 穿越: {s}"
    low = [x.lower() for x in parts]
    if any(x in DENY_PARTS for x in low):
        return "", f"拒绝敏感目录: {s}"
    name = parts[-1] if parts else ""
    if name.lower() in DENY_NAMES or Path(name).suffix.lower() in DENY_SUFFIX:
        return "", f"拒绝敏感文件: {s}"
    return "/".join(parts), ""


def _manifest_path(root: Path, change_id: str) -> Path:
    return Path(root) / "_harden" / "deploy" / change_id / "manifest.json"


def _load_manifest(root: Path, manifest: str) -> tuple[Path, dict | None, str]:
    mp = Path(manifest)
    if not mp.is_absolute():
        mp = Path(root) / mp
    data, err = _read_json(mp)
    if err:
        return mp, None, f"manifest 不可读: {err}"
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return mp, None, "manifest 形状非法（缺 files）"
    return mp, data, ""


def _sandbox_target(root: Path, rel: str) -> Path:
    """把部署相对路径映射到显式沙箱根；绝不解释 manifest 的真实 target。"""
    clean, err = _validate_rel(rel)
    if err:
        raise ValueError(err)
    base = Path(root).resolve()
    target = (base / clean).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"沙箱目标越界: {rel}") from exc
    return target


def _copy_replace(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".deploy-tmp-" + uuid.uuid4().hex)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def build(root: Path = ROOT, change_id: str = "", files: list[str] | None = None,
          dest_prefix: str = DEFAULT_DEST, note: str = "") -> dict:
    root = Path(root)
    if not CHANGE_ID_RE.fullmatch(str(change_id or "")):
        return {"ok": False, "msg": f"change_id 非法（只允许字母数字._-，≤64）: {change_id!r}"}
    rels, failures = [], []
    for raw in files or []:
        rel, err = _validate_rel(raw)
        if err:
            failures.append(err)
        elif rel not in rels:
            rels.append(rel)
    if not rels:
        return {"ok": False, "msg": "没有可部署文件（--files 为空或全部被拒）: " + "; ".join(failures)}
    prefix = str(dest_prefix or DEFAULT_DEST).rstrip("/") + "/"
    entries = []
    for rel in rels:
        fp = root / rel
        if not fp.is_file():
            failures.append(f"文件不存在: {rel}")
            continue
        st = fp.stat()
        entries.append({
            "path": rel,
            "target": prefix + rel,
            "size": st.st_size,
            "sha256": _sha256(fp),
            "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
        })
    if failures:
        return {"ok": False, "msg": "; ".join(failures)}
    data = {
        "schema": 1,
        "change_id": change_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dest_prefix": prefix,
        "note": note,
        "files": entries,
        "total_size": sum(e["size"] for e in entries),
    }
    out = _manifest_path(root, change_id)
    if not root.is_dir():
        return {"ok": False, "msg": f"root 不存在: {root}"}
    _write_json(out, data)
    return {"ok": True, "path": out, "manifest": data,
            "msg": f"已生成部署清单 {out}（{len(entries)} 个文件，{data['total_size']} 字节）"}


def verify_local(root: Path, manifest: str) -> dict:
    root = Path(root)
    mp, data, err = _load_manifest(root, manifest)
    if err:
        return {"ok": False, "msg": err}
    fails = []
    for e in data["files"]:
        rel = str(e.get("path") or "")
        p, perr = _validate_rel(rel)
        if perr:
            fails.append(perr)
            continue
        fp = root / p
        if not fp.is_file():
            fails.append(f"缺失: {rel}")
            continue
        size, sha = fp.stat().st_size, _sha256(fp)
        if size != int(e.get("size", -1)):
            fails.append(f"大小变化: {rel} 实际={size} 清单={e.get('size')}")
        if sha != str(e.get("sha256") or ""):
            fails.append(f"sha256 变化: {rel} 实际={sha[:16]}… 清单={str(e.get('sha256'))[:16]}…")
    return {"ok": not fails, "msg": "PASS" if not fails else "FAIL: " + "; ".join(fails),
            "fails": fails, "manifest": str(mp)}


def backup_path(manifest: str) -> Path:
    mp = Path(manifest)
    return mp.parent / "backup.json"


def snapshot_local(root: Path, manifest: str, deployment_root: str,
                   backup_dir: str = "", record_path: str = "") -> dict:
    """在显式本地沙箱中记录部署前版本；不访问生产。

    backup.json 是回滚的前置证据：缺它、条目不全或备份哈希漂移时，
    rollback-plan 与 sandbox-restore 都必须失败。
    """
    root = Path(root)
    mp, data, err = _load_manifest(root, manifest)
    if err:
        return {"ok": False, "msg": err}
    local = verify_local(root, str(mp))
    if not local.get("ok"):
        return local
    dep = Path(deployment_root).resolve()
    if not dep.is_dir():
        return {"ok": False, "msg": f"沙箱部署根不存在: {dep}"}
    bdir = Path(backup_dir).resolve() if backup_dir else mp.parent / "backup"
    if bdir == dep or dep in bdir.parents:
        return {"ok": False, "msg": "备份目录不能位于待部署沙箱内部"}
    records = []
    try:
        for entry in data["files"]:
            rel = str(entry.get("path") or "")
            src = _sandbox_target(dep, rel)
            rec = {"path": rel, "existed": src.is_file(), "backup": ""}
            if src.is_file():
                dst = bdir / rel
                _copy_replace(src, dst)
                rec.update({
                    "before_sha256": _sha256(src),
                    "before_size": src.stat().st_size,
                    "backup": str(dst),
                    "backup_sha256": _sha256(dst),
                })
            records.append(rec)
        rec_data = {
            "schema": 1,
            "change_id": str(data.get("change_id") or ""),
            "manifest_sha256": _sha256(mp),
            "deployment_root": str(dep),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": records,
        }
        rp = Path(record_path) if record_path else backup_path(str(mp))
        if not rp.is_absolute():
            rp = root / rp
        _write_json(rp, rec_data)
    except Exception as e:
        return {"ok": False, "msg": f"沙箱备份失败: {type(e).__name__}: {e}"}
    return {"ok": True, "msg": f"已记录 {len(records)} 个回滚前文件: {rp}",
            "record": str(rp), "backup": rec_data}


def _load_backup_record(root: Path, manifest_path: Path, manifest: dict,
                        record_path: str = "") -> tuple[dict | None, str]:
    rp = Path(record_path) if record_path else backup_path(str(manifest_path))
    if not rp.is_absolute():
        rp = root / rp
    rec, err = _read_json(rp)
    if err:
        return None, f"回滚备份证据缺失或不可读: {err}"
    if not isinstance(rec, dict) or not isinstance(rec.get("files"), list):
        return None, "回滚备份证据形状非法"
    if str(rec.get("change_id") or "") != str(manifest.get("change_id") or ""):
        return None, "回滚备份不属于当前 change_id"
    if str(rec.get("manifest_sha256") or "") != _sha256(manifest_path):
        return None, "manifest 在备份后发生变化，拒绝沿用旧备份"
    want = {str(e.get("path") or "") for e in manifest["files"]}
    got = {str(e.get("path") or "") for e in rec["files"]}
    if want != got:
        return None, "回滚备份条目与 manifest 文件集合不一致"
    for entry in rec["files"]:
        rel = str(entry.get("path") or "")
        if not entry.get("existed"):
            continue
        backup = Path(str(entry.get("backup") or ""))
        if not backup.is_absolute():
            backup = rp.parent / backup
        if not backup.is_file():
            return None, f"回滚备份文件缺失: {rel}"
        if _sha256(backup) != str(entry.get("backup_sha256") or ""):
            return None, f"回滚备份文件已漂移: {rel}"
        if str(entry.get("before_sha256") or "") != str(entry.get("backup_sha256") or ""):
            return None, f"备份录取哈希不一致: {rel}"
    return rec, ""


def sandbox_apply(root: Path, manifest: str, deployment_root: str,
                  record_path: str = "") -> dict:
    """把候选文件放进显式本地沙箱；必须有完整备份证据，绝不操作真实 target。"""
    root = Path(root)
    mp, data, err = _load_manifest(root, manifest)
    if err:
        return {"ok": False, "msg": err}
    local = verify_local(root, str(mp))
    if not local.get("ok"):
        return local
    dep = Path(deployment_root).resolve()
    rec, rerr = _load_backup_record(root, mp, data, record_path)
    if rerr:
        return {"ok": False, "msg": rerr}
    if Path(str(rec.get("deployment_root") or "")).resolve() != dep:
        return {"ok": False, "msg": "备份证据与当前沙箱部署根不一致"}
    try:
        for entry in data["files"]:
            rel = str(entry.get("path") or "")
            _copy_replace(root / rel, _sandbox_target(dep, rel))
    except Exception as e:
        return {"ok": False, "msg": f"沙箱应用失败: {type(e).__name__}: {e}"}
    return {"ok": True, "msg": f"已在本地沙箱应用 {len(data['files'])} 个文件"}


def sandbox_restore(root: Path, manifest: str, deployment_root: str,
                    record_path: str = "") -> dict:
    """按 backup.json 把本地沙箱恢复到部署前；先全量校验再写入。"""
    root = Path(root)
    mp, data, err = _load_manifest(root, manifest)
    if err:
        return {"ok": False, "msg": err}
    dep = Path(deployment_root).resolve()
    rec, rerr = _load_backup_record(root, mp, data, record_path)
    if rerr:
        return {"ok": False, "msg": rerr}
    if Path(str(rec.get("deployment_root") or "")).resolve() != dep:
        return {"ok": False, "msg": "备份证据与当前沙箱部署根不一致"}
    try:
        for entry in rec["files"]:
            target = _sandbox_target(dep, str(entry.get("path") or ""))
            if entry.get("existed"):
                _copy_replace(Path(str(entry.get("backup") or "")), target)
            elif target.exists():
                target.unlink()
    except Exception as e:
        return {"ok": False, "msg": f"沙箱恢复失败: {type(e).__name__}: {e}"}
    fails = []
    for entry in rec["files"]:
        target = _sandbox_target(dep, str(entry.get("path") or ""))
        if entry.get("existed"):
            if not target.is_file() or _sha256(target) != str(entry.get("before_sha256") or ""):
                fails.append(str(entry.get("path") or ""))
        elif target.exists():
            fails.append(str(entry.get("path") or ""))
    return {"ok": not fails,
            "msg": "已恢复到部署前字节" if not fails else "恢复后校验失败: " + ", ".join(fails)}


def sandbox_rehearse(root: Path, change_id: str, files: list[str],
                     deployment_root: str, dest_prefix: str = DEFAULT_DEST) -> dict:
    """真实本地 v1→v2→v1 演练；只读写显式沙箱与证据目录。"""
    root = Path(root)
    made = build(root, change_id, files, dest_prefix, note="sandbox rehearsal")
    if not made.get("ok"):
        return made
    mp = Path(str(made["path"]))
    snap = snapshot_local(root, str(mp), deployment_root)
    if not snap.get("ok"):
        return snap
    applied = sandbox_apply(root, str(mp), deployment_root)
    if not applied.get("ok"):
        return applied
    restored = sandbox_restore(root, str(mp), deployment_root)
    evidence = {
        "schema": 1,
        "change_id": change_id,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "manifest": str(mp),
        "deployment_root": str(Path(deployment_root).resolve()),
        "v1_to_v2": applied.get("ok", False),
        "v2_to_v1": restored.get("ok", False),
        "msg": restored.get("msg", ""),
    }
    _write_json(mp.parent / "sandbox_evidence.json", evidence)
    return {"ok": bool(evidence["v1_to_v2"] and evidence["v2_to_v1"]),
            "msg": "PASS：v1→v2→v1 字节恢复一致" if evidence["v1_to_v2"] and evidence["v2_to_v1"]
                   else str(restored.get("msg") or applied.get("msg") or "FAIL"),
            "evidence": mp.parent / "sandbox_evidence.json", "detail": evidence}


def rollback_plan(root: Path, manifest: str, backup_dir: str = "",
                  record_path: str = "") -> dict:
    root = Path(root)
    mp, data, err = _load_manifest(root, manifest)
    if err:
        return {"ok": False, "msg": err}
    rec, rerr = _load_backup_record(root, mp, data, record_path)
    if rerr:
        return {"ok": False, "msg": rerr + "；拒绝生成可能删错文件的恢复步骤"}
    lines, entries = [], []
    targets = {str(e.get("path") or ""): str(e.get("target") or "") for e in data["files"]}
    for record in rec["files"]:
        rel = str(record.get("path") or "")
        target = targets[rel]
        bak = str(record.get("backup") or "")
        entries.append({"path": rel, "target": target, "backup": bak,
                        "existed": bool(record.get("existed"))})
        lines.append(f"# {rel}")
        if record.get("existed"):
            lines.append(f"cp -- {bak} {target}")
        else:
            lines.append(f"rm -f -- {target}")
    return {"ok": True, "msg": f"rollback plan（{len(entries)} 个文件；未执行任何动作）",
            "entries": entries, "lines": lines}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="部署清单 dry-run（不执行远端动作）")
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--change-id", required=True)
    b.add_argument("--files", required=True, help="逗号分隔的相对路径")
    b.add_argument("--dest-prefix", default=DEFAULT_DEST)
    b.add_argument("--note", default="")
    v = sub.add_parser("verify-local")
    v.add_argument("--manifest", required=True)
    r = sub.add_parser("rollback-plan")
    r.add_argument("--manifest", required=True)
    r.add_argument("--backup-dir", default="")
    r.add_argument("--backup-record", default="")
    s = sub.add_parser("sandbox-rehearse")
    s.add_argument("--change-id", required=True)
    s.add_argument("--files", required=True, help="逗号分隔的候选相对路径")
    s.add_argument("--sandbox-root", required=True, help="v1 所在显式本地沙箱")
    s.add_argument("--dest-prefix", default=DEFAULT_DEST)
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if a.cmd == "build":
        files = [x.strip() for x in str(a.files).split(",") if x.strip()]
        out = build(root, a.change_id, files, a.dest_prefix, a.note)
    elif a.cmd == "verify-local":
        out = verify_local(root, a.manifest)
    elif a.cmd == "sandbox-rehearse":
        files = [x.strip() for x in str(a.files).split(",") if x.strip()]
        out = sandbox_rehearse(root, a.change_id, files, a.sandbox_root, a.dest_prefix)
    else:
        out = rollback_plan(root, a.manifest, a.backup_dir, a.backup_record)
    if out.get("ok"):
        print("V " + str(out.get("msg")))
        if a.cmd == "rollback-plan":
            print("\n".join(out.get("lines") or []))
        return 0
    print("X " + str(out.get("msg")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
