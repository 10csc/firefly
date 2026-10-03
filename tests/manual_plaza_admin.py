# -*- coding: utf-8 -*-
"""真实浏览器验证「角色卡广场治理」（P4-3：举报入口 + 管理台界面）。

**手工/按需运行**（同 manual_plaza_ui.py 惯例，不在 tests/test_*.py 自动化套件内）。
用法：`python tests/manual_plaza_admin.py`（本机 8765 空闲时；被占用则退出 3，绝不误杀真实进程）。

它做五件事：
1. 自建沙箱数据根（`user_data/` + `plaza/`），**绝不碰真实 user_data**；
2. 往沙箱广场种 3 张卡：2 张公开 + 1 张**未公开**（pending：公开列表看不到、管理台看得到）；
3. 起一个**本地**服务（`FIREFLY_SERVER=1` ⇒ 广场端点本端实现，**不代理到生产服务器**、
   不产生任何真实网络流量），用 Playwright 走真实路径：
   广场 → 详情 →「举报」→ 选理由 + 补一句 → 提交 → 断言**结果提示**；
4. 举报之后**直查后端**两路取证：
   · HTTP：`fetch /plaza/api/admin/list` → 断言 **403**（这个端点只认 admin，非 admin 拿不到）；
   · 数据：**服务器盘上的 `meta.json`**（权威口径）+ 同一个 store 的 `admin_list()` 投影，
     断言 `report_count` 真的从 0 变 1、理由落盘正确；
5. 管理台：先点一次**不用桩**的入口 → 后端 403 → 断言人话提示；
   再 `pg.route` 桩 `/plaza/api/admin/*`（**桩里的数据取自同一个沙箱 store，不是编的 JSON**）
   → 断言列表渲染出未公开卡与举报数 → 按状态筛选 → 点「下架」→ 关掉管理台
   → **断言该卡从公开广场列表里消失**（不是只看提示文字）→ 再点「恢复」→ 断言它回到公开列表。

为什么 report_count 不用 fetch 直查：`/plaza/api/admin/list` 只认 admin，而本 harness 没有
登录态（本端实现里 `h._bearer_user` 不存在 ⇒ `_current_user()` 恒为 None），该端点**必然 403**。
所以权限事实用 fetch 直查（断言 403），计数事实用**服务器盘上的 meta.json + store 投影**直读
（比走 HTTP 更权威：这就是后端写下去的那一份）。这是刻意的取舍，不是漏测。

退出码：0 全过；1 有断言失败；2 服务没起来；3 端口被占。
"""
import json
import os
import re
import socket as _sk
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765                     # app_config.PORT 硬编码（不读 FIREFLY_PORT）
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_ui_gov_"))
PLAZA_DIR = SANDBOX / "plaza"
DATA_DIR = SANDBOX / "user_data"
SHOTS = SANDBOX / "shots"
for _d in (PLAZA_DIR, DATA_DIR, SHOTS):
    _d.mkdir(parents=True, exist_ok=True)
print("沙箱 USER_DIR:", DATA_DIR, flush=True)
print("沙箱 plaza  :", PLAZA_DIR, flush=True)

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}" + (f"   {extra}" if extra else ""), flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"   {extra}" if extra else ""), flush=True)


# ── 1. 种卡（本进程直接写沙箱广场，不经过 HTTP）─────────────────
# 注意路径语义（app/core/paths.py:24-26）：FIREFLY_ANDROID=1 时
# `USER_DIR = FIREFLY_DATA_DIR / "user_data"` —— 传沙箱根、不是 user_data 本身。
os.environ["FIREFLY_ANDROID"] = "1"
os.environ["FIREFLY_DATA_DIR"] = str(SANDBOX)
os.environ["FIREFLY_PLAZA_DIR"] = str(PLAZA_DIR)
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = DATA_DIR
cfg.CONFIG_FILE = DATA_DIR / "config.json"

from plaza import store as st      # noqa: E402


def solid_png(w, h, rgb):
    """造一张纯色 PNG（**纯 stdlib**：zlib + struct，不引 Pillow）。"""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def _chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8bit truecolor
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


