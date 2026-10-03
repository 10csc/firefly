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
        # 跳过首次引导遮罩（会拦截点击）：① 预置 done 标记；② **用 CSS 隐藏而不是删节点**
        #   —— 删节点会让引导自己的后续代码去给已移除的元素挂 onclick，抛出假异常
        #   （本脚本现在要真实点击 #pv-tree 里的文件行，所以这一层必须处理；同款写法见 manual_plaza_ui.py）。
        #   ⚠ 必须在 `documentElement` 存在后再 observe —— init script 早于任何 DOM 节点。
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
        # ⚠ 2026-10-01（P4-10 修陈旧选择器）：旧 id `#pv-prompts` 在产品里**已不存在** —— 包详情页
        #   早已改由 panels/pack_tree.js 渲染进 `#pv-tree`：域 = `details.pt-domain[data-key]`，
        #   文件 = `.pt-file` 里可点击的 `.pt-row`，编辑器是**点开才懒建**的
        #   `.pt-editor > textarea.pt-ta`（+ 保存/恢复默认/收起）。
        #   原来那两步等/点 `#pv-prompts` 因此**永远超时**（假失败：既不能当验收依据，又会盖住真回归）。
        #   现在等/点的是**同一件事的现等价物**：人设域在位 → 点开首份可编辑文件 → 编辑器真的可见。
        #   断言没有放宽（仍然要求"编辑器出现"这一可观测结果）。
        step("打开 story 包详情页", lambda: pg.evaluate("window.openPackView('story')"))

        def _open_first_persona_editor():
            """展开「人设与口吻」域 → 点开第一份**可编辑**文件 → 返回该文件行。

            「本包未附带」的文件（`.pt-row-missing`）没有编辑器，跳过。"""
            pg.wait_for_selector('#pv-tree details.pt-domain[data-key="persona"]',
                                 state="attached", timeout=10000)
            # `<details>` 默认折叠：不展开则行不可见、点不到（等价于旧脚本点 summary）
            pg.evaluate("""() => {
                const d = document.querySelector('#pv-tree details.pt-domain[data-key="persona"]');
                if (d) d.open = true;
            }""")
            rows = pg.locator('#pv-tree details.pt-domain[data-key="persona"] .pt-file .pt-row')
            for i in range(rows.count()):
                if "pt-row-missing" in (rows.nth(i).get_attribute("class") or ""):
                    continue
                rows.nth(i).click()
                pg.wait_for_selector('#pv-tree details.pt-domain[data-key="persona"] '
                                     '.pt-editor textarea.pt-ta', state="visible", timeout=8000)
                return rows.nth(i)
            raise AssertionError("人设域里没有可编辑文件（全是「本包未附带」）")

        step("等人设树渲染（人设域节点在位）",
             lambda: pg.wait_for_selector("#pv-tree details.pt-domain", state="attached", timeout=8000))
        step("展开第一段人设（点开首份可编辑文件，编辑器真的出现）",
             lambda: _open_first_persona_editor())
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
