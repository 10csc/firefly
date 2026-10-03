# -*- coding: utf-8 -*-
"""复现+守卫：广场筛选行的「搜索框塌成窄条 / 增强下拉吃满整行」（2026-10-01 用户截图报障）

**手工/按需运行**（同 manual_platform_* 惯例，不在 tests/test_*.py 套件内）。
用法：`python tests/manual_plaza_search_layout.py`（端口由系统分配，不占用户实例；零外网、零模型费用）。

两处都测（用户说的是"平台页/手机端广场"）：
  ① **平台页** `server/frontend/platform.html`（真浏览器 + 桩 API）
  ② **App 广场** `app/static/index.html` 的 `#plaza-view`（同一份 plaza 源码 + 同一份 style.css）
两个视口各来一遍：**桌面 1280×900** 与 **手机 390×844**。

判据（"看着对"不算数，量出来才算）：
  · `#pz-search` 宽度 ≥ 120px（手机 ≥ 120px 也要成立：390 减去固定宽下拉后仍绰绰有余）；
  · 搜索框与**可见的**增强下拉（`.ui-select-btn`）**不重叠**（rect 不交叠）；
  · 原生 `#pz-sort` 必须被增强层隐藏（opacity 0 / 0 尺寸）。

截图存 `_harden/shots/`：`30_search_platform_1280.png` 等 4 张。
退出码：0 全过；1 有断言失败。
"""
import functools
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "_harden" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
# `FIREFLY_SIMULATE_BEFORE=1`：只用来**留一张"修之前"的对照截图**（把修复规则从响应里剔掉，
# 不改磁盘）。此时布局断言**预期会红**，只看截图不看判定。
SIMULATE_BEFORE = os.environ.get("FIREFLY_SIMULATE_BEFORE") == "1"
PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}" + (f"   {extra}" if extra else ""), flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"   {extra}" if extra else ""), flush=True)


