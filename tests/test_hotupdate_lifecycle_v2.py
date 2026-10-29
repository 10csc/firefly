# -*- coding: utf-8 -*-
"""0.9.2 热更新器生命周期加固回归。

三个独立 oracle：
  * py 补丁的重启待办必须来自持久 state，不能被同一进程的内存状态丢失；
  * hotupdate 在业务包完全不可导入时仍可独立运行；
  * pending 候选启动崩溃后，下一次启动在注入覆盖层之前自动回滚。
"""
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


def _state(hu, **fields) -> dict:
    state = dict(hu._DEFAULTS)
    state["base_version"] = "0.9.2"
    state.update(fields)
    return state


@pytest.fixture
def hotupdate(tmp_path, monkeypatch):
    data_root = tmp_path / "firefly_data"
    monkeypatch.setenv("FIREFLY_DATA_DIR", str(data_root))
    monkeypatch.setenv("FIREFLY_BASE_VERSION", "0.9.2")
    monkeypatch.delenv("FIREFLY_ANDROID", raising=False)

    import hotupdate as hu

    configure = getattr(hu, "configure_runtime", None)
    if configure is not None:
        configure(base_version="0.9.2", data_root=data_root)
    hu.reset_for_test()
    yield hu
    hu.reset_for_test()


def test_py_pending_survives_runtime_reset_and_requests_native_restart(hotupdate, monkeypatch):
    hu = hotupdate
    hu.save_state(_state(
        hu,
        applied_serial=8,
        pending_serial=8,
        applied_layer="py",
        applied_pid=os.getpid(),
        pending_boot_pid=os.getpid(),
    ))
    hu.reset_for_test()

    status = hu.status()
    assert status["pending_serial"] == 8
    assert status["restart_pending"] is True
    assert status["reload_pending"] is False

    calls: list[tuple[int, str]] = []
    from hotupdate import native

    native.set_restart_handler(
        lambda pid, reason: calls.append((pid, reason)) or True
    )
    result = hu.request_native_restart()
    assert result["ok"] is True
    assert calls == [(os.getpid(), "py-pending")]


