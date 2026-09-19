# -*- coding: utf-8 -*-
"""PC（Windows 冻结版）冒烟测试：起 dist/firefly/firefly.exe，验关键端点与前端资源。

覆盖的是"用户拿到安装包后到底跑的是不是 0.9.0 的新前端"：
  /config 200 + 版本号、/pc.css 存在且含三栏外壳、/index.html 引 pc.css + pc_shell.js、
  以及热更/公告端点在本包里也在（本地版才有）。
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "firefly" / "firefly.exe"
PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def get(path, timeout=20):
    req = urllib.request.Request(f"http://127.0.0.1:8765{path}",
                                 headers={"User-Agent": "FireflySmoke/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


if not EXE.is_file():
    print(f"X 找不到 {EXE}（先跑 python -m PyInstaller firefly.spec --noconfirm）")
    sys.exit(2)

print("=== 启动冻结版 exe ===")
p = subprocess.Popen([str(EXE)], cwd=str(EXE.parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ok = False
    for _ in range(30):
        time.sleep(1.5)
        try:
            st, _b = get("/config", timeout=5)
            if st == 200:
                ok = True
                break
        except Exception:
            continue
    check("exe 起来并在 8765 提供 /config 200", ok)
    if not ok:
        print(f"\n统计: PASS={PASS} FAIL={FAIL}")
        sys.exit(1)

    print("\n=== A. 版本与前端资源 ===")
    st, raw = get("/index.html")
    html = raw.decode("utf-8", "replace")
    # 第二版 PC 适配层：**不许有自造的第二套导航**（第一版被否掉的 pc-shell/rail/list）
    check("index.html 不含自造外壳（pc-shell/pc-rail/pc-list/pc-chat-side）",
          all(k not in html for k in ('id="pc-shell"', 'id="pc-rail"', 'id="pc-list"',
                                      'id="pc-chat-side"')))
    check("index.html 有底部状态栏（只放信息）", 'id="pc-statusbar"' in html)
    check("index.html 保留手机端的 ☰ 菜单入口与菜单本体",
          'id="menu-btn"' in html and 'id="menu-drawer"' in html and "与角色相关" in html)
    check("index.html 引 pc.css", 'href="pc.css' in html)
    check("index.html 引 pc_shell.js", "js/pc_shell.js" in html)
    check("index.html 不再引 pc_nav.js", "pc_nav.js" not in html)
    st, css = get("/pc.css")
    c = css.decode("utf-8", "replace")
    check("/pc.css 可取、有断点隔离、菜单是右侧停靠",
          "@media (min-width: 1100px)" in c and "--pc-menu-w" in c
          and "pointer-events: none" in c, f"{len(css)} bytes")
    st, js = get("/js/pc_shell.js")
    check("/js/pc_shell.js 可取且只做状态栏",
          b"__pcShell" in js and b"renderStatus" in js and b"RAIL" not in js, f"{len(js)} bytes")
    st, b = get("/js/bundle.js")
    check("/js/bundle.js 含公告渲染 + self-init",
          b"notice-server" in b and b"DOMContentLoaded" in b)

    print("\n=== B. 关键后端端点（本地版专属）===")
    st, b = get("/modes")
    d = json.loads(b.decode("utf-8"))
    check("/modes 返回角色包清单", bool(d.get("modes")), f"{len(d.get('modes') or [])} 个包")
    st, b = get("/hotupdate/status")
    h = json.loads(b.decode("utf-8"))
    check("/hotupdate/status 可用（本地版）", h.get("ok") is True,
          f"base={h.get('base_version')} backend={h.get('verify_backend')}")
    check("PC 端验签走自实现（无 Kotlin 桥）", h.get("verify_backend") == "python",
          str(h.get("verify_backend")))
    st, b = get("/notice")
    n = json.loads(b.decode("utf-8"))
    check("/notice 可用（本地版）", n.get("ok") is True,
          f"serial={n.get('serial')} entries={len(n.get('entries') or [])}")
finally:
    p.terminate()
    try:
        p.wait(timeout=15)
    except Exception:
        p.kill()

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