class _Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _serve(directory: Path) -> tuple:
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(_Q, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


srv_platform, port_pf = _serve(ROOT / "server" / "frontend")
srv_app, port_app = _serve(ROOT / "app" / "static")
base_pf = f"http://127.0.0.1:{port_pf}"
base_app = f"http://127.0.0.1:{port_app}"
time.sleep(0.3)
print(f"静态服：平台 {base_pf} / App {base_app}", flush=True)

LIST_JSON = ('{"ok":true,"total":1,"page":1,"size":20,"categories":["陪伴"],'
             '"items":[{"id":"c1","name":"布局验证卡","char_name":"小验","desc":"d",'
             '"category":"陪伴","tags":[],"downloads":1,"size_bytes":2048,'
             '"published_at":1790849000,"author":{"display":"作者","official":false},'
             '"thumb":"thumb.webp"}]}')

MEASURE = """() => {
    const q = (s) => document.querySelector(s);
    const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect();
        return {x: Math.round(b.x), w: Math.round(b.width), h: Math.round(b.height)}; };
    const inp = q('#pz-search');
    const sel = q('#pz-sort');
    const btn = q('.pz-search-row .ui-select-btn');
    const row = q('.pz-search-row');
    const cs = sel ? getComputedStyle(sel) : null;
    const overlap = (a, b) => (a && b) ? !(a.x + a.w <= b.x || b.x + b.w <= a.x) : false;
    return {row: r(row), input: r(inp), native: r(sel), enhanced: r(btn),
            nativeOpacity: cs ? cs.opacity : null,
            inputDisplay: inp ? getComputedStyle(inp).display : null,
            overlapInputEnhanced: overlap(r(inp), r(btn))};
}"""


def run_case(pw, kind: str, base: str, url: str, vw: int, vh: int, shot: str):
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": vw, "height": vh})
    pg.set_default_timeout(15000)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    def _stub(route):
        """一条 catch-all：按 URL 分发。比"一堆 lambda 字典"清楚，也不会有闭包坑。"""
        u = route.request.url
        def _json(body):
            route.fulfill(status=200, content_type="application/json", body=body)
        if u.endswith("/config.js"):
            route.fulfill(status=200, content_type="application/javascript",
                          body=f'window.FIREFLY_MODE="local";window.FIREFLY_SERVER_BASE="{base}";')
        elif SIMULATE_BEFORE and ("/style.css" in u or "/platform.css" in u):
            # 「修之前」对照：把修好的那条 scoped 规则**从响应里剔掉**（不动磁盘文件），
            # 用来复现用户截图里"搜索框塌成窄条"的样子，留一张 before 截图。
            # ⚠ 必须按"URL 里含 /style.css"判断：App 页引用带 `?v=<指纹>` 查询串，
            #   用 endswith("/style.css") 会**静默不命中**（第一次就踩了，量出来还是修后的 494px）。
            src = (ROOT / "app" / "static" / "style.css") if "/style.css" in u \
                else (ROOT / "server" / "frontend" / "platform.css")
            css = re.sub(r"\.pz-search-row \.ui-select-btn \{[^}]*\}", "",
                         src.read_text(encoding="utf-8"))
            route.fulfill(status=200, content_type="text/css", body=css)
        elif "/auth/me" in u:
            _json('{"email":"layout@qq.com","role":"user","created_at":1}')
        elif "/auth/state" in u:
            _json('{"logged_in":true,"offline_ok":true}')
        elif "/plaza/api/list" in u:
            _json(LIST_JSON)
        elif "/plaza/api/installed" in u:
            _json('{"ok":true,"items":[],"used_cards":0,"used_bytes":0}')
        elif "/plaza/api/drafts" in u:
            _json('{"ok":true,"items":[]}')
        elif "/plaza/api/asset" in u:
            route.fulfill(status=200, content_type="image/png", body=b"")
        else:
            route.continue_()

    pg.route("**/*", _stub)
    pg.add_init_script("try{localStorage.setItem('firefly_guide_v1_done','1');"
                       "localStorage.setItem('firefly_deep_guide_v1_done','1');}catch(e){}")
    pg.goto(url, wait_until="domcontentloaded")
    pg.wait_for_timeout(900)
    if kind == "app":
        pg.evaluate("window.openPlaza && window.openPlaza()")
        pg.wait_for_timeout(900)
    pg.wait_for_selector("#pz-search", state="attached", timeout=10000)
    m = pg.evaluate(MEASURE)
    tag = f"{kind}@{vw}"
    print(f"\n[{tag}] row={m['row']} 搜索框={m['input']} 原生select={m['native']} "
          f"增强下拉={m['enhanced']} 原生opacity={m['nativeOpacity']}", flush=True)
    pg.screenshot(path=str(SHOTS / shot))
    check(f"[{tag}] 搜索框宽度 ≥ 120px（实际 {m['input']['w'] if m['input'] else 'N/A'}）",
          bool(m["input"]) and m["input"]["w"] >= 120, str(m["input"]))
    check(f"[{tag}] 搜索框与增强下拉不重叠", not m["overlapInputEnhanced"],
          f"input={m['input']} enhanced={m['enhanced']}")
    check(f"[{tag}] 原生 select 已被增强层隐藏（opacity=0 或 0 尺寸）",
          bool(m["native"]) and (m["nativeOpacity"] == "0" or m["native"]["w"] == 0),
          f"opacity={m['nativeOpacity']} rect={m['native']}")
    check(f"[{tag}] 零 pageerror", not errs, str(errs[:2]))
    b.close()


def run_quota(pw, kind: str, base: str, url: str, installed: dict, expects: tuple):
    """配额文案：`max_cards/max_bytes = null` ⇒ 必须显示「（不限）」，不能显示 0/0（用户 2026-10-01 拍板）。"""
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.set_default_timeout(15000)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    body = json.dumps(installed, ensure_ascii=False)

    def _stub(route):
        u = route.request.url
        if u.endswith("/config.js"):
            route.fulfill(status=200, content_type="application/javascript",
                          body=f'window.FIREFLY_MODE="local";window.FIREFLY_SERVER_BASE="{base}";')
        elif "/auth/me" in u:
            route.fulfill(status=200, content_type="application/json",
                          body='{"email":"layout@qq.com","role":"admin"}')
        elif "/auth/state" in u:
            route.fulfill(status=200, content_type="application/json",
                          body='{"logged_in":true,"offline_ok":true}')
        elif "/plaza/api/list" in u:
            route.fulfill(status=200, content_type="application/json", body=LIST_JSON)
        elif "/plaza/api/installed" in u:
            route.fulfill(status=200, content_type="application/json", body=body)
        elif "/plaza/api/drafts" in u:
            route.fulfill(status=200, content_type="application/json", body='{"ok":true,"items":[]}')
        elif "/plaza/api/asset" in u:
            route.fulfill(status=200, content_type="image/png", body=b"")
        else:
            route.continue_()

    pg.route("**/*", _stub)
    pg.add_init_script("try{localStorage.setItem('firefly_guide_v1_done','1');"
                       "localStorage.setItem('firefly_deep_guide_v1_done','1');}catch(e){}")
    pg.goto(url, wait_until="domcontentloaded")
    pg.wait_for_timeout(700)
    if kind == "app":
        pg.evaluate("window.openPlaza && window.openPlaza()")
        pg.wait_for_timeout(900)
    txt = ""
    for _ in range(25):
        txt = pg.eval_on_selector("#pz-quota", "el => el.textContent") or ""
        if txt.strip():
            break
        pg.wait_for_timeout(200)
    tag = f"quota/{kind}"
    print(f"\n[{tag}] 配额文案 = {txt.strip()!r}", flush=True)
    for needle, want in expects:
        check(f"[{tag}] {'含' if want else '不含'} {needle!r}", (needle in txt) is want, txt.strip())
    check(f"[{tag}] 零 pageerror", not errs, str(errs[:2]))
    b.close()


try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for vw, vh, suffix in ((1280, 900, "1280"), (390, 844, "390")):
            tag = "" if not SIMULATE_BEFORE else "_BEFORE"
            run_case(pw, "platform", base_pf, base_pf + "/platform.html", vw, vh,
                     f"30_search_platform_{suffix}{tag}.png")
            run_case(pw, "app", base_app, base_app + "/index.html", vw, vh,
                     f"30_search_app_{suffix}{tag}.png")
        if not SIMULATE_BEFORE:
            # 管理员：服务端将返回 max_* = null ⇒ 文案必须是「（不限）」，绝不是「0 / 0」
            UNLIMITED = {"ok": True, "items": [], "used_cards": 2, "used_bytes": 1048576,
                         "max_cards": None, "max_bytes": None, "server_mode": True}
            NUMERIC = {"ok": True, "items": [], "used_cards": 0, "used_bytes": 0,
                       "max_cards": 5, "max_bytes": 8 * 1024 * 1024, "server_mode": True}
            for kind, base, url in (("platform", base_pf, base_pf + "/platform.html"),
                                    ("app", base_app, base_app + "/index.html")):
                run_quota(pw, kind, base, url, UNLIMITED,
                          (("（不限）", True), ("0 / 0", False), ("卡片 2 张", True)))
                run_quota(pw, kind, base, url, NUMERIC,
                          (("卡片 0 / 5 张", True), ("容量 0 B / 8.0 MB", True), ("（不限）", False)))
finally:
    srv_platform.shutdown(); srv_platform.server_close()
    srv_app.shutdown(); srv_app.server_close()

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
print("判定:", "PASS" if FAIL == 0 else "FAIL", flush=True)
sys.exit(1 if FAIL else 0)
