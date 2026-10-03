# -*- coding: utf-8 -*-
"""真实浏览器验证 F-3（包详情页代际保护）：A 页的"保存"绝不写进 B。

**手工/按需运行的端到端检查**（定位同 manual_10turns.py / e2e_20turns.py，
不在 `tests/test_*.py` 自动化套件内；需要 playwright + chromium）。
用法：`python tests/manual_ui_pack_gen.py`（本机 8765 空闲时；占用则直接退出）。

它自己起一个带**沙箱 USER_DIR** 的本地服务，绝不碰真实 user_data。

核心断言（对应任务卡验收" A→B 快速切换后保存，A 文案不写进 B"）：
打开 haruno 的包详情页 → 切到另一个包（不重开详情页）→ 点该页的「保存」
→ 请求体里的 mode 必须是 **haruno**（本页展示的包），而不是切换后的 story。
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
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_ui_packgen_"))
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
env["FIREFLY_ANDROID"] = "1"
env["FIREFLY_DATA_DIR"] = str(SANDBOX)
env["PYTHONUTF8"] = "1"
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUNBUFFERED"] = "1"

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
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)
    print("服务已就绪", flush=True)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.set_default_timeout(20000)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        # 首次引导遮罩会拦截点击（新 profile 没有完成标记）：① 预置完成标记；
        # ② **用 CSS 隐藏而不是删节点** —— 删节点会让引导自己的后续代码去给已移除的元素挂 onclick，
        #    抛 `Cannot set properties of null` 假异常（manual_plaza_ui.py 踩过，那里有完整注释）。
        #    本脚本现在要真实点击 #pv-tree 里的文件行/保存按钮，这一层必须处理。
        #    ⚠ 必须在 `documentElement` 存在后再 observe —— init script 早于任何 DOM 节点。
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
        # 只桩登录态，让 showChat 能继续（不桩任何被测逻辑）
        pg.route("**/auth/state", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"logged_in": true, "offline_ok": true}'))

        bodies = []
        pg.on("request", lambda r: bodies.append((r.url, r.post_data)) if r.method == "POST" else None)

        pg.goto(base, wait_until="domcontentloaded")
        pg.wait_for_selector("#carousel-dots span", state="attached", timeout=10000)
        pg.wait_for_timeout(1200)

        # ⚠ 2026-10-01（P4-10 修陈旧选择器）：旧 id `#pv-prompts` 在产品里**已不存在** —— 包详情页
        #   早已改由 panels/pack_tree.js 渲染进 `#pv-tree`：域 = `details.pt-domain[data-key]`，
        #   文件 = `.pt-file` 里可点击的 `.pt-row`，编辑器**点开才懒建**
        #   （`.pt-editor > textarea.pt-ta` + `button.pt-save`，保存走 /character-file-update）。
        #   本脚本原来等的 `#pv-prompts textarea` 永远超时（假失败）。现在用同一件事的现等价物：
        #   「人设与口吻」域里首份**可编辑**且**有正文**的人设槽位文件——被测语义（F-3：A 页的保存
        #   必须写进 A）一个字没变。
        _PERSONA = '#pv-tree details.pt-domain[data-key="persona"]'

        def _open_first_persona_row():
            """展开人设域 → 点开首份可编辑文件 → 返回该 `.pt-file` 行（编辑器已可见且有正文）。

            树是 `#pv-tree.innerHTML = ""` **整块重渲**的：若在上一次渲染的残树上点，节点会
            中途 detach（点击落在不可见的旧行上）。所以这里对每次点击都设短超时并**整轮重试**，
            让 Playwright 重新解析定位器。"""
            for _round in range(8):
                try:
                    pg.wait_for_selector(_PERSONA, state="attached", timeout=8000)
                    # `<details>` 默认折叠：不展开则行不可见、点不到
                    pg.evaluate("""() => {
                        const d = document.querySelector('#pv-tree details.pt-domain[data-key="persona"]');
                        if (d) d.open = true;
                    }""")
                    rows = pg.locator(_PERSONA + " .pt-file")
                    for i in range(rows.count()):
                        wrap = rows.nth(i)
                        row = wrap.locator(".pt-row")
                        if "pt-row-missing" in (row.get_attribute("class") or ""):
                            continue                      # 本包未附带：没有编辑器
                        row.click(timeout=4000)
                        pg.wait_for_selector(_PERSONA + " .pt-editor textarea.pt-ta",
                                             state="visible", timeout=8000)
                        # 正文由 _loadFileContent 异步填入；空正文保存会被前端拦下
                        # （"内容不能为空"）→ 等它有正文再交出去，否则换下一份。
                        filled = False
                        for _ in range(30):
                            if wrap.locator(".pt-ta").input_value().strip():
                                filled = True
                                break
                            pg.wait_for_timeout(100)
                        if filled:
                            return wrap
                        row.click(timeout=4000)           # 收起这份空文件，试下一份
                except Exception:
                    pg.wait_for_timeout(250)              # 多半是重渲把节点换掉了：重新来一轮
            raise AssertionError("人设域里没有「有正文的可编辑文件」（或树一直在重渲）")

        def _open_pack_persona(mode):
            """打开 mode 的包详情页，并**等这一包的 /pack-structure 响应**回来后再点人设文件。

            直接 `openPackView()` 后立刻等 `#pv-tree` 会命中**上一个包的残树**（渲染是异步的）。"""
            with pg.expect_response(
                    lambda r: "/pack-structure" in r.url and ("mode=" + mode) in r.url,
                    timeout=15000):
                pg.evaluate(f"window.openPackView('{mode}')")
            pg.wait_for_timeout(300)          # 响应之后 pack_tree 才 _render()
            return _open_first_persona_row()

        def _click_save(wrap):
            """点该文件编辑器里的「保存」（编辑器已在 _open_first_persona_row 里展开）。"""
            wrap.locator(".pt-save").click(timeout=8000)
            pg.wait_for_timeout(900)

        # 1) 打开 haruno 的包详情页（loadPackView 捕获 mode=haruno / gen）
        row_a = _open_pack_persona("haruno")
        pg.wait_for_timeout(400)
        scene = pg.inner_text("#pv-scene").strip()
        print("详情页当前展示:", scene, "| A 页编辑的文件:", row_a.locator(".pt-editing").inner_text().strip(), flush=True)

        # 2) 切到另一个包（不重开详情页 —— 模拟"切走后回到还开着的编辑页再点保存"）
        pg.evaluate("window.__setCurrentMode('story')")
        pg.wait_for_timeout(200)
        scene_after = pg.inner_text("#pv-scene").strip()     # 前提断言：A 页 DOM 不能跟着切走
        print("已切到 story（详情页仍是 haruno 的 DOM，展示:", scene_after, "）", flush=True)

        # 3) 点该页的「保存」→ 请求体 mode 必须是 haruno
        bodies.clear()
        _click_save(row_a)
        upd = [(u, pd) for (u, pd) in bodies if u.endswith("/character-file-update")]
        mode_sent = None
        if upd:
            try:
                mode_sent = json.loads(upd[-1][1]).get("mode")
            except Exception:
                mode_sent = None
        print("保存请求体 mode =", mode_sent, flush=True)

        # 4) 反向 sanity：重开 story 的详情页再保存 → mode 应为 story
        row_b = _open_pack_persona("story")
        pg.wait_for_timeout(400)
        bodies.clear()
        _click_save(row_b)
        upd2 = [(u, pd) for (u, pd) in bodies if u.endswith("/character-file-update")]
        mode_sent2 = json.loads(upd2[-1][1]).get("mode") if upd2 else None
        print("重开 story 后保存请求体 mode =", mode_sent2, flush=True)

        print("页面 JS 错误:", errs[:3] if errs else "无", flush=True)
        b.close()

    checks = [
        ("haruno 详情页确实打开了（展示包名=剧本模式）", scene == "剧本模式"),
        ("前提：切包后 A 页展示的仍是 A（详情页 DOM 没被切走）", scene_after == "剧本模式"),
        ("切换后 A 页保存写回的是 A（mode=haruno，不是 story）", mode_sent == "haruno"),
        ("反向 sanity：重开 B 页保存写回 B（mode=story）", mode_sent2 == "story"),
    ]
    for name, ok in checks:
        print(f"  {'V' if ok else 'X'} {name}", flush=True)
    allok = all(ok for _, ok in checks)
    print("总判定:", "PASS" if allok else "FAIL", flush=True)
    sys.exit(0 if allok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
