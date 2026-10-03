# -*- coding: utf-8 -*-
"""PC 三栏外壳（第 1 步：骨架）的真实浏览器验收。

**手工/按需运行**（同 manual_* 惯例）。用法：`python tests/manual_pc_shell_step1.py`
它用**静态** http.server（两个端口，避开 8765 被队友占用）分别服务：
  · 基线 = `.tmp_test/pc_ia/baseline/`（改造前的 app/static 快照）
  · 现状 = `app/static`（加了三栏骨架之后）
然后：
 1) **手机端逐像素回归**：390×844 与 768×1024 两档，`#home-view` 首页 / `#cards-view` 卡库 /
    `#app` 聊天页 三个视图各截一张，**基线 vs 现状逐字节相等**（PNG 完全一致）；
    再断言 `#pc-shell` 在两档下 `display:none` 且不产生盒子（宽高 0）；
 2) **PC 端骨架证据**：1600×1000 与 1280×800 下 `#pc-shell` 可见、三栏几何正确
    （侧边栏 72 / 联系人列 288 / 第三栏起点 = 360）、视图层已右移、无横向滚动；
    截三张图存 `_harden/shots/`；
 3) `pageerror` 计数：手机两档必须为 0。

退出码：0 全过；1 有断言失败；3 端口被占。
"""
import functools
import http.server
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
CUR = ROOT / "app" / "static"
BASE = ROOT / ".tmp_test" / "pc_ia" / "baseline"
SHOTS = ROOT / "_harden" / "shots"
OUT = Path(tempfile.mkdtemp(prefix="ff_pc_ia_"))
P0, P1 = 8897, 8896          # 基线 / 现状（避开 8765）

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}" + (f"   {extra}" if extra else ""), flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"   {extra}" if extra else ""), flush=True)


if not BASE.is_dir():
    print(f"缺少基线快照 {BASE}（应先复制改造前的 app/static）", flush=True)
    sys.exit(2)
SHOTS.mkdir(parents=True, exist_ok=True)