def test_loader_imports_and_reports_status_without_any_business_module(tmp_path):
    data_root = tmp_path / "firefly_data"
    code = r"""
import importlib.abc
import json
import sys

FORBIDDEN = {"api", "core", "modules", "routes", "shared_http", "voice"}

class BlockBusiness(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in FORBIDDEN:
            raise RuntimeError("business import blocked: " + fullname)
        return None

sys.meta_path.insert(0, BlockBusiness())
import hotupdate
configure = getattr(hotupdate, "configure_runtime", None)
if configure:
    configure(base_version="0.9.2")
hotupdate.reset_for_test()
print(json.dumps(hotupdate.status(), ensure_ascii=False))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP)
    env["FIREFLY_DATA_DIR"] = str(data_root)
    env["FIREFLY_BASE_VERSION"] = "0.9.2"
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["base_version"] == "0.9.2"


def test_interrupted_candidate_boot_rolls_back_before_overlay_injection(hotupdate):
    hu = hotupdate
    overlay = hu.py_dir()
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "candidate.py").write_text("raise RuntimeError('bad patch')\n", encoding="utf-8")
    hu.save_state(_state(
        hu,
        applied_serial=8,
        pending_serial=8,
        applied_layer="py",
        applied_pid=111111,
        pending_boot_pid=222222,
    ))

    result = hu.prepare_startup()

    state = hu.load_state()
    assert result["rolled_back"] is True
    assert state["pending_serial"] == 0
    assert state["applied_serial"] == 0
    assert "未能完成启动" in state["rolled_back_reason"]
    assert not overlay.exists()


def test_successful_new_process_boot_clears_pending(hotupdate, monkeypatch):
    hu = hotupdate
    hu.save_state(_state(
        hu,
        applied_serial=8,
        pending_serial=8,
        applied_layer="py",
        applied_pid=111111,
    ))
    monkeypatch.setattr(hu.os, "getpid", lambda: 333333)

    prepared = hu.prepare_startup()
    assert prepared["boot_candidate"] is True
    assert hu.load_state()["pending_boot_pid"] == 333333

    result = hu.boot_ok()
    assert result["ok"] is True
    assert hu.load_state()["pending_serial"] == 0
    assert hu.load_state()["boot_fail_count"] == 0


def test_base_upgrade_is_recorded_once_before_candidate_boot(hotupdate):
    """底座换代清掉旧覆盖层后，必须把新底座写回状态。

    否则重启后的新进程会再次看到旧的 base_version，把刚应用的候选补丁当成
    上一底座遗留物回滚。这个错误只在替换 APK 后热更并重启的连续两次启动中出现。
    """
    hu = hotupdate
    overlay = hu.py_dir()
    overlay.mkdir(parents=True)
    (overlay / "old.py").write_text("OLD = True\n", encoding="utf-8")
    hu.save_state(_state(
        hu,
        base_version="0.9.1",
        applied_serial=7,
        pending_serial=0,
        applied_layer="py",
        applied_pid=111111,
    ))

    first = hu.prepare_startup()
    state = hu.load_state()
    assert first["rolled_back"] is True
    assert first["reason"] == "base-changed"
    assert state["base_version"] == "0.9.2"
    assert state["applied_serial"] == 0
    assert not overlay.exists()

    second = hu.prepare_startup()
    assert second["rolled_back"] is False
    assert hu.load_state()["base_version"] == "0.9.2"


def test_android_bootstrap_quarantines_bad_overlay_and_retries_base(hotupdate, tmp_path):
    hu = hotupdate
    overlay = tmp_path / "hotupdate" / "py"
    overlay.mkdir(parents=True)
    (overlay / "broken.py").write_text("raise RuntimeError('broken overlay')\n", encoding="utf-8")
    hu.save_state(_state(
        hu,
        applied_serial=8,
        pending_serial=8,
        applied_layer="py",
        applied_pid=111111,
        pending_boot_pid=os.getpid(),
    ))

    path = ROOT / "android" / "app" / "src" / "main" / "python" / "start_server.py"
    spec = importlib.util.spec_from_file_location("firefly_start_server_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    attempts = []

    def load_server():
        attempts.append(1)
        if len(attempts) == 1:
            raise ImportError("bad python overlay")
        return "base-server"

    result = module._load_server_with_overlay_recovery(str(overlay), load_server)

    assert result == "base-server"
    assert len(attempts) == 2
    assert not overlay.exists()
    assert hu.load_state()["pending_serial"] == 0


def test_android_restart_uses_visible_isolated_handoff_process():
    """Android 14 会拦截“进程已死后再由闹钟拉起 Activity”的 BAL。

    重启必须先在独立进程里取得一个可见交接窗口，确认它已就绪后才能销毁旧进程；
    交接窗口再拉起新的默认进程。否则旧进程消失时 PID 虽然改变，但 Python 服务
    永远不会启动，真机会表现为连接被关闭。
    """
    manifest = (
        ROOT / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    ).read_text(encoding="utf-8")
    bridge = (
        ROOT
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "firefly"
        / "android"
        / "HotUpdateBridge.kt"
    ).read_text(encoding="utf-8")
    handoff_path = (
        ROOT
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "firefly"
        / "android"
        / "HotUpdateRestartActivity.kt"
    )

    assert 'android:name=".HotUpdateRestartActivity"' in manifest
    assert 'android:process=":hotupdate_restart"' in manifest
    assert "import android.app.AlarmManager" not in bridge
    assert "import android.app.PendingIntent" not in bridge
    assert "PendingIntent.getActivity" not in bridge
    assert "HotUpdateRestartActivity::class.java" in bridge
    assert "Process.killProcess" not in bridge
    assert handoff_path.is_file()
    handoff = handoff_path.read_text(encoding="utf-8")
    assert "override fun onResume()" in handoff
    assert "Process.killProcess(oldPid)" in handoff
    assert "MainActivity::class.java" in handoff


def test_restart_handoff_kills_known_old_pid_without_proc_probe():
    """ColorOS 可能让应用看不到其他进程的 /proc 条目。

    真机 E2E 中交接页因此误判旧进程已退出，MainActivity 被旧的同 PID 进程复用，
    Python 的 68 个模块完全没有重载。独立交接进程与旧进程同 UID，必须按 Intent
    中携带的旧 PID 直接执行原生 kill，不能再把 /proc 可见性当作退出证据。
    """
    source_dir = (
        ROOT
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "firefly"
        / "android"
    )
    bridge = (source_dir / "HotUpdateBridge.kt").read_text(encoding="utf-8")
    handoff = (source_dir / "HotUpdateRestartActivity.kt").read_text(encoding="utf-8")

    assert "Process.killProcess(oldPid)" in handoff
    assert 'File("/proc/' not in handoff
    assert "Process.killProcess" not in bridge
    assert "startActivity(" in handoff
