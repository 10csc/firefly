#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上服务器端点探测：**线上到底缺哪些端点**（`docs/未完成事项清单.md` P0-8 的证据工具）。

## 背景

`tools/check_server_contract.py` 只能证明"**仓库里**的服务器端够用"。
而 P0-8 记的是另一件事：**线上跑的那份 app 层落后于前端**，于是网页版/安卓服务器模式
调 `/memory-status`、`/archive`、`/voice/*`、`/hotupdate/*` 全部 404。
那个发现方式太晚（靠人在浏览器里逐个点），这个工具把它变成一条命令。

## 怎么判"缺"（不靠登录、不碰用户数据）

服务器对未登录请求的**响应码本身就区分得很清楚**：

    /memory-status  →  404  ⇒ 这个端点**没注册**（线上就是缺它）
    /memory-status  →  401  ⇒ 注册了，只是要登录（**不缺**）
    /memory-status  →  403  ⇒ 注册了，但服务器版明确禁用（正常）

所以用**不带凭证**的请求探测即可：只有 404（或 405）才算"线上缺"。
**绝不带 token、绝不带 X-API-Key**（那会把用户凭证打到公网明文链路上）。

## 用法

    python tools/probe_live_server.py                     # 用默认线上地址
    python tools/probe_live_server.py --base http://host:port
    python tools/probe_live_server.py --only-new          # 只探"较新的"端点（排除老端点噪声）
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
DEFAULT_BASE = "http://101.200.14.200:8787"   # 占位；真实值见 paths.AUTH_SERVER_DEFAULT

# 已知"老客户端/老服务器"就有的端点：探测时默认跳过（只看新增面）
BASELINE_PATHS = {
    "/health", "/config", "/modes", "/history", "/chat", "/set-config", "/set-key",
    "/models", "/check-update", "/update-download", "/export-data", "/import-data",
    "/sticker", "/stickers", "/favorite", "/favorites", "/undo",
}

_UA = "FireflyContractProbe/1.0"


def _base_from_repo() -> str:
    try:
        from core import paths
        return str(paths.AUTH_SERVER_DEFAULT).rstrip("/")
    except Exception:
        return ""


def probe(base: str, path: str, timeout: float = 8.0) -> tuple:
    """返回 (状态码或 None, 说明)。**不带任何凭证。**"""
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Cache-Control": "no-store"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, "ok"
    except urllib.error.HTTPError as e:
        return e.code, e.reason or ""
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


def registered_paths() -> list:
    """服务器该有的端点（= 复用的路由表 + 硬编码路径）。"""
    out = set()
    for mod_name in ("api.router",):
        try:
            m = __import__(mod_name, fromlist=["x"])
            for attr in ("GET_ROUTES", "POST_ROUTES"):
                t = getattr(m, attr, None)
                if isinstance(t, dict):
                    out.update(t.keys())
        except Exception as e:
            print(f"!! {mod_name} 导入失败：{type(e).__name__}: {e}")
    sp = ROOT / "server" / "app.py"
    if sp.is_file():
        txt = sp.read_text(encoding="utf-8")
        for m in re.finditer(r"""path\s*==\s*["'](/[^"']*)["']""", txt):
            out.add(m.group(1))
        for m in re.finditer(r"""path\s+in\s+\(([^)]*)\)""", m2 := txt):
            for lit in re.findall(r"""["'](/[^"']*)["']""", m.group(1)):
                out.add(lit)
        for m in re.finditer(r"""path\s+in\s+(_[A-Z_]+)""", txt):
            block = re.search(m.group(1) + r"\s*=\s*\{(.*?)\}", txt, re.S)
            if block:
                for lit in re.findall(r"""["'](/[^"']*)["']""", block.group(1)):
                    out.add(lit)
    return sorted(p for p in out if not p.endswith("/") and not p.startswith(("/static", "/assets")))


def main() -> int:
    ap = argparse.ArgumentParser(description="线上服务器端点探测（不带凭证）")
    ap.add_argument("--base", default="", help="线上基址（默认取仓库里的 AUTH_SERVER_DEFAULT）")
    ap.add_argument("--only-new", action="store_true", help="只探不在 BASELINE_PATHS 里的端点")
    ap.add_argument("--list", action="store_true", help="只列出将要探测的端点，不发请求")
    a = ap.parse_args()

    base = (a.base or _base_from_repo()).rstrip("/")
    if not base:
        print("X 拿不到线上地址（用 --base 指定）")
        return 2
    paths = registered_paths()
    if a.only_new:
        paths = [p for p in paths if p not in BASELINE_PATHS]
    print(f"=== 探测 {base}（{len(paths)} 个端点，不带任何凭证）===")
    if a.list:
        for p in paths:
            print("  ", p)
        return 0

    missing, ok401, ok_other, unreach = [], [], [], []
    for p in paths:
        code, note = probe(base, p)
        if code is None:
            unreach.append((p, note))
        elif code in (404, 405):
            missing.append((p, code))
        elif code in (401, 403):
            ok401.append((p, code))
        else:
            ok_other.append((p, code))

    print(f"\n注册且可用（非 404）: {len(ok401) + len(ok_other)}")
    print(f"  要登录/禁用（401/403）: {len(ok401)}")
    print(f"  其它（200/302/…）: {len(ok_other)}")
    if unreach:
        print(f"\n!! 连不上（{len(unreach)}）—— 网络或地址不对，结论不可用：")
        for p, n in unreach[:5]:
            print(f"    {p}  {n}")
    if missing:
        print(f"\n★ 线上**没有注册**的端点（{len(missing)}）—— 这些就是 P0-8 的缺口：")
        for p, c in missing:
            print(f"    HTTP {c}  {p}")
    else:
        print("\n结果: PASS 线上注册表覆盖了仓库里的全部端点")
    print("\n注意：本工具只证明「端点存在」；返回值正确性与数据布局仍需按"
          "docs/服务器管理规范.md §4.5 的六项验证逐条走。")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
