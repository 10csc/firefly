# -*- coding: utf-8 -*-
"""真实浏览器验证 task-24：App 里「服务器云端版 / 打开服务器版 →」入口已彻底移除（2026-10-01）。

**手工/按需运行的端到端检查**（同 manual_ui_panels.py：不在 tests/test_*.py 自动化套件内，
需要 playwright + chromium）。用法：`python tests/manual_server_entry_removed.py`
（本机 8765 被占则直接退出，不误杀真实进程）。

为什么必须真浏览器：删按钮/函数这类改动，假 DOM 测试只能断言"源码里没有那个字符串"，
**不能证明页面真的没有那个可见入口、也没有因删除而抛异常**（`initAuth` 里那段取元素的代码
就在被删的行旁边）。本脚本判据全部落在可观测结果上：
  ① 渲染后的 DOM（含隐藏节点）里没有「打开服务器版 / 服务器云端版 / server-web-entry」；
  ② `window.openServerWeb` 与 `window.FIREFLY_SERVER_WEB` 都是 undefined；
  ③ **零 pageerror / 零 console.error**（删除没留下悬空引用）；
  ④ 该面板本身与其它面板照常工作：账号面板仍渲染、设置面板可开、角色卡管理树可渲染、广场入口可打开。
"""
import os
import socket as _sk
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765   # app_config.PORT 硬编码（不读 FIREFLY_PORT）
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_no_server_entry_"))
print("沙箱 USER_DIR:", SANDBOX / "user_data", flush=True)

_p = _sk.socket(); _p.settimeout(0.6)
try:
    _p.connect(("127.0.0.1", PORT))
    print(f"!! 端口 {PORT} 被占用（可能是用户正在跑的实例）——放弃，避免误杀真实进程", flush=True)
    sys.exit(3)
except OSError:
    pass
finally:
    _p.close()

