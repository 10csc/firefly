# -*- coding: utf-8 -*-
"""hot5 同步测试的真实 HTTP harness。

测试进程先设置独立 FIREFLY_DATA_DIR，再启动：
- 真实的本地 app/server.py FireflyHandler（/sync/now 走生产路由）；
- 真实的 ThreadingHTTPServer 服务器端点（manifest/state/ops/import/export
  直接调用生产 handler，并各自注入服务器用户上下文）。

没有 mock fetch/urllib；multipart、zip、JSON、认证头和落盘都是真实 HTTP 链路。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


AUTH_FIXTURE = "harness-bearer"
_HARNESS = None


class _DaemonThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


class _AuthServerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FireflySyncHarness/1"

    def log_message(self, fmt, *args):
        return

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization", "") == "Bearer " + AUTH_FIXTURE

    def _run_with_server_context(self, fn):
        cfg = _HARNESS.cfg
        token = cfg.set_user_context(user_dir=_HARNESS.server_root)
        try:
            return fn()
        except Exception as exc:
            self._json({"ok": False, "code": "HARNESS_SERVER_ERROR",
                        "error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            cfg.reset_user_context(token)

    def do_GET(self):
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        path = urlparse(self.path).path
        from api import data_sync

        def dispatch():
            if path == "/sync/state":
                return data_sync.sync_state(self)
            if path == "/sync/manifest":
                return data_sync.sync_manifest(self)
            if path == "/time":
                return self._json({"ok": True, "date": "2026-10-27"})
            return self._json({"error": "not found"}, 404)

        self._run_with_server_context(dispatch)

    def do_POST(self):
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        path = urlparse(self.path).path
        from api import data_sync

        def dispatch():
            if path == "/sync/ops":
                return data_sync.sync_ops(self)
            if path == "/sync/import":
                return data_sync.sync_import(self)
            if path == "/sync/export":
                return data_sync.sync_export(self)
            return self._json({"error": "not found"}, 404)

        self._run_with_server_context(dispatch)


class SyncHarness:
    def __init__(self):
        global _HARNESS
        _HARNESS = self
        self.tmp = Path(tempfile.mkdtemp(prefix="firefly_sync_hot5_"))
        self.user_root = self.tmp / "user_data"
        self.server_root = self.tmp / "server_root"
        self.user_root.mkdir(parents=True)
        self.server_root.mkdir(parents=True)
        os.environ["FIREFLY_DATA_DIR"] = str(self.tmp)
        # 开发态 core.paths 只在 FIREFLY_ANDROID 下使用 FIREFLY_DATA_DIR；
        # 打开它可让整个真实后端把临时目录当数据根，测试绝不碰项目 user_data。
        os.environ["FIREFLY_ANDROID"] = "1"
        os.environ["FIREFLY_NO_BROWSER"] = "1"
        os.environ.pop("FIREFLY_SERVER", None)

        self.auth_server = _DaemonThreadingHTTPServer(
            ("127.0.0.1", 0), _AuthServerHandler)
        self.auth_port = self.auth_server.server_address[1]
        self.auth_thread = threading.Thread(
            target=self.auth_server.serve_forever, daemon=True)
        self.auth_thread.start()

        self._write_seed_auth_and_config()
        app_dir = Path(__file__).resolve().parent.parent / "app"
        if str(app_dir) not in sys.path:
            sys.path.insert(0, str(app_dir))

        from modules import app_config as cfg
        import server as local_server

        self.cfg = cfg
        self.local_server_module = local_server
        self.device_roots: dict[str, Path] = {}

        harness = self

        class DeviceHandler(local_server.FireflyHandler):
            def do_POST(self_h):
                root = harness.device_roots.get(
                    self_h.headers.get("X-Test-Device", ""))
                if root is None:
                    return super().do_POST()
                token = harness.cfg.set_user_context(user_dir=root)
                try:
                    return super().do_POST()
                finally:
                    harness.cfg.reset_user_context(token)

        self.local_server = local_server.LocalServer(
            ("127.0.0.1", 0), DeviceHandler)
        self.local_port = self.local_server.server_address[1]
        self.local_thread = threading.Thread(
            target=self.local_server.serve_forever, daemon=True)
        self.local_thread.start()

    def _write_seed_auth_and_config(self):
        (self.user_root / "config.json").write_text(json.dumps({
            "auth_server_url": f"http://127.0.0.1:{self.auth_port}",
            "api_key": "",
            "model": "deepseek-chat",
        }, ensure_ascii=False), encoding="utf-8")
        (self.user_root / "auth.json").write_text(json.dumps({
            "token": AUTH_FIXTURE,
            "email": "hot5@example.invalid",
            "role": "user",
            "expires_at": "2099-12-31 23:59:59",
            "saved_at": "2026-10-27 00:00:00",
        }, ensure_ascii=False), encoding="utf-8")

    def close(self):
        for srv in (getattr(self, "local_server", None),
                    getattr(self, "auth_server", None)):
            if srv is None:
                continue
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def reset(self):
        for root in [self.server_root, *self.device_roots.values()]:
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)

    def device(self, name: str, clean: bool = True) -> Path:
        root = self.tmp / "devices" / name
        if clean and root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        self.device_roots[name] = root
        return root

    def mode_root(self, device: str, mode: str = "story", clean: bool = False) -> Path:
        root = self.device(device, clean=clean) / mode
        root.mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "journal").mkdir(parents=True, exist_ok=True)
        (root / "images").mkdir(parents=True, exist_ok=True)
        return root

    def server_mode_root(self, mode: str = "story") -> Path:
        root = self.server_root / mode
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "journal").mkdir(parents=True, exist_ok=True)
        (root / "images").mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _request(url: str, method: str = "GET", data: bytes | None = None,
                 headers: dict | None = None, timeout: float = 20):
        req = urllib.request.Request(
            url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def local_post(self, device: str, path: str, payload: dict | None = None):
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        return self._request(
            f"http://127.0.0.1:{self.local_port}{path}", "POST", data,
            {"Content-Type": "application/json", "X-Test-Device": device})

    def sync_now(self, device: str, mode: str = "all"):
        status, headers, body = self.local_post(device, "/sync/now", {"mode": mode})
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = {"ok": False, "error": "non-json", "raw": body.decode("utf-8", "replace")}
        return status, headers, parsed

    def server_call(self, method: str, path: str, data: bytes | None = None,
                    headers: dict | None = None, timeout: float = 20):
        merged = {"Authorization": "Bearer " + AUTH_FIXTURE}
        merged.update(headers or {})
        return self._request(
            f"http://127.0.0.1:{self.auth_port}{path}",
            method, data, merged, timeout)

    def server_json(self, method: str, path: str, payload: dict | None = None,
                    timeout: float = 20):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, resp_headers, body = self.server_call(
            method, path, data, headers, timeout)
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            parsed = {"raw": body.decode("utf-8", "replace")}
        return status, resp_headers, parsed

    def state(self, mode: str = "story"):
        return self.server_json("GET", f"/sync/state?mode={mode}")

    def manifest(self, mode: str = "story"):
        return self.server_json("GET", f"/sync/manifest?mode={mode}")

    def ops(self, mode: str, ops: list):
        return self.server_json("POST", "/sync/ops", {"mode": mode, "ops": ops})

    def export(self, mode: str, files: list, state_rev=None):
        payload = {"mode": mode, "files": files}
        if state_rev is not None:
            payload["state_rev"] = state_rev
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.server_call(
            "POST", "/sync/export", data,
            {"Content-Type": "application/json"})

    def upload(self, mode: str, files: dict, mtimes: dict | None = None,
               base_sha: dict | None = None, state_rev=None):
        boundary = "hot5" + uuid.uuid4().hex
        chunks = []

        def field(name: str, value: str):
            chunks.append(
                b"--" + boundary.encode() + b"\r\n"
                + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + value.encode("utf-8") + b"\r\n")

        field("mode", mode)
        field("mtimes", json.dumps(mtimes or {}, ensure_ascii=False))
        field("base_sha", json.dumps(base_sha or {}, ensure_ascii=False))
        if state_rev is not None:
            field("state_rev", str(state_rev))
        for name, value in files.items():
            raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
            chunks.append(
                b"--" + boundary.encode() + b"\r\n"
                + f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n".encode()
                + raw + b"\r\n")
        chunks.append(b"--" + boundary.encode() + b"--\r\n")
        body = b"".join(chunks)
        return self._server_multipart(body, boundary)

    def _server_multipart(self, body: bytes, boundary: str):
        status, headers, raw = self.server_call(
            "POST", "/sync/import", body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}",
             "X-Firefly-Sync-Protocol": "2"})
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            parsed = {"raw": raw.decode("utf-8", "replace")}
        return status, headers, parsed


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def jsonl_records(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                out.append(value)
        except Exception:
            continue
    return out
