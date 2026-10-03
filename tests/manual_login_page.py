# -*- coding: utf-8 -*-
"""服务器专属页背景统一后的回归（task-18）：静态起服 → 三页截图 + 登录页功能。

**手工/按需运行**（同 manual_plaza_* 惯例，不在 tests/test_*.py 套件内）。
用法：`python tests/manual_login_page.py`（**固定 8765**；被占则退出 3，绝不杀进程）。

它做四件事：
1. 用**静态** http.server 起在 8765，根目录 = `server/frontend/`（登录页/管理台是纯静态页），
   并把 `server/download_page.py` 的模板渲染成 `_download_preview.html` 一起提供（只为截图对比）；
2. 同宽同高（1280×900）截三页 → `_harden/shots/`（下载页 / 登录页 / 管理台）；
3. 登录页功能回归：`#loginForm`+`#loginPass` 可见 → 点「注册」切到 `#registerForm` →
   回登录 → 点「忘记密码？」到 `#resetForm`；全程 `pageerror` = 0；
4. 断页背景确实是 bg-sunset.css 那套（body 的 background-image 含下载页同一个色标 `#9a559a`）。

退出码：0 全过；1 有断言失败；3 端口被占。
"""
import functools
import http.server
import os
import socketserver
import socket as _sk
import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "server" / "frontend"
SHOTS = ROOT / "_harden" / "shots"
PORT = int(os.environ.get("FIREFLY_TEST_PORT", "8765"))
# 默认**固定 8765**（被占退出 3，绝不杀进程）。`FIREFLY_TEST_PORT` 只是给"队友正占着 8765"时
# 自己换端口跑同一套断言用的逃生口——本脚本是**纯静态**页检查，换端口不影响任何被测行为。

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}" + (f"   {extra}" if extra else ""), flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"   {extra}" if extra else ""), flush=True)


# ── 端口检查（占则退出 3，绝不误杀）──────────────────────────────
_p = _sk.socket()
_p.settimeout(0.6)
try:
    _p.connect(("127.0.0.1", PORT))
    print(f"!! 端口 {PORT} 被占用（可能是用户正在跑的实例）——放弃，避免误杀真实进程", flush=True)
    sys.exit(3)
except OSError:
    pass
finally:
    _p.close()

SHOTS.mkdir(parents=True, exist_ok=True)
# 下载页预览（只为截图对比；不改 download_page.py，它保持自包含）
sys.path.insert(0, str(ROOT / "server"))
import download_page as dp                                    # noqa: E402

_ver = {"name": "dev", "version": "0.0.0"}
try:
    sys.path.insert(0, str(ROOT / "app"))
    from modules import app_config as _cfg
    _ver = {"name": getattr(_cfg, "APP_NAME", "流萤"),
            "version": getattr(_cfg, "VERSION", getattr(_cfg, "APP_VERSION", "0.9.0"))}
except Exception:
    pass
html = dp.INDEX_TPL
for k, v in _ver.items():
    html = html.replace("{{" + k + "}}", str(v))
(FRONT / "_download_preview.html").write_text(html, encoding="utf-8")


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):        # 静音
        pass


handler = functools.partial(_Handler, directory=str(FRONT))
httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{PORT}"
time.sleep(0.4)
print("静态服务已就绪:", base, flush=True)

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.set_default_timeout(15000)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console.error: " + m.text)
              if m.type == "error" else None)

        # ── 三页截图（同宽同高，肉眼可判"同一套背景"）──
        for url, name in (("/_download_preview.html", "10_download.png"),
                          ("/login.html", "11_login.png"),
                          ("/admin.html", "12_admin.png")):
            pg.goto(base + url, wait_until="domcontentloaded")
            pg.wait_for_timeout(700)
            pg.screenshot(path=str(SHOTS / name))
        bg_dl = None
        pg.goto(base + "/_download_preview.html", wait_until="domcontentloaded")
        bg_dl = pg.evaluate("() => getComputedStyle(document.body).backgroundImage")
        pg.goto(base + "/login.html", wait_until="domcontentloaded")
        bg_login = pg.evaluate("() => getComputedStyle(document.body).backgroundImage")
        pg.goto(base + "/admin.html", wait_until="domcontentloaded")
        bg_admin = pg.evaluate("() => getComputedStyle(document.body).backgroundImage")
        for nm, val in (("下载页", bg_dl), ("登录页", bg_login), ("管理台", bg_admin)):
            check(f"背景含下载页同一色标 #9a559a（{nm}）", "154, 85, 154" in val or "154,85,154" in val,
                  val[:64])

        # ── 登录页功能回归 ──
        pg.goto(base + "/login.html", wait_until="domcontentloaded")
        pg.wait_for_timeout(400)
        check("① 登录表单默认可见（#loginForm / #loginPass）",
              pg.is_visible("#loginForm") and pg.is_visible("#loginPass"))
        check("①b 登录表单默认显示、注册/重置表单默认隐藏",
              pg.is_visible("#registerForm") is False and pg.is_visible("#resetForm") is False)
        pg.click("#toRegister")
        pg.wait_for_timeout(250)
        check("② 点「注册」切到注册表单（#toLogin 出现）",
              pg.is_visible("#registerForm") and pg.is_visible("#toLogin"))
        pg.click("#toLogin")
        pg.wait_for_timeout(250)
        check("③ 点「已有账号？登录」切回登录表单",
              pg.is_visible("#loginForm") and pg.is_visible("#registerForm") is False)
        pg.click("#toReset")
        pg.wait_for_timeout(250)
        check("④ 「忘记密码？」可达重置表单（#resetForm）", pg.is_visible("#resetForm"))
        check("⑤ 防枚举文案还在（源码里有「已注册」这类措辞）",
              "已注册" in (FRONT / "login.html").read_text(encoding="utf-8"))
        check(f"⑥ 全程无 pageerror（{len([e for e in errs if 'pageerror' in e])} 条）",
              not [e for e in errs if "pageerror" in e])
        b.close()
finally:
    httpd.shutdown()
    try:
        (FRONT / "_download_preview.html").unlink()
    except OSError:
        pass

print(f"\n截图目录：{SHOTS}", flush=True)
print(f"结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
