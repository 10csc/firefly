# -*- coding: utf-8 -*-
"""独立回归：配置持久化、审核错误分流、恢复备份与代理响应阶段。

不访问网络、生产目录或真实用户数据。配置重启用独立子进程验证；快照恢复只写临时目录；
代理测试用假上游和假响应写入器验证状态行数量。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
SERVER = ROOT / "server"
for _p in (ROOT, APP, SERVER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  V", name)
    else:
        FAIL += 1
        print("  X", name, detail)


def _subprocess_env(data_root: Path) -> dict:
    env = os.environ.copy()
    env.update({
        "FIREFLY_ANDROID": "1",
        "FIREFLY_ANDROID_APP_DIR": str(APP),
        "FIREFLY_DATA_DIR": str(data_root),
        "PYTHONPATH": os.pathsep.join([str(APP), str(ROOT)]),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _run_config_roundtrip(tmp: Path) -> None:
    env = _subprocess_env(tmp)
    write_code = r"""
import json
from core import config
from core import paths

config.config["memory_window_turns"] = 137
config.config["memory_keep_turns"] = 41
config.config["memory_warn_turns"] = 109
config.save_config()
print("RESULT=" + json.dumps(json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))))
"""
    loaded_code = r"""
import json
from core import config
from routes_config import get_config

class H:
    def _json(self, payload, status=200):
        print("RESULT=" + json.dumps({"status": status, "payload": payload}))

