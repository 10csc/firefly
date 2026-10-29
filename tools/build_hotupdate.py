#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热更新 · 补丁产出器（v2：`web/` 前端层 + `py/` Python 层）。

见 `docs/热更新/02_实现契约.md`。本脚本负责**发布端**的全部工作，且严格按模块铁律：
**审查（输入合法）→ 处理（组装+签名）→ 验证（重新读包校验+公钥自检）→ 输出**。

用法：
    python tools/build_hotupdate.py --init                    # 建立 base 基线（base 刚发布时跑一次）
    python tools/build_hotupdate.py --note "修语音条间距"       # 产出累积补丁（serial 自动 +1）
    python tools/build_hotupdate.py --init --note "…"         # 一次做完（新 base 首发补丁）
    python tools/build_hotupdate.py --rollout-percent 100 --promotion-of 3
                                                              # 同内容灰度晋级 → serial 4

产物（默认 `hotupdate_dist/prod/`，**gitignored**，上传到服务器即可）：
    <dist>/<base>/latest                       {"base":"0.8.1","serial":3}   ← 客户端先拿它
    <dist>/<base>/patch-<N>.json               {v, manifest_b64, sig}
    <dist>/<base>/patch-<N>.zip

设计要点（都踩过坑，别改）：
  · **累积式**：每个包都是 `base → 当前工作区` 的完整差异，客户端不必按序应用。
  · **只增/只改**：发现"基线里有、工作区没有"的文件 → **直接拒绝**（删文件必须发整包）。
  · **传规范化字节**（manifest_b64）而不是 manifest 对象，消灭跨语言 JSON 规范化风险。
  · **已存在的 serial 永不覆盖**（内容寻址+不可变）。
  · **两层同属一个 serial**（规范 §三）：`web/` 放 `app/static/**`，`py/` 放 `app/**/*.py`。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
STATIC_DIR = ROOT / "app" / "static"
CONFIG_PY = ROOT / "app" / "core" / "config.py"
GRADLE = ROOT / "android" / "app" / "build.gradle.kts"

# ★ py 层不打包的目录（写入即拒绝，不是"警告"）：
#   · hotupdate/ —— 它是补丁链路的**自身的加载器**。安卓端在注入覆盖层之前就已经
#     import 了它（start_server 里 `hotupdate_root` 那条路的必经之路），覆盖它只会造成
#     "写 state 的和读 state 的不是同一份代码"，是最难查的一类不一致。改热更自身 = 发整包。
#   · assets/ —— 角色卡资产属用户数据域，规范 §一 明令禁止混进热更。
#   · __pycache__ —— 编译产物，不是源。
PY_SKIP_DIRS = {"__pycache__", "hotupdate", "assets"}

DEFAULT_KEY = Path.home() / ".firefly" / "hotupdate_key.pem"
DEFAULT_BUILD = ROOT / ".tmp_hotupdate"          # 基线 + serial 计数（gitignored）
DEFAULT_DIST = ROOT / "hotupdate_dist" / "prod"  # 生产候选区（gitignored，上传用）

_OPENSSL_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    "/usr/bin/openssl",
)


# ── 工具 ────────────────────────────────────────
def die(msg: str, code: int = 1):
    print(f"X {msg}")
    sys.exit(code)


def find_openssl() -> str:
    import os
    p = os.environ.get("OPENSSL") or shutil.which("openssl")
    if p and Path(p).is_file():
        return p
    for c in _OPENSSL_CANDIDATES:
        if Path(c).is_file():
            return c
    die("找不到 openssl（签名要用）。装一个或用环境变量 OPENSSL 指定。")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def canonical_bytes(obj) -> bytes:
    """★ 规范化**只有一处**：转发到 `app/hotupdate/net.py` 的同名函数。

    早期版本在这里自己写了一份 json.dumps(...)，那样就有两个地方定义签名口径 ——
    改了一处忘另一处，症状是"产出的补丁全部验签失败"。现在工具与客户端共用同一个实现。
    """
    sys.path.insert(0, str(ROOT / "app"))
    from hotupdate.net import canonical_bytes as _cb
    return _cb(obj)


