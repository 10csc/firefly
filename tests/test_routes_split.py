# -*- coding: utf-8 -*-
"""routes.py 拆分（阶段 2 · 任务 2.3）的**纯移动 oracle**

拆分把 `app/routes.py` 拆成 `app/api/{chat,debug,router}.py`（chat() 的三函数拆分见 2.3a），
原文件退化为兼容层。对外契约（端点键集）由 `test_routes_oracle.py` 守着，本文件守**命名空间契约**：
1. 拆分前 34 个顶层名字全部可达，且实现都在新模块；
2. 兼容层身份（`_ShimModule`，不留业务函数）；
3. **"顺带 import" 的名字也要暴露**——tests 直接经 `routes.*` 取用 88 个项目内名字
   （`routes.auth_proxy`、`routes._IMG_ID_RE`、`routes._fetch_json`、`routes.handle_chat` …），
   少一个就是 AttributeError。这是本卡最容易漏的地方，故逐条断言；
4. monkeypatch：`routes.parse_multipart = stub` 要能被运行时 `from routes import parse_multipart`
   读到（多份测试靠它模拟 multipart）；
5. chat() 已是编排壳（拆分前 215 行 → 现在只剩分支编排），三个子函数各司其职；
6. 导入顺序无关（新模块可独立导入）。
"""
import inspect
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import routes
from api import chat as achat
from api import debug as adebug
from api import router as arouter

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


# 拆分前 routes.py 的 34 个顶层定义/赋值
BEFORE_DEFS = [
    "GET_ROUTES", "POST_ROUTES", "_CHAT_WINDOWS", "_CHAT_WINDOW_IDLE", "_CHAT_WINDOW_LOCK",
    "_CHAT_WINDOW_MAX", "_CHAT_WINDOW_MAX_MSGS", "_CHAT_WINDOW_SEC", "_chat_window_cleanup", "_chat_window_key",
    "_ingest_user_messages", "_merge_window", "_resolve_today", "_run_pipeline", "chat",
    "chat_flush", "chat_hint", "clear_history", "get_chat_stage", "get_history",
    "get_archive", "get_journal", "get_memory_status", "get_metrics", "get_pipeline",
    "get_requests", "get_time",
    "get_user_memory", "get_wake_status", "logger", "memory_action", "open_mode",
    "proactive_status",
    "rest", "save_journal", "save_user_memory", "undo",
]

# 拆分前"顺带 import 进来"的项目内名字（88 个，tests 经 routes.* 取用）
BEFORE_IMPORTS = [
    "ContextManager", "DEFAULT_MODE", "_AUTH_PROXY_MAP", "_CONTENT_MAX", "_IMG_ID_RE",
    "_SESSIONS_LOCK", "_auth_server_base", "_body_mode", "_body_mode_ex", "_fetch_json",
    "_get_asset_url", "_is_server", "_load_image_data_url", "_match_asset", "_mk_auth_proxy",
    "_notify_reply_if_background", "_query_mode", "_query_mode_ex", "_read_json", "_session_key",
    "_write_replies", "add_favorite_route", "add_sticker_route", "assets_index", "assets_raw",
    "auth_proxy", "auth_state", "backup_create", "backup_delete", "backup_restore",
    "backups_list", "cfg", "character_file_update", "check_key", "check_update",
    "create_pack", "delete_character_file", "delete_favorite_route", "delete_pack", "delete_pack_asset",
    "export_data", "get_balance", "get_character_files", "get_config", "get_favorites",
    "get_image", "get_latest_release", "get_models", "get_modes", "get_pack_files",
    "get_session", "get_stickers", "handle_chat", "import_data",
    "parse_multipart", "parse_qs", "relay_pending",
    "relay_proxy", "relay_result", "sessions", "set_config", "set_key",
    "set_pack_config", "setting_fix_apply", "setting_fix_dismiss", "setting_fix_message", "setting_fix_reset",
    "setting_fix_rollback", "setting_fix_start", "setting_fix_status", "snapshot_create", "snapshot_delete",
    "snapshot_download", "snapshot_list", "snapshot_restore", "sticker_delete", "sticker_update",
    "sync_export", "sync_import", "sync_manifest", "sync_now", "update_download",
    "upload_image", "upload_pack_asset", "urlparse",
]

SOURCES = (achat, adebug, arouter)

print("=== A. 名字一个不少（含顺带 import 的对外命名空间） ===")
_missing = [n for n in BEFORE_DEFS if not hasattr(routes, n)]
check(f"A1 拆分前 {len(BEFORE_DEFS)} 个定义名全部可达", not _missing)
if _missing:
    print("    缺失:", _missing)
_missing_imp = [n for n in BEFORE_IMPORTS if not hasattr(routes, n)]
check(f"A2 拆分前 {len(BEFORE_IMPORTS)} 个顺带导入名全部可达（tests 依赖）", not _missing_imp)
if _missing_imp:
    print("    缺失:", _missing_imp)
