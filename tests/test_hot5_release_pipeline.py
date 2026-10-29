# -*- coding: utf-8 -*-
"""最新热更新发布物全链路验收。

这份测试只依赖签名 `latest` 指向的真实发布物和 git HEAD 底座，不复用旧的 hotupdate 单测：

  latest -> manifest -> RSA 验签 -> zip 下载 -> 逐文件校验 -> base 重建
  -> 覆盖层新进程导入 -> 并发下载/验签 soak

另外覆盖后续版本最危险的回归点：
  * 补丁内容与当前源码逐字节一致；
  * 当前 serial 累积包含上一 serial 的全部路径；
  * Python 顶层包闭包完整；
  * 灰度落桶稳定；
  * 坏签名、坏 zip 哈希、篡改文件都整包拒绝且不留下半成品。

文件名保留为 test_hot5_release_pipeline.py，避免破坏既有活动测试入口；serial 与文件数
必须从磁盘发布物读取，不能随版本升级继续手改常量。
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
PYTHON = sys.executable
DEFAULT_DIST_ROOT = ROOT / "hotupdate_dist" / "prod"
DECLARE = json.loads((ROOT / "hotupdate_declare.json").read_text(encoding="utf-8"))
DECLARED_ID = str(DECLARE.get("hotupdate") or "").strip()
if DECLARED_ID.lower() == "none":
    if __name__ == "__main__":
        print("SKIP: hotupdate_declare.json 显式声明本次不发热更，跳过候选发布物验收")
        raise SystemExit(0)
    raise __import__("unittest").SkipTest("本次未声明热更新")

DECLARED_BASE, DECLARED_SERIAL = DECLARED_ID.split("/", 1)
LATEST_POINTER = json.loads(
    (DEFAULT_DIST_ROOT / DECLARED_BASE / "latest").read_text(encoding="utf-8")
)
BASE_VERSION = str(LATEST_POINTER["base"])
SERIAL = int(LATEST_POINTER["serial"])
DIST = DEFAULT_DIST_ROOT / BASE_VERSION

if BASE_VERSION != DECLARED_BASE or SERIAL != int(DECLARED_SERIAL):
    raise RuntimeError(
        "hotupdate_declare.json 与 latest 不一致："
        f"declared={DECLARED_BASE}/{DECLARED_SERIAL}, "
        f"latest={BASE_VERSION}/{SERIAL}"
    )

sys.path.insert(0, str(APP))

from hotupdate import _rollout_decision  # noqa: E402
from hotupdate import net, verify  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  V", name)
    else:
        FAIL += 1
        print("  X", name, detail)


class _ReleaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FireflyHotRelease/1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        rel = self.path.split("?", 1)[0].lstrip("/")
        target = (DIST / rel).resolve()
        try:
            target.relative_to(DIST.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if target.name == "latest":
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)


class _ReleaseServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def _manifest(serial: int) -> tuple[bytes, dict, str]:
    payload = json.loads(
        (DIST / f"patch-{serial}.json").read_text(encoding="utf-8")
    )
    raw = base64.b64decode(payload["manifest_b64"], validate=True)
    return raw, json.loads(raw.decode("utf-8")), payload["sig"]


def _source_for(arc: str) -> Path:
    if arc.startswith("py/"):
        return APP / arc[len("py/"):]
    if arc.startswith("web/"):
        return APP / "static" / arc[len("web/"):]
    raise ValueError(arc)


def _git_base(tmp: Path) -> Path:
    archive = tmp / "base.zip"
    result = subprocess.run(
        ["git", "archive", "--format=zip", f"--output={archive}", "HEAD", "app"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            "git archive 失败: " + (result.stderr or result.stdout)[-500:]
        )
    shutil.unpack_archive(str(archive), str(tmp / "base"))
    return tmp / "base" / "app"


def _import_from_overlay(
    base_app: Path,
    overlay: Path,
    modules: list[str],
    *,
    require_static: bool = True,
    android_app_dir: Path | None = None,
) -> tuple[bool, str]:
    code = r"""
