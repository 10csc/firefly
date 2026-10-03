# -*- coding: utf-8 -*-
"""真浏览器最小冒烟：**角色卡平台页**（task-22）——服务器只做后台，平台独立可跑。

**手工/按需运行**（同 manual_plaza_* / manual_login_page.py 惯例，不在 tests/test_*.py 套件内）。
用法：`python tests/manual_platform_smoke.py`（默认 8765；被占则退出 3，绝不误杀真实进程；
`FIREFLY_TEST_PORT` 只是"队友正占着 8765"时的逃生口 —— 本脚本是**纯静态页 + 桩 API**，换端口不影响被测行为）。

做法：静态 http.server 起在 **server/frontend/**（平台页/登录页/管理台都是纯静态页），
API 全部用 Playwright 路由打桩（零外网、零真实数据）。两条路径各跑一遍：
  A. **未登录**（/auth/me → 401）⇒ 平台页必须落到 `login.html`，且零 pageerror；
  B. **已登录**（/auth/me → 200）⇒ 广场容器真的渲染、导航四个 tab 可用、零 pageerror、
     **DOM 里 0 个 App 功能入口**、平台 bundle 里 0 个 App 壳模块。
外加平台自身的样式断言（bg-sunset 背景 + 固定头部 + 广场栅格）与**平台 bundle 体积**。
"""
import functools
import http.server
import io
import os
import re
import socket as _sk
import socketserver
import sys
import threading
import time
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "server" / "frontend"
SHOTS = ROOT / "_harden" / "shots"
PORT = int(os.environ.get("FIREFLY_TEST_PORT", "8765"))

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
_p = _sk.socket(); _p.settimeout(0.6)
try:
    _p.connect(("127.0.0.1", PORT))
    print(f"!! 端口 {PORT} 被占用（可能是用户正在跑的实例）——放弃，避免误杀真实进程", flush=True)
    sys.exit(3)
except OSError:
    pass
finally:
    _p.close()


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


handler = functools.partial(_Handler, directory=str(FRONT))
httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{PORT}"
time.sleep(0.4)
SHOTS.mkdir(parents=True, exist_ok=True)
print("静态服务已就绪（根 = server/frontend/）:", base, flush=True)

# ── 平台 bundle 的结构与体积（发布方要看到的数字）────────────────
BUNDLE_FP = FRONT / "platform.bundle.js"
bundle_text = BUNDLE_FP.read_text(encoding="utf-8") if BUNDLE_FP.is_file() else ""
bundle_kb = len(bundle_text.encode("utf-8")) / 1024
sections = re.findall(r"/\* ── 来源：([^ ]+) ── \*/", bundle_text)
APP_BUNDLE_KB = (ROOT / "app" / "static" / "js" / "bundle.js").stat().st_size / 1024
print(f"\n平台 bundle: {bundle_kb:.1f} KB（App bundle {APP_BUNDLE_KB:.1f} KB）；"
      f"模块 {len(sections)} 个", flush=True)
check(f"平台 bundle 体积 {bundle_kb:.1f} KB < App 的一半（{APP_BUNDLE_KB / 2:.1f} KB）",
      bundle_kb < APP_BUNDLE_KB / 2, f"{bundle_kb:.1f} KB")
check(f"平台 bundle 只含 15 个模块（3 基础 + 敏感头加密 + 10 广场域 + 平台入口）：{len(sections)}",
      len(sections) == 15, str(sections))
for bad in ("panels/data", "panels/debug", "panels/packs", "panels/stickers", "panels/pack_tree",
            "panels.js", "chat_media", "chat_render", "settings.js", "voice_plugin", "diag.js",
            "hotupdate.js", "notice.js", "relay.js", "guide.js", "fix.js", "state.js", "api.js"):
    check(f"平台 bundle 不含 App 模块 {bad}", bad not in sections)

# App 功能入口标识符（DOM id：平台页里必须一个都查不到）
APP_ONLY_IDS = ("app", "chat-view", "menu-btn", "messages", "cards-view", "pack-view",
                "pv-tree", "settings-panel", "voice-view", "fix-view", "guide-mask",
                "history-view", "debug-panel")

def _wire_errs(pg):
    """收集 pageerror / console.error，并记录所有 ≥400 的网络响应（归因用）。"""
    errs, net_bad = [], []

    def _resp(r):
        try:
            if r.status >= 400:
                net_bad.append((r.status, r.url))
        except Exception:
            pass

    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console.error: " + m.text)
          if m.type == "error" else None)
    pg.on("response", _resp)
    return errs, net_bad


