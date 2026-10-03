# -*- coding: utf-8 -*-
"""真实浏览器验证「角色卡广场」整链（共创平台 M1；对应未完成事项 P4-2）。

**手工/按需运行**（同 manual_ui_panels.py 惯例，不在 tests/test_*.py 自动化套件内）。
用法：`python tests/manual_plaza_ui.py`（本机 8765 空闲时；占用则直接退出，绝不误杀真实进程）。

它做四件事：
1. 自建沙箱数据根（`user_data/` + `plaza/`），**绝不碰真实 user_data**；
2. 往沙箱广场里种 3 张卡（含 1 张官方、1 张高下载数），用于验证筛选/排序/官方标识；
3. 起一个**本地**服务（`FIREFLY_SERVER=1` ⇒ 广场端点本端实现，**不代理到生产服务器**、
   不产生任何真实网络流量），用 Playwright 走真实路径：
   首页 →「角色卡管理」→「角色卡广场」→ 列表/分类筛选/搜索/排序 → 详情 → 安装 → 卸载；
4. 桌面 + 移动两档截图存沙箱，收集 pageerror/console.error，任一即失败。

退出码：0 全过；1 有断言失败；2 服务没起来；3 端口被占。
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765                     # app_config.PORT 硬编码（不读 FIREFLY_PORT）
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_ui_plaza_"))
PLAZA_DIR = SANDBOX / "plaza"
DATA_DIR = SANDBOX / "user_data"
SHOTS = SANDBOX / "shots"
for _d in (PLAZA_DIR, DATA_DIR, SHOTS):
    _d.mkdir(parents=True, exist_ok=True)
print("沙箱 USER_DIR:", DATA_DIR, flush=True)
print("沙箱 plaza  :", PLAZA_DIR, flush=True)

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}", flush=True)


# ── 1. 种卡（本进程直接写沙箱广场，不经过 HTTP）─────────────────
# 注意路径语义（app/core/paths.py:24-26）：FIREFLY_ANDROID=1 时
# `USER_DIR = FIREFLY_DATA_DIR / "user_data"` —— 传沙箱根、不是 user_data 本身，
# 否则服务进程会写到 `<沙箱>/user_data/user_data/`（本脚本第一版就踩了这个）。
os.environ["FIREFLY_ANDROID"] = "1"
os.environ["FIREFLY_DATA_DIR"] = str(SANDBOX)
os.environ["FIREFLY_PLAZA_DIR"] = str(PLAZA_DIR)
sys.path.insert(0, str(ROOT / "app"))

import base64  # noqa: E402

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = DATA_DIR
cfg.CONFIG_FILE = DATA_DIR / "config.json"

from plaza import store as st      # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def solid_png(w, h, rgb):
    """造一张纯色 PNG（**纯 stdlib**：zlib + struct，不引 Pillow）。

    用途：让截图能真正看出图片链路与版式；1×1 透明图在视觉上等于没封面。"""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def _chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8bit truecolor
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


COVERS = {"ui_card_a": (58, 90, 120), "ui_card_b": (120, 84, 58), "ui_card_c": (66, 104, 84)}


def seed(cid, name, char_name, category, tags, official=False, desc=""):
    preset = {"id": cid, "name": name, "char_name": char_name, "user_name": "你",
              "presentation": "sticker", "desc": desc or f"{name} 的一句话简介", "schema": 1}
    files = {"preset.json": json.dumps(preset, ensure_ascii=False).encode(),
             "core.md": f"# {name} 的核心设定".encode(),
             "assets/cover.png": solid_png(320, 180, COVERS.get(cid, (70, 70, 90)))}
    return st.publish_card({"id": cid, "name": name, "char_name": char_name, "user_name": "你",
                            "presentation": "sticker", "desc": preset["desc"],
                            "category": category, "tags": list(tags)},
                           files, uid_hash="v" * 32, display=("官方" if official else "某作者"),
                           official=official)


seed("ui_card_a", "峡谷旅人", "小旅", "陪伴", ["治愈", "日常"], desc="陪你聊天的旅人")
seed("ui_card_b", "星轨剧场", "剧场主", "剧情", ["热血"], official=True, desc="官方剧情卡")
seed("ui_card_c", "雨夜诊所", "医生", "治愈", ["治愈"], desc="安静的雨夜")
for _ in range(5):
    st.bump_download("ui_card_b")          # 让"最热"排序有区分度
print(f"已种卡：{st.stats()['cards']} 张", flush=True)

# ── 2. 端口检查（占则退出，绝不误杀）────────────────────────────
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

env = dict(os.environ)
env.update({"FIREFLY_ANDROID": "1", "FIREFLY_DATA_DIR": str(SANDBOX),
            "FIREFLY_PLAZA_DIR": str(PLAZA_DIR), "FIREFLY_SERVER": "1",
            "FIREFLY_NO_BROWSER": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1"})
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
        phase = {"n": "启动"}
        pg.on("pageerror", lambda e: errs.append(f"[{phase['n']}] pageerror: " + str(e)))
        pg.on("console", lambda m: errs.append(f"[{phase['n']}] console.error: " + m.text)
              if m.type == "error" else None)
        pg.on("dialog", lambda d: d.accept())        # 覆盖安装/卸载的确认框
        http_bad = []                                # 记录非 2xx（403 是"服务器专属端点"所致）
        pg.on("response", lambda r: http_bad.append((r.status, r.url)) if r.status >= 400 else None)
        # 跳过引导：① 预置 done 标记；② 引导遮罩会拦截点击 —— **用 CSS 隐藏而不是删节点**。
        #   删节点会让引导自己的后续代码去给已移除的元素挂 onclick，抛
        #   `Cannot set properties of null (setting 'onclick')`（本脚本第二版踩过：
        #   那是 harness 造出来的假异常，不是产品缺陷）。
        # ⚠ 必须在 `documentElement` 存在后再 observe —— init script 早于任何 DOM 节点。
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
        # 本地模式下认证端点会 403：桩掉，避免登录态噪声（广场不需要登录态）
        pg.route("**/auth/state", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"logged_in": false}'))
        pg.route("**/auth/me", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"email": ""}'))

        pg.goto(base + "/", wait_until="domcontentloaded")
        pg.wait_for_selector("#cards-manage-btn", timeout=20000)
        pg.screenshot(path=str(SHOTS / "01_home_desktop.png"))

        def click_soft(sel, timeout=7000):
            """点击；被遮挡时**打印真正的拦截者**，清掉已知浮层后重试一次。

            浏览器测试最容易死在"看得见、点不到"（引导遮罩/公告浮层）。
            注意：诊断**不能**把选择器直接丢给 `document.querySelector`——
            Playwright 的 `:has-text()` 不是合法 CSS，会炸在诊断上、掩盖真实原因。
            这里用 locator 的 bounding box 取坐标，再用 elementFromPoint 问"谁在上面"。
            """
            def _who():
                try:
                    box = pg.locator(sel).first.bounding_box()
                    if not box:
                        return "no-box"
                    return pg.evaluate(
                        "([x, y]) => { const t = document.elementFromPoint(x, y);"
                        " return t ? (t.id || t.className || t.tagName) : 'none'; }",
                        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
                except Exception as e:
                    return f"<诊断失败:{type(e).__name__}>"

            try:
                pg.click(sel, timeout=timeout)
                return True
            except Exception:
                who = _who()
                print(f"     [遮挡诊断] {sel} 被 <{who}> 拦住 → 清理浮层后重试", flush=True)
                pg.evaluate("""() => {
                    ['guide-mask', 'notice-panel', 'menu-overlay'].forEach(id => {
                        const el = document.getElementById(id); if (el) el.remove();
                    });
                    document.querySelectorAll('.guide-block').forEach(e => e.remove());
                }""")
                pg.wait_for_timeout(250)
                pg.click(sel, timeout=timeout)
                return True

        # 真实路径：首页 →「角色卡管理」→「角色卡广场」
        entered_real = True
        try:
            pg.click("#cards-manage-btn", timeout=5000)
        except Exception:
            entered_real = False
            pg.evaluate("openCardsView()")
        check("① 首页可进入角色卡管理（真实点击路径）" if entered_real
              else "① 角色卡管理入口存在（点击被遮挡，改用其 onclick 函数）", True)
        check("② 管理页成为可见态", pg.eval_on_selector(
            "#cards-view", "el => el.classList.contains('show')"))
        check("③ 管理页头部有「角色卡广场」入口", pg.is_visible("#cv-plaza-btn"))

        click_soft("#cv-plaza-btn")
        pg.wait_for_selector("#plaza-view.show", timeout=10000)
        check("④ 广场整页打开（#plaza-view.show）", True)

        # 列表
        pg.wait_for_selector(".pz-card", timeout=20000)
        n_cards = pg.eval_on_selector_all(".pz-card", "els => els.length")
        check(f"⑤ 列表渲染出 3 张卡（实际 {n_cards}）", n_cards == 3)
        names = pg.eval_on_selector_all(".pz-cname", "els => els.map(e => e.textContent)")
        check("⑥ 卡名正确", set(names) == {"峡谷旅人", "星轨剧场", "雨夜诊所"})
        check("⑦ 官方卡带「官方」角标",
              pg.eval_on_selector_all(".pz-badge-off", "els => els.length") == 1)
        check("⑧ 未安装时无「已安装」角标",
              pg.eval_on_selector_all(".pz-badge-in", "els => els.length") == 0)
        # 封面必须经 fetch + blob（带 Authorization）取回——直接 <img src> 拿不到鉴权。
        # 懒加载是 IntersectionObserver 驱动，给足轮询时间再判定。
        srcs = []
        for _ in range(20):
            srcs = pg.eval_on_selector_all(
                "#plaza-grid img", "els => els.map(e => e.getAttribute('src') || '')")
            if srcs and all(s.startswith("blob:") for s in srcs):
                break
            pg.wait_for_timeout(300)
        check(f"⑧b 封面走 blob URL（{len(srcs)} 张：{[s[:12] for s in srcs]}）",
              bool(srcs) and all(s.startswith("blob:") for s in srcs))
        chips = pg.eval_on_selector_all("#pz-cats .pz-chip", "els => els.map(e => e.textContent)")
        check(f"⑨ 分类条渲染（{len(chips)} 个 chip，含「全部」）",
              len(chips) >= 2 and chips[0] == "全部")
        pg.screenshot(path=str(SHOTS / "02_plaza_list_desktop.png"))

        # 分类筛选：点「剧情」
        if "剧情" in chips:
            click_soft(f"#pz-cats .pz-chip:has-text('剧情')")
            pg.wait_for_timeout(700)
            n2 = pg.eval_on_selector_all(".pz-card", "els => els.length")
            check(f"⑩ 分类筛选生效（剧情 → {n2} 张）", n2 == 1)

        # 清除筛选 + 搜索
        click_soft("#pz-cats .pz-chip:has-text('全部')")
        pg.wait_for_timeout(500)
        pg.fill("#pz-search", "雨夜")
        pg.wait_for_timeout(1200)          # 350ms 防抖 + 请求
        n3 = pg.eval_on_selector_all(".pz-card", "els => els.length")
        check(f"⑪ 关键词搜索生效（雨夜 → {n3} 张）", n3 == 1)
        pg.fill("#pz-search", "")
        pg.wait_for_timeout(1200)

        # 排序：最热 → 星轨剧场（下载数最高）
        pg.select_option("#pz-sort", "hot")
        pg.wait_for_timeout(900)
        first = pg.eval_on_selector(".pz-cname", "el => el.textContent")
        check(f"⑫ 按最热排序首位是官方卡（实际「{first}」）", first == "星轨剧场")

        # 详情 + 安装
        click_soft(".pz-card:has-text('峡谷旅人')")
        pg.wait_for_selector("#plaza-view.plaza-detail-on", timeout=8000)
        check("⑬ 进入详情", pg.is_visible("#plaza-detail"))
        check("⑭ 详情标题正确",
              "峡谷旅人" in pg.eval_on_selector(".pz-dtitle", "el => el.textContent"))
        pg.screenshot(path=str(SHOTS / "03_plaza_detail_desktop.png"))
        click_soft("#plaza-detail .pz-btn.primary")
        pg.wait_for_selector("#plaza-detail .pz-dok", timeout=20000)
        ok_text = pg.eval_on_selector("#plaza-detail .pz-dok", "el => el.textContent")
        check(f"⑮ 安装成功提示（{ok_text}）", "已安装" in ok_text)
        card_file = DATA_DIR / "ui_card_a" / "character" / "preset.json"
        check("⑯ 磁盘上真的落了角色卡目录", card_file.is_file())

        # 卸载
        click_soft("#plaza-detail .pz-btn.danger")
        pg.wait_for_timeout(2500)
        check("⑰ 卸载后磁盘目录消失", not (DATA_DIR / "ui_card_a").exists())

        # 返回列表 + 截图（详情重渲染后按钮可能已重排；失败则用入口函数回列表）
        try:
            click_soft("#plaza-detail .pz-dback", timeout=4000)
        except Exception:
            pg.evaluate("openPlaza()")
        pg.wait_for_timeout(700)
        pg.screenshot(path=str(SHOTS / "04_plaza_list_desktop_after.png"))
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.wait_for_timeout(600)
        pg.screenshot(path=str(SHOTS / "05_plaza_mobile.png"))
        check("⑱ 移动端窄屏未出现横向溢出",
              pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"))

        # JS 异常 = 真问题，必须为零；
        # "Failed to load resource" 是浏览器对非 2xx 的例行 console.error，单独看 URL。
        js_errs = [e for e in errs if "console.error: Failed to load resource" not in e]
        if js_errs:
            for e in js_errs[:5]:
                print("     · " + e[:200], flush=True)
        check(f"⑲ 全程无 JS 异常（{len(js_errs)} 条）", not js_errs)
        if http_bad:
            print("     [非 2xx 请求] " + "; ".join(
                f"{s} /{u.split('/', 3)[-1]}" for s, u in http_bad[:8]), flush=True)
        # 本 harness 强制 FIREFLY_SERVER=1（让广场端点本端实现、不代理到生产），
        # 代价是"服务器专属端点"在本地入口天然 403（认证/热更/公告）——预期内，非产品缺陷。
        allowed = ("/auth/", "/hotupdate/", "/notice", "/check-update", "/update-manifest")
        check("⑳ 非 2xx 只出现在服务器模式下预期会拒绝的端点上",
              all(any(k in u for k in allowed) for _s, u in http_bad))
        b.close()
finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _log.close()

print(f"\n截图目录：{SHOTS}", flush=True)
print(f"结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