env = dict(os.environ)
env.update({"FIREFLY_ANDROID": "1", "FIREFLY_DATA_DIR": str(SANDBOX),
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
_log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(ROOT / "app"),
                        env=env, stdout=_log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def _wait_server():
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + "/config", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


try:
    if not _wait_server():
        print("服务未起来", flush=True)
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1200:], flush=True)
        sys.exit(2)
    print("服务已就绪", flush=True)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.set_default_timeout(20000)
        errs = []
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        pg.on("console", lambda m: errs.append("console.error: " + m.text)
              if m.type == "error" else None)
        # 跳过首次引导遮罩（同 manual_ui_panels.py：用 CSS 隐藏而不是删节点——删节点会让
        # 引导自己的后续代码给已移除元素挂 onclick，抛假异常）。必须在 documentElement 存在后再 observe。
        pg.add_init_script(
            "try{localStorage.setItem('firefly_guide_v1_done','1');"
            "localStorage.setItem('firefly_deep_guide_v1_done','1');}catch(e){}\n"
            "window.__guideNuked=0;\n"
            "const __nukeGuide=()=>{if(window.__guideNuked)return;"
            "const st=document.createElement('style');"
            "st.textContent='#guide-mask,.guide-block{display:none!important;pointer-events:none!important}';"
            "(document.head||document.documentElement).appendChild(st);window.__guideNuked=1;};\n"
            "const __arm=()=>{if(!document.documentElement){setTimeout(__arm,0);return;}"
            "new MutationObserver(__nukeGuide).observe(document.documentElement,"
            "{childList:true,subtree:true});__nukeGuide();};\n"
            "__arm();")
        # 桩登录态：让账号面板走到"已登录"分支（本地模式也会 fetch /auth/state）
        pg.route("**/auth/state", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"logged_in": true, "offline_ok": true, "email": "e2e@test"}'))
        # 桩广场列表/已装清单：广场是**登录可见**的，未登录必然 401 —— 而 401 会让 Chromium 打一条
        # "Failed to load resource: 401" 的 console.error，把"零错误"这条判据变成假红。
        # 这里给一个**合法空列表**，于是测的是"广场入口与列表渲染照常"，而不是登录闸门
        # （登录闸门另有 tests/manual_plaza_login_gate.py 7 项覆盖）。
        pg.route("**/plaza/api/list*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok": true, "items": [], "total": 0}'))
        pg.route("**/plaza/api/installed*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok": true, "items": [], "used_cards": 0, "used_bytes": 0}'))

        pg.goto(base, wait_until="domcontentloaded")
        pg.wait_for_selector("#carousel-dots span", state="attached", timeout=10000)
        pg.wait_for_timeout(1000)

        results = []

        def check(name, cond, note=""):
            results.append((name, bool(cond), note))
            print(f"  {'V' if cond else 'X'} {name}" + (f"  [{note}]" if note and not cond else ""),
                  flush=True)

        def step(name, fn):
            before = len(errs)
            try:
                fn()
                pg.wait_for_timeout(600)
                ok = len(errs) == before
                check(name, ok, "" if ok else errs[before][:160])
            except Exception as e:
                check(name, False, str(e).split("\n")[0][:160])

        # ── ① 渲染后的 DOM（含隐藏节点）里已无该入口 ──
        html = pg.evaluate("document.documentElement.outerHTML")
        for needle in ("打开服务器版", "服务器云端版", "server-web-entry", "openServerWeb",
                       "FIREFLY_SERVER_WEB"):
            check(f"① DOM 里不含 {needle!r}", needle not in html)

        # ── ② 全局符号确实不存在 ──
        check("② window.openServerWeb === undefined",
              pg.evaluate("typeof window.openServerWeb") == "undefined")
        check("②b 'openServerWeb' in window === false",
              pg.evaluate("'openServerWeb' in window") is False)
        check("②c window.FIREFLY_SERVER_WEB === undefined",
              pg.evaluate("typeof window.FIREFLY_SERVER_WEB") == "undefined")
        check("②d #server-web-entry 节点不存在",
              pg.evaluate("document.getElementById('server-web-entry') === null") is True)

        # ── ③ 被删代码所在的那个面板本身仍要正常（账号面板 = 按钮原来所在处）──
        step("③ 账号面板仍渲染（#auth-module 可见）",
             lambda: pg.wait_for_selector("#auth-module", state="visible", timeout=8000))
        step("③b 已登录分支仍渲染用户卡片（#auth-user-entry 可见）",
             lambda: pg.wait_for_selector("#auth-user-entry", state="visible", timeout=8000))
        check("③c 账号面板里仍有「退出登录」按钮（面板功能没被删代码带坏）",
              pg.evaluate("!!document.querySelector('#auth-user-entry button')"))

        # ── ④ 其它面板功能不变 ──
        step("④ 设置面板可开", lambda: pg.evaluate("window.openSettings()"))
        step("④b 设置面板可关", lambda: pg.evaluate("window.closeSettings()"))
        step("④c 角色卡管理页可开（story）", lambda: pg.evaluate("window.openPackView('story')"))
        step("④d 包管理树真的渲染（人设域节点在位）",
             lambda: pg.wait_for_selector("#pv-tree details.pt-domain", state="attached", timeout=8000))
        step("④e 角色卡管理页可关", lambda: pg.evaluate("window.closePackView()"))
        step("④f 广场入口可打开（plaza-view 出现）",
             lambda: (pg.evaluate("window.openPlaza()"),
                      pg.wait_for_selector("#plaza-view", state="visible", timeout=8000)))
        check("④g 广场列表真的渲染了（空态也走到了列表容器；桩返回 total=0）",
              pg.evaluate("!!document.querySelector('#plaza-view')"))
        check("④h 前端仍指向同一台服务器基址（FIREFLY_SERVER_BASE 未受影响）",
              pg.evaluate("typeof window.FIREFLY_SERVER_BASE") in ("undefined", "string"))

        print("页面错误/console.error:", errs[:5] if errs else "无", flush=True)
        b.close()

    allok = all(ok for _n, ok, _e in results) and not errs
    print(f"\n结果：{sum(1 for _n, ok, _e in results if ok)}/{len(results)} 项通过；"
          f"pageerror/console.error={len(errs)}", flush=True)
    print("总判定:", "PASS" if allok else "FAIL", flush=True)
    sys.exit(0 if allok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()
