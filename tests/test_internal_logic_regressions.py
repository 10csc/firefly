# -*- coding: utf-8 -*-
"""内部逻辑、并发与规范清理的独立回归。"""
from __future__ import annotations

import ast
import importlib
import io
import json
import logging
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
for _path in (ROOT, APP):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


class FakeHandler:
    def __init__(self, body: dict | None = None):
        raw = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.path = "/"
        self.payload = None
        self.status = 200

    def _json(self, payload, status=200):
        self.payload = payload
        self.status = status


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buf.getvalue()


def test_replacement_quota_excludes_existing_card_bytes(monkeypatch, tmp_path):
    from plaza import install

    old_bytes = 5 * 1024 * 1024
    new_bytes = 4 * 1024 * 1024
    root = tmp_path / "user"
    old_card = root / "old-card"
    old_card.mkdir(parents=True)
    (old_card / "payload.bin").write_bytes(b"x" * old_bytes)

    monkeypatch.setattr(install, "_is_server", lambda: True)
    monkeypatch.setattr(install, "_user_root", lambda: root)
    monkeypatch.setattr(
        install,
        "plaza_usage",
        lambda: {"count": 1, "bytes": old_bytes, "ids": ["old-card"]},
    )

    replacing = install.check_quota(
        new_bytes, replacing=True, replacing_id="old-card")
    fresh = install.check_quota(new_bytes, replacing=False)

    assert replacing == (True, "")
    assert fresh[0] is False


def test_pack_registry_initializes_once_under_concurrency(monkeypatch, tmp_path):
    registry_module = importlib.import_module("core.pack_registry")
    registry_module._REGISTRIES.clear()
    created = []
    gate = threading.Barrier(2)

    class SlowRegistry:
        def __init__(self, path):
            self.path = path
            created.append(self)

        def load(self, persist=False):
            assert persist is False
            time.sleep(0.1)

    monkeypatch.setattr(registry_module, "PackRegistry", SlowRegistry)
    results = []
    errors = []
    target = tmp_path / "packs.json"

    def worker():
        try:
            gate.wait(timeout=5)
            results.append(registry_module.pack_registry(target))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(created) == 1
    assert results[0] is results[1] is created[0]


