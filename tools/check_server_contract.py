#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约门禁：**客户端会调的端点 ⊆ 服务器注册表**（`docs/维护与开发双轨.md` §五 标为"待做"）。

## 为什么必须有它（这不是理论问题，是踩过的）

服务器跑的是**线上那份 app 层**（`server/app.py` + 一份 app 目录），而客户端走的是
**APK 里那份前端**。两者**部署时点不同**：0.9.0 发版时线上 app 层落后于前端，
于是新前端的 `server/frontend/` 调 `/memory-status`、`/archive`、`/voice/*`、`/hotupdate/*`
全部 404 —— 这就是 `docs/未完成事项清单.md` P0-8 记的那件事，
当时的处置是"**因此只部署了公告接收端，没有部署新前端**（部署了反而让服务器网页 UI 降级）"。

那种发现方式太晚（要靠人在浏览器里逐个点）。这个门禁把它提前到**发版前**：

    客户端 fetch 的每个 `/path`  →  必须在服务器能处理的集合里

## 覆盖范围与口径

**登记"客户端请求"**：`app/static/js/**` 与 `app/static/*.html` 里所有
`fetch("...")` / `fetch('...')` / `src=` / `href=` 的**站内绝对路径**。
**登记"服务器能处理"**：
  1. `server/app.py` 里显式判断的路径（`path == "/x"`、`path in {...}`、`path.startswith("/x/")`）
  2. 复用的路由表 `app/api/router.py` 的 `GET_ROUTES` / `POST_ROUTES`（服务器版就是用它）
  3. 静态资源前缀（`/static/`、`/assets/`）与文件名（`/index.html`、`/login.html` …）

**只报"客户端会调、服务器明确没有"的**（多余项不报 —— 服务器有而前端不调是正常的）。

用法：
    python tools/check_server_contract.py            # 校验（有缺口退 1）
    python tools/check_server_contract.py -v         # 打印全部命中
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
STATIC = APP / "static"
SERVER_APP = ROOT / "server" / "app.py"

# 只关心这些（前端真正会打到后端的），静态资源单独归类
_API_LIKE = re.compile(r"^/(?!static/|assets/|vendor/)[A-Za-z0-9._\-/]*")
# fetch("/x") / fetch('/x') / fetch(`/x`)
_FETCH_RE = re.compile(r"""fetch\(\s*[`'"](/[^`'"$?]*)""")
# 模板字符串里的 `API_BASE + "/x"`
_CONCAT_RE = re.compile(r"""API_BASE\s*\+\s*["'`](/[^"'`?]*)""")
# 直接用 <img src="/notice-image?..."> 之类
_SRC_RE = re.compile(r"""(?:src|href)=["'](/[^"'?#]+)""")

# 前端里合法的"非 API"站内路径（页面/资源/锚点）——显式列出，避免误报
_NON_API_OK = {
    "/", "/index.html", "/login.html", "/admin.html", "/app", "/app.html", "/index",
    "/favicon.ico", "/manifest.json", "/style.css", "/pc.css", "/config.js",
}


def collect_client_paths() -> dict:
    """{path: [出处…]}"""
    out: dict[str, list] = {}

    def add(p: str, where: str):
        p = p.split("?")[0].split("#")[0]
        if not p or p in _NON_API_OK:
            return
        if p.startswith(("/static/", "/assets/", "/vendor/")):
            return
        out.setdefault(p, []).append(where)

    files = sorted(STATIC.rglob("*.js")) + sorted(STATIC.glob("*.html"))
    for fp in files:
        rel = fp.relative_to(ROOT).as_posix()
        if fp.name == "bundle.js":          # 产物，源里已经查过（避免重复计数）
            continue
        try:
            txt = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in _FETCH_RE.finditer(txt):
            add(m.group(1), rel)
        for m in _CONCAT_RE.finditer(txt):
            add(m.group(1), rel)
        for m in _SRC_RE.finditer(txt):
            add(m.group(1), rel)
    return out


