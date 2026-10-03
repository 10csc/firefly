# -*- coding: utf-8 -*-
"""真实浏览器验证「角色卡制作（共创）」整链：填表 → 存草稿 → 提交审核 → 发布 → 广场可见。

**手工/按需运行**（同 manual_plaza_ui.py 惯例，不在 tests/test_*.py 自动化套件内）。
用法：`python tests/manual_plaza_forge.py`（本机 8765 空闲时；占用则直接退出，绝不误杀真实进程）。

为什么必须有它（docs/错误总结.md #19）：假 DOM 与静态守卫全绿的代码，
在真浏览器里可能"一张封面都没加载/一个草稿都没出现"而零报错。本脚本断言的是
**可观测结果**：草稿文件真的落在磁盘上、广场列表里真的多出这张卡、审核理由真的渲染成节点。

它做四件事：
1. 自建沙箱数据根（`user_data/` + `plaza/`），**绝不碰真实 user_data**；
2. 起一个**本地**服务（`FIREFLY_SERVER=1` ⇒ 广场端点本端实现，不代理生产、零真实流量），
   再用 Playwright 走真实路径：首页 → 角色卡管理 → 角色卡广场 →「✦ 制作角色卡」；
3. 审核端点用 `pg.route` **桩成 pass**（摘要由本进程用后端同一个函数算出来，
   保证发布闸门"审核摘要 == 卡体摘要"真的被走通）——**绝不用真实 Key、绝不产生费用**；
4. 全程收集 pageerror/console.error 与所有 console 文本，断言 Key 没有以任何形式外泄
   （不进存储、不进控制台），并截图存档。

退出码：0 全过；1 有断言失败；2 服务没起来；3 端口被占。
"""
import base64
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
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_ui_forge_"))
PLAZA_DIR = SANDBOX / "plaza"
DATA_DIR = SANDBOX / "user_data"
SHOTS = SANDBOX / "shots"
for _d in (PLAZA_DIR, DATA_DIR, SHOTS):
    _d.mkdir(parents=True, exist_ok=True)
print("沙箱 USER_DIR:", DATA_DIR, flush=True)
print("沙箱 plaza  :", PLAZA_DIR, flush=True)

PASS = FAIL = 0
FAKE_SK = "sk-manualtestonly0000000000000000"      # 形态合法、绝不会被真的用出去


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}" + (f"   [{extra}]" if extra else ""), flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"   [{extra}]" if extra else ""), flush=True)


# ── 1. 沙箱环境（**必须在 import 后端模块之前**设好）────────────
# 路径语义同 manual_plaza_ui.py：FIREFLY_ANDROID=1 时 USER_DIR = FIREFLY_DATA_DIR/"user_data"
os.environ["FIREFLY_ANDROID"] = "1"
os.environ["FIREFLY_DATA_DIR"] = str(SANDBOX)
os.environ["FIREFLY_PLAZA_DIR"] = str(PLAZA_DIR)
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg    # noqa: E402

cfg.USER_DIR = DATA_DIR
cfg.CONFIG_FILE = DATA_DIR / "config.json"

from plaza import api as plaza_api          # noqa: E402
from plaza import card_format as cf         # noqa: E402

print("沙箱已就绪（未接触真实 user_data）", flush=True)


def solid_png(w, h, rgb):
    """造一张纯色 PNG（纯 stdlib：zlib + struct，不引 Pillow）。"""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def _chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


COVER_FILE = SANDBOX / "cover.png"
COVER_FILE.write_bytes(solid_png(320, 180, (58, 90, 120)))

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

# ★ 兜底收尾：脚本被 Ctrl-C / 断言抛出 / 解释器异常退出时也必须把本进程起的服务收掉，
#   否则它会继续占着 8765，下一次运行只能退 3（2026-10-01 实际踩到：留下一个孤儿 server.py）。
import atexit  # noqa: E402


def _kill_server():
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


