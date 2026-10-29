# -*- coding: utf-8 -*-
"""发布产物清单（不可变台账）生成/校验器（2026-10-27，J3/M2）。

## 它解决什么

`server/update.json` 记录"最新已发布版本"的 tag 与资产 sha256/size；但仓库里没有一份
**入库可追溯**的产物身份台账。`tools/check_download_page.py` 早就在用
`_harden/release_manifest_<ver>.json`（gitignore 证据目录），换机器/清目录就消失。

本工具把台账升级为入库事实 `release_manifests/release_manifest_<ver>.json`：

  - `build`：从**最终构建产物**生成候选台账（写入 size/sha256/version_code/git 信息）；
  - `import-published`：从 `server/update.json` 反向登记**已发布**版本（不依赖本地产物，
    用于补历史台账；0.9.1 用这个补）；
  - `verify`：校验台账形状 + 本地产物是否逐字节一致。

## 不可变规则（与 GitHub Immutable Releases 同精神，2026-10-25 GA）

  - 一旦某版本出现在 `server/update.json`（= 已发布），它的台账与产物身份就不可替换；
  - 本地重建出不同字节 → `build` 拒绝覆盖，只能升版本；
  - 候选版本（> 已发布）允许重建覆盖，旧台账备份到 `_harden/release_manifests_history/`。

用法：
    python tools/release_manifest.py build                     # 用 APP_VERSION / 默认产物路径
    python tools/release_manifest.py build --version 0.9.2 --version-code 902
    python tools/release_manifest.py import-published --version-code 901 --legacy-manifest _harden/release_manifest_0.9.1.json
    python tools/release_manifest.py verify --manifest release_manifests/release_manifest_0.9.1.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS))
import version_truth as vt  # noqa: E402

ROOT = _TOOLS.parent
DEFAULT_OUT_DIR = "release_manifests"
LEGACY_HISTORY_DIR = "_harden/release_manifests_history"


def _read_app_version(root: Path) -> str:
    text = (root / "app" / "core" / "config.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _read_version_code(root: Path) -> int:
    text = (root / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    m = re.search(r'versionCode\s*=\s*(\d+)', text)
    return int(m.group(1)) if m else 0


def _git_info(root: Path) -> dict:
    """best-effort 记录构建源（不阻断；git 不可用就留空）。"""
    info = {"commit": "", "dirty": None, "changed_files": None}
    # 只在真正的仓库根目录跑 git；测试/临时目录不跑（否则 git -C 会向上找到工作区，
    # 在 temp fixture 里触发对整个仓库的 status 扫描，测试会莫名变慢）。
    if not (root / ".git").exists():
        return info
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            info["commit"] = p.stdout.strip()
        p2 = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10)
        if p2.returncode == 0:
            lines = [x for x in p2.stdout.splitlines() if x.strip()]
            info["dirty"] = bool(lines)
            info["changed_files"] = len(lines)
    except Exception:
        pass
    return info


def _published_tag(root: Path) -> str:
    """server/update.json 的 tag（已发布版本）。读不到返回 ''。"""
    raw, err = vt.load_json(root / "server" / "update.json")
    if err or not isinstance(raw, dict):
        return ""
    return vt.normalize_tag(raw.get("tag"))


def _load_ledger(path: Path):
    data, err = vt.load_json(path)
    return data, err


def _manifest_dict(root: Path, version: str, version_code: int, artifacts: list,
                   released_at: str = "", note: str = "", source: str = "") -> dict:
    return {
        "schema": 2,
        "version": version,
        "version_code": int(version_code),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "released_at": released_at,
        "note": note,
        "source": source,
        "git": _git_info(root),
        "artifacts": artifacts,
    }


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _same_identity(a: dict, b: dict) -> bool:
    """比较两份台账的产物身份（只比 version / version_code / name+size+sha256）。"""
    if vt.normalize_tag(a.get("version")) != vt.normalize_tag(b.get("version")):
        return False
    try:
        if int(a.get("version_code") or 0) != int(b.get("version_code") or 0):
            return False
    except (TypeError, ValueError):
        return False
    aa, bb = vt.ledger_artifacts(a), vt.ledger_artifacts(b)
    if set(aa) != set(bb):
        return False
    for name in aa:
        try:
            if int(aa[name].get("size")) != int(bb[name].get("size")):
                return False
        except (TypeError, ValueError):
            return False
        if str(aa[name].get("sha256") or "").lower() != str(bb[name].get("sha256") or "").lower():
            return False
    return True


def build(root: Path, version: str = "", version_code: int = 0,
          apk: str = "", exe: str = "", out_dir: str = DEFAULT_OUT_DIR,
          released_at: str = "", note: str = "", dry_run: bool = False) -> dict:
    """从最终产物生成候选台账。返回 {ok, path, msg}。"""
    version = vt.normalize_tag(version or _read_app_version(root))
    if not version:
        return {"ok": False, "path": None, "msg": "无法确定版本号（APP_VERSION / --version）"}
    version_code = int(version_code or _read_version_code(root))
    if version_code <= 0:
        return {"ok": False, "path": None, "msg": "无法确定 versionCode（build.gradle.kts / --version-code）"}

    pub_tag = _published_tag(root)
    if pub_tag and vt.compare(version, pub_tag) < 0:
        return {"ok": False, "path": None, "msg": f"候选 {version} 低于已发布 {pub_tag}，禁止回退"}

    artifacts = []
    for kind, rel in (("apk", apk or vt.ASSET_PATHS["apk"]), ("exe", exe or vt.ASSET_PATHS["exe"])):
        fp = Path(rel)
        if not fp.is_absolute():
            fp = root / rel
        if not fp.is_file():
            return {"ok": False, "path": None, "msg": f"缺少 {kind} 产物: {fp}"}
        artifacts.append({
            "name": vt.ASSET_NAMES[kind],
            "path": str(Path(rel).as_posix()),
            "size": fp.stat().st_size,
            "sha256": vt.sha256_file(fp),
            "version": version,
        })

    out = (root / out_dir / f"release_manifest_{version}.json").resolve()
    old = None
    if out.is_file():
        old, err = _load_ledger(out)
        if err:
            return {"ok": False, "path": out, "msg": f"已有台账不可读: {err}"}

    if pub_tag and version == pub_tag:
        if old is None:
            return {"ok": False, "path": None,
                    "msg": "该版本已发布但缺台账；不能从本地重建反推发布身份，请先 import-published"}
        cand = _manifest_dict(root, version, version_code, artifacts, released_at, note)
        if not _same_identity(old, cand):
            return {"ok": False, "path": out,
                    "msg": f"拒绝：{version} 已发布，本地产物与已发布台账不一致（同版本产物不可替换，差异只能升版本）"}
        return {"ok": True, "path": out, "msg": f"{version} 已发布且产物一致（幂等，未改写台账）"}

    # 候选版本：允许重建覆盖；旧台账留档到 _harden（证据，不是权威）。
    # 注意先判"产物没变"——候选阶段重复 build 必须幂等，不能每次写一个新 generated_at。
    cand = _manifest_dict(root, version, version_code, artifacts, released_at, note,
                          source="build: " + ", ".join(a["name"] for a in artifacts))
    if old is not None and _same_identity(old, cand):
        return {"ok": True, "path": out, "msg": f"{version} 候选产物未变化（幂等，未改写台账）"}
    if old is not None and not dry_run:
        stamp = str(old.get("generated_at") or "old").replace(":", "").replace("+", "_")
        bak = root / LEGACY_HISTORY_DIR / version / f"{stamp}.json"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, bak)

    if dry_run:
        return {"ok": True, "path": out, "msg": f"[dry-run] 将写入 {out}", "manifest": cand}
    _atomic_write(out, cand)
    return {"ok": True, "path": out, "msg": f"已写入 {out}", "manifest": cand}


def import_published(root: Path, version: str = "", version_code: int = 0,
                     legacy_manifest: str = "", out_dir: str = DEFAULT_OUT_DIR,
                     note: str = "") -> dict:
    """从 server/update.json 反向登记已发布台账（不依赖本地产物）。"""
    pub = vt.load_published_manifest(root)
    if pub["err"]:
        return {"ok": False, "path": None, "msg": f"server/update.json 不可用: {pub['err']}"}
    san = pub["sanitized"] or {}
    tag = vt.normalize_tag(san.get("tag"))
    version = vt.normalize_tag(version or tag)
    if not tag or version != tag:
        return {"ok": False, "path": None,
                "msg": f"import-published 只能登记当前已发布版本：update.json={tag!r} 请求={version!r}"}

    legacy = None
    if legacy_manifest:
        lp = Path(legacy_manifest)
        if not lp.is_absolute():
            lp = root / legacy_manifest
        legacy, err = _load_ledger(lp)
        if err:
            return {"ok": False, "path": None, "msg": f"legacy manifest 不可读: {err}"}

    if not version_code:
        if legacy:
            try:
                version_code = int(legacy.get("version_code"))
            except (TypeError, ValueError):
                version_code = 0
        if not version_code:
            return {"ok": False, "path": None, "msg": "import-published 需要 --version-code（或 legacy manifest 里带）"}
    if int(version_code) <= 0:
        return {"ok": False, "path": None, "msg": "version_code 必须为正整数"}

    artifacts = vt.artifact_entries_from_published(san)
    if len(artifacts) != 2:
        return {"ok": False, "path": None, "msg": f"update.json 资产不完整（需要 apk+exe，实际 {len(artifacts)} 个）"}
    fails = []
    for a in artifacts:
        if not vt.SHA256_RE.fullmatch(a["sha256"]):
            fails.append(f"{a['name']} sha256 非法")
        if int(a["size"]) <= 0:
            fails.append(f"{a['name']} size 非法")
        if legacy is not None:
            le = vt.ledger_artifacts(legacy).get(a["name"]) or {}
            if str(le.get("sha256") or "").lower() != a["sha256"] or int(le.get("size") or 0) != int(a["size"]):
                fails.append(f"{a['name']} 与 legacy manifest 的哈希/大小不一致")
            if le.get("built_at"):
                a["built_at"] = le["built_at"]
    if fails:
        return {"ok": False, "path": None, "msg": "import 校验失败: " + "; ".join(fails)}

    released_at = str(san.get("released_at") or "")
    data = _manifest_dict(root, version, int(version_code), artifacts,
                          released_at=released_at, note=note,
                          source="import-published from server/update.json")
    # 已发布台账记录的是"发布时"的身份；import 发生在事后，当前工作区 git 状态与发布无关，
    # 不能写进去冒充构建来源。只沿用 legacy manifest 里真实记录过的 git 信息。
    if isinstance(legacy, dict) and isinstance(legacy.get("git"), dict):
        data["git"] = legacy["git"]
    else:
        data["git"] = {"commit": "", "dirty": None, "changed_files": None}
    data["imported_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    out = (root / out_dir / f"release_manifest_{version}.json").resolve()
    if out.is_file():
        old, err = _load_ledger(out)
        if err:
            return {"ok": False, "path": out, "msg": f"已有台账不可读: {err}"}
        if not _same_identity(old, data):
            return {"ok": False, "path": out,
                    "msg": "拒绝：已存在同版本但不同身份的台账（已发布产物不可替换）"}
        return {"ok": True, "path": out, "msg": f"{version} 台账已存在且身份一致（幂等）"}
    _atomic_write(out, data)
    return {"ok": True, "path": out, "msg": f"已登记已发布台账 {out}"}


def verify(root: Path, manifest: str) -> dict:
    mp = Path(manifest)
    if not mp.is_absolute():
        mp = root / manifest
    data, err = _load_ledger(mp)
    if err:
        return {"ok": False, "msg": f"台账不可读: {err}"}
    fails = vt.validate_ledger(data)
    if not fails:
        afails, _ = vt.check_artifacts(root, data)
        fails.extend(afails)
    return {"ok": not fails, "msg": "PASS" if not fails else "FAIL: " + "; ".join(fails)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发布产物清单（不可变台账）生成/校验")
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="从最终产物生成候选台账")
    b.add_argument("--version", default="")
    b.add_argument("--version-code", type=int, default=0)
    b.add_argument("--apk", default="")
    b.add_argument("--exe", default="")
    b.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    b.add_argument("--released-at", default="")
    b.add_argument("--note", default="")
    b.add_argument("--dry-run", action="store_true")

    i = sub.add_parser("import-published", help="从 update.json 反向登记已发布台账")
    i.add_argument("--version", default="")
    i.add_argument("--version-code", type=int, default=0)
    i.add_argument("--legacy-manifest", default="")
    i.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    i.add_argument("--note", default="")

    v = sub.add_parser("verify", help="校验台账 + 本地产物")
    v.add_argument("--manifest", required=True)

    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    if a.cmd == "build":
        r = build(root, a.version, a.version_code, a.apk, a.exe, a.out_dir,
                  a.released_at, a.note, a.dry_run)
    elif a.cmd == "import-published":
        r = import_published(root, a.version, a.version_code, a.legacy_manifest, a.out_dir, a.note)
    else:
        r = verify(root, a.manifest)
    print(("V " if r.get("ok") else "X ") + str(r.get("msg")))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
