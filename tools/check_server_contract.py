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
# ⚠ 2026-10-01 用户纠正架构：**服务器只做后台，不提供 App 前端**。
#   因此 App 页路径（/index.html、/index、/app、/app.html、App 的 /style.css、/pc.css）
#   **不再放进这份白名单** —— 放进来的话，将来有人把"打开服务器版 App 页"的引用写回前端，
#   本门禁会**静默放行**（历史上 `app/static/js/api.js` 的 openServerWeb 就是这样溜过去的）。
_NON_API_OK = {
    "/", "/login.html", "/admin.html", "/platform.html", "/platform.css",
    "/platform.bundle.js", "/platform.js", "/bg-sunset.css",
    "/favicon.ico", "/manifest.json", "/config.js",
}

# 服务器**不再提供**的 App 侧路径：前端（本地版也一样）不得带前导 `/` 引用它们
_APP_PAGE_FORBIDDEN = ("/index.html", "/app.html", "/index", "/app", "/style.css", "/pc.css", "/js/bundle.js")


def check_no_app_page_refs() -> list:
    """扫 app/static/** 找"引用服务器 App 页"的写法（引号/括号包裹 + 带前导 /）。

    为什么要这条：`fetch("/x")` / `src="/x"` 这类正则只覆盖 API 与资源，抓不到
    `window.open(base + "/index.html")` 这种拼接式引用；而那正是 2026-10-01 排查到的真实依赖。
    """
    bad = []
    for fp in sorted(STATIC.rglob("*")):
        if not fp.is_file() or fp.suffix not in (".js", ".html", ".css", ".json"):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in _APP_PAGE_FORBIDDEN:
            if re.search(r"""["'`(]""" + re.escape(pat) + r"""["'`)]""", text):
                bad.append((fp.relative_to(ROOT).as_posix(), pat))
    return bad


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
    "/shutdown": "**本地版专用**：只在 app/server.py 注册（shared_http 里有来源校验，仅本机可触发）；"
                 "服务器版不得暴露它 —— 所以契约门禁不该要求服务器注册这个端点。",
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

    apppage = check_no_app_page_refs()
    if apppage:
        print(f"\nX 前端引用了服务器**不再提供**的 App 页/App 资源（{len(apppage)} 处）：")
        for path, pat in apppage[:10]:
            print(f"    {path}  →  {pat}")
        print("\n处置：服务器只做后台（2026-10-01 用户纠正）—— 前端不得引用服务器上的 App 页/App 样式；"
              "本地版要用的同一文件请用**相对路径**（不带前导 /）。")
        return 1

    if not missing:
        print("\n结果: PASS 客户端调用面 ⊆ 服务器注册表，且前端未引用服务器 App 前端")
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