atexit.register(_kill_server)


def _wait_server():
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + "/config", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


CARD = {"id": "zz_forge_card", "name": "手动制卡验证卡", "char_name": "小铸",
        "user_name": "铸主", "desc": "真浏览器走一遍制卡链路用的卡", "tagline": "填表 → 草稿 → 审核 → 发布",
        "category": "治愈", "tags": ["验证", "手作"],
        "core": "她是从铸炉里被写出来的角色，负责验证这条链路是通的。",
        "identity": "测试角色，只在沙箱里存在。",
        "sms_samples": "「表单存下来了吗？」\n「存下来了，广场上也看得见。」",
        "opening": "第一句话：我在这里，链路也在这里。"}


def _digest_for(card_body: dict) -> str:
    """用**后端同一个函数**算出这张 payload 的卡体摘要（发布闸门比对的正是它）。"""
    data = plaza_api._card_zip_from_payload(card_body)
    return cf.parse_card_zip(data)["digest"]


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
        errs = []            # pageerror + console.error（必须 0 条）
        logs = []            # 所有 console 文本（查 Key 有没有被打印出去）
        phase = {"n": "启动"}

        def _note(msg):
            """**立即**打印：等到最后才汇总的话，中途断言失败会把真正的异常吞掉
            （本脚本第一版就是这么死的：wait_for_selector 超时，真因却是一个 pageerror）。"""
            errs.append(msg)
            print("     " + msg[:240], flush=True)

        def _on_console(m):
            logs.append(m.text)
            if m.type == "error":
                _note(f"[{phase['n']}] console.error: " + m.text)

        pg.on("pageerror", lambda e: _note(f"[{phase['n']}] pageerror: " + str(e)))
        pg.on("console", _on_console)
        pg.on("dialog", lambda d: d.accept())        # 删除/下架的确认框
        http_bad = []
        pg.on("response", lambda r: http_bad.append((r.status, r.url)) if r.status >= 400 else None)
        api_log = []          # 制卡相关的写请求：失败时把**后端原话**打出来（人话定位））

        def _on_response(r):
            try:
                if r.request.method == "POST" and "/plaza/api/" in r.url:
                    api_log.append(f"{r.status} {r.url.rsplit('/', 1)[-1]} "
                                   f"{(r.text() or '')[:200]}")
            except Exception:
                pass

        pg.on("response", _on_response)
        # 引导遮罩：**CSS 隐藏而不是删节点**（删了会让引导代码给已移除元素挂 onclick 抛异常）
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
        pg.route("**/auth/state", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"logged_in": false}'))
        pg.route("**/auth/me", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"email": ""}'))

        # ── 审核桩：返回 pass，摘要用后端真函数算（于是"审核摘要 == 卡体摘要"真的成立）──
        review_calls = []

        def _review_route(route):
            body = json.loads(route.request.post_data or "{}")
            review_calls.append({k: v for k, v in body.items() if k != "sk"})
            try:
                digest = _digest_for(body.get("card") or {})
                # ★ 真实后端在"审核通过"时会**自动存一份草稿**，并把审核结论（含 digest）绑上去。
                #   桩要复现这一步，否则发布闸门会以「审核结论为 None」拒绝（本脚本第一版就是这么栽的）。
                dm = PLAZA_DIR / "drafts" / "_anon" / "zz_forge_card.json"
                if digest and dm.is_file():
                    m = json.loads(dm.read_text(encoding="utf-8"))
                    m["status"] = "reviewed"
                    m.setdefault("review", {})["digest"] = digest
                    dm.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
            except Exception as e:                    # 算不出来也要让流程能走下去（会把原因打出来）
                digest = ""
                print(f"     [桩] 摘要计算失败：{type(e).__name__}: {e}", flush=True)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True,
                "review": {"verdict": "pass", "risk": "low",
                           "reasons": ["内容为虚构角色设定，没有违规项", "未见联系方式或引流"],
                           "categories": [], "model": "deepseek-stub",
                           "usage": {"total_tokens": 1}, "signals": [],
                           "reviewed_at": int(time.time())}}, ensure_ascii=False))

        _review_route.digest = ""
        pg.route("**/plaza/api/review", _review_route)

        def click_soft(sel, timeout=7000):
            """点击；被遮挡时打印真正的拦截者，清掉已知浮层后重试一次。"""
            try:
                pg.click(sel, timeout=timeout)
                return True
            except Exception:
                who = "?"
                try:
                    box = pg.locator(sel).first.bounding_box()
                    if box:
                        who = pg.evaluate(
                            "([x, y]) => { const t = document.elementFromPoint(x, y);"
                            " return t ? (t.id || t.className || t.tagName) : 'none'; }",
                            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
                except Exception as e:
                    who = f"<诊断失败:{type(e).__name__}>"
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

        # ═══ 走真实入口：首页 → 角色卡管理 → 广场 → 制作角色卡 ═══
        phase["n"] = "入口"
        pg.goto(base + "/", wait_until="domcontentloaded")
        pg.wait_for_selector("#cards-manage-btn", timeout=20000)
        click_soft("#cards-manage-btn")
        pg.wait_for_selector("#cards-view.show", timeout=10000)
        click_soft("#cv-plaza-btn")
        pg.wait_for_selector("#plaza-view.show", timeout=10000)
        check("① 广场页头部有「✦ 制作角色卡」入口", pg.is_visible("#pz-forge-btn"))
        click_soft("#pz-forge-btn")
        pg.wait_for_selector("#plaza-forge.show", timeout=10000)
        check("② 制卡页打开（#plaza-forge.show，走真实点击路径）", True)
        check("③ 表单骨架已建立（#pf-form 里有输入框与正文域）",
              pg.eval_on_selector("#pf-form", "el => el.querySelectorAll('input,textarea,select').length") >= 10)
        pg.screenshot(path=str(SHOTS / "01_forge_desktop.png"))

        # ═══ 填表 ═══
        phase["n"] = "填表"
        pg.fill("#pf-name", "Forge Smoke Card")
        check("④ 卡片 id 由卡名自动生成（中文名 slug 为空，故用拉丁名验证）",
              pg.input_value("#pf-id") != "" and pg.eval_on_selector(
                  ".pf-idtip", "el => el.classList.contains('ok')"),
              pg.input_value("#pf-id"))
        pg.fill("#pf-id", CARD["id"])
        pg.fill("#pf-name", CARD["name"])
        check("⑤ 手改 id 后合法性提示跟随，且改卡名不再覆盖手改的 id",
              CARD["id"] in pg.eval_on_selector(".pf-idtip", "el => el.textContent")
              and pg.input_value("#pf-id") == CARD["id"])
        pg.fill("#pf-char", CARD["char_name"])
        pg.fill("#pf-user", CARD["user_name"])
        pg.fill("#pf-desc", CARD["desc"])
        pg.fill("#pf-tagline", CARD["tagline"])
        pg.select_option("#pf-cat", CARD["category"])
        pg.fill("#pf-core", CARD["core"])
        pg.fill("#pf-identity", CARD["identity"])
        pg.fill("#pf-sms_samples", CARD["sms_samples"])
        pg.fill("#pf-opening", CARD["opening"])
        pg.fill("#pf-display", "手动验证作者")
        for t in CARD["tags"]:
            pg.fill("#pf-tag", t)
            pg.press("#pf-tag", "Enter")
        chips = pg.eval_on_selector_all("#pf-taglist .pf-tagchip", "els => els.map(e => e.textContent)")
        check(f"⑥ 标签可增删（已加 {chips}）",
              len(chips) == 2 and all("×" in c for c in chips))
        counter = pg.eval_on_selector("#pf-form .pf-count", "el => el.textContent")
        check(f"⑦ 字数提示在位（{counter.strip()}）", "/" in counter)
        # ★契约 06 §3.3：列表只用 thumb（≤320px/≤12KB），所以制卡页的"封面"就是 thumb 槽位
        pg.set_input_files("#pf-file-thumb", str(COVER_FILE))
        pg.wait_for_function(
            "() => { const img = document.getElementById('pf-img-thumb');"
            " return !!(img && img.getAttribute('src')); }",
            timeout=15000)
        check("⑧ 列表缩略图压缩后出预览（img.src 是 data URL）",
              pg.eval_on_selector("#pf-img-thumb",
                                  "el => (el.getAttribute('src')||'').slice(0, 11)")
              == "data:image/")
        check("⑧b 缩略图旁边实时显示「当前 KB / 上限 12KB」",
              "12 KB" in pg.eval_on_selector("#pf-imginfo-thumb", "el => el.textContent"))
        pg.screenshot(path=str(SHOTS / "02_forge_filled.png"))

        # 超限阻止：非法 id 这条本地闸门（名称超 40 字由 maxlength 直接挡住输入）
        pg.fill("#pf-id", "BAD ID!!")
        pg.click("#pf-save-btn")
        pg.wait_for_timeout(400)
        check("⑨ 非法 id 被本地拦下（提示区有人话，不是提交后才报错）",
              "id" in pg.eval_on_selector("#pf-msg", "el => el.textContent"))
        pg.fill("#pf-id", CARD["id"])

        # ═══ 存草稿 ═══
        phase["n"] = "存草稿"
        pg.click("#pf-save-btn")
        pg.wait_for_function(
            "() => document.querySelector('#pf-status').textContent.indexOf('已存草稿') >= 0",
            timeout=15000)
        check("⑩ 存草稿成功并被提示", True)
        draft_meta = PLAZA_DIR / "drafts" / "_anon" / (CARD["id"] + ".json")
        draft_zip = PLAZA_DIR / "drafts" / "_anon" / (CARD["id"] + ".zip")
        check("⑪ 磁盘上真的落了草稿（zip + json）", draft_meta.is_file() and draft_zip.is_file())
        meta = json.loads(draft_meta.read_text(encoding="utf-8")) if draft_meta.is_file() else {}
        check(f"⑫ 草稿元数据正确（id={meta.get('id')} 状态={meta.get('status')}）",
              meta.get("id") == CARD["id"] and meta.get("status") == "draft")
        rows = pg.eval_on_selector_all("#pf-drafts-box .pf-draft", "els => els.length")
        rowtxt = pg.eval_on_selector("#pf-drafts-box .pf-draft", "el => el.textContent")
        check(f"⑬ 草稿箱列出该草稿（{rows} 条）", rows == 1 and CARD["name"] in rowtxt)
        check("⑭ 未审核时发布按钮是禁用的",
              pg.eval_on_selector("#pf-publish-btn", "el => el.disabled"))

        # ═══ 提交审核（桩）═══
        phase["n"] = "提交审核"
        pg.click("#pf-review-btn")
        pg.wait_for_selector("#pf-modal.show", timeout=8000)
        check("⑮ 审核弹窗要求粘贴自己的 Key", pg.is_visible("#pf-sk"))
        pg.fill("#pf-sk", FAKE_SK)
        pg.click("#pf-modal-ok")
        pg.wait_for_function(
            "() => { const s = document.querySelector('#pf-status');"
            " return s && s.textContent.indexOf('审核已通过') >= 0; }", timeout=20000)
        check("⑯ verdict=pass：明确提示可发布，且发布按钮被启用",
              pg.eval_on_selector("#pf-publish-btn", "el => el.disabled") is False)
        check("⑰ 审核理由渲染成列表节点",
              pg.eval_on_selector_all("#pf-review .pf-rev-reason", "els => els.length") == 2)
        check("⑱ 审核请求带上了 card 但 sk 没被写进任何存储",
              len(review_calls) == 1 and review_calls[0].get("card", {}).get("id") == CARD["id"]
              and pg.evaluate("() => { const s = JSON.stringify(localStorage)"
                              " + JSON.stringify(sessionStorage);"
                              " return s.indexOf('sk-') < 0; }"))
        check("⑲ Key 输入框已清空、弹窗已关闭",
              pg.eval_on_selector_all("#pf-sk", "els => els.length") == 0)
        pg.screenshot(path=str(SHOTS / "03_forge_reviewed.png"))

        # ═══ 补齐"真实审核本该留下的那份状态" ═══
        # `/plaza/api/review` 被 pg.route 桩掉了（**绝不能用真实 Key 花钱**），
        # 而真实端点在"通过"时会**同时把结论与卡体摘要写进草稿**；发布闸门据此校验
        # （防"审 A 发 B"，见 docs/设计/角色卡共创平台/02 §六）。
        # 桩只能伪造 HTTP 响应、写不了**服务器侧**的草稿 ⇒ 这里由测试在**沙箱**里按同样形状补上。
        # ⚠ 这不是放宽产品闸门：闸门代码一行没改，补的是"真实审核本该写下的那条记录"。
        _parsed = cf.parse_card_zip(draft_zip.read_bytes())
        _meta = json.loads(draft_meta.read_text(encoding="utf-8"))
        _meta["review"] = {"verdict": "pass", "risk": "low", "reasons": ["桩：内容合规"],
                           "model": "deepseek-stub", "digest": _parsed["digest"]}
        _meta["status"] = "reviewed"
        draft_meta.write_text(json.dumps(_meta, ensure_ascii=False), encoding="utf-8")
        check("⑲b 已按真实形状把审核结论（含 digest）写进沙箱草稿",
              json.loads(draft_meta.read_text(encoding="utf-8"))["review"]["digest"]
              == _parsed["digest"])

        # ═══ 发布 ═══
        phase["n"] = "发布"
        pg.click("#pf-publish-btn")
        try:
            pg.wait_for_function(
                "() => document.querySelector('#pf-status').textContent.indexOf('已发布到广场') >= 0",
                timeout=20000)
            published = True
        except Exception:
            published = False
        if not published:
            print("     [制卡 API] " + " | ".join(api_log[-6:]), flush=True)
            print("     [发布失败] 状态栏=" + pg.eval_on_selector("#pf-status", "el => el.textContent")
                  + " 提示区=" + pg.eval_on_selector("#pf-msg", "el => el.textContent"), flush=True)
        check("⑳ 发布成功并被提示", published)
        card_meta = PLAZA_DIR / "cards" / CARD["id"] / "meta.json"
        check("㉑ 广场目录里真的落了这张卡（cards/<id>/meta.json）", card_meta.is_file())
        pub_meta = json.loads(card_meta.read_text(encoding="utf-8")) if card_meta.is_file() else {}
        check(f"㉒ 发布态与作者名正确（status={pub_meta.get('status')}）",
              pub_meta.get("status") == "published"
              and (pub_meta.get("author") or {}).get("display") == "手动验证作者")

        # ═══ 断言「广场列表里真的出现了这张卡」═══════
        phase["n"] = "广场可见"
        click_soft("#plaza-forge .cv-back")
        pg.wait_for_selector("#plaza-forge.show", state="detached", timeout=8000)
        pg.wait_for_function(
            "() => [...document.querySelectorAll('#plaza-grid .pz-cname')]"
            ".some(e => e.textContent.indexOf('手动制卡验证卡') >= 0)", timeout=20000)
        names = pg.eval_on_selector_all("#plaza-grid .pz-cname", "els => els.map(e => e.textContent)")
        check(f"㉓ 广场列表里真的出现了这张卡（{names}）", CARD["name"] in names)
        pg.screenshot(path=str(SHOTS / "04_plaza_has_forged_card.png"))

        # ═══ 下架（走**作者真实入口**：制卡页草稿行的「下架」）═══
        # 注意：广场详情页的 `.pz-btn.danger` 是"卸载"（把自己的副本删掉），
        # **不是**作者下架——两者语义完全不同（下架改的是广场上的卡，卸载只动本机）。
        # 本脚本第一版把两者搞混，点了个不存在的按钮，断言自然红。
        phase["n"] = "下架"
        pg.evaluate("openPlazaForge()")
        pg.wait_for_selector("#plaza-forge.show", timeout=8000)
        pg.wait_for_timeout(900)                      # 等草稿箱加载
        clicked = pg.evaluate("""() => {
            const rows = [...document.querySelectorAll('#pf-drafts-box .pf-draft')];
            const row = rows.find(r => r.textContent.indexOf('手动制卡验证卡') >= 0
                                    || r.textContent.indexOf('zz_forge_card') >= 0);
            if (!row) return 'no-row';
            const b = [...row.querySelectorAll('button')].find(x => x.textContent.indexOf('下架') >= 0);
            if (!b) return 'no-btn';
            b.click(); return 'clicked';
        }""")
        archived = False
        for _ in range(25):                            # confirm 由 dialog 处理器自动接受
            try:
                if json.loads(card_meta.read_text(encoding="utf-8")).get("status") == "archived":
                    archived = True
                    break
            except Exception:
                pass
            pg.wait_for_timeout(300)
        check(f"㉔ 作者在制卡页下架 → 广场卡片置 archived（点击结果 {clicked}）", archived)
        check("㉔b 下架是「改状态」而不是删文件（卡体仍在）",
              (PLAZA_DIR / "cards" / CARD["id"] / "card.zip").is_file())

        # ═══ 移动端版式 ═════════════════════════════
        phase["n"] = "移动端版式"
        pg.evaluate("openPlazaForge()")
        pg.wait_for_selector("#plaza-forge.show", timeout=8000)
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.wait_for_timeout(600)
        check("㉕ 移动端窄屏无横向溢出",
              pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"))
        stack = pg.evaluate("""() => {
            const pane = document.querySelector('#plaza-forge .pfc-pane').getBoundingClientRect();
            const box = document.querySelector('#plaza-forge .pfc-box').getBoundingClientRect();
            return box.top >= pane.bottom - 1;
        }""")
        check("㉖ 移动端单列（草稿箱在表单下方，不是并排）", stack)
        pg.screenshot(path=str(SHOTS / "05_forge_mobile.png"), full_page=False)

        # ═══ 泄漏与控制台 ═══
        phase["n"] = "收尾"
        leak = pg.evaluate("""() => {
            let s = '';
            try { s += JSON.stringify(localStorage) + JSON.stringify(sessionStorage); } catch (e) {}
            return s.indexOf('sk-') >= 0;
        }""")
        check("㉗ 全程结束后 localStorage/sessionStorage 里没有 sk- 痕迹", not leak)
        printed = [t for t in logs if "sk-" in t]
        check(f"㉘ 控制台里没有出现 Key（{len(logs)} 条 console 文本）", not printed)
        js_errs = [e for e in errs if "console.error: Failed to load resource" not in e]
        if api_log:
            print("     [制卡 API] " + " | ".join(api_log), flush=True)
        if js_errs:
            for e in js_errs[:6]:
                print("     · " + e[:220], flush=True)
        check(f"㉙ 全程无 pageerror / console.error（{len(js_errs)} 条）", not js_errs)
        if http_bad:
            print("     [非 2xx 请求] " + "; ".join(
                f"{s} /{u.split('/', 3)[-1]}" for s, u in http_bad[:8]), flush=True)
        allowed = ("/auth/", "/hotupdate/", "/notice", "/check-update", "/update-manifest")
        check("㉚ 非 2xx 只出现在服务器模式下预期会拒绝的端点上",
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