COVERS = {"gov_card_a": (58, 90, 120), "gov_card_b": (120, 84, 58), "gov_card_c": (66, 104, 84)}


def seed(cid, name, char_name, category, tags, status="published", desc=""):
    preset = {"id": cid, "name": name, "char_name": char_name, "user_name": "你",
              "presentation": "sticker", "desc": desc or f"{name} 的一句话简介", "schema": 1}
    files = {"preset.json": json.dumps(preset, ensure_ascii=False).encode(),
             "core.md": f"# {name} 的核心设定".encode(),
             "assets/cover.png": solid_png(320, 180, COVERS.get(cid, (70, 70, 90)))}
    return st.publish_card({"id": cid, "name": name, "char_name": char_name, "user_name": "你",
                            "presentation": "sticker", "desc": preset["desc"],
                            "category": category, "tags": list(tags)},
                           files, status=status, uid_hash="g" * 32, display="某作者")


A = "gov_card_a"      # 公开卡：用来举报
B = "gov_card_b"      # 公开卡：用来下架/恢复
H = "gov_card_hidden"  # 未公开卡（pending）：公开列表看不到

seed(A, "治理卡A", "小甲", "陪伴", ["治愈"], desc="用来举报的卡")
seed(B, "治理卡B", "小乙", "剧情", ["热血"], desc="用来下架的卡")
seed(H, "未公开卡C", "小丙", "其他", ["待审"], status="pending", desc="还在待复核的卡")
print(f"已种卡：{st.stats()['cards']} 张（其中未公开 1 张）", flush=True)

