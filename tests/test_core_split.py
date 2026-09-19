# -*- coding: utf-8 -*-
"""app/core 拆分（阶段 2 · 任务 2.1）的**纯移动 oracle**

拆分把 `app/modules/app_config.py`（1043 行）拆成 `app/core/{paths,userctx,config,presets,migrations}.py`，
原文件退化为兼容层。本文件守四件事（都是"纯移动"必须成立的不变量）：

1. **名字一个不少**：拆分前 84 个顶层名字，全部仍能经 `modules.app_config` 拿到；
2. **兼容层真的是转发层**：`modules.app_config` 是 `_ShimModule`，实现都在 core.*；
3. **可变全局量转发**：`cfg.USER_DIR = X` 必须改到 `core.paths.USER_DIR`（否则约 20 个测试的
   沙箱注入失效 → 会写进真实 user_data）、`cfg.MODES` 活读、`cfg.get_client = stub` 要同时
   落到 `core.config`（拆分前它们同处一个命名空间，补丁会连带影响模块内部调用）；
4. **导入顺序与环境无关**：先 import 任一 core 子模块都不允成环；安卓模式
   （FIREFLY_ANDROID=1，`APP_DIR` 在该模式下按原逻辑就不存在）下也必须能正常导入。

沙箱纪律：USER_DIR/CONFIG_FILE 的写入在用例结束时还原。
"""
import subprocess
import re
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg
from core import config as cconfig
from core import migrations as cmigrations
from core import paths as cpaths
from core import presets as cpresets
from core import userctx as cuserctx

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


# ── 拆分前 app_config.py 的 84 个顶层名字（AST 抽取后冻结，勿手改）──
BEFORE_NAMES = [
    "API_BASE", "APP_DIR", "APP_VERSION", "AUTH_SERVER_DEFAULT", "BASE_DIR", "CONFIG_FILE",
    "DEFAULT_MODE", "GO_BASE", "MODEL", "MODES", "PORT", "PRESETS",
    "PRESET_SCHEMA", "ROOT", "STATIC_DIR", "SUGGESTED_MODELS", "SUGGESTED_PROVIDERS", "USER_DIR",
    "VALID_EFFORTS", "_BASE_MAX_LEN", "_CONFIG_BAK_KEEP", "_DEFAULTS", "_LEGACY_PRE_MOVE_ZIP", "_MODEL_MAX_LEN",
    "_PACK_CFG_NAME", "_PRESET_ID_RE", "_active_provider", "_backup_legacy_sources", "_clean_knowledge_dirs", "_clean_model",
    "_clean_provider", "_cleanup_stale_defaults", "_deepseek_preset", "_defaults_map", "_discover_presets", "_legacy_targets",
    "_load_config", "_migrate_legacy_providers", "_on_caps_probe", "_parse_preset", "_proxy_quota_checker", "_proxy_quota_counter",
    "_proxy_quota_failer", "_sync_derived", "_user_ctx", "_user_ctx_dir", "_user_overlay", "_valid_http_base",
    "active_provider", "bundled_character_dir", "char_name", "config", "eff_cfg", "get_api_key",
    "get_client", "get_user_overlay", "logger", "mode_character_dir", "mode_data_dir", "mode_journal_dir",
    "mode_root", "normalize_providers", "pack_cfg", "provider_by_id", "relay_needs_key", "reload_presets",
    "remove_provider", "reset_user_context", "resolve_asset", "run_legacy_migration", "run_startup_init", "save_config",
    "set_active_provider", "set_pack_cfg", "set_proxy_quota_checker", "set_proxy_quota_counter", "set_proxy_quota_failer", "set_user_context",
    "set_user_overlay", "upsert_provider", "user_dir_id", "user_has_key", "user_name", "user_scope_key",
]

CORE_MODULES = (cpaths, cuserctx, cconfig, cpresets, cmigrations)

print("=== A. 名字一个不少（84 个顶层名字全部可达） ===")
_missing = [n for n in BEFORE_NAMES if not hasattr(cfg, n)]
check(f"A1 拆分前 {len(BEFORE_NAMES)} 个名字全部可达", not _missing)
if _missing:
    print("    缺失:", _missing)
# 每个名字都能在某个 core 子模块里找到（说明实现真的搬走了，而不是留在兼容层）
_not_in_core = [n for n in BEFORE_NAMES
                if n != "logger" and not any(hasattr(m, n) for m in CORE_MODULES)]
check("A2 每个名字都由 core.* 持有（兼容层不留实现）", not _not_in_core)
if _not_in_core:
    print("    未在 core 找到:", _not_in_core)

print("=== B. 兼容层身份 ===")
check("B1 modules.app_config 是转发用 _ShimModule", type(cfg).__name__ == "_ShimModule")
_own_defs = [k for k, v in vars(cfg).items()
             if not k.startswith("__") and callable(v)
             and getattr(v, "__module__", "") == "modules.app_config"]