import importlib
import json
import sys
names = json.loads(sys.argv[1])
out = {}
for name in names:
    m = importlib.import_module(name)
    out[name] = str(getattr(m, "__file__", "") or "")
    if name == "core.paths":
        out["__android_paths"] = {
            "base": str(m.BASE_DIR),
            "root": str(m.ROOT),
            "static": str(m.STATIC_DIR),
            "index_exists": (m.STATIC_DIR / "index.html").is_file(),
            "style_exists": (m.STATIC_DIR / "style.css").is_file(),
        }
print(json.dumps(out, ensure_ascii=False))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(overlay), str(base_app)])
    env["FIREFLY_DATA_DIR"] = str(overlay.parent / "isolated_data")
    env["FIREFLY_ANDROID"] = "1"
    if android_app_dir is not None:
        env["FIREFLY_ANDROID_APP_DIR"] = str(android_app_dir)
    result = subprocess.run(
        [PYTHON, "-s", "-c", code, json.dumps(modules)],
        cwd=str(overlay.parent),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        return False, (result.stdout or "") + (result.stderr or "")
    try:
        files = json.loads(result.stdout)
    except Exception:
        return False, result.stdout[-1000:]
    bad = []
    overlay_s = str(overlay.resolve()).lower()
    for name, file_name in files.items():
        if name == "__android_paths":
            continue
        if not str(Path(file_name).resolve()).lower().startswith(overlay_s):
            bad.append(f"{name}={file_name}")
    android_paths = files.get("__android_paths") or {}
    expected_base = base_app.resolve()
    expected_static = (expected_base / "static").resolve()
    try:
        actual_base = Path(android_paths.get("base") or "").resolve()
        actual_root = Path(android_paths.get("root") or "").resolve()
        actual_static = Path(android_paths.get("static") or "").resolve()
    except Exception:
        actual_base = actual_root = actual_static = Path("__invalid__")
    if not (
        actual_base == expected_base
        and actual_root == expected_base.parent.resolve()
        and actual_static == expected_static
        and android_paths.get("index_exists") is True
        and android_paths.get("style_exists") is True
        if require_static
        else (
            actual_base == expected_base
            and actual_root == expected_base.parent.resolve()
            and actual_static == expected_static
        )
    ):
        bad.append(
            "android paths="
            + json.dumps(android_paths, ensure_ascii=False)
            + f"; expected base={expected_base} static={expected_static}"
        )
    return not bad, " | ".join(bad)


def _corrupt_zip(src: Path, dst: Path) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        names = zin.namelist()
        for index, name in enumerate(names):
            data = zin.read(name)
            if index == 0:
                data += b"\n# latest hotupdate tampered release file\n"
            zout.writestr(name, data)


def run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="firefly_hot_release_"))
    server = None
    thread = None
    try:
        print("=== A. 真实 HTTP 发布索引 / 清单 / 签名 / 包结构 ===")
        server = _ReleaseServer(("127.0.0.1", 0), _ReleaseHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{port}"

        latest = net.fetch_latest(base_url)
        raw, manifest, sig = _manifest(SERIAL)
        http_raw, http_sig = net.fetch_manifest(base_url, SERIAL)

        check(
            f"A1 latest 指向 {BASE_VERSION}/serial {SERIAL}",
            latest == {"base": BASE_VERSION, "serial": SERIAL},
            repr(latest),
        )
        check(
            "A2 HTTP 清单字节与磁盘发布物完全一致",
            http_raw == raw and http_sig == sig,
        )
        check(
            "A3 manifest 字节就是规范 canonical 字节",
            net.canonical_bytes(json.loads(raw.decode("utf-8"))) == raw,
        )
        check("A4 RSA-2048/SHA-256 真实验签通过", verify.verify(http_raw, sig))
        check(
            f"A5 字段与 serial {SERIAL} 发布决策一致",
            manifest["base_version"] == BASE_VERSION
            and manifest["serial"] == SERIAL
            and manifest["cumulative"] is True
            and manifest["layer"] == "web+py"
            and manifest["rollout_percent"] == 100
            and manifest["min_safe_serial"] == 1
            and manifest["revoked_serials"] == []
            and "promotion_of" not in manifest,
            json.dumps(
                {
                    k: manifest.get(k)
                    for k in (
                        "base_version",
                        "serial",
                        "layer",
                        "rollout_percent",
                        "promotion_of",
                    )
                }
            ),
        )

        paths = [f["path"] for f in manifest["files"]]
        archive = DIST / manifest["zip"]["name"]
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            actual = {
                name: (hashlib.sha256(zf.read(name)).hexdigest(), zf.getinfo(name).file_size)
                for name in names
            }
        expected = {
            f["path"]: (f["sha256"], int(f["size"])) for f in manifest["files"]
        }
        check(
            f"A6 包内文件与清单 {len(paths)} 个完全一致",
            set(names) == set(paths),
        )
        check("A7 文件路径无重复、无越界、仅 web/py", (
            len(paths) == len(set(paths))
            and all(
                p.startswith(("web/", "py/"))
                and not p.startswith("/")
                and "\\" not in p
                and ".." not in p.split("/")
                for p in paths
            )
        ))
        check(f"A8 zip 内 {len(paths)} 个文件逐字节哈希/大小匹配", actual == expected)
        check(
            "A9 zip 本体 sha256 与 size 匹配",
            net.sha256_file(archive) == manifest["zip"]["sha256"]
            and archive.stat().st_size == int(manifest["zip"]["size"]),
        )
        check(
            "A10 同步、relay 与 plaza 拆分文件均在累积包内",
            all(
                p in paths
                for p in (
                    "web/js/sync.js",
                    "py/routes_assets.py",
                    "py/plaza/card_payload.py",
                    "py/plaza/http_transport.py",
                )
            ),
        )
        check(
            "A11 hotupdate 自身绝不在补丁内",
            not any(p.startswith("py/hotupdate/") for p in paths),
        )

        print("=== B. 累积关系 / 当前源码一致性 / Python 闭包 ===")
        _, prev, _ = _manifest(SERIAL - 1)
        prev_paths = {f["path"] for f in prev["files"]}
        current_paths = set(paths)
        check(
            f"B1 serial {SERIAL} 累积包含 serial {SERIAL - 1} 全部路径",
            prev_paths <= current_paths,
            f"缺 {sorted(prev_paths - current_paths)[:5]}",
        )
        mismatched = []
        for arc in paths:
            src = _source_for(arc)
            if not src.is_file() or hashlib.sha256(src.read_bytes()).hexdigest() != expected[arc][0]:
                mismatched.append(arc)
        check(
            f"B2 {len(paths)} 个补丁文件与当前源码逐字节一致",
            not mismatched,
            repr(mismatched[:8]),
        )

        sys.path.insert(0, str(ROOT / "tools"))
        import build_hotupdate as BH

        current = {f"py/{k}": v for k, v in BH.collect_py().items()}
        expanded, roots, missing_init = BH.expand_py_package_closure(
            {p: {} for p in paths if p.startswith("py/")}, current
        )
        expected_roots = {
            p.split("/", 2)[1]
            for p in paths
            if p.startswith("py/") and p.count("/") >= 2
        }
        check(
            f"B3 触及的顶层包为 {'/'.join(sorted(expected_roots))}",
            set(roots) == expected_roots,
            repr(roots),
        )
        check(
            f"B4 Python {len(roots)} 包闭包完整且均有 __init__.py",
            set(expanded) <= current_paths and not missing_init,
            f"缺 {sorted(set(expanded) - current_paths)[:8]} / init={missing_init}",
        )

        print("=== C. 干净 HEAD 底座应用 + 覆盖层新进程导入 ===")
        base_app = _git_base(tmp)
        patched = tmp / "patched"
        apply_result = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "apply_hotupdate.py"),
                "--patch",
                str(archive),
                "--manifest",
                str(DIST / f"patch-{SERIAL}.json"),
                "--base",
                str(base_app),
                "--out",
                str(patched),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        check(
            f"C1 serial {SERIAL} 可应用到 git HEAD 干净底座",
            apply_result.returncode == 0,
            (apply_result.stdout or "")[-800:] + (apply_result.stderr or "")[-400:],
        )
        patched_mismatch = []
        for arc in paths:
            if arc.startswith("py/"):
                out = patched / arc[len("py/"):]
            else:
                out = patched / "static" / arc[len("web/"):]
            if not out.is_file() or hashlib.sha256(out.read_bytes()).hexdigest() != expected[arc][0]:
                patched_mismatch.append(arc)
        check(f"C2 应用结果 {len(paths)} 文件全部等于签名清单", not patched_mismatch,
              repr(patched_mismatch[:8]))

        py_files = [f for f in manifest["files"] if f["path"].startswith("py/")]
        overlay = tmp / "overlay"
        extracted = net.extract_verified(
            archive, py_files, overlay, strip_prefix="py/", expect_names=set(names)
        )
        check(f"C3 客户端解包器接受 serial {SERIAL} py 层", extracted == len(py_files),
              f"{extracted}/{len(py_files)}")
        ok_import, import_detail = _import_from_overlay(
            base_app,
            overlay,
            [
                "core.paths",
                "modules.sync_state",
                "modules.sync_engine",
                "modules.sync_server_state",
                "api.data_sync",
            ],
        )
        check(
            "C4 新进程导入来自覆盖层且底座静态目录未漂移",
            ok_import,
            import_detail[-1000:],
        )

        lazy_base = tmp / "lazy-base"
        lazy_overlay = tmp / "lazy-overlay"
        lazy_base.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base_app / "server.py", lazy_base / "server.py")
        # 必须测试发布包里的新版 core.paths；git HEAD 的旧底座正是事故版本，
        # 拿它做 C5 只会稳定复现旧 __file__ 漂移，不能验证修复。
        shutil.copytree(overlay / "core", lazy_overlay / "core")
        lazy_ok, lazy_detail = _import_from_overlay(
            lazy_base, lazy_overlay, ["core.paths"], require_static=False
        )
        check(
            "C5 Chaquopy 尚未懒解压 core.paths/static 时仍能定位底座",
            lazy_ok,
            lazy_detail[-1000:],
        )
        virtual_base = tmp / "assetfinder" / "app" / "backend" / "app"
        virtual_ok, virtual_detail = _import_from_overlay(
            virtual_base,
            overlay,
            ["core.paths"],
            require_static=False,
            android_app_dir=virtual_base,
        )
        check(
            "C6 AssetFinder 虚拟底座不能因 os.stat 失败而被误判",
            virtual_ok,
            virtual_detail[-1000:],
        )

        print("=== D. 攻击/损坏包必须整包拒绝 ===")
        tampered = bytearray(raw)
        serial_field = f'"serial":{SERIAL}'.encode("ascii")
        pos = raw.index(serial_field) + len(b'"serial":')
        tampered[pos] = ord("0") if tampered[pos] != ord("0") else ord("1")
        check("D1 篡改 canonical manifest 后验签失败",
              not verify.verify(bytes(tampered), sig))

        bad_download = tmp / "bad-download.zip"
        try:
            net.download_zip(base_url, manifest["zip"]["name"], bad_download, "00" * 32)
            bad_hash_rejected = False
        except ValueError:
            bad_hash_rejected = True
        check(
            "D2 错误 zip sha256 被拒绝且无半成品",
            bad_hash_rejected and not bad_download.exists()
            and not bad_download.with_suffix(".zip.part").exists(),
        )

        corrupt = tmp / "corrupt.zip"
        _corrupt_zip(archive, corrupt)
        corrupt_out = tmp / "corrupt-out"
        try:
            net.extract_verified(
                corrupt, manifest["files"], corrupt_out, strip_prefix="py/",
                expect_names=set(names),
            )
            corrupt_rejected = False
        except ValueError:
            corrupt_rejected = True
        check(
            "D3 单文件篡改导致解包失败并清空暂存目录",
            corrupt_rejected and not corrupt_out.exists(),
        )

        malicious_meta = [dict(manifest["files"][0], path="../escape.py")]
        try:
            net.extract_verified(
                archive, malicious_meta, tmp / "escape-out",
                strip_prefix="py/", expect_names={malicious_meta[0]["path"]},
            )
            traversal_rejected = False
        except ValueError:
            traversal_rejected = True
        check("D4 路径穿越在落盘前被拒绝", traversal_rejected)

        print("=== E. 灰度落桶未来回归 ===")
        ten = {"rollout_percent": 10}
        buckets = []
        for i in range(10000):
            state = {"client_id": f"hot-rollout-{i}"}
            selected = _rollout_decision(state, ten, 999)
            buckets.append((state["rollout_bucket"], selected))
        selected_n = sum(1 for _, selected in buckets if selected)
        repeated = all(
            _rollout_decision({"client_id": f"hot-rollout-{i}"}, ten, 999)
            == _rollout_decision({"client_id": f"hot-rollout-{i}"}, ten, 999)
            for i in range(200)
        )
        state_100 = {"client_id": "must-hit"}
        check("E1 10% 灰度比例真实接近十分之一", 850 <= selected_n <= 1150,
              str(selected_n))
        check("E2 同 client_id/serial 落桶稳定", repeated)
        check("E3 100% 灰度必中",
              _rollout_decision(state_100, {"rollout_percent": 100}, SERIAL))

        print("=== F. 48 并发 × 多轮 HTTP 下载/清单/验签 soak ===")
        workers = 48
        rounds = 4
        start = time.perf_counter()
        durations = []
        errors = []
        lock = threading.Lock()

        def one_round(round_no: int):
            local_start = time.perf_counter()
            try:
                got_latest = net.fetch_latest(base_url)
                got_raw, got_sig = net.fetch_manifest(base_url, SERIAL)
                if got_latest.get("serial") != SERIAL:
                    raise AssertionError(f"latest={got_latest}")
                if got_raw != raw or not verify.verify(got_raw, got_sig):
                    raise AssertionError("manifest/signature mismatch")
                out = tmp / "soak" / f"{round_no:02d}.zip"
                net.download_zip(
                    base_url,
                    manifest["zip"]["name"],
                    out,
                    manifest["zip"]["sha256"],
                    int(manifest["zip"]["size"]),
                )
                if net.sha256_file(out) != manifest["zip"]["sha256"]:
                    raise AssertionError("downloaded zip hash mismatch")
                out.unlink()
            except Exception as exc:
                with lock:
                    errors.append(f"round={round_no}: {type(exc).__name__}: {exc}")
            finally:
                with lock:
                    durations.append(time.perf_counter() - local_start)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one_round, i) for i in range(workers * rounds)]
            for future in futures:
                future.result()
        elapsed = time.perf_counter() - start
        durations.sort()
        p50 = statistics.median(durations)
        p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
        p99 = durations[min(len(durations) - 1, int(len(durations) * 0.99))]
        transferred_mb = workers * rounds * archive.stat().st_size / 1048576
        check("F1 soak 无失败/无哈希/无验签错误", not errors, repr(errors[:5]))
        check("F2 200 次完整下载全部清理临时文件",
              not list((tmp / "soak").glob("*.zip*"))
              and not list((tmp / "soak").glob("*.part")))
        check("F3 本地并发 p95 保持可用", p95 < 5.0, f"p95={p95:.3f}s")
        print(
            f"    （{workers} workers × {rounds} 轮 = {workers * rounds} 次；"
            f"总耗时 {elapsed:.2f}s，传输 {transferred_mb:.1f} MB，"
            f"p50={p50:.3f}s p95={p95:.3f}s p99={p99:.3f}s）"
        )

    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    global FAIL
    try:
        run()
    except Exception:
        FAIL += 1
        print("  X 未捕获异常")
        traceback.print_exc()

    print(f"\n=== latest hotupdate release pipeline: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