CARD_A_META = PLAZA_DIR / "cards" / A / "meta.json"
CARD_B_META = PLAZA_DIR / "cards" / B / "meta.json"

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
        pg.on("dialog", lambda d: d.accept())        # 下架的确认框
        http_bad = []                                # 记录非 2xx（403 是"非 admin"的预期答复）
        pg.on("response", lambda r: http_bad.append((r.status, r.url)) if r.status >= 400 else None)
        report_posts = []                            # 举报请求体（验证前端发的形状）
        pg.on("request", lambda r: report_posts.append(r.post_data or "")
              if (r.method == "POST" and "/plaza/api/report" in r.url) else None)
        # 跳过引导：① 预置 done 标记；② 引导遮罩会拦截点击 —— **用 CSS 隐藏而不是删节点**
        #   （删节点会让引导自己的后续代码去给已移除的元素挂 onclick，抛假异常）。
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

        def click_soft(sel, timeout=7000):
            """点击；被遮挡时打印真正的拦截者，清掉已知浮层后重试一次（同 manual_plaza_ui）。"""
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

        def wait_for(fn, timeout=8000, step=200):
            """轮询一个可观测条件（DOM/盘上事实），超时返回 False。"""
            t0 = time.time()
            while (time.time() - t0) * 1000 < timeout:
                try:
                    if fn():
                        return True
                except Exception:
                    pass
                pg.wait_for_timeout(step)
            return False

        def public_names():
            return pg.eval_on_selector_all(".pz-cname", "els => els.map(e => e.textContent)")

        def row_texts():
            return pg.eval_on_selector_all(".pza-row", "els => els.map(e => e.innerText)")

        def find_row(name):
            rows = pg.locator(".pza-row")
            for i in range(rows.count()):
                if name in rows.nth(i).inner_text():
                    return i
            return -1

        # ── 列表响应记录器（task-16：断言要有鉴别力）──────────────────
        # 记下每一次 `/plaza/api/list` 响应里的卡名集合；断言分两步：
        #   ★ DOM == 最后一次响应（前端有没有把响应落地）
        #   ★★ 该响应 == 动作后的期望状态（服务端有没有生效）
        list_responses = []          # [[名字…], …] 按到达顺序

        def _on_list_response(r):
            try:
                if "/plaza/api/list" not in r.url:
                    return
                d = r.json()
                list_responses.append([str(x.get("name", "")) for x in (d.get("items") or [])])
            except Exception:
                pass

        pg.on("response", _on_list_response)

        def _await_dom_matches_last_response(min_len, timeout=15000):
            """轮询到"响应数 ≥ min_len 且 DOM 与该响应一致"（不猜时间），返回 (dom, last_resp)。"""
            t0 = time.time()
            while (time.time() - t0) * 1000 < timeout:
                if len(list_responses) >= min_len:
                    last = list_responses[-1]
                    if set(public_names()) == set(last):
                        return public_names(), last
                pg.wait_for_timeout(150)
            return public_names(), (list_responses[-1] if list_responses else [])

        def _dump_list_diag(pzlog):
            """失败时把现场打出来：响应时间线 + 广场内部状态 + 后端此刻的真值。"""
            print(f"     [诊断] list 响应时间线（共 {len(list_responses)} 次）:",
                  list_responses[-6:], flush=True)
            print("     [诊断] 广场内部状态:", pg.evaluate(
                "() => ({loading: _pzS.loading, gen: _pzGen, cards: _pzCards.length,"
                " grid: _pzGrid ? _pzGrid.children.length : -1,"
                " msg: _pzMsg ? _pzMsg.textContent : ''})"), flush=True)
            print("     [诊断] plazaReloadList/_pzLoad 调用记录:", pzlog, flush=True)
            try:
                with urllib.request.urlopen(base + "/plaza/api/list?size=20", timeout=5) as _r:
                    _d = json.loads(_r.read().decode("utf-8"))
                print("     [诊断] 后端此刻的真值:",
                      [(x.get("id"), x.get("status")) for x in _d.get("items", [])], flush=True)
            except Exception as _e:
                print("     [诊断] 直查 list 失败:", _e, flush=True)

        # ── 进入广场（真实点击路径）─────────────────────────────
        phase["n"] = "进入广场"
        entered_real = True
        try:
            pg.click("#cards-manage-btn", timeout=5000)
        except Exception:
            entered_real = False
            pg.evaluate("openCardsView()")
        check("① 首页可进入角色卡管理（真实点击路径）" if entered_real
              else "① 角色卡管理入口存在（点击被遮挡，改用其 onclick 函数）", True)
        click_soft("#cv-plaza-btn")
        pg.wait_for_selector("#plaza-view.show", timeout=10000)
        pg.wait_for_selector(".pz-card", timeout=20000)
        check("② 广场整页打开（#plaza-view.show）", True)
        names = public_names()
        check(f"③ 公开列表只有 2 张公开卡（实际 {names}）",
              set(names) == {"治理卡A", "治理卡B"})
        check("③b 未公开卡不在公开列表里", "未公开卡C" not in names)
        pg.screenshot(path=str(SHOTS / "01_list_desktop.png"))

        # ── 举报 ─────────────────────────────────────────────
        phase["n"] = "举报"
        click_soft(".pz-card:has-text('治理卡A')")
        pg.wait_for_selector("#plaza-view.plaza-detail-on", timeout=8000)
        check("④ 详情页有「举报」入口（.pz-report-btn）", pg.is_visible("#plaza-detail .pz-report-btn"))
        click_soft("#plaza-detail .pz-report-btn")
        pg.wait_for_selector("#pz-modal.show", timeout=8000)
        n_reasons = pg.eval_on_selector_all(".pzr-reason", "els => els.length")
        check(f"⑤ 举报弹层打开且理由齐全（{n_reasons} 个理由）", n_reasons >= 7)
        check("⑤b 没选理由时提交按钮是禁用的（不预选默认理由，避免理由错了白举报）",
              pg.eval_on_selector("#pzr-ok", "el => el.disabled"))
        pg.screenshot(path=str(SHOTS / "02_report_modal.png"))
        click_soft(".pzr-reason:has-text('越狱注入')")
        check("⑤c 选中理由后提交按钮启用，且该理由被标记为选中",
              not pg.eval_on_selector("#pzr-ok", "el => el.disabled")
              and pg.eval_on_selector(".pzr-reason.on", "el => el.textContent") == "越狱注入")
        pg.fill("#pzr-note", "疑似越狱提示词")
        pg.click("#pzr-ok")
        got_done = wait_for(lambda: pg.is_visible("#pzr-done"), timeout=10000)
        done_txt = pg.inner_text("#pzr-done") if got_done else ""
        check(f"⑥ 提交后弹层给出结果提示（{done_txt.splitlines()[0] if done_txt else '无'}）",
              got_done and "已收到" in done_txt)
        pg.screenshot(path=str(SHOTS / "03_report_done.png"))

        payload = {}
        try:
            payload = json.loads(report_posts[-1]) if report_posts else {}
        except Exception:
            payload = {}
        check(f"⑦ 前端真的 POST 了举报（id={payload.get('id')} reason={payload.get('reason')}）",
              payload.get("id") == A and "越狱注入" in str(payload.get("reason"))
              and "疑似越狱提示词" in str(payload.get("reason")))
        check("⑦b 举报请求只带了 id/reason 两个字段",
              set(payload.keys()) == {"id", "reason"})

        # 直查后端 ①：HTTP 层面 —— 治理列表只认 admin
        admin_status = pg.evaluate(
            "async () => { const r = await fetch('/plaza/api/admin/list?status=all');"
            " return r.status; }")
        check(f"⑧ fetch 直查 /plaza/api/admin/list 被拒（HTTP {admin_status}，非 admin 拿不到治理数据）",
              admin_status == 403)
        # 直查后端 ②：数据层面 —— 服务器盘上那一份 meta.json（权威口径）
        meta = json.loads(CARD_A_META.read_text("utf-8"))
        reports = meta.get("reports") or []
        check(f"⑨ 服务器盘上真的落了 1 条举报（reports={len(reports)}）", len(reports) == 1)
        check("⑨b 落盘的举报理由正确",
              bool(reports) and "越狱注入" in str(reports[0].get("reason"))
              and "疑似越狱提示词" in str(reports[0].get("reason")))
        proj = [x for x in st.admin_list(statuses=None, page=1, size=20)["items"] if x["id"] == A]
        check(f"⑩ store 投影里 report_count = {proj[0].get('report_count') if proj else '无'}",
              bool(proj) and proj[0].get("report_count") == 1)
        click_soft("#pz-modal .pz-btn.primary")          # 「知道了」
        check("⑩b 结果提示可关闭", not pg.is_visible("#pz-modal.show"))

        # 回列表（详情页里头部是不可点的，先退详情）
        click_soft("#plaza-detail .pz-dback", timeout=5000)
        pg.wait_for_timeout(400)

        # ── 管理台：非 admin 的真实 403（不用任何桩）──────────────
        phase["n"] = "管理台403"
        click_soft("#pz-admin-btn")
        pg.wait_for_selector("#plaza-admin.show", timeout=8000)
        ok403 = wait_for(lambda: "管理员" in pg.inner_text("#pza-msg"), timeout=8000)
        check(f"⑪ 非 admin 进管理台 → 403 人话提示（{pg.inner_text('#pza-msg').strip()[:40]}）", ok403)
        check("⑪b 权限不足时一条卡片行都不渲染（不拿假数据糊弄）",
              pg.eval_on_selector_all(".pza-row", "els => els.length") == 0)
        pg.screenshot(path=str(SHOTS / "04_admin_403.png"))

        # ── 管理台：桩 /plaza/api/admin/*（数据取自同一个沙箱 store，不是编的）──
        admin_calls = []

        def _admin_list_route(route):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
            raw = (q.get("status") or ["all"])[0]
            statuses = None if raw in ("", "all") else tuple(s for s in raw.split(",") if s)
            data = st.admin_list(statuses=statuses,
                                 page=(q.get("page") or ["1"])[0],
                                 size=(q.get("size") or ["20"])[0])
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(data, ensure_ascii=False))

        def _admin_status_route(route):
            body = json.loads(route.request.post_data or "{}")
            cid = str(body.get("id") or "")
            status = str(body.get("status") or "")
            admin_calls.append((cid, status))
            ok = st.valid_card_id(cid) and status in st.STATUSES and st.set_status(cid, status)
            if ok:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "id": cid, "status": status},
                                              ensure_ascii=False))
            else:
                route.fulfill(status=404, content_type="application/json",
                              body='{"ok": false, "error": "卡片不存在"}')

        pg.route(re.compile(r".*/plaza/api/admin/list.*"), _admin_list_route)
        pg.route(re.compile(r".*/plaza/api/admin/status.*"), _admin_status_route)

        phase["n"] = "管理台列表"
        pg.click("#pza-refresh-btn")
        ok_rows = wait_for(lambda: pg.eval_on_selector_all(".pza-row", "els => els.length") == 3,
                           timeout=10000)
        rows = row_texts()
        check(f"⑫ 管理台渲染出全部 3 张卡（含未公开的）", ok_rows, f"rows={len(rows)}")
        check("⑬ 未公开卡C 出现在管理台（公开广场看不到它）",
              any("未公开卡C" in t for t in rows))
        row_a = next((t for t in rows if "治理卡A" in t), "")
        check("⑭ 举报数渲染在位（治理卡A → 举报 1 次）", "举报 1 次" in row_a, row_a.splitlines()[0][:40])
        check("⑭b 举报理由渲染在位", "越狱注入" in row_a)
        check("⑭c 审核结论栏在位", any("审核：" in t for t in rows))
        check("⑭d 已发布状态徽标在位", "已发布" in row_a)
        pg.screenshot(path=str(SHOTS / "05_admin_rows.png"))

        # ── 按状态筛选 ────────────────────────────────────────
        phase["n"] = "管理台筛选"
        click_soft("#pza-filters .pz-chip:has-text('待复核')")
        ok_one = wait_for(lambda: pg.eval_on_selector_all(".pza-row", "els => els.length") == 1,
                          timeout=8000)
        rows2 = row_texts()
        check(f"⑮ 按「待复核」筛选只剩未公开卡C（{len(rows2)} 行）",
              ok_one and len(rows2) == 1 and "未公开卡C" in rows2[0])
        click_soft("#pza-filters .pz-chip:has-text('全部')")
        wait_for(lambda: pg.eval_on_selector_all(".pza-row", "els => els.length") == 3, timeout=8000)

        # ── 下架：关键断言是"公开列表里真的没了"────────────────
        phase["n"] = "下架"
        pg.evaluate("""() => {
            window.__pzCallLog = [];
            const o = window._pzLoad;
            window._pzLoad = function (reset) {
                window.__pzCallLog.push({fn: '_pzLoad', reset: !!reset, gen: _pzGen,
                                         loading: _pzS.loading,
                                         show: _pzRoot.classList.contains('show')});
                return o.apply(this, arguments);
            };
            const rl = window.plazaReloadList;
            window.plazaReloadList = function () {
                window.__pzCallLog.push({fn: 'plazaReloadList',
                                         show: _pzRoot.classList.contains('show')});
                return rl.apply(this, arguments);
            };
        }""")
        i_b = find_row("治理卡B")
        check("⑯ 管理台里找得到治理卡B", i_b >= 0)
        pg.locator(".pza-row").nth(i_b).locator(".pza-arch").click()
        ok_arch = wait_for(lambda: "已归档" in (row_texts()[find_row("治理卡B")]
                                                if find_row("治理卡B") >= 0 else ""), timeout=10000)
        check("⑰ 下架后管理台里该卡变「已归档」", ok_arch)
        check(f"⑰b 下架真的打到 admin/status（{admin_calls}）", (B, "archived") in admin_calls)
        check("⑰c 磁盘上它的状态真的是 archived",
              json.loads(CARD_B_META.read_text("utf-8")).get("status") == "archived")
        pg.screenshot(path=str(SHOTS / "06_admin_after_archive.png"))

        click_soft("#plaza-admin .cv-back")              # 回公开列表
        # 诊断钩子（只在本脚本里用）：记录下架前后 plazaReloadList/_pzLoad 到底有没有被调用
        _pzlog = pg.evaluate("() => window.__pzCallLog || null")
        # ⚠ 断言要有**鉴别力**（task-16）：原来是"列表里没有 B"——若页内还留着下架前的两张卡，
        #   这条会恒真（假通过），且分不清"响应旧"与"DOM 没落地"。现在分两步报：
        #   ⑱★ DOM == 最后一次 /plaza/api/list 响应（前端落地了吗）
        #   ⑱★★ 该响应 == 动作后的期望状态（服务端生效了吗）
        _n0 = len(list_responses)
        dom_a, resp_a = _await_dom_matches_last_response(_n0, timeout=15000)
        ok_dom_a = bool(resp_a) and set(dom_a) == set(resp_a)
        check(f"⑱★ DOM == 最后一次 /plaza/api/list 响应（前端落地）dom={dom_a} resp={resp_a}", ok_dom_a)
        check(f"⑱★★ 该响应 == 下架后的期望状态（服务端生效，期望只有 治理卡A）resp={resp_a}",
              set(resp_a) == {"治理卡A"})
        check("⑱b 其它公开卡还在（不是把整页清空）", "治理卡A" in dom_a)
        if not (ok_dom_a and set(resp_a) == {"治理卡A"}):
            _dump_list_diag(_pzlog)
        pg.screenshot(path=str(SHOTS / "07_public_after_archive.png"))

        # ── 恢复 ─────────────────────────────────────────────
        phase["n"] = "恢复"
        click_soft("#pz-admin-btn")
        wait_for(lambda: pg.eval_on_selector_all(".pza-row", "els => els.length") == 3, timeout=10000)
        i_b2 = find_row("治理卡B")
        check("⑲ 管理台里仍有治理卡B（已归档）", i_b2 >= 0)
        pg.locator(".pza-row").nth(i_b2).locator(".pza-restore").click()
        wait_for(lambda: "已发布" in (row_texts()[find_row("治理卡B")]
                                      if find_row("治理卡B") >= 0 else ""), timeout=10000)
        check(f"⑳ 恢复打到 admin/status（{admin_calls}）", (B, "published") in admin_calls)
        click_soft("#plaza-admin .cv-back")
        # 同样分两步报（鉴别力）：DOM 是否落地 vs 响应是否已生效
        _n1 = len(list_responses)
        dom_b, resp_b = _await_dom_matches_last_response(_n1, timeout=15000)
        ok_dom_b = bool(resp_b) and set(dom_b) == set(resp_b)
        check(f"㉑★ DOM == 最后一次 /plaza/api/list 响应（前端落地）dom={dom_b} resp={resp_b}", ok_dom_b)
        check(f"㉑★★ 该响应 == 恢复后的期望状态（期望 治理卡A + 治理卡B）resp={resp_b}",
              set(resp_b) == {"治理卡A", "治理卡B"})
        if not (ok_dom_b and set(resp_b) == {"治理卡A", "治理卡B"}):
            _dump_list_diag(pg.evaluate("() => window.__pzCallLog || null"))

        # ── 移动端（管理台与广场都不能横向溢出）──────────────
        phase["n"] = "移动端"
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.wait_for_timeout(600)
        pg.screenshot(path=str(SHOTS / "08_mobile_list.png"))
        check("㉒ 移动端窄屏广场未出现横向溢出",
              pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"))
        click_soft("#pz-admin-btn")
        wait_for(lambda: pg.eval_on_selector_all(".pza-row", "els => els.length") == 3, timeout=10000)
        pg.screenshot(path=str(SHOTS / "09_mobile_admin.png"))
        check("㉓ 移动端窄屏管理台未出现横向溢出",
              pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"))

        # ── 异常收口 ─────────────────────────────────────────
        phase["n"] = "收尾"
        js_errs = [e for e in errs if "console.error: Failed to load resource" not in e]
        if js_errs:
            for e in js_errs[:5]:
                print("     · " + e[:200], flush=True)
        check(f"㉔ 全程无 JS 异常（pageerror / console.error：{len(js_errs)} 条）", not js_errs)
        if http_bad:
            print("     [非 2xx 请求] " + "; ".join(
                f"{s} /{u.split('/', 3)[-1]}" for s, u in http_bad[:8]), flush=True)
        allowed = ("/auth/", "/hotupdate/", "/notice", "/check-update", "/update-manifest",
                   "/plaza/api/admin/")
        check("㉕ 非 2xx 只出现在预期会拒绝的端点上（认证/热更/公告/非 admin 的管理端点）",
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