print("LOADED=" + json.dumps({
    "memory_window_turns": config.config.get("memory_window_turns"),
    "memory_keep_turns": config.config.get("memory_keep_turns"),
    "memory_warn_turns": config.config.get("memory_warn_turns"),
}))
get_config(H())
"""
    first = subprocess.run(
        [sys.executable, "-B", "-c", write_code],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    check("配置写入子进程退出码为 0", first.returncode == 0,
          first.stderr.strip()[-1000:])
    first_lines = [x for x in first.stdout.splitlines() if x.startswith("RESULT=")]
    saved = json.loads(first_lines[-1][len("RESULT="):]) if first_lines else {}
    check("三项记忆配置真实写入 config.json",
          saved.get("memory_window_turns") == 137
          and saved.get("memory_keep_turns") == 41
          and saved.get("memory_warn_turns") == 109,
          str(saved))

    second = subprocess.run(
        [sys.executable, "-B", "-c", loaded_code],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    check("配置重新导入子进程退出码为 0", second.returncode == 0,
          second.stderr.strip()[-1000:])
    lines = second.stdout.splitlines()
    loaded_line = next((x for x in lines if x.startswith("LOADED=")), "")
    loaded = json.loads(loaded_line[len("LOADED="):]) if loaded_line else {}
    check("新进程从磁盘恢复三项记忆配置",
          loaded == {"memory_window_turns": 137,
                     "memory_keep_turns": 41,
                     "memory_warn_turns": 109},
          str(loaded))
    result_line = next((x for x in lines if x.startswith("RESULT=")), "")
    response = json.loads(result_line[len("RESULT="):]) if result_line else {}
    payload = response.get("payload") or {}
    check("GET /config 回显三项记忆配置",
          payload.get("memory_window_turns") == 137
          and payload.get("memory_keep_turns") == 41
          and payload.get("memory_warn_turns") == 109,
          str(payload))


def _review_response(code: str) -> dict:
    from plaza import api, drafts as dr, review as rv
    import routes

    class H:
        headers: dict = {}
        path = "/plaza/api/review"

        def __init__(self):
            self.result = {}

        def _json(self, payload, status=200):
            self.result = {"status": status, "payload": payload}

    handler = H()
    with patch.object(api, "_plaza_side_post", return_value=False), \
         patch.object(routes, "_read_json", return_value={"sk": "A" * 16, "card": {}}), \
         patch.object(api, "_current_user", return_value={"id": 1}), \
         patch.object(api, "_is_admin_user", return_value=False), \
         patch.object(api, "_card_zip_from_payload", return_value=b"PK-card"), \
         patch.object(rv, "validate_sk_format", return_value=(True, "")), \
         patch.object(rv, "review_card_data", return_value={"verdict": "pass"}), \
         patch.object(dr, "owner_hash", return_value="owner"), \
         patch.object(dr, "clean_display", return_value=""), \
         patch.object(dr, "save_draft", side_effect=dr.DraftError(code, "forced")):
        api.plaza_review(handler)
    return handler.result


def _review_errors() -> None:
    from plaza import drafts as dr

    storage = _review_response(dr.DRAFT_PUBLISH_FAILED)
    storage_payload = storage.get("payload") or {}
    check("审核落盘失败返回 HTTP 500 且不伪装成功",
          storage.get("status") == 500 and storage_payload.get("ok") is False,
          str(storage))
    check("审核落盘失败标记 storage_error",
          storage_payload.get("error_kind") == "storage_error"
          and storage_payload.get("code") == dr.DRAFT_PUBLISH_FAILED,
          str(storage_payload))

    limit = _review_response(dr.DRAFT_LIMIT)
    limit_payload = limit.get("payload") or {}
    check("草稿数量上限不归入 storage_error",
          limit.get("status") == 400
          and limit_payload.get("error_kind") == "draft_limit"
          and limit_payload.get("code") == dr.DRAFT_LIMIT,
          str(limit))


def _snapshot_bytes(top: str, *, existing: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        preset = json.dumps({
            "id": "custom1", "name": "新卡", "char_name": "流萤",
            "user_name": "开拓者", "presentation": "sticker",
        }, ensure_ascii=False).encode("utf-8")
        zf.writestr(f"{top}/character/preset.json", preset)
        zf.writestr(f"{top}/data/conversation.jsonl",
                    (b'{"seq":2,"mid":"new","content":"new"}\n'))
    return buf.getvalue()


def _tree_snapshot(root: Path) -> dict:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _restore_backup_contract(tmp: Path) -> None:
    from infra.sync import restore

    class Registry:
        def __init__(self):
            self.items = set()

        def register(self, pid, **_kwargs):
            self.items.add(pid)

        def get(self, pid):
            return {"id": pid} if pid in self.items else None

    def run_case(name: str, user_root: Path, mode_root: Path, payload: bytes,
                 valid_mode: str = "") -> tuple:
        registry = Registry()

        def mode_path(mode):
            return mode_root / mode

        with patch.object(restore.cfg, "_user_ctx_dir", return_value=user_root), \
             patch.object(restore.cfg, "mode_root", side_effect=mode_path), \
             patch.object(restore.cfg, "valid_mode", side_effect=lambda m: m == valid_mode), \
             patch.object(restore.cfg, "all_packs", return_value=[]), \
             patch.object(restore.cfg, "pack_registry", return_value=registry), \
             patch.object(restore.cfg, "reload_presets", return_value=None):
            return restore._restore_full_snapshot(payload, backup=True)

    existing_root = tmp / "existing" / "user_data"
    old_pack = existing_root / "custom1"
    (old_pack / "character").mkdir(parents=True)
    (old_pack / "data").mkdir(parents=True)
    (old_pack / "character" / "preset.json").write_text(
        '{"v":"old"}', encoding="utf-8")
    (old_pack / "data" / "conversation.jsonl").write_text(
        '{"v":"old"}\n', encoding="utf-8")

    ok, msg, _ = run_case("existing", existing_root, existing_root / "story",
                          _snapshot_bytes("custom1"))
    backups = sorted((existing_root / "backups").glob("pre-restore-custom1-*.zip"))
    check("已有自建包恢复成功", ok, msg)
    check("已有自建包只生成一份 pre-restore 备份", len(backups) == 1,
          str([p.name for p in backups]))
    if backups:
        with zipfile.ZipFile(backups[0]) as zf:
            old = zf.read("character/preset.json").decode("utf-8")
        check("自建包备份保留恢复前角色定义", old == '{"v":"old"}', old)

    new_root = tmp / "new" / "user_data"
    ok, msg, _ = run_case("new", new_root, new_root / "story",
                          _snapshot_bytes("custom1"))
    new_backups = sorted((new_root / "backups").glob("pre-restore-custom1-*.zip"))
    check("首次恢复新自建包成功", ok, msg)
    check("首次恢复新自建包不生成备份", not new_backups,
          str([p.name for p in new_backups]))

    mode_root = tmp / "mode" / "user_data"
    old_mode = mode_root / "story"
    old_mode.mkdir(parents=True)
    (old_mode / "data").mkdir()
    (old_mode / "data" / "conversation.jsonl").write_text(
        '{"v":"old-mode"}\n', encoding="utf-8")
    ok, msg, _ = run_case("mode", mode_root, mode_root,
                          _snapshot_bytes("story"), valid_mode="story")
    mode_backups = sorted((mode_root / "backups").glob("pre-restore-story-*.zip"))
    check("已有模式包恢复成功并只备份一次", ok and len(mode_backups) == 1,
          f"{msg} backups={ [p.name for p in mode_backups] }")

    fail_root = tmp / "fail" / "user_data"
    fail_pack = fail_root / "custom1"
    (fail_pack / "character").mkdir(parents=True)
    (fail_pack / "character" / "preset.json").write_text('{"v":"keep"}', encoding="utf-8")
    before = _tree_snapshot(fail_pack)
    with patch.object(restore, "_backup_current_mode",
                      side_effect=OSError("forced backup failure")):
        ok, msg, _ = run_case("fail", fail_root, fail_root / "story",
                              _snapshot_bytes("custom1"))
    after = _tree_snapshot(fail_pack)
    check("恢复前备份失败时中止且不改目标包", not ok and before == after, msg)


def _proxy_contract(tmp: Path) -> None:
    server_entry = SERVER / "download_server.py"
    if not server_entry.is_file():
        print("  SKIP 代理响应测试：server/ 是私有源码，不随公开主仓分发")
        return
    old_argv = sys.argv[:]
    old_env = {k: os.environ.get(k) for k in ("FIREFLY_BW_KBPS",)}
    sys.argv = [str(server_entry), "0", str(tmp)]
    os.environ["FIREFLY_BW_KBPS"] = "0"
    try:
        spec = importlib.util.spec_from_file_location(
            "download_server_review_test", server_entry)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.argv = old_argv

    class Response:
        status = 200
        reason = "OK"

        def __init__(self):
            self.used = False

        def getheader(self, name):
            return "text/plain; charset=utf-8" if name.lower() == "content-type" else ""

        def getheaders(self):
            return []

        def read(self, _n):
            if self.used:
                return b""
            self.used = True
            return b"body"

    class Conn:
        fail_before_response = False

        def __init__(self, *_args, **_kwargs):
            self.closed = False
            self.response = Response()

        def request(self, *_args, **_kwargs):
            return None

        def getresponse(self):
            if self.fail_before_response:
                raise OSError("forced upstream failure")
            return self.response

        def close(self):
            self.closed = True

    class BrokenWriter:
        def write(self, _data):
            raise BrokenPipeError("forced body failure")

    class H:
        headers = {"Content-Length": "0"}
        command = "GET"
        path = "/proxy-test"
        rfile = io.BytesIO()
        client_address = ("127.0.0.1", 12345)

        def __init__(self):
            self.statuses = []
            self.close_connection = False
            self.wfile = BrokenWriter()

        def _rate_key(self):
            return "test"

        def send_response(self, code, reason=None):
            self.statuses.append(code)

        def send_header(self, *_args):
            return None

        def end_headers(self):
            return None

    created = []

    def factory(*args, **kwargs):
        conn = Conn(*args, **kwargs)
        created.append(conn)
        return conn

    requests = []
    with patch.object(module, "_rate_ok", return_value=True), \
         patch.object(module, "_record_request",
                      side_effect=lambda _h, code: requests.append(code)), \
         patch.object(module.http.client, "HTTPConnection", side_effect=factory), \
         contextlib.redirect_stderr(io.StringIO()):
        started = H()
        module.Handler._proxy(started)
        before = H()
        Conn.fail_before_response = True
        module.Handler._proxy(before)
        Conn.fail_before_response = False

    check("正文写失败后不再补发 502",
          started.statuses == [200] and started.close_connection is True,
          f"statuses={started.statuses} close={started.close_connection}")
    check("响应开始后的异常也会关闭上游连接",
          bool(created) and created[0].closed is True)
    check("响应头前失败只返回一次 502",
          before.statuses == [502] and requests.count(502) >= 1,
          f"statuses={before.statuses} records={requests}")
    check("响应头前失败同样关闭上游连接",
          len(created) > 1 and created[1].closed is True)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="firefly_review_fixes_") as td:
        tmp = Path(td)
        print("=== A. 配置字段写入、重启读取与 GET 回显 ===")
        _run_config_roundtrip(tmp / "config")
        print("=== B. 审核落盘恢复与容量错误分流 ===")
        _review_errors()
        print("=== C. 快照恢复的备份次数和失败关闭 ===")
        _restore_backup_contract(tmp / "restore")
        print("=== D. 代理响应阶段与连接关闭 ===")
        _proxy_contract(tmp / "server")


def main() -> int:
    global FAIL
    try:
        run()
    except Exception:
        FAIL += 1
        print("  X 未捕获异常")
        traceback.print_exc()
    print(f"\n=== reviewed bugfixes: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