def _unexpected(errs, net_bad):
    """剔除"刻意桩出来的非 2xx"与"静态沙箱里必然缺的 App 资产"后的真错误。

    · 401/403 是脚本自己的桩（登录探测、非管理员治理台）；Chromium 会为它们打一条
      "Failed to load resource" 的 console.error —— 那是**预期**，不是页面 bug；
    · `assets/StarRailFont.ttf` 只有**登录页**引用（App 的字体），静态沙箱只服务
      server/frontend/ 所以 404；服务器上 `/assets/` 由 resolve_asset 正常下发。
    判据：先算出"非预期网络响应"（必须为空）；console.error 只在**它对应的状态码
    确实来自白名单 URL**时才被豁免——否则一个真 404 会被同状态码的桩掩盖。"""
    benign = ("/auth/me", "/plaza/api/admin", "StarRailFont.ttf", "favicon.ico")
    unexpected_net = [x for x in net_bad if not any(b in x[1] for b in benign)]
    benign_status = {st for st, url in net_bad if any(b in url for b in benign)}
    out = []
    for e in errs:
        m = re.search(r"status of (\d+)", e)
        if m and int(m.group(1)) in benign_status:
            continue
        out.append(e)
    return out, unexpected_net


try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()

        # ══ A. 未登录：/auth/me → 401 ⇒ 必须落到 login.html ══
        print("\n== A. 未登录访问平台页 ==", flush=True)
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.set_default_timeout(15000)
        errs_a, bad_a = _wire_errs(pg)
        pg.route("**/auth/me", lambda route: route.fulfill(
            status=401, content_type="application/json", body='{"error":"未登录"}'))
        pg.goto(base + "/platform.html", wait_until="domcontentloaded")
        try:
            pg.wait_for_url("**/login.html", timeout=8000)
        except Exception:
            pass
        check("A1 未登录 → 自动落到 login.html", pg.url.endswith("/login.html"), pg.url)
        real_a, real_bad_a = _unexpected(errs_a, bad_a)
        check("A2 全程零 pageerror / 零意外错误", not real_a and not real_bad_a,
              f"{real_a[:3]} {real_bad_a[:3]}")
        pg.close()

        # ══ B. 已登录：广场必须真的渲染；DOM 里 0 个 App 入口 ══
        print("\n== B. 已登录访问平台页 ==", flush=True)
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.set_default_timeout(15000)
        errs, bad = _wire_errs(pg)
        # 预置登录 token：验证平台自己的 fetch 包装真的把凭证带上（且优先走加密头）
        pg.add_init_script("try{localStorage.setItem('firefly_token','tok-e2e');}catch(e){}")
        seen_headers = {}
        # 桩：登录态 + 广场列表（空）+ 已装配额 + 治理台（403：非管理员走人话提示路径）
        def _me_route(route):
            seen_headers.update({str(k).lower(): v for k, v in route.request.headers.items()})
            route.fulfill(status=200, content_type="application/json",
                          body='{"email":"e2e@qq.com","role":"user","created_at":1}')

        pg.route("**/auth/me", _me_route)
        pg.route("**/plaza/api/list*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"items":[],"total":0,"categories":[],"tags":[]}'))
        pg.route("**/plaza/api/installed*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"items":[],"used_cards":0,"used_bytes":0}'))
        pg.route("**/plaza/api/drafts*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"ok":true,"items":[]}'))
        pg.route("**/plaza/api/admin*", lambda route: route.fulfill(
            status=403, content_type="application/json", body='{"ok":false,"error":"需要管理员权限"}'))

        pg.goto(base + "/platform.html", wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)

        check("B1 停在平台页（未被踢去登录）", pg.url.endswith("/platform.html"), pg.url)
        # 平台自己的 fetch 包装：必须把登录态带上；敏感头加密生效时**不留明文 Authorization**
        # （与 App 的 api.js 同款口径；服务器两种都收，加密失败退回明文不算失败）
        _hl = seen_headers
        check("B1b 平台页请求确实带上了登录凭证（加密头或明文 Bearer）",
              ("x-firefly-enc" in _hl) or (_hl.get("authorization") == "Bearer tok-e2e"),
              str({k: (v[:24] + "…" if len(v) > 24 else v) for k, v in _hl.items() if k in
                   ("authorization", "x-firefly-enc")}))
        check("B1c 加密生效时明文 Authorization 已剔除（同一次请求不双发凭证）",
              not ("x-firefly-enc" in _hl and "authorization" in _hl),
              str(list(_hl.keys())))
        check("B2 头部渲染 + 显示登录邮箱",
              pg.evaluate("(document.querySelector('#pf-shell-user')||{}).textContent") == "e2e@qq.com")
        check("B3 平台导航四个 tab（广场/制卡/我的卡/治理台）",
              pg.evaluate("Array.from(document.querySelectorAll('.pf-tab')).map(b=>b.textContent).join('/')")
              == "广场/制卡/我的卡/治理台",
              pg.evaluate("Array.from(document.querySelectorAll('.pf-tab')).map(b=>b.textContent).join('/')"))
        check("B4 广场面板可见（#plaza-view.show）",
              pg.evaluate("!!document.querySelector('#plaza-view.show')"))
        check("B5 广场列表容器与筛选条在位",
              pg.evaluate("!!document.querySelector('#plaza-list') && !!document.querySelector('#pz-cats')"
                          " && !!document.querySelector('#plaza-grid')"))
        check("B6 广场空态给人话（没有卡时不静默空白）",
              "还没有" in pg.evaluate("(document.querySelector('#plaza-msg')||{}).textContent || ''"),
              pg.evaluate("(document.querySelector('#plaza-msg')||{}).textContent"))
        check("B7 App 里没有的平台头部/导航真的只有平台有（#pf-shell-head 固定定位）",
              pg.evaluate("getComputedStyle(document.getElementById('pf-shell-head')).position") == "fixed")

        # 样式：晚霞背景 + 广场栅格 + 面板不被头部盖住
        bg = pg.evaluate("() => getComputedStyle(document.body).backgroundImage")
        check("B8 平台页用的是 bg-sunset 那套背景（含色标 #9a559a）",
              "154, 85, 154" in bg or "154,85,154" in bg, bg[:60])
        check("B9 广场栅格样式已生效（display:grid）",
              pg.evaluate("getComputedStyle(document.getElementById('plaza-grid')).display") == "grid")
        check("B10 广场面板让出头部高度（top = 头部高）",
              pg.evaluate("getComputedStyle(document.getElementById('plaza-view')).top") == "52px",
              pg.evaluate("getComputedStyle(document.getElementById('plaza-view')).top"))
        # 平台 CSS 是从 App style.css **按选择器抽**的：两个基础模块自己的样式不在 plaza 前缀下，
        # 最容易漏（漏了不报错、只是难看）——这里用**计算样式**把它们钉住。
        check("B10b 增强下拉生效（原生 select 被隐藏 ui-select-native）",
              pg.evaluate("getComputedStyle(document.getElementById('pz-sort')).opacity") == "0",
              pg.evaluate("getComputedStyle(document.getElementById('pz-sort')).opacity"))
        check("B10c toast 样式在平台 CSS 里（#app-toast 固定定位；plaza 有 30 处 showToast）",
              pg.evaluate("""() => {
                  const d = document.createElement('div');
                  d.id = 'app-toast'; document.body.appendChild(d);
                  const pos = getComputedStyle(d).position;
                  d.remove(); return pos;
              }""") == "fixed",
              pg.evaluate("""() => {
                  const d = document.createElement('div');
                  d.id = 'app-toast'; document.body.appendChild(d);
                  const pos = getComputedStyle(d).position;
                  d.remove(); return pos;
              }"""))

        # App 功能入口：DOM 里必须 0 命中
        hits = [i for i in APP_ONLY_IDS if pg.evaluate(f"!!document.getElementById({i!r})")]
        check(f"B11 DOM 里 0 个 App 功能入口（命中 {hits}）", not hits, str(hits))
        html = pg.evaluate("document.documentElement.outerHTML")
        for needle in ("打开服务器版", "角色卡管理", "聊天", "设置", "记忆", "语音插件"):
            check(f"B12 页面文本不含 App 功能字样 {needle!r}", needle not in html)

        # 导航功能：制卡 / 我的卡 / 治理台都真的能到
        pg.evaluate("document.querySelector('.pf-tab[data-pf=\"forge\"]').click()")
        pg.wait_for_timeout(700)
        check("B13 点「制卡」→ 制卡层打开（#plaza-view.plaza-forge-on）",
              pg.evaluate("!!document.querySelector('#plaza-view.plaza-forge-on')"))
        check("B14 制卡页真的建出表单（#pf-body 有内容）",
              pg.evaluate("(document.getElementById('pf-body')||{}).children.length") > 0)
        # ★ 制卡两列布局（`.pfc-*`）必须抽进 platform.css：2026-10-01 抽漏它 ⇒ 表单被撑到 4696px、
        #   审核弹窗落到表单后面点不到（假 DOM 测试看不见，真浏览器 e2e 才抓到）。
        check("B14b 制卡两列布局生效（.pfc-cols 绝对定位铺满面板）",
              pg.evaluate("(() => { const c = document.querySelector('.pfc-cols');"
                          " return c ? getComputedStyle(c).position : 'missing'; })()") == "absolute",
              pg.evaluate("(() => { const c = document.querySelector('.pfc-cols');"
                          " return c ? getComputedStyle(c).position : 'missing'; })()"))
        check("B14c 制卡层高度收敛在视口内（长表单由 .pfc-pane 内部滚，不顶出超长页面）",
              pg.evaluate("document.getElementById('plaza-forge').getBoundingClientRect().height")
              <= pg.evaluate("window.innerHeight") + 4,
              str(pg.evaluate("document.getElementById('plaza-forge').getBoundingClientRect().height")))
        pg.screenshot(path=str(SHOTS / "13_platform_forge.png"))
        pg.evaluate("document.querySelector('.pf-tab[data-pf=\"mine\"]').click()")
        pg.wait_for_timeout(700)
        check("B15 点「我的卡」→ 草稿箱容器在位（#pf-drafts-box）",
              pg.evaluate("!!document.getElementById('pf-drafts-box')"))
        pg.evaluate("document.querySelector('.pf-tab[data-pf=\"admin\"]').click()")
        pg.wait_for_timeout(800)
        check("B16 点「治理台」→ 管理台层打开（#plaza-admin 有内容；非管理员走人话提示）",
              pg.evaluate("(document.getElementById('plaza-admin')||{}).children.length") > 0)
        pg.evaluate("document.querySelector('.pf-tab[data-pf=\"plaza\"]').click()")
        pg.wait_for_timeout(700)
        check("B17 点「广场」→ 回到列表，且制卡/治理台浮层都已收（导航是切换不是叠加）",
              not pg.evaluate("!!document.querySelector('#plaza-view.plaza-forge-on')")
              and not pg.evaluate("!!document.querySelector('#plaza-view.plaza-admin-on')"))

        # ★ 筛选行布局守卫（用户 2026-10-01 截图报障：搜索框塌成 ~26px 窄条）。
        #   根因是 ui_select 增强下拉的 `.ui-select-btn{width:100%}` 在 flex 行里吃满整行；
        #   这里用**计算样式 + 几何**在**桌面与手机两个视口**各钉一次（"看着对、算起来错"这类问题）。
        MEASURE = """() => {
            const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect();
                return {x: Math.round(b.x), w: Math.round(b.width)}; };
            const inp = r(document.querySelector('#pz-search'));
            const btn = r(document.querySelector('.pz-search-row .ui-select-btn'));
            const sel = document.querySelector('#pz-sort');
            const overlap = (a, b) => (a && b) ? !(a.x + a.w <= b.x || b.x + b.w <= a.x) : false;
            return {input: inp, enhanced: btn, overlap: overlap(inp, btn),
                    sortHidden: sel ? getComputedStyle(sel).opacity === '0' : false};
        }"""
        for w, h in ((1280, 900), (390, 844)):
            pg.set_viewport_size({"width": w, "height": h})
            pg.wait_for_timeout(450)
            m = pg.evaluate(MEASURE)
            check(f"B19 [{w}px] 搜索框拿到剩余宽度（≥120px；实际 {m['input']['w'] if m['input'] else 'N/A'}）",
                  bool(m["input"]) and m["input"]["w"] >= 120, str(m["input"]))
            check(f"B19b [{w}px] 搜索框与增强下拉不重叠（下拉固定宽 {m['enhanced']['w'] if m['enhanced'] else 'N/A'}px）",
                  bool(m["enhanced"]) and not m["overlap"], str(m))
            check(f"B19c [{w}px] 原生 select 已被增强层隐藏", bool(m["sortHidden"]), str(m))

        # ══ B20/B21 制卡页两条 UI 守卫（2026-10-01 用户手机截图）══
        #   B20 提示条（#pf-msg）不得与「我的草稿箱」标题**相交** —— 用户截图里两者糊在一起；
        #   B21 未发布的卡不得出现「下架」按钮 —— 截图里新卡也带「下架」。
        pg.evaluate("window.openPlazaForge && window.openPlazaForge()")
        pg.wait_for_timeout(900)
        pg.fill("#pf-name", "")                 # 制造一条人话提示（"卡名不能为空。"）
        pg.click("#pf-save-btn")
        pg.wait_for_timeout(500)
        RECTS = """() => {
            const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect();
                return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height)}; };
            const inter = (a, b) => { if (!a || !b || !a.w || !b.h) return false;
                return !(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y); };
            const msg = document.getElementById('pf-msg');
            const title = document.querySelector('#pf-drafts-box .pf-box-title');
            const unp = document.getElementById('pf-unpublish-btn');
            // ★ 槽位按钮（选择图片/清除）与表情包删除 ×：都必须与草稿箱标题**不相交**
            const slotBtns = Array.from(document.querySelectorAll('.pfi-bar .pf-btn, .pfi-stkdel'));
            const hits = slotBtns.filter((e) => inter(r(e), r(title))).map((e) => e.id || e.className);
            // ★ 层级检查（Lead 2026-10-02 要求）：**在屏且未被裁掉**的按钮，其中心点 elementFromPoint
            //   必须命中它自己或其后代 —— 几何断言抓不到"被别的层压住/压住别人"的层叠问题。
            const form = document.getElementById('pf-form');
            const fb = form ? form.getBoundingClientRect() : null;
            const vis = (e) => { const b = e.getBoundingClientRect();
                if (!b.width || !b.height) return false;
                if (!fb) return false;
                return b.x + b.width > fb.x && b.x < fb.x + fb.width
                    && b.y + b.height > fb.y && b.y < fb.y + fb.height; };   // 与滚动框相交 = 可能可见
            const badHit = [];
            for (const e of slotBtns) {
                if (!vis(e)) continue;                       // 滚出视野的跳过（点会落在别处，不是问题）
                const b = e.getBoundingClientRect();
                const x = Math.round(b.x + b.width / 2), y = Math.round(b.y + b.height / 2);
                const top = document.elementFromPoint(x, y);
                if (!top || !(top === e || e.contains(top))) {
                    badHit.push((e.id || e.className) + '→' + (top ? (top.id || top.className) : 'null'));
                }
            }
            const pv = document.getElementById('plaza-view');
            return {msg: r(msg), title: r(title), overlap: inter(r(msg), r(title)),
                    slotHits: hits, slotCount: slotBtns.length, badHit: badHit,
                    forgeBackdrop: pv ? getComputedStyle(pv).backdropFilter : 'missing',
                    msgShown: msg ? getComputedStyle(msg).display !== 'none' : false,
                    unpDisplay: unp ? getComputedStyle(unp).display : 'missing',
                    unpVisible: unp ? (unp.getBoundingClientRect().width > 0) : false};
        }"""
        # ★ 视口集合（含**用户真机** 1264×2780 @DPR3.5 ⇒ 361×794；Lead 实测）：
        #   361×794 / 360×794 / 393×864 / 393×873 / 360×780 / 412×915 / 393×500（键盘弹出）/ 390×844 / 1280×900
        for w, h in ((361, 794), (360, 794), (393, 864), (393, 873), (360, 780), (412, 915),
                     (393, 500), (390, 844), (1280, 900)):
            pg.set_viewport_size({"width": w, "height": h})
            pg.wait_for_timeout(500)
            g = pg.evaluate(RECTS)
            check(f"B20 [{w}x{h}] 提示条与「我的草稿箱」标题**不相交**"
                  f"（msg={g['msg']} title={g['title']}）",
                  g["msgShown"] and not g["overlap"], str(g))
            check(f"B20b [{w}x{h}] 槽位按钮/表情包×（{g['slotCount']} 个）与标题**不相交**",
                  not g["slotHits"], str(g["slotHits"]))
            check(f"B20c [{w}x{h}] 层级：可见槽位按钮中心 `elementFromPoint` 命中自己"
                  f"（违例={g['badHit']}）", not g["badHit"], str(g))
            check(f"B21 [{w}x{h}] 新卡（未发布）**没有**「下架」按钮（display={g['unpDisplay']}）",
                  g["unpDisplay"] in ("none", "missing") and not g["unpVisible"], str(g))
        pg.screenshot(path=str(SHOTS / "14_forge_guards.png"))

        pg.screenshot(path=str(SHOTS / "13_platform.png"))
        real, real_bad = _unexpected(errs, bad)
        if real or real_bad:
            print("  诊断: console/pageerror =", real[:5], flush=True)
            print("  诊断: 非预期网络响应 =", real_bad[:5], flush=True)
        check("B18 全程零 pageerror / 零意外错误", not real and not real_bad,
              f"{real[:3]} {real_bad[:3]}")
        b.close()
finally:
    httpd.shutdown()
    httpd.server_close()

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
print("判定:", "PASS" if FAIL == 0 else "FAIL", flush=True)
sys.exit(1 if FAIL else 0)