_not_in_new = [n for n in BEFORE_DEFS
               if n != "logger" and not any(hasattr(m, n) for m in SOURCES)]
check("A3 定义名都由新模块持有（兼容层不留实现）", not _not_in_new)
if _not_in_new:
    print("    未在新模块找到:", _not_in_new)

print("=== B. 兼容层身份与分发表归属 ===")
check("B1 routes 是转发用 _ShimModule", type(routes).__name__ == "_ShimModule")
_own = [k for k, v in vars(routes).items()
        if not k.startswith("__") and callable(v) and getattr(v, "__module__", "") == "routes"]
check("B2 兼容层只定义转发类", _own == ["_ShimModule"])
check("B3 两张分发表定义在 api.router", hasattr(arouter, "POST_ROUTES") and hasattr(arouter, "GET_ROUTES"))
check("B4 兼容层暴露的是同一批对象（不是副本）",
      routes.POST_ROUTES is arouter.POST_ROUTES and routes.GET_ROUTES is arouter.GET_ROUTES)
def _oracle_counts() -> tuple:
    """从 `test_routes_oracle.py` **读出**端点数量 —— 端点数只有那一处真相源。

    2026-09-19：这里原先写死 `70 / 39`，加两个公告端点后本文件三处断言一起假失败。
    这正是项目里反复踩的"写死数字 → 每次合理变更都报假警"（同 config.py 的版本号）。
    改成 AST 解析 oracle 的集合字面量：以后新增端点只需改 oracle 一处。
    """
    import ast
    src = (ROOT / "tests" / "test_routes_oracle.py").read_text(encoding="utf-8")
    out = {}
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("EXPECTED_POST", "EXPECTED_GET")):
            out[node.targets[0].id] = len(ast.literal_eval(node.value))
    return out.get("EXPECTED_POST", 0), out.get("EXPECTED_GET", 0)


_N_POST, _N_GET = _oracle_counts()

check(f"B5 端点键集不变（{_N_POST} POST / {_N_GET} GET，数量取自 test_routes_oracle）",
      len(routes.POST_ROUTES) == _N_POST and len(routes.GET_ROUTES) == _N_GET,
      f"实际 {len(routes.POST_ROUTES)} / {len(routes.GET_ROUTES)}")

print("=== C. monkeypatch：打桩 routes.parse_multipart 必须被运行时读到 ===")
_old_pm = routes.parse_multipart
_stub_pm = lambda h, **kw: ({}, {})          # noqa: E731
try:
    routes.parse_multipart = _stub_pm
    check("C1 兼容层读到补丁", routes.parse_multipart is _stub_pm)
    _ns = {}
    exec("from routes import parse_multipart", _ns)
    check("C2 运行时 `from routes import parse_multipart` 读到补丁（handler 靠这条）",
          _ns["parse_multipart"] is _stub_pm)
finally:
    routes.parse_multipart = _old_pm
check("C3 还原后一致", routes.parse_multipart is _old_pm)

print("=== D. chat() 已是编排壳，子函数各司其职 ===")
_src = inspect.getsource(achat.chat)
check("D1 chat() 瘦身到 60 行以内（拆分前 215 行）", len(_src.splitlines()) <= 60)
check("D2 三个子函数都在 chat 模块",
      all(hasattr(achat, n) for n in ("_ingest_user_messages", "_merge_window", "_run_pipeline")))
check("D3 三个子函数的参数签名为编排所需",
      list(inspect.signature(achat._ingest_user_messages).parameters) == ["h", "body", "mode"]
      and list(inspect.signature(achat._merge_window).parameters) == ["session_id", "mode", "user_input", "vision_urls"]
      and list(inspect.signature(achat._run_pipeline).parameters)[:6] ==
      ["h", "session_id", "mode", "client", "user_input", "hint"])
check("D4 chat() 里不再直接调 handle_chat（流水线只在 _run_pipeline）",
      "handle_chat" not in _src and "handle_chat" in inspect.getsource(achat._run_pipeline))
check("D5 合并窗口常量与锁留在 chat 模块",
      all(hasattr(achat, n) for n in ("_CHAT_WINDOW_SEC", "_CHAT_WINDOW_MAX_MSGS",
                                      "_CHAT_WINDOW_LOCK", "_CHAT_WINDOWS")))

print("=== E. 独立导入 / 导入顺序无关 ===")
_CHILD = ("import sys; sys.path.insert(0, r'{app}');\n"
          "import {first};\n"
          "import api.chat; import api.debug; import api.router;\n"
          "import routes as r; print('OK', len(r.POST_ROUTES), len(r.GET_ROUTES))")
for first in ("api.chat", "api.debug", "api.router"):
    p = subprocess.run([sys.executable, "-c", _CHILD.format(app=str(ROOT / "app"), first=first)],
                       capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    check(f"E1 先导入 {first} 也能跑通", p.returncode == 0 and f"OK {_N_POST} {_N_GET}" in (p.stdout or ""))
    if p.returncode != 0:
        print("   ", (p.stdout or "") + (p.stderr or "")[-300:])

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
