# -*- coding: utf-8 -*-
"""真实浏览器验证「按面板拆 panels.js」后各面板仍可正常打开（阶段 2 · 任务 2.5）。

**手工/按需运行的端到端检查**（定位同 manual_10turns.py / e2e_20turns.py，
不在 `tests/test_*.py` 自动化套件内；需要 playwright + chromium）。
用法：`python tests/manual_ui_panels.py`（本机 8765 空闲时；占用则直接退出）。

它自己起一个带**沙箱 USER_DIR** 的本地服务，绝不碰真实 user_data。

覆盖：菜单抽屉 → 五个 tab（char/state/fav/log/pipeline）逐个点开；包详情页（外壳拆出的
packs 模块）打开并渲染人设编辑器；全程收集 pageerror，任何一个即失败。
"""
import json
import os
import socket as _sk
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765   # app_config.PORT 硬编码（不读 FIREFLY_PORT）
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_ui_panels_"))
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
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console.error: " + m.text)
              if m.type == "error" else None)
        # 跳过首次引导遮罩（会拦截点击）
        pg.add_init_script(
            "try{localStorage.setItem('firefly_guide_v1_done','1');"
            "localStorage.setItem('firefly_deep_guide_v1_done','1');}catch(e){}")
        # 桩登录态，让 showChat 能继续
        pg.route("**/auth/state", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"logged_in": true, "offline_ok": true}'))

        pg.goto(base, wait_until="domcontentloaded")
        pg.wait_for_selector("#carousel-dots span", state="attached", timeout=10000)
        pg.wait_for_timeout(800)

        results = []

        def step(name, fn):
            before = len(errs)
            try:
                fn()
                pg.wait_for_timeout(600)
                results.append((name, len(errs) == before, ""))
            except Exception as e:
                results.append((name, False, str(e).split("\n")[0][:120]))

        # 1) 菜单抽屉 + 五个 tab（外壳 + data + debug 模块的 loader 全过一遍）
        step("打开菜单抽屉", lambda: pg.evaluate("window.openMenu()"))
        # tab 按钮可能在抽屉滚动区外（Playwright 判定不可见）→ 用 JS 触发，
        # 目的是驱动各面板 loader（data/debug 模块），不是测可点性
        for tab in ("char", "state", "fav", "log", "pipeline"):
            step(f"切到 tab={tab}",
                 lambda t=tab: pg.evaluate(
                     f"document.querySelector('.menu-tab[data-tab=\"{t}\"]').click()"))
        step("关闭菜单", lambda: pg.evaluate("window.closeMenu()"))

        # 2) 设置/反馈面板（外壳模块）
        step("打开设置面板", lambda: pg.evaluate("window.openSettings()"))
        step("关闭设置面板", lambda: pg.evaluate("window.closeSettings()"))

        # 3) 包详情页（packs 模块：人设编辑器 + 专属表情包 + 主动消息）
        step("打开 story 包详情页", lambda: pg.evaluate("window.openPackView('story')"))
        step("等待人设编辑器", lambda: pg.wait_for_selector("#pv-prompts details", state="attached",
                                                           timeout=8000))
        step("展开第一段人设", lambda: pg.locator("#pv-prompts summary").first.click())
        step("切到 haruno 详情页", lambda: pg.evaluate("window.openPackView('haruno')"))
        step("关闭详情页", lambda: pg.evaluate("window.closePackView()"))

        print("页面错误/console.error:", errs[:5] if errs else "无", flush=True)
        b.close()

    for name, ok, err in results:
        print(f"  {'V' if ok else 'X'} {name}" + (f"  [{err}]" if err else ""), flush=True)
    allok = all(ok for _, ok, _e in results) and not errs
    print("总判定:", "PASS" if allok else "FAIL", flush=True)
    sys.exit(0 if allok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()
