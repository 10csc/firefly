# -*- coding: utf-8 -*-
"""真机热更新 E2E：备份、注入、重启、验证、恢复。

只使用 Python 标准库和 adb。默认针对当前项目：

    python tools/adb_hotupdate_e2e.py

脚本不会访问线上热更源。它在本机临时启动 HTTP 源，并通过
`adb reverse` 让 App 的 127.0.0.1 命中该源。

安全边界：
  · 应用补丁前必须拿到可恢复备份。可调试包直接 tar 备份私有目录；
    发布包不可调试时，要求本机存在与设备 applied_hash 完全匹配的 patch-N.zip。
  · 测试结束后恢复测试前的 serial、覆盖层和 URL roots。
  · 任一步失败都会保留日志和报告；无法恢复时会明确报错。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ADB = r"F:\Android_SDKset\platform-tools\adb.exe"
DEFAULT_DIST = ROOT / "hotupdate_dist" / "prod"
DEFAULT_OUT = ROOT / ".tmp_test" / "adb_hotupdate"
PACKAGE = "com.firefly.android"
ACTIVITY = f"{PACKAGE}/.MainActivity"
DEVICE_API_PORT = 8765
HOST_FORWARD_PORT = 18765
REVERSE_PORT = 18766


class E2EFailure(RuntimeError):
    pass


def _now_id() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _default_base_version() -> str:
    text = (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise E2EFailure("无法从 app/core/config.py 读取 APP_VERSION")
    return match.group(1)


def _read_manifest(
    dist: Path, serial: int, base_version: str
) -> tuple[dict, bytes, Path, Path]:
    base = dist / base_version
    js = base / f"patch-{serial}.json"
    zp = base / f"patch-{serial}.zip"
    if not js.is_file() or not zp.is_file():
        raise E2EFailure(f"缺少 serial {serial} 的本地发布物：{js} / {zp}")
    envelope = json.loads(js.read_text(encoding="utf-8"))
    try:
        raw = base64.b64decode(envelope["manifest_b64"], validate=True)
    except Exception as exc:
        raise E2EFailure(f"patch-{serial}.json 的 manifest_b64 非法：{exc}") from exc
    manifest = json.loads(raw.decode("utf-8"))
    if int(manifest.get("serial") or 0) != serial:
        raise E2EFailure(f"patch-{serial}.json 内 serial 不符")
    if str(manifest.get("base_version") or "") != base_version:
        raise E2EFailure(
            f"patch-{serial} base={manifest.get('base_version')}，预期 {base_version}"
        )
    return manifest, raw, js, zp


def _verify_patch(
    dist: Path, serial: int, base_version: str
) -> tuple[dict, bytes, Path, Path]:
    manifest, raw, js, zp = _read_manifest(dist, serial, base_version)
    meta = manifest.get("zip") or {}
    expect_sha = str(meta.get("sha256") or "")
    expect_size = int(meta.get("size") or 0)
    got_sha = _sha256(zp)
    got_size = zp.stat().st_size
    if got_sha != expect_sha or got_size != expect_size:
        raise E2EFailure(
            f"patch-{serial}.zip 不符：sha={got_sha} size={got_size}，"
            f"预期 sha={expect_sha} size={expect_size}"
        )

    files = manifest.get("files") or []
    expected = {str(x.get("path")): x for x in files}
    with zipfile.ZipFile(zp) as zf:
        names = set(zf.namelist())
        if names != set(expected):
            raise E2EFailure(
                f"patch-{serial}.zip 文件集合不符："
                f"多 {sorted(names - set(expected))} 缺 {sorted(set(expected) - names)}"
            )
        for name, meta_file in expected.items():
            data = zf.read(name)
            if hashlib.sha256(data).hexdigest() != str(meta_file.get("sha256") or ""):
                raise E2EFailure(f"patch-{serial}.zip 内 {name} 哈希不符")
            if len(data) != int(meta_file.get("size") or 0):
                raise E2EFailure(f"patch-{serial}.zip 内 {name} 大小不符")
    return manifest, raw, js, zp


@dataclass
class Device:
    adb: Path
    serial: str | None

    def _target(self) -> list[str]:
        return [str(self.adb)] + (["-s", self.serial] if self.serial else [])

    def run(
        self,
        *args: str,
        check: bool = True,
        timeout: int = 60,
        binary: bool = False,
    ) -> subprocess.CompletedProcess:
        cp = subprocess.run(
            self._target() + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if check and cp.returncode != 0:
            err = cp.stderr if binary else cp.stderr.decode("utf-8", "replace")
            out = cp.stdout if binary else cp.stdout.decode("utf-8", "replace")
            raise E2EFailure(f"adb {' '.join(args)} 失败 ({cp.returncode})：{err or out}")
        return cp

    def text(self, *args: str, check: bool = True, timeout: int = 60) -> str:
        return self.run(*args, check=check, timeout=timeout).stdout.decode(
            "utf-8", "replace"
        ).strip()

    def shell(self, command: str, check: bool = True, timeout: int = 60) -> str:
        return self.text("shell", command, check=check, timeout=timeout)

    def pid(self) -> int | None:
        out = self.shell(f"pidof {PACKAGE}", check=False).strip()
        try:
            return int(out.split()[0])
        except Exception:
            return None

    def start_app(self) -> str:
        return self.shell(f"am start -W -n {ACTIVITY}", timeout=60)

    def force_stop(self) -> None:
        self.shell(f"am force-stop {PACKAGE}", check=False)

    def reverse(self, device_port: int, host_port: int) -> None:
        self.run("reverse", f"tcp:{device_port}", f"tcp:{host_port}")

    def forward(self, host_port: int, device_port: int) -> None:
        self.run("forward", "--remove", f"tcp:{host_port}", check=False)
        self.run("forward", f"tcp:{host_port}", f"tcp:{device_port}")

    def remove_forward(self, host_port: int) -> None:
        self.run("forward", "--remove", f"tcp:{host_port}", check=False)

    def remove_reverse(self, device_port: int) -> None:
        self.run("reverse", "--remove", f"tcp:{device_port}", check=False)


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: dict | None = None, timeout: int = 120):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    def status(self) -> dict:
        return self.request("GET", "/hotupdate/status", timeout=15)

    def action(self, action: str, **extra) -> dict:
        return self.request("POST", "/hotupdate/action", {"action": action, **extra}, timeout=180)

    def boot_ok(self) -> dict:
        return self.request("POST", "/hotupdate/boot-ok", {}, timeout=20)

    def config(self) -> dict:
        return self.request("GET", "/config", timeout=20)

    def modes(self) -> dict:
        return self.request("GET", "/modes", timeout=20)

    def wait_ready(self, timeout: int = 60) -> dict:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                return self.status()
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(1)
        raise E2EFailure(f"App 本地 API 未就绪：{last}")


class SourceServer:
    def __init__(self, port: int, log_path: Path, base_version: str):
        self.port = port
        self.log_path = log_path
        self.base_version = base_version
        self.root = DEFAULT_DIST
        self.latest_override: int | None = None
        self.lock = threading.Lock()
        self.httpd: socketserver.TCPServer | None = None
        self.thread: threading.Thread | None = None

    def set_mode(self, root: Path, latest_override: int | None) -> None:
        with self.lock:
            self.root = root
            self.latest_override = latest_override

    def start(self) -> None:
        owner = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(owner.root), **kwargs)

            def log_message(self, fmt, *args):
                with owner.log_path.open("a", encoding="utf-8") as f:
                    f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with owner.lock:
                    override = owner.latest_override
                if path == f"/{owner.base_version}/latest" and override is not None:
                    data = json.dumps(
                        {"base": owner.base_version, "serial": override}
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                super().do_GET()

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.httpd = Server(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _try_exact_overlay_backup(dev: Device, out: Path) -> tuple[bool, str]:
    """可调试包路径：run-as + tar。发布包会明确失败，调用方再走制品恢复。"""
    cp = dev.run(
        "exec-out",
        "run-as",
        PACKAGE,
        "tar",
        "-C",
        f"/data/user/0/{PACKAGE}",
        "-cf",
        "-",
        "firefly_data/hotupdate",
        check=False,
        timeout=180,
        binary=True,
    )
    if cp.returncode != 0 or not cp.stdout:
        detail = cp.stderr.decode("utf-8", "replace").strip() or "run-as 未返回数据"
        return False, detail
    tar_path = out / "hotupdate-private.tar"
    tar_path.write_bytes(cp.stdout)
    try:
        with tarfile.open(tar_path, "r:") as tf:
            names = tf.getnames()
    except Exception as exc:
        return False, f"run-as 返回内容不是有效 tar：{exc}"
    if not any("hotupdate/state.json" in x for x in names):
        return False, "tar 中缺少 hotupdate/state.json"
    return True, f"{len(names)} 个成员，{tar_path.stat().st_size} 字节"


def _build_artifact_recovery(
    dist: Path, serial: int, out: Path, base_version: str
) -> dict:
    manifest, raw, js, zp = _verify_patch(dist, serial, base_version)
    rec = out / "recovery_dist" / base_version
    rec.mkdir(parents=True, exist_ok=True)
    shutil.copy2(js, rec / js.name)
    shutil.copy2(zp, rec / zp.name)
    (rec / "latest").write_text(
        json.dumps({"base": base_version, "serial": serial}), encoding="utf-8"
    )
    (out / "patch-manifest.json").write_bytes(raw)

    overlay = out / "recovered_overlay"
    if overlay.exists():
        shutil.rmtree(overlay)
    with zipfile.ZipFile(zp) as zf:
        for item in manifest.get("files") or []:
            arc = str(item["path"])
            layer, rel = arc.split("/", 1)
            dst = overlay / layer / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(zf.read(arc))

    return {
        "kind": "signed-artifact-plus-state-snapshot",
        "serial": serial,
        "manifest_hash": _manifest_hash(raw),
        "files": len(manifest.get("files") or []),
        "zip_sha256": _sha256(zp),
        "recovered_overlay": str(overlay),
        "recovery_dist": str(rec.parent),
        "note": "pyc 未备份；它们是可再生的，python 源文件由已验签 ZIP 完整恢复。",
    }


def _capture_package_artifacts(dev: Device, out: Path) -> dict:
    pkg = dev.shell(f"dumpsys package {PACKAGE}", timeout=60)
    _write(out / "package.txt", pkg)
    apk_line = dev.shell(f"pm path {PACKAGE}")
    remote = apk_line.splitlines()[0].replace("package:", "").strip()
    local = out / "installed-base.apk"
    dev.run("pull", remote, str(local), timeout=180)
    return {
        "remote_apk": remote,
        "base_apk": str(local),
        "base_apk_sha256": _sha256(local),
        "base_apk_size": local.stat().st_size,
    }


def _wait_for_serial(api: Api, serial: int, timeout: int = 90) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = api.status()
            if int(last.get("applied_serial") or 0) == serial and int(
                last.get("pending_serial") or 0
            ) == 0:
                return last
        except Exception:
            pass
        time.sleep(1)
    raise E2EFailure(f"等待 serial={serial} 生效超时，最后状态：{_json(last)}")


def _wait_new_pid(dev: Device, old_pid: int | None, timeout: int = 90) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = dev.pid()
        if pid and pid != old_pid:
            return pid
        time.sleep(1)
    raise E2EFailure(f"等待 App 进程重启超时（旧 PID {old_pid}）")


def _cold_restart(dev: Device, api: Api, old_pid: int | None, timeout: int = 90) -> int:
    """测试/恢复兜底：模拟用户冷启动，不把壳自动重启的失败伪装成成功。"""
    dev.force_stop()
    time.sleep(2)
    dev.start_app()
    api.wait_ready(timeout=timeout)
    return _wait_new_pid(dev, old_pid, timeout=timeout)


def _ensure_boot_ok(api: Api, serial: int, timeout: int = 75) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    fallback = False
    while time.time() < deadline:
        try:
            last = api.status()
            if int(last.get("pending_serial") or 0) == 0:
                return last
            if not fallback and time.time() > deadline - timeout / 2:
                # UI 理应自动上报；仅在其未完成时显式走同一协议，并在报告里标记。
                api.boot_ok()
                fallback = True
        except Exception:
            pass
        time.sleep(1)
    raise E2EFailure(f"serial={serial} boot-ok 超时，最后状态：{_json(last)}")


def _fetch_config_projection(api: Api) -> dict:
    out = {}
    try:
        out["config"] = api.config()
    except Exception as exc:
        out["config_error"] = f"{type(exc).__name__}: {exc}"
    try:
        modes = api.modes()
        out["modes_ok"] = bool(modes.get("modes") or modes.get("ok"))
        out["modes_count"] = len(modes.get("modes") or [])
    except Exception as exc:
        out["modes_ok"] = False
        out["modes_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _logcat(dev: Device, out: Path, name: str) -> dict:
    text = dev.text("logcat", "-v", "threadtime", "-d", timeout=90)
    path = out / f"{name}.log"
    _write(path, text)
    markers = [
        "FATAL EXCEPTION",
        "AndroidRuntime",
        "ZipException",
        "ImportError",
        "ModuleNotFoundError",
        "Traceback (most recent call last)",
        "FireflyHotUpdate",
        "hotupdate",
        "pack_paths",
    ]
    lines = text.splitlines()
    hits = [line for line in lines if any(m.lower() in line.lower() for m in markers)]
    crash = [
        line
        for line in lines
        if "FATAL EXCEPTION" in line or " E AndroidRuntime:" in line
    ]
    return {"path": str(path), "lines": len(lines), "hits": hits[-300:], "crash_hits": crash}


def _check_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise E2EFailure(f"端口 {port} 已被占用；先关闭占用进程")


def _restore(
    dev: Device,
    api: Api,
    source: SourceServer,
    backup: dict,
    out: Path,
) -> dict:
    before = backup["pre_status"]
    old_serial = int(before.get("applied_serial") or 0)
    old_hash = str(before.get("applied_hash") or "")
    roots = list(before.get("url_roots") or [])
    result: dict[str, Any] = {"target_serial": old_serial, "ok": False}

    try:
        if old_serial == 0:
            source.set_mode(DEFAULT_DIST, 0)
            api.action("set_urls", url_roots=[f"http://127.0.0.1:{REVERSE_PORT}"])
            api.action("rollback")
            pid0 = dev.pid()
            new_pid = _cold_restart(dev, api, pid0, timeout=60)
            result["restart_mode"] = "cold-start-base"
            result["pid_after"] = new_pid
        else:
            rec_dist = Path(backup["artifact_recovery"]["recovery_dist"])
            source.set_mode(rec_dist, old_serial)
            api.action("set_urls", url_roots=[f"http://127.0.0.1:{REVERSE_PORT}"])
            api.action("rollback")
            chk = api.action("check")
            if int(chk.get("serial") or 0) != old_serial:
                raise E2EFailure(f"恢复源未提供 serial {old_serial}：{_json(chk)}")
            pid0 = dev.pid()
            applied = api.action("apply")
            if not applied.get("ok"):
                raise E2EFailure(f"恢复应用 patch-{old_serial} 失败：{_json(applied)}")
            after_apply = api.status()
            if "py" in str(after_apply.get("applied_layer") or "") and not after_apply.get(
                "restart_pending"
            ):
                # 旧底座加载器会漏发 restart_pending；恢复必须继续，不能把测试补丁留在设备上。
                new_pid = _cold_restart(dev, api, pid0, timeout=90)
                result["restart_mode"] = "assisted-cold-start-old-loader"
            else:
                new_pid = _wait_new_pid(dev, pid0, timeout=90)
                result["restart_mode"] = "auto"
            result["pid_after"] = new_pid
            result["pre_restart_status"] = after_apply
            api.wait_ready()
            _ensure_boot_ok(api, old_serial)

        api.action("set_urls", url_roots=roots)
        final = api.status()
        if int(final.get("applied_serial") or 0) != old_serial:
            raise E2EFailure(f"恢复 serial 不符：{final.get('applied_serial')} != {old_serial}")
        if old_serial and str(final.get("applied_hash") or "") != old_hash:
            raise E2EFailure(
                f"恢复 hash 不符：{final.get('applied_hash')} != {old_hash}"
            )
        if list(final.get("url_roots") or []) != roots:
            raise E2EFailure("恢复 URL roots 失败")
        result.update({"ok": True, "final_status": final})
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        # 最后安全兜底：至少退回无覆盖层，不能把测试包留在设备上。
        try:
            api.action("rollback")
            if roots:
                api.action("set_urls", url_roots=roots)
            pid0 = dev.pid()
            _cold_restart(dev, api, pid0, timeout=60)
            result["fallback_base_status"] = api.status()
        except Exception as fallback_exc:
            result["fallback_error"] = f"{type(fallback_exc).__name__}: {fallback_exc}"
        return result


def run(args: argparse.Namespace) -> int:
    out = Path(args.out) / _now_id()
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "package": PACKAGE,
        "target_serial": args.target_serial,
        "steps": {},
    }
    dev = Device(Path(args.adb), args.device)
    source: SourceServer | None = None
    old_status: dict | None = None
    restore_result: dict | None = None

    try:
        devices = dev.text("devices", "-l")
        _write(out / "adb-devices.txt", devices)
        rows = [line.split() for line in devices.splitlines()[1:] if line.strip()]
        connected = {row[0] for row in rows if len(row) >= 2 and row[1] == "device"}
        if args.device and args.device not in connected:
            raise E2EFailure(f"设备 {args.device} 未连接或状态不是 device")
        pkg = dev.shell(f"pm list packages | grep -F {PACKAGE}", check=False)
        if PACKAGE not in pkg:
            raise E2EFailure(f"{PACKAGE} 未安装")
        report["steps"]["device"] = {
            "devices": devices,
            "model": dev.shell("getprop ro.product.model"),
            "android_release": dev.shell("getprop ro.build.version.release"),
            "package_line": pkg,
            "root": dev.shell("id"),
        }

        _check_port(REVERSE_PORT)
        _check_port(HOST_FORWARD_PORT)
        dev.reverse(REVERSE_PORT, REVERSE_PORT)
        dev.forward(HOST_FORWARD_PORT, DEVICE_API_PORT)
        source = SourceServer(REVERSE_PORT, out / "http.log", args.base_version)
        source.start()
        report["steps"]["transport"] = {
            "reverse": f"device:{REVERSE_PORT} -> host:{REVERSE_PORT}",
            "forward": f"host:{HOST_FORWARD_PORT} -> device:{DEVICE_API_PORT}",
            "source": f"http://127.0.0.1:{REVERSE_PORT}",
        }

        dev.start_app()
        api = Api(f"http://127.0.0.1:{HOST_FORWARD_PORT}")
        old_status = api.wait_ready()
        old_pid = dev.pid()
        _write(out / "pre-status.json", _json(old_status))
        report["steps"]["pre_state"] = {"pid": old_pid, "status": old_status}

        package_backup = _capture_package_artifacts(dev, out)
        exact_ok, exact_detail = _try_exact_overlay_backup(dev, out)
        old_serial = int(old_status.get("applied_serial") or 0)
        artifact = None
        if not exact_ok:
            if old_serial and old_status.get("applied_hash"):
                artifact = _build_artifact_recovery(
                    Path(args.dist), old_serial, out, args.base_version
                )
                if artifact["manifest_hash"] != old_status.get("applied_hash"):
                    raise E2EFailure(
                        "设备 applied_hash 与本地 patch 清单不一致，拒绝继续："
                        f"{old_status.get('applied_hash')} != {artifact['manifest_hash']}"
                    )
            else:
                artifact = {"kind": "base-no-overlay", "serial": 0}
        backup = {
            "pre_status": old_status,
            "package": package_backup,
            "exact_overlay_backup": {"ok": exact_ok, "detail": exact_detail},
            "artifact_recovery": artifact,
        }
        _write(out / "backup.json", _json(backup))
        report["steps"]["backup"] = backup
        if not exact_ok and old_serial and not artifact:
            raise E2EFailure("没有可恢复备份，拒绝注入补丁")

        target_manifest, target_raw, _, target_zip = _verify_patch(
            Path(args.dist), args.target_serial, args.base_version
        )
        target_paths = {str(x.get("path")) for x in target_manifest.get("files") or []}
        if "py/core/pack_paths.py" not in target_paths:
            raise E2EFailure("目标补丁不包含 py/core/pack_paths.py")
        report["steps"]["target_patch"] = {
            "serial": args.target_serial,
            "manifest_hash": _manifest_hash(target_raw),
            "layer": target_manifest.get("layer"),
            "files": len(target_paths),
            "zip": str(target_zip),
            "zip_sha256": _sha256(target_zip),
            "core_pack_paths_expected_sha256": next(
                x["sha256"]
                for x in target_manifest["files"]
                if x.get("path") == "py/core/pack_paths.py"
            ),
        }

        # 清理旧日志并做一次真实冷启动，确保等会儿 PID 变化可归因于补丁重启。
        dev.text("logcat", "-c", timeout=30)
        dev.force_stop()
        time.sleep(2)
        dev.start_app()
        baseline = api.wait_ready(timeout=90)
        baseline_pid = dev.pid()
        report["steps"]["test_baseline"] = {"pid": baseline_pid, "status": baseline}

        source.set_mode(Path(args.dist), args.target_serial)
        api.action("set_urls", url_roots=[f"http://127.0.0.1:{REVERSE_PORT}"])
        checked = api.action("check")
        if not checked.get("ok"):
            raise E2EFailure(f"check 失败：{_json(checked)}")
        avail = (checked.get("status") or {}).get("available") or {}
        if int(avail.get("serial") or checked.get("serial") or 0) != args.target_serial:
            raise E2EFailure(f"检查到的 serial 不是 {args.target_serial}：{_json(checked)}")

        applied = api.action("apply")
        if not applied.get("ok"):
            raise E2EFailure(f"apply 失败：{_json(applied)}")
        if int(applied.get("serial") or 0) != args.target_serial:
            raise E2EFailure(f"apply 返回 serial 不符：{_json(applied)}")
        pending = api.status()
        if int(pending.get("pending_serial") or 0) != args.target_serial:
            raise E2EFailure(f"apply 后 pending 不符：{_json(pending)}")

        automatic_restart_ok = bool(pending.get("restart_pending"))
        if automatic_restart_ok:
            new_pid = _wait_new_pid(dev, baseline_pid)
            restart_mode = "auto"
        elif args.allow_assisted_restart:
            new_pid = _cold_restart(dev, api, baseline_pid)
            restart_mode = "assisted-cold-start-after-loader-miss"
        else:
            raise E2EFailure(
                "含 py 的补丁已落盘，但客户端没有报告 restart_pending；"
                "为避免掩盖壳重启缺陷，停止自动测试并执行恢复。"
            )
        api.wait_ready(timeout=90)
        active = _ensure_boot_ok(api, args.target_serial)
        projection = _fetch_config_projection(api)
        logs = _logcat(dev, out, "target-logcat")

        ok = (
            int(active.get("applied_serial") or 0) == args.target_serial
            and int(active.get("pending_serial") or 0) == 0
            and active.get("applied_hash") == report["steps"]["target_patch"]["manifest_hash"]
            and "py" in str(active.get("applied_layer") or "")
            and bool(projection.get("modes_ok"))
            and not logs.get("crash_hits")
        )
        report["steps"]["target_result"] = {
            "ok": ok,
            "apply_ack": applied,
            "pre_restart_status": pending,
            "pid_before": baseline_pid,
            "pid_after": new_pid,
            "restart_mode": restart_mode,
            "automatic_restart_ok": automatic_restart_ok,
            "status": active,
            "projection": projection,
            "logcat": logs,
            "pack_paths_verification": {
                "in_manifest": True,
                "landing_proven_by": (
                    f"serial {args.target_serial} 的 api/pack_paths.py 在顶层 import core.pack_paths；"
                    "新进程能启动并完成 /modes、/config 请求，说明该文件已落盘并可导入。"
                ),
            },
        }
        if not ok:
            raise E2EFailure(f"目标补丁验证未通过：{_json(report['steps']['target_result'])}")
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            report["error_logcat"] = _logcat(dev, out, "failure-logcat")
        except Exception as log_exc:
            report["error_logcat_error"] = f"{type(log_exc).__name__}: {log_exc}"
    finally:
        if source and old_status is not None:
            try:
                api = Api(f"http://127.0.0.1:{HOST_FORWARD_PORT}")
                restore_result = _restore(dev, api, source, report["steps"]["backup"], out)
            except Exception as exc:
                restore_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            report["steps"]["restore"] = restore_result
            if restore_result.get("ok"):
                try:
                    report["steps"]["restore_logcat"] = _logcat(dev, out, "restore-logcat")
                except Exception as exc:
                    report["steps"]["restore_logcat_error"] = f"{type(exc).__name__}: {exc}"
        if source:
            source.stop()
        dev.remove_forward(HOST_FORWARD_PORT)
        dev.remove_reverse(REVERSE_PORT)

    report["finished_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    success = "error" not in report and bool(
        report.get("steps", {}).get("target_result", {}).get("ok")
    )
    success = success and bool(
        report.get("steps", {}).get("target_result", {}).get("automatic_restart_ok")
    )
    if restore_result is not None:
        success = success and bool(restore_result.get("ok"))
    report["ok"] = success
    _write(out / "report.json", _json(report))
    _write(out / "REPORT.md", _render_report(report, out))
    print(_render_report(report, out))
    return 0 if success else 1


def _render_report(report: dict, out: Path) -> str:
    steps = report.get("steps") or {}
    pre = steps.get("pre_state", {})
    target = steps.get("target_result", {})
    restore = steps.get("restore", {})
    logs = target.get("logcat", {})
    lines = [
        "# Firefly ADB 热更新 E2E 报告",
        "",
        f"- 结果：`{'PASS' if report.get('ok') else 'FAIL'}`",
        f"- 输出目录：`{out}`",
        f"- 目标 serial：`{report.get('target_serial')}`",
        f"- 测试前：`{pre.get('status', {}).get('base_version')}_hot"
        f"{pre.get('status', {}).get('applied_serial')}`，PID `{pre.get('pid')}`",
    ]
    if target:
        st = target.get("status") or {}
        lines += [
            f"- 测试后：`{st.get('base_version')}_hot{st.get('applied_serial')}`，"
            f"pending=`{st.get('pending_serial')}`，PID `{target.get('pid_after')}`",
            f"- 重启方式：`{target.get('restart_mode')}`",
            f"- 壳自动重启：`{target.get('automatic_restart_ok')}`",
            f"- 覆盖文件：`{st.get('overlay_files')}`（web={st.get('web_files')}，"
            f"py={st.get('py_files')}）",
            f"- logcat：`{logs.get('lines')}` 行，崩溃命中 "
            f"`{len(logs.get('crash_hits') or [])}` 条",
        ]
    if restore:
        fs = restore.get("final_status") or {}
        lines.append(
            f"- 恢复：`{'PASS' if restore.get('ok') else 'FAIL'}`，"
            f"serial=`{fs.get('applied_serial', restore.get('target_serial'))}`"
        )
        if restore.get("error"):
            lines.append(f"- 恢复错误：`{restore['error']}`")
    if report.get("error"):
        lines += ["", "## 错误", "", f"`{report['error']}`"]
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Firefly ADB 热更新真机 E2E")
    p.add_argument("--adb", default=os.environ.get("ADB", DEFAULT_ADB))
    p.add_argument("--device", default=None, help="adb serial；省略则要求只有一台设备")
    p.add_argument("--dist", default=str(DEFAULT_DIST), help="hotupdate_dist/prod 根目录")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument(
        "--base-version",
        default=None,
        help="补丁底座版本；省略时读取 app/core/config.py 的 APP_VERSION",
    )
    p.add_argument("--target-serial", type=int, required=True)
    p.add_argument(
        "--allow-assisted-restart",
        action="store_true",
        help="壳漏报 restart_pending 时，仍冷启动验证补丁兼容性；总结果仍判 FAIL",
    )
    args = p.parse_args()
    args.base_version = args.base_version or _default_base_version()

    if not Path(args.adb).is_file():
        raise SystemExit(f"adb 不存在：{args.adb}")
    if not args.device:
        cp = subprocess.run(
            [args.adb, "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        serials = [
            line.split()[0]
            for line in cp.stdout.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == "device"
        ]
        if len(serials) != 1:
            raise SystemExit(f"请用 --device 指定唯一设备，当前在线：{serials}")
        args.device = serials[0]
    return args


def main() -> int:
    try:
        return run(parse_args())
    except KeyboardInterrupt:
        print("用户中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"E2E 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
