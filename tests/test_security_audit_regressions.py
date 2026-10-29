# -*- coding: utf-8 -*-
"""安全审计回归：服务器暴露、路径穿越、存储配额、配置泄漏与上传竞态。"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
for _p in (ROOT, APP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class FakeH:
    def __init__(self, path: str = "/", body: dict | None = None):
        raw = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        self.path = path
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = 200
        self.payload = None
        self.out_headers: list[tuple[str, str]] = []

    def _json(self, payload, status=200):
        self.payload = payload
        self.status = status

    def send_response(self, status, reason=None):
        self.status = status

    def send_header(self, key, value):
        self.out_headers.append((key, value))

    def end_headers(self):
        pass


@pytest.mark.parametrize(
    ("handler_name", "path"),
    [
        ("voice_status", "/voice/status"),
        ("voice_tts", "/voice/tts"),
        ("voice_file", "/voice-file?mode=story&name=v1.wav"),
        ("voice_plugin", "/voice/plugin"),
        ("voice_plugin_action", "/voice/plugin"),
    ],
)
def test_all_voice_handlers_are_denied_on_server(monkeypatch, handler_name, path):
    from voice import plugin as voice_plugin_mod
    from voice import routes as voice_routes

    monkeypatch.setenv("FIREFLY_SERVER", "1")
    monkeypatch.setattr(voice_routes.voice, "status", lambda: {"engine_ok": True})
    monkeypatch.setattr(voice_routes.voice, "synthesize",
                        lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(voice_plugin_mod, "state", lambda: {"id": "ready"})

    handler = FakeH(path, {})
    getattr(voice_routes, handler_name)(handler)

    assert handler.status == 403
    assert handler.payload and handler.payload.get("ok") is False


def test_voice_downloader_rejects_path_traversal(monkeypatch, tmp_path):
    from voice import downloader, plugin

    root = tmp_path / "voice"
    called: list[Path] = []

    def fake_download(_url, dst, *_args, **_kwargs):
        called.append(dst)
        dst.write_bytes(b"escaped")

    monkeypatch.setattr(downloader.paths, "plugin_root", lambda: root)
    monkeypatch.setattr(downloader, "_manifest", lambda _template: {})
    monkeypatch.setattr(downloader, "_download_one", fake_download)
    plugin._DL.clear()
    try:
        downloader._run("https://example.invalid/{file}", ["../escape.txt"])
    finally:
        status = dict(plugin._DL)
        plugin._DL.clear()

    assert called == []
    assert not (root / "escape.txt").exists()
    assert status.get("error")


def test_voice_downloader_accepts_safe_flat_filename(monkeypatch, tmp_path):
    from voice import downloader, plugin

    root = tmp_path / "voice"
    called: list[Path] = []

    def fake_download(_url, dst, *_args, **_kwargs):
        called.append(dst)
        dst.write_bytes(b"model")

    monkeypatch.setattr(downloader.paths, "plugin_root", lambda: root)
    monkeypatch.setattr(downloader, "_manifest", lambda _template: {})
    monkeypatch.setattr(downloader, "_download_one", fake_download)
    plugin._DL.clear()
    try:
        downloader._run("https://example.invalid/{file}", ["config.json"])
        status = dict(plugin._DL)
    finally:
        plugin._DL.clear()

    assert called == [(root / "models" / "config.json").resolve()]
    assert (root / "models" / "config.json").read_bytes() == b"model"
    assert not status.get("error")


def test_draft_owner_byte_quota_rejects_before_write(monkeypatch, tmp_path):
    from plaza import drafts

    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "old.zip").write_bytes(b"x" * 80)
    writes: list[Path] = []

    monkeypatch.setattr(drafts, "owner_dir", lambda _ohash: owner)
    monkeypatch.setattr(drafts, "DRAFT_MAX_BYTES", 100, raising=False)
    monkeypatch.setattr(drafts.cf, "parse_card_zip", lambda *_args, **_kwargs: {
        "manifest": {"id": "new-card", "name": "新卡", "char_name": "角色", "category": "剧情"},
        "digest": "digest",
    })
    monkeypatch.setattr(
        drafts, "atomic_write_bytes",
        lambda fp, _data: writes.append(fp) or True,
    )
    monkeypatch.setattr(drafts, "atomic_write_json", lambda *_args: True)

    with pytest.raises(drafts.DraftError) as exc:
        drafts.save_draft("owner", b"y" * 30)

    assert exc.value.code == drafts.DRAFT_LIMIT
    assert writes == []


def test_server_config_api_uses_safe_field_whitelist(monkeypatch):
    from routes_config import _is_server
    import routes_config as rc

    monkeypatch.setenv("FIREFLY_SERVER", "1")
    assert _is_server()
    secret_base = "https://internal-provider.example/v1"
    config = {
        "api_base": secret_base,
        "active_provider": "internal",
        "providers": [{
            "id": "internal",
            "name": "Internal",
            "base_url": secret_base,
            "models": ["m"],
            "caps": {},
            "api_key": "server-only-key",
        }],
        "analyzer_model": "deepseek-flash",
    }
    monkeypatch.setattr(rc.cfg, "active_provider", lambda: config["providers"][0])
    monkeypatch.setattr(rc.cfg, "get_api_key", lambda: "")
    handler = FakeH("/config")

    with patch.dict(rc.cfg.config, config, clear=True):
        rc.get_config(handler)

    payload = handler.payload or {}
    assert "api_base" not in payload
    assert "api_bases" not in payload
    assert secret_base not in json.dumps(payload, ensure_ascii=False)


def test_server_backup_config_uses_safe_field_whitelist(monkeypatch, tmp_path):
    from api import data_export

    monkeypatch.setenv("FIREFLY_SERVER", "1")
    secret_base = "https://internal-provider.example/v1"
    config = {
        "api_base": secret_base,
        "GO_BASE": "https://go.internal.example/v1",
        "providers": [{"id": "private", "base_url": secret_base, "api_key": "secret"}],
        "analyzer_model": "deepseek-flash",
        "memory_window_turns": 100,
    }

    with patch.dict(data_export.cfg.config, config, clear=True):
        blob = data_export._build_backup_zip(tmp_path, "story")

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        packaged = json.loads(zf.read("_config.json").decode("utf-8"))

    assert packaged.get("analyzer_model") == "deepseek-flash"
    assert "api_base" not in packaged
    assert "GO_BASE" not in packaged
    assert "providers" not in packaged
    assert secret_base not in json.dumps(packaged, ensure_ascii=False)


def test_upload_image_quota_check_and_write_are_serialized(monkeypatch, tmp_path):
    import routes
    import routes_assets as ra

    used = 0
    barrier = threading.Barrier(2)
    results: list[dict] = []

    monkeypatch.setattr(ra.cfg, "valid_mode", lambda _mode: True)
    monkeypatch.setattr(ra.cfg, "user_dir_id", lambda: 7)
    monkeypatch.setattr(ra, "_image_dir", lambda _mode: tmp_path)
    monkeypatch.setattr(ra, "_image_quota_bytes", lambda: 10)

    def fake_used():
        snapshot = used
        time.sleep(0.05)
        return snapshot

    def fake_write(_fp, data):
        nonlocal used
        time.sleep(0.02)
        used += len(data)
        return True

    def fake_multipart(_handler, **_kwargs):
        return {"mode": "story"}, {
            "file": {"filename": "probe.png", "data": b"123456"},
        }

    monkeypatch.setattr(ra, "_image_used_bytes", fake_used)
    monkeypatch.setattr("modules.storage.atomic_write_bytes", fake_write)
    monkeypatch.setattr(routes, "parse_multipart", fake_multipart)
    locks = getattr(ra, "_IMAGE_UPLOAD_LOCKS", None)
    if isinstance(locks, dict):
        locks.clear()

    def worker():
        barrier.wait(timeout=5)
        h = FakeH("/upload-image")
        ra.upload_image(h)
        results.append(h.payload or {})

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(1 for result in results if result.get("ok")) == 1
    assert sum(1 for result in results if not result.get("ok")) == 1
    assert used == 6


def test_get_image_sets_nosniff(monkeypatch, tmp_path):
    import routes_assets as ra

    img_id = "img_0123456789ab"
    (tmp_path / f"{img_id}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(ra.cfg, "valid_mode", lambda _mode: True)
    monkeypatch.setattr(ra, "_image_dir", lambda _mode: tmp_path)
    handler = FakeH(f"/image?id={img_id}&mode=story")

    ra.get_image(handler)

    assert handler.status == 200
    assert ("X-Content-Type-Options", "nosniff") in handler.out_headers
