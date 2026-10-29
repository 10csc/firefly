# -*- coding: utf-8 -*-
"""版本真相读取与校验（标准库；无第三方依赖）。

## 为什么拆出这个模块（2026-10-27，J3/M2）

`tools/check_version.py` 原来只比较 7 处版本号字面量。它有两个盲区（未完成清单 P6-1/P6-2）：
  1. 不校验 `server/update.json`——它是服务器端"最新已发布版本"的**唯一真相源**；
  2. 不比较"本地重建产物"与"已发布产物"的哈希——版本号没变、字节却变了也能 PASS。

本模块把"读真相 / 判语义"抽出来，供 `check_version.py`、`tools/release_manifest.py`
以及后续 M2 场景门禁复用。所有函数**只读**，root 由调用方传入（可测试、可在干净 clone 上运行）。

## 事实模型（与 docs/治理/03_状态地图.md 对齐）

  - candidate（开发/候选版本）：`app/core/config.py::APP_VERSION` + 各处投影
  - published（最新已发布版本）：`server/update.json`（tag/assets/sha256/size/sig）
  - compatibility projection：`server/version.json`（由 update.json 生成，禁止手写）
  - immutable artifact identity：`release_manifests/release_manifest_<ver>.json`（入库，不可变）
  - build evidence：`_harden/release_manifest_<ver>.json`（旧位置，只作历史证据，不是权威）

核心规则：本地重建产物可以与被发布产物不同；**同一版本一旦发布，产物身份不可替换**，
差异要发布就必须升版本。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
from pathlib import Path

PROFILES = ("dev", "release", "deploy")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
KINDS = ("apk", "exe")
ASSET_NAMES = {"apk": "firefly.apk", "exe": "firefly-setup.exe"}
ASSET_PATHS = {"apk": "android/firefly.apk", "exe": "package/firefly-setup.exe"}
LEDGER_DIR = "release_manifests"
LEGACY_LEDGER_DIR = "_harden"
# 与 server/update_manifest.py::_OFFICIAL_ASSET_PREFIXES 同口径；模块缺失时兜底。
OFFICIAL_ASSET_PREFIXES = (
    "https://gitee.com/cpt-asymmetry/firefly/releases/download/",
    "https://github.com/10csc/firefly/releases/download/",
)


# ── 基础版本号处理（与 server/update_manifest.py 同口径）────────
def normalize_tag(tag) -> str:
    """'v0.9.1' / '0.9.1' → '0.9.1'；非法返回 ''。

    必须与 server/update_manifest.py::normalize_tag 完全同口径：
    只允许单个小写 v 前缀，禁止大写 V / 多个 v / 后缀 / 段数不足。
    """
    t = str(tag or "").strip()
    if t.startswith("v"):
        t = t[1:]
    return t if VERSION_RE.fullmatch(t) else ""


def parse_version(tag) -> tuple:
    n = normalize_tag(tag)
    return tuple(int(x) for x in n.split(".")) if n else (0, 0, 0)


def compare(a, b) -> int:
    pa, pb = parse_version(a), parse_version(b)
    for i in range(3):
        d = pa[i] - pb[i]
        if d:
            return d
    return 0


# ── 文件 / JSON 读取 ───────────────────────────────────────────
def read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def load_json(path: Path):
    """返回 (obj, err)。文件不存在 → (None, 'missing')。"""
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── server/update.json 的加载与审查 ────────────────────────────
def load_update_manifest_module(root: Path):
    """按路径动态加载 `server/update_manifest.py`（不污染 sys.modules）。

    返回 (module, err)。server/ 不存在（干净 clone / 仅客户端检出）→ (None, 'missing')。
    不直接用 sys.path 导入，避免测试多 root 时模块缓存串台。
    """
    path = root / "server" / "update_manifest.py"
    if not path.is_file():
        return None, "missing"
    try:
        name = f"_firefly_update_manifest_{abs(hash(str(path)))}"
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            return None, "spec 创建失败"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def load_published_manifest(root: Path) -> dict:
    """读 server/update.json，并用服务器自己的 sanitize() 审查。

    返回 dict：
      raw, sanitized, module, path, err
    err: '' 成功 / 'missing' 文件不存在 / 其它为解析或审查失败
    """
    out = {"raw": None, "sanitized": None, "module": None,
           "path": root / "server" / "update.json", "err": ""}
    raw, err = load_json(out["path"])
    if err:
        out["err"] = err
        return out
    out["raw"] = raw
    mod, merr = load_update_manifest_module(root)
    out["module"] = mod
    if mod is None:
        out["err"] = "update_manifest 模块缺失: " + merr
        return out
    try:
        out["sanitized"] = mod.sanitize(raw)
    except Exception as e:
        out["err"] = f"sanitize 异常: {type(e).__name__}: {e}"
    return out


def project_version_json(sanitized: dict, module=None) -> dict:
    """update.json → version.json 兼容投影。优先用服务器模块，保持单一实现。"""
    if module is not None and hasattr(module, "project_version_json"):
        try:
            return module.project_version_json(sanitized or {})
        except Exception:
            pass
    m = sanitized or {}
    assets = m.get("assets") or {}
    return {
        "tag": ("v" + m["tag"]) if m.get("tag") else "",
        "exe": (assets.get("exe") or {}).get("url", ""),
        "apk": (assets.get("apk") or {}).get("url", ""),
    }


def load_version_json(root: Path) -> dict:
    """返回 (obj, err)。"""
    return load_json(root / "server" / "version.json")


def projection_diff(projected: dict, actual) -> list[str]:
    """比较投影与 server/version.json。只比较 3 个兼容字段；额外字段给 warning 由调用方处理。"""
    diffs = []
    if not isinstance(actual, dict):
        return ["server/version.json 不是 JSON 对象"]
    for k in ("tag", "exe", "apk"):
        if str((actual or {}).get(k) or "") != str(projected.get(k) or ""):
            diffs.append(f"{k}: 投影={projected.get(k)!r} 实际={actual.get(k)!r}")
    return diffs


def sig_shape_ok(raw: dict) -> bool:
    """更新清单签名形状：base64 解码后 256 字节（RSA-2048）。真实验签另跑 check_update_manifest.py。"""
    try:
        sig = str((raw or {}).get("sig") or "").strip()
        return bool(sig) and len(base64.b64decode(sig, validate=True)) == 256
    except Exception:
        return False


def validate_update_manifest(raw, sanitized, module, profile: str) -> tuple[list[str], list[str]]:
    """审查 update.json 的字段与 URL 语义。返回 (failures, warnings)。

    profile 决定严格度：dev 容忍"平台未重发"等历史形态；release/deploy 要求完整。
    """
    fails: list[str] = []
    warns: list[str] = []
    strict = profile in ("release", "deploy")
    if not isinstance(raw, dict):
        return ["server/update.json 不是 JSON 对象"], warns
    if not isinstance(sanitized, dict) or not sanitized.get("tag"):
        return ["server/update.json 的 tag 缺失或非法（应为 x.y.z）"], warns

    tag = sanitized["tag"]

    # 1) 原始字段被 sanitize 丢弃 ⇒ 说明清单里有非法字段（例如 http 资产 URL / 坏 sha256）
    raw_assets = raw.get("assets") if isinstance(raw.get("assets"), dict) else {}
    clean_assets = sanitized.get("assets") or {}
    for kind in KINDS:
        ra = raw_assets.get(kind) if isinstance(raw_assets.get(kind), dict) else {}
        ca = clean_assets.get(kind) or {}
        if ra.get("url") and not ca.get("url"):
            fails.append(f"{kind}.url 未通过官方 https 前缀校验: {str(ra.get('url'))[:100]!r}")
        if ra.get("sha256") and not ca.get("sha256"):
            fails.append(f"{kind}.sha256 格式非法")
        if ra.get("size") and not ca.get("size"):
            fails.append(f"{kind}.size 非法")
        if strict:
            for f in ("url", "sha256", "size"):
                if not ca.get(f):
                    fails.append(f"{kind}.{f} 缺失（release/deploy 要求完整）")

    # 2) 资产 URL 必须钉在当前 tag 上（防 P6-2：tag 说新版本、URL 仍指向旧版本）
    prefixes = getattr(module, "_OFFICIAL_ASSET_PREFIXES", OFFICIAL_ASSET_PREFIXES) if module else OFFICIAL_ASSET_PREFIXES
    for kind, a in clean_assets.items():
        url = str((a or {}).get("url") or "")
        if not url:
            continue
        name = ASSET_NAMES.get(kind, "")
        if not any(url.startswith(p) for p in prefixes):
            fails.append(f"{kind}.url 不是官方仓库路径: {url[:100]!r}")
        if f"/download/v{tag}/" not in url:
            fails.append(f"{kind}.url 未指向当前 tag v{tag}: {url[:120]!r}")
        if name and not url.endswith(name):
            fails.append(f"{kind}.url 文件名不是 {name}: {url[:120]!r}")

    # 3) 签名形状（真实验签交给 check_update_manifest.py，避免这里重复实现密码学）
    if strict and not sig_shape_ok(raw):
        fails.append("sig 缺失或不是 RSA-2048 base64 形状（release/deploy 要求已签名清单）")
    elif not sig_shape_ok(raw):
        warns.append("server/update.json 没有可识别的 sig（旧客户端忽略；release 前必须补签名）")

    # 4) notes_url / released_at / min_supported 的软检查
    notes = str(sanitized.get("notes_url") or "")
    if notes and f"/tag/v{tag}" not in notes:
        warns.append(f"notes_url 未指向 tag v{tag}: {notes[:100]!r}")
    released = str(sanitized.get("released_at") or "")
    if released and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", released):
        warns.append(f"released_at 不是 YYYY-MM-DD: {released!r}")
    ms = sanitized.get("min_supported") or "0.0.0"
    if compare(ms, tag) > 0:
        fails.append(f"min_supported={ms} 大于当前 tag={tag}")
    return fails, warns


# ── 不可变台账（release_manifests/）─────────────────────────────
def ledger_path(root: Path, version: str) -> Path:
    return root / LEDGER_DIR / f"release_manifest_{normalize_tag(version)}.json"


def legacy_ledger_path(root: Path, version: str) -> Path:
    return root / LEGACY_LEDGER_DIR / f"release_manifest_{normalize_tag(version)}.json"


def find_ledger(root: Path, version: str, allow_legacy: bool = True) -> dict:
    """读取台账。返回 {path, data, legacy, err}。"""
    v = normalize_tag(version)
    cand = ledger_path(root, v)
    if cand.is_file():
        data, err = load_json(cand)
        return {"path": cand, "data": data, "legacy": False, "err": err}
    if allow_legacy:
        old = legacy_ledger_path(root, v)
        if old.is_file():
            data, err = load_json(old)
            return {"path": old, "data": data, "legacy": True, "err": err}
    return {"path": None, "data": None, "legacy": False, "err": "missing"}


def validate_ledger(data: dict, version: str = "") -> list[str]:
    """校验台账形状。只返回错误，不做跨文件比较。"""
    fails = []
    if not isinstance(data, dict):
        return ["台账不是 JSON 对象"]
    v = normalize_tag(data.get("version"))
    if not v:
        fails.append("台账 version 缺失或非法")
    elif version and v != normalize_tag(version):
        fails.append(f"台账 version={v} 与期望 {version} 不一致")
    try:
        vc = data.get("version_code")
        if not isinstance(vc, int) or isinstance(vc, bool) or vc <= 0:
            fails.append("台账 version_code 必须是正整数（不接受字符串/布尔）")
    except Exception:
        fails.append("台账 version_code 缺失或非法")
    arts = data.get("artifacts")
    if not isinstance(arts, list) or not arts:
        fails.append("台账 artifacts 必须是非空数组")
        return fails
    seen = set()
    for i, a in enumerate(arts):
        if not isinstance(a, dict):
            fails.append(f"artifacts[{i}] 不是对象")
            continue
        name = str(a.get("name") or "").strip()
        if name not in ASSET_NAMES.values():
            fails.append(f"artifacts[{i}] name={name!r} 不是 {sorted(ASSET_NAMES.values())}")
        if name in seen:
            fails.append(f"artifacts 中 name 重复: {name}")
        seen.add(name)
        if not SHA256_RE.fullmatch(str(a.get("sha256") or "").strip().lower()):
            fails.append(f"artifacts[{i}] sha256 非法")
        size = a.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            fails.append(f"artifacts[{i}] size 必须是正整数（不接受字符串/布尔）")
    for kind, name in ASSET_NAMES.items():
        if name not in seen:
            fails.append(f"台账缺 {kind} 产物（{name}）")
    return fails


def ledger_artifacts(data: dict) -> dict:
    """name → artifact dict。"""
    out = {}
    for a in (data or {}).get("artifacts") or []:
        if isinstance(a, dict) and a.get("name"):
            out[str(a["name"])] = a
    return out


def check_artifacts(root: Path, ledger: dict, paths: dict | None = None) -> tuple[list[str], list[str]]:
    """比较本地产物与台账。返回 (failures, warnings)。

    只比较台账里出现的 name；缺文件算 failure（release/deploy 的调用方要求文件必须存在）。
    """
    fails, warns = [], []
    paths = paths or {}
    arts = ledger_artifacts(ledger)
    for kind, name in ASSET_NAMES.items():
        ent = arts.get(name)
        if not ent:
            continue
        rel = paths.get(kind) or ASSET_PATHS[kind]
        fp = Path(rel)
        if not fp.is_absolute():
            fp = root / rel
        if not fp.is_file():
            fails.append(f"本地缺少 {kind} 产物: {fp}")
            continue
        size = fp.stat().st_size
        sha = sha256_file(fp)
        try:
            want_size = int(ent.get("size"))
        except (TypeError, ValueError):
            want_size = -1
        want_sha = str(ent.get("sha256") or "").strip().lower()
        if size != want_size:
            fails.append(f"{name} 大小不一致: 本地={size} 台账={want_size}")
        if sha != want_sha:
            fails.append(f"{name} sha256 不一致: 本地={sha[:16]}… 台账={want_sha[:16]}…")
    return fails, warns


def artifact_entries_from_published(manifest: dict) -> list[dict]:
    """从已发布 update.json 生成台账 artifacts（import-published 用）。"""
    out = []
    assets = (manifest or {}).get("assets") or {}
    for kind in KINDS:
        a = assets.get(kind) or {}
        name = ASSET_NAMES[kind]
        if a.get("url"):
            out.append({
                "name": name,
                "path": ASSET_PATHS[kind],
                "size": int(a.get("size") or 0),
                "sha256": str(a.get("sha256") or "").lower(),
                "source_url": a.get("url", ""),
            })
    return out