def test_import_restore_reports_cache_reload_failure(
        monkeypatch, tmp_path, caplog):
    from infra.sync import restore
    from modules import llm_base

    payload = _zip_bytes({"data/memory.md": b"restored"})
    summary = {}
    monkeypatch.setattr(restore.cfg, "mode_root", lambda _mode: tmp_path / "story")
    monkeypatch.setattr(restore, "_backup_current_mode", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(restore, "_swap_dir_into_place", lambda *_args: "")
    monkeypatch.setattr(
        llm_base,
        "clear_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("cache reload failed")),
    )
    caplog.set_level(logging.WARNING)

    ok, error, _count = restore._import_zip_to_mode(
        payload, "story", summary=summary)

    assert ok, error
    assert summary["cache_reload_ok"] is False
    assert "cache reload failed" in caplog.text


def test_snapshot_restore_reports_cache_reload_failure(
        monkeypatch, tmp_path, caplog):
    from infra.sync import restore
    from modules import llm_base

    payload = _zip_bytes({
        "story/character/preset.json": b'{"id":"story"}',
        "story/data/memory.md": b"restored",
    })
    summary = {}
    monkeypatch.setattr(restore.cfg, "_user_ctx_dir", lambda: tmp_path)
    monkeypatch.setattr(restore.cfg, "valid_mode", lambda mode: mode == "story")
    monkeypatch.setattr(restore.cfg, "all_packs", lambda: [])
    monkeypatch.setattr(restore.cfg, "mode_root", lambda _mode: tmp_path / "story")
    monkeypatch.setattr(restore, "_swap_dir_into_place", lambda *_args: "")
    monkeypatch.setattr(
        llm_base,
        "clear_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("snapshot cache failed")),
    )
    caplog.set_level(logging.WARNING)

    ok, error, _count = restore._restore_full_snapshot(
        payload, backup=False, summary=summary)

    assert ok, error
    assert summary["cache_reload_ok"] is False
    assert "snapshot cache failed" in caplog.text


def test_import_endpoint_surfaces_cache_reload_failure(monkeypatch):
    from api import data_export

    payload = _zip_bytes({"data/memory.md": b"restored"})
    handler = FakeHandler()
    monkeypatch.setattr(
        data_export,
        "parse_multipart",
        lambda _handler, max_bytes: (
            {"mode": "story"},
            {"file": {"data": payload}},
        ),
    )
    monkeypatch.setattr(data_export, "_is_snapshot_zip", lambda _archive: False)
    monkeypatch.setattr(data_export.cfg, "valid_mode", lambda mode: mode == "story")

    def fake_import(data, mode, summary=None):
        assert data == payload
        assert mode == "story"
        summary["cache_reload_ok"] = False
        return True, "", 1

    monkeypatch.setattr(data_export, "_import_zip_to_mode", fake_import)

    data_export.import_data(handler)

    assert handler.payload == {
        "ok": True,
        "files": 1,
        "mode": "story",
        "cache_reload_ok": False,
    }


def test_core_never_imports_api():
    violations = []
    for path in sorted((APP / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "api" or name.startswith("api."):
                    violations.append(f"{path.name}:{node.lineno}: {name}")
    assert not violations, violations


def test_draft_metadata_failure_removes_new_orphan_zip(monkeypatch, tmp_path):
    from plaza import drafts

    owner = tmp_path / "owner"
    owner.mkdir()
    parsed = {
        "manifest": {
            "id": "draft-card",
            "name": "草稿",
            "char_name": "角色",
            "category": "剧情",
        },
        "digest": "digest",
    }

    def write_bytes(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True

    monkeypatch.setattr(drafts, "owner_dir", lambda _owner: owner)
    monkeypatch.setattr(drafts.cf, "parse_card_zip", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(drafts, "atomic_write_bytes", write_bytes)
    monkeypatch.setattr(drafts, "atomic_write_json", lambda *_args: False)
    drafts._DRAFT_SAVE_LOCKS.clear()

    with pytest.raises(drafts.DraftError) as exc:
        drafts.save_draft("owner", b"new-card")

    assert exc.value.code == drafts.DRAFT_PUBLISH_FAILED
    assert not (owner / "draft-card.zip").exists()
    assert not (owner / "draft-card.json").exists()


def test_diag_summary_has_no_empty_placeholder_loop():
    source = (APP / "api" / "diag.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="api/diag.py")
    summary = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_summary"
    )
    empty_loops = [
        node.lineno for node in ast.walk(summary)
        if isinstance(node, (ast.For, ast.While))
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Pass)
    ]
    assert not empty_loops, empty_loops


def test_pre_restore_snapshot_uses_atomic_write(monkeypatch, tmp_path):
    import routes_data
    import routes_snapshot
    from modules import storage

    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    name = "snapshot-20261029-120000.zip"
    (snapshots / name).write_bytes(_zip_bytes({"story/data/x": b"x"}))
    atomic_calls = []
    restore_calls = []
    handler = FakeHandler({"name": name})

    monkeypatch.setattr(routes_snapshot, "_snapshots_dir", lambda: snapshots)
    monkeypatch.setattr(routes_snapshot, "build_full_snapshot_zip",
                        lambda: _zip_bytes({"story/data/backup": b"old"}))
    monkeypatch.setattr(
        storage,
        "atomic_write_bytes",
        lambda path, data: atomic_calls.append((path, data)) or False,
    )

    def fake_restore(data, backup=True, summary=None):
        if summary is not None:
            summary["cache_reload_ok"] = False
        restore_calls.append({"data": data, "backup": backup, "summary": summary})
        return True, "", 1

    monkeypatch.setattr(routes_data, "_restore_full_snapshot", fake_restore)

    routes_snapshot.snapshot_restore(handler)

    assert atomic_calls
    assert handler.payload["backup_ok"] is False
    assert handler.payload["cache_reload_ok"] is False
    assert restore_calls[0]["backup"] is True


def test_balance_rejects_non_exact_api_base_without_request(monkeypatch):
    import routes_config as routes_config_module

    calls = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(routes_config_module.cfg, "API_BASE",
                        "https://api.deepseek.com/v1", raising=False)
    handler = FakeHandler()

    with patch.dict(
        routes_config_module.cfg.config,
        {"api_base": "https://api.deepseek.com.evil/v1"},
        clear=True,
    ):
        routes_config_module.get_balance(handler)

    assert handler.payload["supported"] is False
    assert calls == []


def test_save_user_memory_uses_atomic_write(monkeypatch, tmp_path):
    from api import debug
    from modules import memory_manager

    target = tmp_path / "memory.md"
    writes = []
    monkeypatch.setattr(memory_manager, "_memory_file", lambda _mode: target)
    monkeypatch.setattr(debug, "_body_mode", lambda _body: "story")
    monkeypatch.setattr(
        debug,
        "atomic_write_text",
        lambda path, text: writes.append((path, text)) or False,
        raising=False,
    )
    handler = FakeHandler({"mode": "story", "content": "新记忆"})

    debug.save_user_memory(handler)

    assert writes == [(target, "新记忆")]
    assert handler.payload["ok"] is False
    assert not target.exists()


def test_install_chain_serializes_same_user(monkeypatch, tmp_path):
    from plaza import install

    active = 0
    peak = 0
    state_lock = threading.Lock()
    start = threading.Barrier(2)

    class Registry:
        def register(self, card_id, **_kwargs):
            return {"id": card_id}

    def parse_card(card_id):
        return {
            "manifest": {
                "id": card_id.decode("ascii"),
                "name": card_id.decode("ascii"),
                "char_name": "角色",
            },
            "files": {"preset.json": b"{}"},
            "preset": {"id": card_id.decode("ascii")},
        }

    def quota(*_args, **_kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with state_lock:
            active -= 1
        return True, ""

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True

    monkeypatch.setattr(install, "_user_root", lambda: tmp_path)
    monkeypatch.setattr(install, "fetch_card_zip", lambda card_id: card_id.encode("ascii"))
    monkeypatch.setattr(install.cf, "parse_card_zip", lambda data: parse_card(data))
    monkeypatch.setattr(install, "check_quota", quota)
    monkeypatch.setattr(install, "atomic_write_bytes", write)
    monkeypatch.setattr(install.cfg, "pack_registry", lambda: Registry())
    monkeypatch.setattr(install.cfg, "reload_presets", lambda: None)
    monkeypatch.setattr(install, "_clear_knowledge_cache", lambda _card_id: None)
    monkeypatch.setattr(install, "_knowledge_file_count", lambda _card_id: 0)
    monkeypatch.setattr(install, "_install_pack_stickers",
                        lambda _card_id, _files, _char_name: 0)
    install._INSTALL_LOCKS.clear()
    results = []

    def worker(card_id):
        start.wait(timeout=5)
        results.append(install.install_card(card_id))

    threads = [
        threading.Thread(target=worker, args=("card-a",)),
        threading.Thread(target=worker, args=("card-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert all(result[0] for result in results)
    assert peak == 1


def test_server_process_cleanup_uses_argument_lists(monkeypatch):
    server_module = importlib.import_module("server")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "netstat":
            return SimpleNamespace(
                returncode=0,
                stdout="  TCP  127.0.0.1:8765  0.0.0.0:0  LISTENING  4321\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server_module.subprocess, "run", fake_run)
    killed = server_module._kill_listener_on_port(8765)

    assert killed is True
    assert calls[0][0][:2] == ["netstat", "-ano"]
    assert calls[1][0] == ["taskkill", "/F", "/PID", "4321"]
    assert all(isinstance(args, list) for args, _kwargs in calls)
    assert all("shell" not in kwargs or kwargs["shell"] is False
               for _args, kwargs in calls)


def test_voice_manifest_has_no_schemeless_fallback():
    from voice import downloader

    template = (
        "www.modelscope.cn/api/v1/models/org/name/repo"
        "?Revision=master&FilePath={file}"
    )

    assert downloader._manifest_url(template) is None
    assert not hasattr(downloader, "API_BASE")


def test_plaza_api_uses_static_hashlib_import():
    path = APP / "plaza" / "api.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports_hashlib = any(
        isinstance(node, ast.Import)
        and any(alias.name == "hashlib" for alias in node.names)
        for node in tree.body
    )
    dynamic_imports = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "__import__"
    ]

    assert imports_hashlib
    assert not dynamic_imports


def test_server_directory_has_no_backup_files():
    backups = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "server").rglob("*.bak*")
        if path.is_file()
    )
    assert not backups, backups
