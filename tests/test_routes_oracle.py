# -*- coding: utf-8 -*-
"""路由键集 oracle 测试（阶段 0 · 任务 0.1）。

作用：把当前全部 HTTP 端点集合钉死。阶段 2 的 routes.py 拆分会大量搬动函数，
本测试保证"拆分前后对外端点一个不多、一个不少"。

纪律：任何有意的端点增删，都必须同步改本文件的期望集合——这一步本身就是防护，
不是麻烦。改动若不体现在这里，说明改动是意外而非设计。

沙箱：导入 routes 前把 USER_DIR 指到临时目录，绝不触碰真实 user_data/。
"""
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_routes_oracle_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


import routes

# ── 期望集合（2026-09-13 从 HEAD=3a65e6a 实际导出后逐字贴入） ──
EXPECTED_POST = {
    "/add-sticker",
    "/auth/login",
    "/auth/logout",
    "/auth/mail-send",
    "/auth/register",
    "/auth/reset-password",
    "/auth/reset-send",
    "/auth/verify",
    "/backup/create",
    "/backup/delete",
    "/backup/restore",
    "/character-file-update",
    "/character-file/delete",
    "/chat",
    "/chat/flush",
    "/chat/hint",
    "/check-key",
    "/check-update",
    "/clear-history",
    "/favorite",
    "/favorites/delete",
    "/import-data",
    "/open-mode",
    "/pack-asset",
    "/pack-asset/delete",
    "/pack-config",
    "/pack-create",
    "/pack-delete",
    "/pack-forge/finish",
    "/pack-forge/next",
    "/pack-forge/start",
    "/proactive-status",
    "/relay/pending",
    "/relay/proxy",
    "/relay/result",
    "/rest",
    "/save-journal",
    "/save-user-memory",
    "/set-config",
    "/set-key",
    "/setting-fix/apply",
    "/setting-fix/dismiss",
    "/setting-fix/message",
    "/setting-fix/reset",
    "/setting-fix/rollback",
    "/setting-fix/start",
    "/snapshot/create",
    "/snapshot/delete",
    "/snapshot/restore",
    "/sticker-delete",
    "/sticker-update",
    "/sync/export",
    "/sync/import",
    "/sync/now",
    "/undo",
    "/update-download",
    "/upload-image",
}

EXPECTED_GET = {
    "/assets/index",
    "/assets/raw",
    "/auth/state",
    "/backups",
    "/balance",
    "/character-files",
    "/chat-stage",
    "/check-key",
    "/config",
    "/export-data",
    "/favorites",
    "/history",
    "/image",
    "/journal",
    "/metrics",
    "/models",
    "/modes",
    "/pack-files",
    "/pipeline",
    "/requests",
    "/setting-fix/status",
    "/snapshot/download",
    "/snapshot/list",
    "/stickers",
    "/sync/manifest",
    "/time",
    "/user-memory",
    "/wake-status",
}

print("=== A. 分发表存在且为 dict ===")
check("A1 POST_ROUTES 是 dict", isinstance(routes.POST_ROUTES, dict))
check("A2 GET_ROUTES 是 dict", isinstance(routes.GET_ROUTES, dict))

print("=== B. 集合逐字比对（多一个/少一个/拼错都失败） ===")
post_actual = set(routes.POST_ROUTES)
get_actual = set(routes.GET_ROUTES)
check(f"B1 POST 集合一致（{len(EXPECTED_POST)} 项）", post_actual == EXPECTED_POST)
check(f"B2 GET 集合一致（{len(EXPECTED_GET)} 项）", get_actual == EXPECTED_GET)

missing_post = sorted(EXPECTED_POST - post_actual)
extra_post = sorted(post_actual - EXPECTED_POST)
missing_get = sorted(EXPECTED_GET - get_actual)
extra_get = sorted(get_actual - EXPECTED_GET)
if missing_post:
    print(f"    缺失 POST: {missing_post}")
if extra_post:
    print(f"    多出 POST: {extra_post}")
if missing_get:
    print(f"    缺失 GET: {missing_get}")
if extra_get:
    print(f"    多出 GET: {extra_get}")

print("=== C. 端点值均为可调用函数（拆分后不许退化成字符串/列表） ===")
bad_post = [k for k, v in routes.POST_ROUTES.items() if not callable(v)]
bad_get = [k for k, v in routes.GET_ROUTES.items() if not callable(v)]
check("C1 POST 全部可调用", not bad_post)
check("C2 GET 全部可调用", not bad_get)
if bad_post:
    print(f"    非可调用 POST: {bad_post}")
if bad_get:
    print(f"    非可调用 GET: {bad_get}")

print("=== D. 路由键形态守卫 ===")
check("D1 所有键以 / 开头", all(k.startswith("/") for k in post_actual | get_actual))
check("D2 无重复键冲突（POST/GET 允许同名，如 /check-key）",
      len(post_actual) == len(routes.POST_ROUTES) and len(get_actual) == len(routes.GET_ROUTES))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
