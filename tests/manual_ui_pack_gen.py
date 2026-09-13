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
        # 首次引导遮罩会拦截点击（新 profile 没有完成标记）→ 预置完成标记跳过引导
        pg.add_init_script(
            "try{localStorage.setItem('firefly_guide_v1_done','1');"
            "localStorage.setItem('firefly_deep_guide_v1_done','1');}catch(e){}")
        # 只桩登录态，让 showChat 能继续（不桩任何被测逻辑）
        pg.route("**/auth/state", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"logged_in": true, "offline_ok": true}'))

        bodies = []
        pg.on("request", lambda r: bodies.append((r.url, r.post_data)) if r.method == "POST" else None)

        pg.goto(base, wait_until="domcontentloaded")
        pg.wait_for_selector("#carousel-dots span", state="attached", timeout=10000)
        pg.wait_for_timeout(1200)

        # 1) 打开 haruno 的包详情页（loadPackView 捕获 mode=haruno / gen）
        pg.evaluate("window.openPackView('haruno')")
        pg.wait_for_selector("#pv-prompts textarea", state="attached", timeout=10000)
        pg.wait_for_timeout(400)
        scene = pg.inner_text("#pv-scene").strip()
        print("详情页当前展示:", scene, flush=True)

        # 2) 切到另一个包（不重开详情页 —— 模拟"切走后回到还开着的编辑页再点保存"）
        pg.evaluate("window.__setCurrentMode('story')")
        pg.wait_for_timeout(200)
        print("已切到 story（详情页仍是 haruno 的 DOM）", flush=True)

        # 3) 点该页的「保存」→ 请求体 mode 必须是 haruno
        def _click_first_save():
            """人设文案在折叠的 <details> 里，先展开再点保存。"""
            pg.locator("#pv-prompts summary").first.click()
            pg.wait_for_timeout(250)
            pg.locator("#pv-prompts button:has-text('保存')").first.click()
            pg.wait_for_timeout(900)

        bodies.clear()
        _click_first_save()
        upd = [(u, pd) for (u, pd) in bodies if u.endswith("/character-file-update")]
        mode_sent = None
        if upd:
            try:
                mode_sent = json.loads(upd[-1][1]).get("mode")
            except Exception:
                mode_sent = None
        print("保存请求体 mode =", mode_sent, flush=True)

        # 4) 反向 sanity：重开 story 的详情页再保存 → mode 应为 story
        pg.evaluate("window.openPackView('story')")
        pg.wait_for_selector("#pv-prompts textarea", state="attached", timeout=10000)
        pg.wait_for_timeout(400)
        bodies.clear()
        _click_first_save()
        upd2 = [(u, pd) for (u, pd) in bodies if u.endswith("/character-file-update")]
        mode_sent2 = json.loads(upd2[-1][1]).get("mode") if upd2 else None
        print("重开 story 后保存请求体 mode =", mode_sent2, flush=True)

        print("页面 JS 错误:", errs[:3] if errs else "无", flush=True)
        b.close()

    checks = [
        ("haruno 详情页确实打开了（展示包名=春日手信）", scene == "春日手信"),
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
