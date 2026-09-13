# -*- coding: utf-8 -*-
"""真实浏览器验证 F-6.3（当前包持久化）：切到 haruno → 刷新 → 仍是 haruno。

**手工/按需运行的端到端检查**（定位同 manual_10turns.py / e2e_20turns.py，
不在 `tests/test_*.py` 自动化套件内；需要 playwright + chromium）。
用法：`python tests/manual_ui_mode_persist.py`。
它自己起一个带**沙箱 USER_DIR** 的本地服务，绝不碰真实 user_data，跑完自动关闭；
若 8765 已被占用（用户正在用）直接退出，避免误杀真实进程。

可观测点选择（踩过的两个假信号，记录备查）：
- 轮播激活位不可用作证据：showChat→showHome 会把轮播重置回 0；
- 启动早期的 /wake-status?mode=… 也不可用：main.js 在模块求值时就发了，早于 loadModes；
- 真信号 = 聊天页顶部 #chat-mode-tag（showChat 里按 CURRENT_MODE 写入）。
  本地版 showChat 有登录前置，故只**桩掉 /auth/state**（登录态），不桩任何被测逻辑。
"""
import os, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765   # app_config.PORT 硬编码（不读 FIREFLY_PORT）
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_ui_sandbox_"))
print("沙箱 USER_DIR:", SANDBOX / "user_data", flush=True)

import socket as _sk
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

import urllib.request


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
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        pg.set_default_timeout(15000)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        # 只桩登录态，让本地版 showChat 能走到标签更新（不桩任何被测逻辑）
        pg.route("**/auth/state", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"logged_in": true, "offline_ok": true}'))

        def _open_and_read_tag(tag_note):
            pg.reload(wait_until="domcontentloaded")
            pg.wait_for_selector("#carousel-dots span", state="attached", timeout=10000)
            pg.wait_for_timeout(1200)
            return pg.inner_text("#chat-mode-tag").strip()

        pg.goto(base, wait_until="domcontentloaded")
        pg.wait_for_selector("#carousel-dots span", state="attached", timeout=10000)
        pg.wait_for_timeout(1200)
        names = pg.evaluate("Object.fromEntries((window.__getPresets()||[]).map(m => [m.id, m.name]))")
        haruno_name = names.get("haruno", "")
        story_name = names.get("story", "")
        print("注册表:", names, flush=True)

        # 进 haruno（enterMode → _switchMode → 持久化）
        pg.evaluate("window.enterMode('haruno')")
        pg.wait_for_timeout(800)
        saved = pg.evaluate("localStorage.getItem('firefly_last_mode')")
        tag_now = pg.inner_text("#chat-mode-tag").strip()
        print(f"切包后 localStorage={saved} tag={(tag_now == haruno_name)}", flush=True)

        # 刷新（模拟用户重开页面）
        tag_after = _open_and_read_tag("reload")
        saved2 = pg.evaluate("localStorage.getItem('firefly_last_mode')")
        print(f"刷新后 localStorage={saved2} tag==haruno名称: {tag_after == haruno_name}", flush=True)

        # 无效 id → 必须回退默认包
        pg.evaluate("localStorage.setItem('firefly_last_mode','definitely-not-a-pack')")
        tag_bad = _open_and_read_tag("reload-bad")
        print(f"无效 id 后 tag==内置默认(story)名称: {tag_bad == story_name}", flush=True)

        print("页面 JS 错误:", errs[:3] if errs else "无", flush=True)
        b.close()

    checks = [
        ("切包即写 localStorage=haruno", saved == "haruno"),
        ("切包立即生效（聊天页标签=春日手信）", tag_now == haruno_name and bool(haruno_name)),
        ("刷新后 localStorage 仍为 haruno", saved2 == "haruno"),
        ("刷新后当前包仍是 haruno（标签一致）", tag_after == haruno_name),
        ("无效 id 回退内置默认包（标签=剧情模式）", tag_bad == story_name and bool(story_name)),
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