def app_version() -> str:
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                  CONFIG_PY.read_text(encoding="utf-8"))
    if not m:
        die(f"读不到 APP_VERSION：{CONFIG_PY}")
    return m.group(1)


def version_code() -> int:
    m = re.search(r"versionCode\s*=\s*(\d+)", GRADLE.read_text(encoding="utf-8"))
    if not m:
        die(f"读不到 versionCode：{GRADLE}")
    return int(m.group(1))


def collect_web() -> dict:
    """web 层：{相对 app/static 的 posix 路径: {"sha256":…, "size":…}}"""
    out = {}
    for p in sorted(STATIC_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(STATIC_DIR).as_posix()
        out[rel] = {"sha256": sha256_file(p), "size": p.stat().st_size}
    if not out:
        die(f"静态目录为空：{STATIC_DIR}")
    return out


def collect_py() -> dict:
    """py 层：{相对 app/ 的 posix 路径: {"sha256":…, "size":…}}，只收 `.py`。

    ★ 只收 `.py` 是**故意的窄口径**：模型/配置/知识库等数据资产不属于热更
    （规范 §一：角色包数据与 database/ 走原有资源下发链路）。补丁只修代码。
    """
    out = {}
    for p in sorted(APP_DIR.rglob("*.py")):
        if not p.is_file():
            continue
        rel = p.relative_to(APP_DIR)
        if any(part in PY_SKIP_DIRS for part in rel.parts):
            continue
        out[rel.as_posix()] = {"sha256": sha256_file(p), "size": p.stat().st_size}
    return out


def collect_all() -> dict:
    """两层的合并视图：键带层前缀（`web/…` / `py/…`），与 manifest.files 的 path 一一对应。"""
    out = {f"web/{k}": v for k, v in collect_web().items()}
    out.update({f"py/{k}": v for k, v in collect_py().items()})
    return out


def layer_of(paths) -> str:
    have = [l for l in ("web", "py") if any(str(p).startswith(l + "/") for p in paths)]
    return "+".join(have)


def py_package_roots(paths) -> list[str]:
    """返回补丁触及的**顶层 Python 包**（`py/api/x.py` → `api`）。"""
    roots = set()
    for raw in paths:
        arc = str(raw)
        if not arc.startswith("py/"):
            continue
        parts = arc[3:].split("/")
        if len(parts) >= 2 and parts[0]:
            roots.add(parts[0])
    return sorted(roots)


def expand_py_package_closure(changed: dict, current: dict) -> tuple[dict, list[str], list[str]]:
    """把触及的顶层 Python 包补全为**完整包闭包**。

    ★ 2026-10-26 真机事故：hot3 只带了 `py/api/data_sync.py`，没有 `py/api/__init__.py`。
    `sys.path` 虽然首位移到覆盖层，但 Python 在覆盖层找不到 `api` 包，就从 APK 底座加载了
    旧 `api` 包；旧包的 `__path__` 又只指向底座目录，因此新 `data_sync.py` 永远不可见，
    最终 `ImportError: cannot import name 'sync_ops'`。

    只补 `__init__.py` 仍不够：有 `__init__.py` 的普通包会把 `__path__` 收窄到覆盖层，
    包里没被带上的兄弟模块会从"旧版可用"变成"导入失败"。所以这里采用更保守、
    更容易验证的规则：**改到某一个顶层包里的任何文件，就带上该包当前全部 `.py`**。
    包大了几十 KB，但不会制造半套包。

    返回 `(扩展后的 changed, 触及的包, 缺 __init__.py 的包)`。缺初始化文件的包由调用方拒绝；
    本项目所有包含 Python 模块的包都必须有 `__init__.py`。
    """
    out = dict(changed)
    roots = py_package_roots(changed)
    missing_init = []
    for root in roots:
        init = f"py/{root}/__init__.py"
        if init not in current:
            missing_init.append(root)
            continue
        prefix = f"py/{root}/"
        for arc, meta in current.items():
            if str(arc).startswith(prefix):
                out[str(arc)] = meta
    return out, roots, missing_init


def sign(key: Path, data: bytes) -> bytes:
    r = subprocess.run([find_openssl(), "dgst", "-sha256", "-sign", str(key)],
                       input=data, capture_output=True)
    if r.returncode != 0:
        die("签名失败: " + r.stderr.decode("utf-8", "replace")[:300])
    return r.stdout


def verify_sig(key: Path, data: bytes, sig: bytes) -> bool:
    """用私钥导出公钥自检（产出后立刻验一次，验不过不许发布）。"""
    ossl = find_openssl()
    pub = subprocess.run([ossl, "pkey", "-in", str(key), "-pubout"],
                         capture_output=True)
    if pub.returncode != 0:
        return False
    tmp = ROOT / ".tmp_hotupdate" / "_pub.pem"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(pub.stdout)
    r = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(tmp)],
                       input=data, capture_output=True)
    # openssl 的 -verify 需要签名单独给；这里用 -signature 形式重来
    sigf = tmp.with_suffix(".sig")
    sigf.write_bytes(sig)
    r = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(tmp),
                        "-signature", str(sigf)],
                       input=data, capture_output=True)
    tmp.unlink(missing_ok=True)
    sigf.unlink(missing_ok=True)
    return r.returncode == 0