class _H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory, port):
    httpd = socketserver.TCPServer(("127.0.0.1", port), functools.partial(_H, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


for _p in (P0, P1):                       # 端口被占（别的脚本）⇒ 退出 3，绝不杀进程
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", _p))
        print(f"!! 端口 {_p} 被占用——放弃（避免误杀真实进程）", flush=True)
        sys.exit(3)
    except OSError:
        pass
    finally:
        s.close()

a = serve(BASE, P0)
b = serve(CUR, P1)
time.sleep(0.4)
print(f"基线 http://127.0.0.1:{P0}   现状 http://127.0.0.1:{P1}", flush=True)

# 三个视图 + 让它们显示出来的方式（都是既有 window 入口，不写新逻辑）
VIEWS = [("home", "#home-view", "showHome()"),
         ("cards", "#cards-view", "openCardsView()"),
         ("chat", "#app", "showChat()")]
MOBILE = [(390, 844), (768, 1024)]

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        errs = []

        def shot(port, w, h, view, name):
            pg = browser.new_page(viewport={"width": w, "height": h})
            pg.set_default_timeout(15000)
            pg.on("pageerror", lambda e: errs.append(f"{w}x{h}/{view}: " + str(e)))
            # ★ 确定性（否则"逐字节相同"永远不成立）：首页背景 `#firefly-field` 的星点/萤火虫用
            #   `Math.random()` 画，两次加载必然不同像素 ⇒ 固定随机数 + 关动画 + 隐藏该装饰层。
            #   被测的是**布局**（我的改动只可能影响布局），装饰层与本次改动无关。
            pg.add_init_script("Math.random = function () { return 0.42; };")
            pg.route("**/auth/state", lambda r: r.fulfill(
                status=200, content_type="application/json", body='{"logged_in": true}'))
            pg.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
            pg.add_style_tag(content="*,*::before,*::after{animation:none!important;"
                                     "transition:none!important}"
                                     "#firefly-field{display:none!important}")
            pg.wait_for_timeout(500)
            try:
                pg.evaluate(view[2])
            except Exception:
                pass
            pg.wait_for_timeout(500)
            p = OUT / name
            pg.screenshot(path=str(p))
            box = pg.evaluate("""(sel) => { const el = document.querySelector(sel);
                if (!el) return null; const r = el.getBoundingClientRect();
                return [r.x, r.y, r.width, r.height]; }""", view[1])
            shown = pg.evaluate("(sel) => { const el = document.querySelector(sel);"
                                " return !!(el && el.classList.contains('show')); }", view[1])
            pg.close()
            return p.read_bytes(), box, shown

        print("=== A. 手机端逐像素回归（基线 vs 现状：PNG 必须逐字节相同）===", flush=True)
        for (w, h) in MOBILE:
            for name, sel, fn in VIEWS:
                a_png, a_box = shot(P0, w, h, (name, sel, fn), f"base_{w}x{h}_{name}.png")
                b_png, b_box = shot(P1, w, h, (name, sel, fn), f"new_{w}x{h}_{name}.png")
                same = a_png == b_png
                check(f"{w}×{h} · {name}（{sel}）截图与基线逐字节相同", same,
                      f"base={len(a_png)}B new={len(b_png)}B" if not same else "")
                if not same:
                    import hashlib
                    print("       base md5", hashlib.md5(a_png).hexdigest()[:12],
                          "| new md5", hashlib.md5(b_png).hexdigest()[:12], flush=True)
                check(f"{w}×{h} · {name} 几何与基线一致（x/y/w/h）", a_box == b_box,
                      f"{a_box} vs {b_box}")

        print("=== B. 手机端 #pc-shell 必须不存在 ===", flush=True)
        for (w, h) in MOBILE:
            pg = browser.new_page(viewport={"width": w, "height": h})
            pg.goto(f"http://127.0.0.1:{P1}/index.html", wait_until="domcontentloaded")
            pg.wait_for_timeout(400)
            info = pg.evaluate("""() => { const el = document.getElementById('pc-shell');
                if (!el) return 'missing';
                const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
                return [cs.display, r.width, r.height]; }""")
            check(f"{w}×{h} · #pc-shell display:none 且盒子为空", info == ['none', 0, 0], str(info))
            pg.close()

        print("=== C. PC 端骨架（≥1100px）===", flush=True)
        for (w, h) in [(1600, 1000), (1280, 800)]:
            pg = browser.new_page(viewport={"width": w, "height": h})
            pg.set_default_timeout(15000)
            pg.on("pageerror", lambda e: errs.append(f"PC {w}x{h}: " + str(e)))
            pg.route("**/auth/state", lambda r: r.fulfill(
                status=200, content_type="application/json", body='{"logged_in": true}'))
            pg.goto(f"http://127.0.0.1:{P1}/index.html", wait_until="domcontentloaded")
            pg.wait_for_timeout(500)
            pg.evaluate("showHome()")          # 让第三栏的视图层真的显示出来再量几何
            pg.wait_for_timeout(400)
            geo = pg.evaluate("""() => {
                const nav = document.getElementById('pc-nav');
                const list = document.getElementById('pc-contacts');
                const shell = document.getElementById('pc-shell');
                const home = document.getElementById('home-view');
                const r = e => { const b = e.getBoundingClientRect();
                                 return [Math.round(b.x), Math.round(b.y),
                                         Math.round(b.width), Math.round(b.height)]; };
                return { shell: r(shell), nav: r(nav), list: r(list), home: r(home),
                         overflow: document.documentElement.scrollWidth - window.innerWidth,
                         contacts: document.querySelectorAll('.pc-contact').length,
                         navBtns: document.querySelectorAll('.pc-nav-btn').length };
            }""")
            check(f"{w}×{h} · 三栏几何（侧栏 72 + 联系人 288，第三栏从 360 起）",
                  geo["nav"][2] == 72 and geo["list"][2] == 288 and geo["home"][0] == 360,
                  str({k: geo[k] for k in ("nav", "list", "home")}))
            check(f"{w}×{h} · 无横向溢出", geo["overflow"] <= 0, str(geo["overflow"]))
            check(f"{w}×{h} · 侧边栏 7 个卡外入口在位", geo["navBtns"] == 7, str(geo["navBtns"]))
            pg.screenshot(path=str(SHOTS / f"pc_step1_{w}x{h}.png"))
            pg.evaluate("openCardsView()")
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(SHOTS / f"pc_step1_{w}x{h}_cards.png"))
            pg.close()

        js_errs = [e for e in errs if "pageerror" in e.lower() or "Error" in e]
        check(f"D. 全程 pageerror = 0（{len(errs)} 条）", not errs, " | ".join(errs[:3]))
        browser.close()
finally:
    a.shutdown()
    b.shutdown()

print(f"\n截图：{SHOTS}（PC）与 {OUT}（回归对比原图）", flush=True)
print(f"结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