def collect_server_paths() -> tuple[set, list]:
    """(服务器能处理的路径集合, 可读的来源说明)。"""
    ok: set[str] = set()
    notes: list[str] = []

    # ① server/app.py 里的显式路径
    if SERVER_APP.is_file():
        txt = SERVER_APP.read_text(encoding="utf-8")
        notes.append(f"{SERVER_APP.relative_to(ROOT).as_posix()}（{len(txt)} 字节）")
        for m in re.finditer(r"""path\s*==\s*["'](/[^"']*)["']""", txt):
            ok.add(m.group(1))
        for m in re.finditer(r"""path\s+in\s+\(([^)]*)\)""", txt):
            for lit in re.findall(r"""["'](/[^"']*)["']""", m.group(1)):
                ok.add(lit)
        for m in re.finditer(r"""path\s+in\s+(_[A-Z_]+)""", txt):
            name = m.group(1)
            block = re.search(name + r"\s*=\s*\{(.*?)\}", txt, re.S)
            if block:
                for lit in re.findall(r"""["'](/[^"']*)["']""", block.group(1)):
                    ok.add(lit)
        for m in re.finditer(r"""path\.startswith\(\s*["'](/[^"']*)["']""", txt):
            ok.add(m.group(1))
    else:
        notes.append("!! server/app.py 不存在（本机没有 server/ 目录）")

    # ② 复用的路由表（服务器版就是用它）
    sys.path.insert(0, str(APP))
    import importlib
    for mod in ("api.router",):
        try:
            m = importlib.import_module(mod)
            for attr in ("GET_ROUTES", "POST_ROUTES"):
                table = getattr(m, attr, None)
                if isinstance(table, dict):
                    ok.update(table.keys())
                    notes.append(f"{mod}.{attr}（{len(table)} 条）")
        except Exception as e:
            notes.append(f"!! {mod} 导入失败：{type(e).__name__}: {e}")

    # ③ 服务器还硬编码服务这些（静态/页面/下载页/管理台）
    ok.update({"/health", "/time", "/update-manifest", "/version.json", "/notice-image",
               "/auth/me", "/login", "/login.html", "/admin.html", "/download/", "/download",
               "/static/", "/assets/"})
    return ok, notes


# ── 显式豁免（写明理由，禁止无理由放行）────────────────
# 键 = 客户端路径，值 = 为什么"服务器没有"是允许的
ALLOW = {
    "/": "根路径由服务器自己处理（返回前端首页）",
}


def main() -> int:
    verbose = "-v" in sys.argv
    client = collect_client_paths()
    server, notes = collect_server_paths()

    print("=== 客户端 → 服务器的端点契约门禁 ===")
    for n in notes:
        print(f"  · {n}")
    print(f"  · 客户端引用 {len(client)} 个站内路径；服务器可处理 {len(server)} 条")

    missing = {}
    for p, wheres in sorted(client.items()):
        if p in ALLOW:
            continue
        # 前缀匹配：服务器 `path.startswith("/download/")` 这类要能覆盖 /download/x
        if p in server or any(p.startswith(s) for s in server if s.endswith("/")):
            continue
        missing[p] = wheres

    if verbose:
        for p in sorted(client):
            mark = "缺" if p in missing else "ok"
            print(f"  [{mark}] {p}   ← {', '.join(sorted(set(client[p]))[:3])}")

    if not missing:
        print("\n结果: PASS 客户端调用面 ⊆ 服务器注册表")
        return 0

    print(f"\nX 客户端会调、但服务器**没有注册**的 {len(missing)} 个端点：")
    for p, wheres in sorted(missing.items()):
        print(f"    {p}")
        for w in sorted(set(wheres))[:3]:
            print(f"        ← {w}")
    print("\n处置：要么在服务器侧补注册（并部署），要么确认这个调用只发生在"
          "「本地版 / PC」路径上 —— 后者请在 ALLOW 里写明理由。")
    print("（历史上 P0-8 就是这条：线上 app 层落后于前端 ⇒ 网页版 UI 降级）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