# ── 主流程 ──────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="热更新补丁产出器（v2：web 层 + py 层）")
    ap.add_argument("--init", action="store_true", help="建立/刷新 base 基线")
    ap.add_argument("--note", default="", help="本补丁说明（写进 manifest/CHANGELOG）")
    ap.add_argument("--rollout-percent", type=int, default=10,
                    help="灰度比例 1..100；先用 10%% 验证，再产出下一个 serial 升到 100%%")
    ap.add_argument("--promotion-of", type=int, default=0, metavar="SERIAL",
                    help="同内容灰度晋级：以上一个 serial 的同一文件集产出新 serial")
    ap.add_argument("--key", default=str(DEFAULT_KEY), help="私钥路径")
    ap.add_argument("--build-dir", default=str(DEFAULT_BUILD))
    ap.add_argument("--dist", default=str(DEFAULT_DIST))
    ap.add_argument("--base", default=None, help="base 版本（默认取 APP_VERSION）")
    a = ap.parse_args()

    base = a.base or app_version()
    vcode = version_code()
    build = Path(a.build_dir) / base
    dist = Path(a.dist) / base
    baseline_f = build / "baseline.json"
    serial_f = build / "serial.txt"
    key = Path(a.key).expanduser()
    if not (1 <= a.rollout_percent <= 100):
        die("--rollout-percent 必须在 1..100 之间")
    if not key.is_file():
        die(f"私钥不存在：{key}\n  先跑 `python tools/hotupdate_keys.py --gen`")

    print("=== ① 审查 ===")
    if not STATIC_DIR.is_dir():
        die(f"静态目录不存在：{STATIC_DIR}")
    cur = collect_all()
    cur_web = sum(1 for k in cur if k.startswith("web/"))
    cur_py = sum(1 for k in cur if k.startswith("py/"))
    print(f"  V 工作区：web {cur_web} 个文件 / py {cur_py} 个文件（共 {len(cur)}）")
    print(f"  V base = {base}（versionCode {vcode}）")

    # --init：写基线
    if a.init:
        build.mkdir(parents=True, exist_ok=True)
        if baseline_f.exists():
            old = json.loads(baseline_f.read_text(encoding="utf-8"))
            print(f"  ! 已存在基线（{len(old.get('files', {}))} 文件）→ 覆盖为新基线")
        baseline_f.write_text(json.dumps(
            {"base_version": base, "base_version_code": vcode, "files": cur},
            ensure_ascii=False, indent=1), encoding="utf-8")
        serial_f.write_text("0", encoding="utf-8")
        print(f"  V 基线已写入：{baseline_f}（serial 归零）")
        if not a.note:
            print("  （只建基线，未产出补丁。要出补丁请再跑一次不带 --init）")
            return 0

    if not baseline_f.is_file():
        die(f"没有基线：{baseline_f}\n  先跑一次 `--init`（在 base 版本刚发布时做）。")
    bl = json.loads(baseline_f.read_text(encoding="utf-8"))
    if bl.get("base_version") != base:
        die(f"基线 base 不符：基线 {bl.get('base_version')} vs 当前 {base}")
    base_files = bl["files"]

    # ★ v1 → v2 基线迁移（一次性、幂等）：v1 基线只含 `web/…`。
    #   若直接按 v2 口径比 diff，会把**每一个 py 文件**都当成"新增"，产出一个假的大补丁。
    #   所以先把缺的层以**当前工作区**的哈希补齐 —— 迁移后 diff 仍真实反映"之后改了什么"。
    if not a.init:
        # 2026-10-25 修复：只对**整层缺失**做 v1→v2 基线迁移。
        # 旧实现把任何“基线里没有的文件”都补进基线，导致之后新增的 py 模块
        # （例如 modules/sync_state.py）永远不进补丁——客户端 import 失败。
        layers_present = {k.split("/", 1)[0] for k in base_files}
        missing_layers = {k.split("/", 1)[0] for k in cur} - layers_present
        absent = [k for k in cur
                  if k not in base_files and k.split("/", 1)[0] in missing_layers]
        if absent:
            layers = sorted({k.split("/", 1)[0] for k in absent})
            base_files.update({k: cur[k] for k in absent})
            bl["files"] = base_files
            baseline_f.write_text(json.dumps(bl, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
            print(f"  ! 基线缺 {len(absent)} 个文件（层 {layers}）—— 已按当前工作区补齐为基线。")
            print("    这是 v1（只有 web 层）基线的一次性迁移，不计入本次补丁。")

    # ★ 只增/只改：删除必须走整包
    removed = sorted(set(base_files) - set(cur))
    if removed:
        die("检测到**删除**的文件（补丁不允许删文件，请发整包）：\n    " + "\n    ".join(removed))

    changed = {k: v for k, v in cur.items()
               if k not in base_files or base_files[k]["sha256"] != v["sha256"]}
    changed_before_closure = set(changed)
    changed, package_roots, missing_init = expand_py_package_closure(changed, cur)
    if missing_init:
        die("以下 Python 包缺少 __init__.py，覆盖层会退化成底座包，拒绝打包：\n    "
            + "\n    ".join(missing_init))
    closure_added = sorted(set(changed) - changed_before_closure)
    if closure_added:
        print(f"  ! Python 包闭包：触及 {len(package_roots)} 个包 "
              f"({', '.join(package_roots)})，自动补入 {len(closure_added)} 个同包文件")
    if not changed and not a.init:
        die("与基线相比没有任何变化 —— 没有东西可发。")
    new_n = len(set(cur) - set(base_files))
    print(f"  V 变更文件 {len(changed)} 个（新增 {new_n}，"
          f"修改 {len(set(changed) & set(base_files))}）"
          f"  层 = {layer_of(changed)}")

    serial = int(serial_f.read_text(encoding="utf-8").strip() or "0") + 1
    dist.mkdir(parents=True, exist_ok=True)
    zip_name = f"patch-{serial}.zip"
    json_name = f"patch-{serial}.json"

    # 灰度晋级：文件内容不变，只把签名清单的 rollout_percent 升到下一档。
    # latest 不受信任，不能在服务端直接改百分比；必须产出下一个已签名 serial。
    # 其余“内容完全相同但没声明晋级”的重复构建一律拒绝，防止手滑白占 serial。
    prev_manifest = None
    prev_serial = int(a.promotion_of or 0)
    if prev_serial:
        if prev_serial != serial - 1:
            die(f"--promotion-of 必须指向刚发布的 serial {serial - 1}，收到 {prev_serial}")
        prev_json = dist / f"patch-{prev_serial}.json"
        if not prev_json.is_file():
            die(f"要晋级的补丁清单不存在：{prev_json}")
        try:
            payload = json.loads(prev_json.read_text(encoding="utf-8"))
            prev_raw = base64.b64decode(payload["manifest_b64"], validate=True)
            prev_sig = base64.b64decode(payload["sig"], validate=True)
            if not verify_sig(key, prev_raw, prev_sig):
                die(f"serial {prev_serial} 的签名自检失败，禁止在其上晋级")
            prev_manifest = json.loads(prev_raw.decode("utf-8"))
        except SystemExit:
            raise
        except Exception as e:
            die(f"读取 serial {prev_serial} 清单失败：{type(e).__name__}: {e}")
        if int(prev_manifest.get("serial") or 0) != prev_serial:
            die(f"serial {prev_serial} 清单内序号不一致：{prev_manifest.get('serial')}")
        try:
            prev_rollout = int(prev_manifest.get("rollout_percent"))
        except Exception:
            die(f"serial {prev_serial} 的 rollout_percent 非法")
        if a.rollout_percent <= prev_rollout:
            die(f"灰度晋级必须提高比例：serial {prev_serial}={prev_rollout}% "
                f"→ 新 serial={a.rollout_percent}%")
    elif serial > 1 and (dist / f"patch-{serial - 1}.json").is_file():
        try:
            payload = json.loads(
                (dist / f"patch-{serial - 1}.json").read_text(encoding="utf-8"))
            prev_raw = base64.b64decode(payload["manifest_b64"], validate=True)
            prev_manifest = json.loads(prev_raw.decode("utf-8"))
        except Exception:
            prev_manifest = None

    current_files = sorted((p, meta["sha256"]) for p, meta in changed.items())
    if prev_manifest is not None:
        prev_files = sorted((str(f.get("path") or ""), str(f.get("sha256") or ""))
                            for f in (prev_manifest.get("files") or []))
        if current_files == prev_files and not prev_serial:
            die(f"内容与 serial {serial - 1} 完全相同，但未声明同内容灰度晋级。\n"
                f"  若本次就是提升灰度，请加 --promotion-of {serial - 1}；否则先改代码。")
        if prev_serial and current_files != prev_files:
            die("灰度晋级时文件集/哈希必须与上一 serial 完全一致；本次工作区内容变了。")

    for n in (zip_name, json_name):
        if (dist / n).exists():
            die(f"发布区已存在 {n} —— 已发布的补丁**永不覆盖**。"
                f"（要么换 serial，要么先把旧产物移走归档）")

    print("\n=== ② 处理：打包 + 签名 ===")
    # 累积式：changed 已经是"相对 base 的完整差异"，直接全量进包
    zpath = dist / zip_name
    files_meta = []
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for arc in sorted(changed):
            layer, _, rel = arc.partition("/")
            src = (STATIC_DIR / rel) if layer == "web" else (APP_DIR / rel)
            if not src.is_file():
                die(f"清单路径无法映射到源文件：{arc}")
            z.write(src, arc)
            files_meta.append({"path": arc,
                               "sha256": changed[arc]["sha256"],
                               "size": changed[arc]["size"]})
    zip_sha = sha256_file(zpath)
    zip_size = zpath.stat().st_size
    print(f"  V 包：{zip_name}  {zip_size/1024:.1f} KB  sha256 {zip_sha[:12]}…")

    manifest = {
        "format": 1,
        "base_version": base,
        "base_version_code": vcode,
        "serial": serial,
        "issued_at": int(time.time()),
        "cumulative": True,
        "layer": layer_of(changed),
        "note": a.note or "(无说明)",
        "zip": {"name": zip_name, "sha256": zip_sha, "size": zip_size},
        "files": files_meta,
        "rollout_percent": a.rollout_percent,
        "min_safe_serial": 1,
        "revoked_serials": [],
    }
    if prev_serial:
        manifest["promotion_of"] = prev_serial
    raw = canonical_bytes(manifest)
    sig = sign(key, raw)
    payload = {"v": 1,
               "manifest_b64": base64.b64encode(raw).decode("ascii"),
               "sig": base64.b64encode(sig).decode("ascii")}
    (dist / json_name).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    print(f"  V 清单：{json_name}（规范化 {len(raw)} 字节，签名 {len(sig)} 字节）")
    print(f"  V 层：{manifest['layer']}"
          + ("（含 py，客户端会**重启进程**生效）" if "py" in manifest["layer"] else ""))
    print(f"  V 灰度：{manifest['rollout_percent']}%")

    print("\n=== ③ 验证 ===")
    # 3.1 公钥自检签名
    if not verify_sig(key, raw, sig):
        (dist / json_name).unlink(missing_ok=True)
        (dist / zpath.name).unlink(missing_ok=True)
        die("签名自检失败，已删除产物。")
    print("  V 签名自检（openssl -verify）通过")
    # 3.2 重新读包，逐文件核对（防"写进去的和清单说的不一致"）
    with zipfile.ZipFile(zpath) as z:
        names = sorted(z.namelist())
        want = sorted(f["path"] for f in files_meta)
        if names != want:
            die(f"包内文件与清单不一致：包 {names} vs 清单 {want}")
        for f in files_meta:
            got = hashlib.sha256(z.read(f["path"])).hexdigest()
            if got != f["sha256"]:
                die(f"包内 {f['path']} 校验不符")
    print(f"  V 回读校验：{len(names)} 个文件 sha256 全部匹配")
    if sha256_file(zpath) != zip_sha:
        die("zip 本体 sha256 与清单不符（写出后又被改？）")
    print("  V zip 本体 sha256 匹配")

    print("\n=== ④ 输出 ===")
    (dist / "latest").write_text(
        json.dumps({"base": base, "serial": serial}, ensure_ascii=False),
        encoding="utf-8")
    serial_f.write_text(str(serial), encoding="utf-8")
    # CHANGELOG：规范 §八要求每条必填「序号/日期/改了哪些文件/为什么」——
    # 这是用户报 bug 时"我到底跑的什么代码"的唯一凭据，不能靠记忆。
    log = Path(a.dist) / "CHANGELOG.md"
    if not log.exists():
        log.write_text("# 热更新 CHANGELOG\n\n"
                       "> 每条对应一个不可变补丁。复现运行版 = base commit + 依次应用 1..N。\n"
                       "> 代码本体在 git（tag hot-<base>-<serial>），这里只记「改了什么 / 为什么」。\n\n",
                       encoding="utf-8")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"## {base} · serial {serial} · "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime())}\n")
        f.write(f"- 说明：{a.note or '(无)'}\n")
        if prev_serial:
            f.write(f"- 灰度晋级：serial {prev_serial} → {serial}，"
                    f"{a.rollout_percent}%\n")
        f.write(f"- 包：`{zip_name}`（{zip_size / 1024:.1f} KB，sha256 `{zip_sha[:16]}…`）\n")
        f.write(f"- 文件（{len(files_meta)}）：\n")
        for fm in files_meta:
            f.write(f"  - `{fm['path']}`\n")
        f.write(f"- git tag：`hot-{base}-{serial}`（**发版时务必打**）\n\n")
    print(f"  V 发布区：{dist}")
    print(f"    latest            → {{'base':'{base}','serial':{serial}}}")
    print(f"    {json_name}")
    print(f"    {zip_name}")
    print(f"    CHANGELOG.md（已追加 serial {serial} 条目）")
    print(f"  V serial 计数推进到 {serial}")
    print(f"\n  下一步：python tools/check_hotupdate.py --dist {dist}   （第 5 门禁）")
    print(f"         然后整目录上传到服务器 hotupdate/{base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