check("B2 兼容层只定义转发类，不留任何业务函数", _own_defs == ["_ShimModule"])
if _own_defs != ["_ShimModule"]:
    print("    兼容层自定义定义:", _own_defs)
check("B3 五个 core 子模块都可导入且有 docstring",
      all(m.__doc__ for m in CORE_MODULES))

print("=== C. 可变全局量：写要落到真正的持有模块 ===")
_old_ud, _old_cf = cfg.USER_DIR, cfg.CONFIG_FILE
_old_client = cconfig.get_client
_tmpdir = Path(tempfile.mkdtemp(prefix="firefly_test_core_split_"))
try:
    cfg.USER_DIR = _tmpdir
    check("C1 cfg.USER_DIR 写入落到 core.paths.USER_DIR", cpaths.USER_DIR == _tmpdir)
    check("C2 cfg.USER_DIR 读取与 core.paths 一致（活读，不是快照）", cfg.USER_DIR == cpaths.USER_DIR)
    cfg.CONFIG_FILE = _tmpdir / "config.json"
    check("C3 CONFIG_FILE 同样转发", cpaths.CONFIG_FILE == _tmpdir / "config.json")
    check("C4 mode_root 用的是改写后的 USER_DIR（沙箱不落真实 user_data）",
          str(cfg.mode_root()).startswith(str(_tmpdir)))
    # monkeypatch 语义：拆分前 cfg.get_client 与该函数同处一个命名空间，
    # 补丁会连带影响模块内部调用（多个测试依赖）→ 必须同步写到 core.config
    cfg.get_client = lambda: "STUB"
    check("C5 cfg.get_client 补丁落到 core.config（模块内部调用也吃到）",
          cconfig.get_client() == "STUB" and cfg.get_client() == "STUB")
finally:
    cfg.get_client = _old_client
    cfg.USER_DIR = _old_ud
    cfg.CONFIG_FILE = _old_cf
check("C6 还原后与 core.paths 一致", cfg.USER_DIR == cpaths.USER_DIR == _old_ud)

print("=== D. MODES 必须活读（reload_presets 会重绑） ===")
check("D1 cfg.MODES 与 core.presets.MODES 同一值", cfg.MODES == cpresets.MODES)
check("D2 cfg.MODES 不是兼容层快照（本模块字典里没有）", "MODES" not in vars(cfg))

print("=== E. 导入顺序无关（不成环） ===")
_CHILD = ("import sys; sys.path.insert(0, r'{app}');\n"
          "import core.{first}; import core.paths; import core.presets; import core.config;\n"
          "import core.migrations; import core.userctx;\n"
          "import modules.app_config as cfg; print('OK', cfg.APP_VERSION)")
for first in ("presets", "paths", "config", "migrations", "userctx"):
    p = subprocess.run([sys.executable, "-c", _CHILD.format(app=str(ROOT / "app"), first=first)],
                       capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    check(f"E1 先导入 core.{first} 也能跑通", p.returncode == 0 and "OK" in (p.stdout or ""))
    if p.returncode != 0:
        print("   ", (p.stdout or "") + (p.stderr or "")[-400:])

# 版本号现读，别写死（2026-09-19：0.8.1 → 0.9.0 时这里假失败过一次）
_VER = re.search(r'APP_VERSION\s*=\s*"([^"]+)"',
                 (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")).group(1)

print("=== F. 环境差异：安卓模式（FIREFLY_ANDROID=1）必须照常导入 ===")
import os
_env = dict(os.environ)
_env["FIREFLY_ANDROID"] = "1"
_env["FIREFLY_DATA_DIR"] = str(_tmpdir / "android")
_F = ("import sys; sys.path.insert(0, r'{app}');\n"
      "import modules.app_config as cfg\n"
      "print('VER', cfg.APP_VERSION); print('USER', cfg.USER_DIR.name)\n"
      "try:\n"
      "    cfg.APP_DIR; print('APPDIR present')\n"
      "except AttributeError:\n"
      "    print('APPDIR absent')").format(app=str(ROOT / "app"))
p = subprocess.run([sys.executable, "-c", _F], capture_output=True, encoding="utf-8",
                   errors="replace", env=_env, cwd=str(ROOT))
_out = (p.stdout or "")
check("F1 安卓模式导入成功", p.returncode == 0 and f"VER {_VER}" in _out)
check("F2 安卓模式下 APP_DIR 仍不存在（与拆分前一致，不是被兼容层造假）",
      "APPDIR absent" in _out)
check("F3 安卓模式数据根来自 FIREFLY_DATA_DIR", "USER user_data" in _out)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
