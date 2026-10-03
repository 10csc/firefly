# -*- coding: utf-8 -*-
"""真实浏览器验证「制卡自由度 + 图片规格」（契约 06 §3.2/§3.3，P4-7c）。

**手工/按需运行**（同 manual_plaza_forge.py 惯例，不在 tests/test_*.py 自动化套件内）。
用法：`python tests/manual_plaza_forge_free.py`（本机 8765 空闲时；占用则退出 3，绝不误杀）。

覆盖（契约 §四 的"前端"两行）：
 1. 文本自由增删：加/删 `knowledge/<名字>.md`（含非法名字被就地拦下）、开场白条目增删；
 2. 图片：传 **8 张表情包**、第 9 张被拒；**实时显示每张 KB 与上限**；thumb 压到 ≤12KB；
 3. **超限不许提交**：从"老草稿带回了超限缩略图"这条真实路径触发校验 → 明确中文原因 + **不发请求**；
 4. payload 形状：`files[]`（含 knowledge/…）、`images{avatar,thumb,display,stickers[]}`、
    `opening{narrations,first_messages}` —— 与契约 §3.2 逐字对齐；
 5. 保存 → 继续编辑**往返一致**（知识库正文、开场白条数、表情包张数都对得上）；
 6. **列表只拉 thumb**：进了广场列表后，`/plaza/api/asset` 里**不得出现** slot=display/avatar/sticker；
    打开详情后才出现 slot=display/sticker-1（"列表只发几 KB 缩略图"的网络层证据）。

桩说明：`/plaza/api/draft`(POST/GET) 与 `/plaza/api/list`、`/plaza/api/card` 用 `pg.route` 桩住——
本脚本要证的是**前端**行为（payload 形状、往返一致、图片分流），不重复后端校验；
真实链路（存草稿→审核→发布→下架）由 tests/manual_plaza_forge.py 走真后端负责。
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
PORT = 8765
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_ui_forgefree_"))
SHOTS = SANDBOX / "shots"
FIX = SANDBOX / "fixtures"
for _d in (SHOTS, FIX):
    _d.mkdir(parents=True, exist_ok=True)

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}" + (f"   {extra}" if extra else ""), flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"   {extra}" if extra else ""), flush=True)


# ── 造图（纯 stdlib：zlib+struct；不用 Pillow）────────────────────────
def _png(w, h, raw):
    import struct
    import zlib

    def _chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


def solid_png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return _png(w, h, raw)


def noise_png(w, h, seed=1):
    """随机噪声（**不可压缩**）：用来造"压也压不下来"的超限图。"""
    import random
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rnd.randrange(256) for _ in range(w * 3)) for _ in range(h))
    return _png(w, h, raw)


COVER = FIX / "cover.png"
COVER.write_bytes(solid_png(320, 180, (58, 90, 120)))
STK = []
for i in range(8):
    p = FIX / f"stk{i}.png"
    p.write_bytes(solid_png(96 + i, 96, (60 + i * 12, 90, 120)))
    STK.append(p)
BIG = FIX / "big.png"
BIG.write_bytes(noise_png(300, 300, 7))
print(f"沙箱 {SANDBOX}", flush=True)
print(f"超限图 big.png = {BIG.stat().st_size} B（PNG 本体；base64 后更大）", flush=True)

# ── 端口检查（占则退出 3，绝不误杀）──────────────────────────────
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
            "FIREFLY_SERVER": "1", "FIREFLY_NO_BROWSER": "1",
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
_log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(ROOT / "app"),
                        env=env, stdout=_log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"
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
        phase = {"n": "启动"}
        pg.on("pageerror", lambda e: errs.append(f"[{phase['n']}] pageerror: " + str(e)))
        pg.on("console", lambda m: errs.append(f"[{phase['n']}] console.error: " + m.text)
              if m.type == "error" else None)
        pg.on("dialog", lambda d: d.accept())
        pg.add_init_script(
            "try{localStorage.setItem('firefly_guide_v1_done','1');"
            "localStorage.setItem('firefly_deep_guide_v1_done','1');}catch(e){}\n"
            "const __nuke=()=>{if(window.__g)return;const st=document.createElement('style');"
            "st.textContent='#guide-mask,.guide-block{display:none!important;pointer-events:none!important}';"
            "(document.head||document.documentElement).appendChild(st);window.__g=1;};"
            "const __arm=()=>{if(!document.documentElement){setTimeout(__arm,0);return;}"
            "new MutationObserver(__nuke).observe(document.documentElement,{childList:true,subtree:true});"
            "__nuke();};__arm();")
        pg.route("**/auth/state", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"logged_in": false}'))
        pg.route("**/auth/me", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"email": ""}'))

        # ── 桩：草稿 POST 记下 payload；GET 回放它（做"往返一致"）；可切换成"超限老草稿" ──
        saved = {"card": None, "display": "", "posts": []}
        over = {"on": False}
        legacy = {"mode": ""}          # ""｜"cover"（有图无 thumb）｜"none"（一张图都没有）

        def _draft_post(route):
            body = json.loads(route.request.post_data or "{}")
            saved["posts"].append(body)
            saved["card"] = body.get("card")
            saved["display"] = body.get("display") or ""
            route.fulfill(status=200, content_type="application/json",
                          body='{"ok": true, "draft": {"id": "free_card", "status": "draft",'
                               ' "updated_at": 1790849000}}')

        BIG_THUMB = "data:image/png;base64," + __import__("base64").b64encode(
            noise_png(300, 300, 11)).decode()
        # 合规小缩略图（给"别的槽位超限"那条用例垫底）与**超限详情图**
        SMALL_THUMB = "data:image/png;base64," + __import__("base64").b64encode(
            solid_png(200, 120, (30, 40, 60))).decode()
        BIG_DISPLAY = "data:image/png;base64," + __import__("base64").b64encode(
            noise_png(500, 500, 23)).decode()

        def _draft_get(route):
            card = dict(saved["card"] or {})
            if over["on"]:
                # ★ 超限用例走**详情图**：thumb 合规（迁移闸门只补/修 thumb），display 超 300KB
                card["images"] = {"thumb": SMALL_THUMB, "display": BIG_DISPLAY}
            if legacy["mode"]:
                # 老草稿（V1 之前）：只有平铺字段，没有 images/files（task-13 的两条迁移路径）
                card = {"id": "free_card", "name": "Free Editor Card", "char_name": "小自",
                        "user_name": "你", "desc": "老草稿", "category": "陪伴",
                        "presentation": "sticker", "tags": [],
                        "core": "老草稿的核心设定。", "identity": "口吻。",
                        "sms_samples": "「在吗？」", "opening": "老开场白"}
                if legacy["mode"] == "cover":
                    card["cover"] = BIG_THUMB                # 有图（大封面）但**没有 thumb**
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "draft": {
                              "meta": {"id": "free_card", "status": "draft",
                                       "display": saved["display"], "updated_at": 1790849000},
                              "card": card}}, ensure_ascii=False))

        pg.route("**/plaza/api/draft?*", _draft_get)
        pg.route("**/plaza/api/draft", lambda r: _draft_post(r) if r.request.method == "POST"
                 else _draft_get(r))
        # 草稿箱列表：POST 过草稿之后就让"继续编辑"点得动（POST 是桩，真实后端里并没有这份草稿）
        pg.route("**/plaza/api/drafts", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "items": ([{
                "id": "free_card", "name": "Free Editor Card", "status": "draft",
                "updated_at": 1790849000, "size_bytes": 2048,
                "review_verdict": ""}] if saved["card"] else [])}, ensure_ascii=False)))
        pg.route("**/plaza/api/drafts?*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "items": ([{
                "id": "free_card", "name": "Free Editor Card", "status": "draft",
                "updated_at": 1790849000, "size_bytes": 2048,
                "review_verdict": ""}] if saved["card"] else [])}, ensure_ascii=False)))

        # ── 桩：广场列表/详情（契约形状：列表只给 thumb；详情才给 display/stickers）──
        asset_reqs = []
        pg.on("request", lambda r: asset_reqs.append(r.url) if "/plaza/api/asset" in r.url else None)
        LIST_ITEMS = [{
            "id": "free_card", "name": "自由编辑器验证卡", "char_name": "小自",
            "desc": "列表只该拿 thumb", "category": "陪伴", "tags": ["验证"],
            "downloads": 1, "size_bytes": 2048, "published_at": 1790849000,
            "author": {"display": "某作者", "official": False},
            "thumb": "thumb.webp",                    # ★ 列表唯一的图
        }]
        DETAIL_CARD = dict(LIST_ITEMS[0])
        DETAIL_CARD.update({"display": "display.webp", "avatar": "avatar.webp",
                            "stickers": [{"file": "sticker-1.webp", "label": "开心"},
                                         {"file": "sticker-2.webp", "label": ""}],
                            "tagline": "", "tags": ["验证"]})
        pg.route("**/plaza/api/list*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "total": 1, "page": 1, "size": 20,
                             "items": LIST_ITEMS, "categories": ["陪伴"]}, ensure_ascii=False)))
        pg.route("**/plaza/api/card?*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "card": DETAIL_CARD}, ensure_ascii=False)))
        pg.route("**/plaza/api/asset*", lambda r: r.fulfill(
            status=200, content_type="image/png", body=solid_png(4, 4, (40, 60, 80))))
        pg.route("**/plaza/api/installed*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"ok": true, "items": [], "server_mode": false}'))

        pg.goto(base + "/", wait_until="domcontentloaded")
        pg.wait_for_selector("#cards-manage-btn", timeout=20000)
        pg.click("#cards-manage-btn")
        pg.wait_for_selector("#cards-view.show", timeout=10000)
        pg.click("#cv-plaza-btn")
        pg.wait_for_selector("#plaza-view.show", timeout=10000)

        def click_soft(sel, timeout=8000):
            try:
                pg.click(sel, timeout=timeout)
                return True
            except Exception:
                pg.evaluate("""() => { ['guide-mask','notice-panel','menu-overlay'].forEach(id => {
                    const el = document.getElementById(id); if (el) el.remove(); });
                    document.querySelectorAll('.guide-block').forEach(e => e.remove()); }""")
                pg.wait_for_timeout(250)
                pg.click(sel, timeout=timeout)
                return True

        def wait_for(fn, timeout=12000, step=200):
            t0 = time.time()
            while (time.time() - t0) * 1000 < timeout:
                try:
                    if fn():
                        return True
                except Exception:
                    pass
                pg.wait_for_timeout(step)
            return False

        phase["n"] = "打开制卡页"
        click_soft("#pz-forge-btn")
        pg.wait_for_selector("#plaza-forge.show", timeout=10000)
        check("① 制卡页打开且文本区/图片区骨架在位（固定三件 + 知识库 + 图片槽位）",
              pg.is_visible("#pf-core") and pg.is_visible("#pf-kb-add")
              and pg.is_visible("#pf-pick-thumb") and pg.is_visible("#pf-stk-add"))
        pg.screenshot(path=str(SHOTS / "01_forge_free.png"))

        # 元数据（必填项）
        pg.fill("#pf-name", "Free Editor Card")
        pg.fill("#pf-id", "free_card")
        pg.fill("#pf-char", "小自")
        pg.fill("#pf-user", "你")
        pg.fill("#pf-desc", "自由编辑器验证卡")
        pg.fill("#pf-core", "核心设定正文：由制卡页自由书写。")
        pg.fill("#pf-identity", "口吻：安静。")
        pg.fill("#pf-sms_samples", "「在吗？」\n「在。」")
        pg.fill("#pf-opening", "第一句开场白")

        # ── ② 知识库：加两份（一份扁平、一份带「域」分组）──
        phase["n"] = "知识库"
        pg.click("#pf-kb-add")
        row = pg.locator(".pff-kb").last
        row.locator(".pff-kname").fill("世界观")
        row.locator(".pff-ktext").fill("这个世界有一套自己的物理。")
        pg.click("#pf-kb-add")                              # ★契约 §3.1：也允许一层分组
        row2 = pg.locator(".pff-kb").last
        row2.locator(".pff-kdir").fill("factions")
        row2.locator(".pff-kname").fill("势力志")
        row2.locator(".pff-ktext").fill("北境有三大势力。")
        check("② 知识库可加（2 份，计数同步）",
              pg.locator(".pff-kb").count() == 2
              and "2 / 6" in pg.eval_on_selector(".pff-kbcount", "el => el.textContent"))
        check("②a 分组行的路径提示是 knowledge/factions/势力志.md",
              "knowledge/factions/势力志.md" in row2.locator(".pf-idtip").inner_text())
        # 非法名字：就地标红 + 保存被拦
        bad = pg.locator(".pff-kb").last
        bad.locator(".pff-kname").fill("坏/名字")
        check("②b 非法知识库名就地标红（人话提示，不等提交）",
              "bad" in (bad.locator(".pf-idtip").get_attribute("class") or ""))
        click_soft("#pf-save-btn")
        pg.wait_for_timeout(500)
        save_msg = pg.eval_on_selector("#pf-msg", "el => el.textContent")
        check(f"②c 非法名字被拦在本地（{save_msg.strip()[:34]}）", "知识库文件名不合法" in save_msg)
        check("②d 被拦时**没有**发出存草稿请求", len(saved["posts"]) == 0)
        bad.locator(".pff-kname").fill("势力志")

        # ── ③ 知识库：再加一份临时文件并删掉它（删的行为要测，但**不动**上面两份）──
        pg.click("#pf-kb-add")
        pg.locator(".pff-kb").last.locator(".pff-kname").fill("临时")
        pg.locator(".pff-kb").last.locator(".pff-ktext").fill("待删除。")
        pg.locator(".pff-kb").last.locator("button", has_text="删除").click()
        check("③ 知识库可删（剩 2 份，扁平 + 分组都在）",
              pg.locator(".pff-kb").count() == 2
              and "2 / 6" in pg.eval_on_selector(".pff-kbcount", "el => el.textContent"))

        # ── ④ 开场白条目增删 ──
        phase["n"] = "开场白"
        pg.click("#pf-open-add")
        pg.locator(".pff-first").last.fill("第二句开场白")
        pg.click("#pf-narr-add")
        pg.locator(".pff-narr").last.fill("旁白：夜色落下来。")
        check("④ 开场白条目可增（2 句 + 1 段旁白）",
              pg.locator(".pff-first").count() == 2 and pg.locator(".pff-narr").count() == 1)
        pg.locator(".pff-first-list .pff-item").last.locator("button").click()
        check("④b 开场白条目可删（剩 1 句）", pg.locator(".pff-first").count() == 1)

        # ── ⑤ 图片：thumb + 8 张表情包 ──
        phase["n"] = "图片"
        pg.set_input_files("#pf-file-thumb", str(COVER))
        ok_thumb = wait_for(lambda: "data:image/" in (
            pg.eval_on_selector("#pf-img-thumb", "el => el.getAttribute('src') || ''")))
        info_thumb = pg.eval_on_selector("#pf-imginfo-thumb", "el => el.textContent")
        check(f"⑤ 缩略图压好并**实时显示 KB/上限**（{info_thumb.strip()}）",
              ok_thumb and "12 KB" in info_thumb and "合格" in info_thumb)

        def _kb_of(text):
            """把「674 B / 12 KB」解析成 KB 数值。"""
            head = text.split("/")[0].strip()
            try:
                v, unit = head.split()
                v = float(v)
            except Exception:
                return -1.0
            return v / 1024.0 if unit.upper() == "B" else (v * 1024.0 if unit.upper() == "MB" else v)

        kb_now = _kb_of(info_thumb)
        check("⑤b 缩略图真的压到 ≤12KB（超限不许进 payload）", 0 <= kb_now <= 12, f"{kb_now:.2f} KB")
        pg.set_input_files("#pf-file-sticker", [str(p) for p in STK])
        ok8 = wait_for(lambda: pg.locator(".pfi-stk").count() == 8, timeout=60000)
        check(f"⑥ 8 张表情包全部收下（网格 {pg.locator('.pfi-stk').count()} 个）", ok8)
        check("⑥b 表情包计数显示 8 / 8",
              "8 / 8" in pg.eval_on_selector(".pfi-stkcount", "el => el.textContent"))
        # ★契约 §3.2 变更（2026-10-01 18:20）：每张表情包可填 label（≤12 字），UI 要提示"建议填"
        labels = pg.locator(".pfi-stk-label")
        check("⑥c 每张表情包都有标签输入框，且提示「建议填」",
              labels.count() == 8
              and "建议填" in (labels.first.get_attribute("placeholder") or ""))
        labels.first.fill("开心")
        labels.nth(1).fill("疑惑")
        check("⑥d 标签可填（≤12 字上限）",
              labels.first.input_value() == "开心" and labels.first.get_attribute("maxlength") == "12")
        pg.set_input_files("#pf-file-sticker", str(STK[0]))
        pg.wait_for_timeout(1200)
        check("⑥e 第 9 张被拒（仍 8 张）", pg.locator(".pfi-stk").count() == 8)
        pg.screenshot(path=str(SHOTS / "02_images.png"))

        # ── ⑦ 存草稿：payload 形状（契约 §3.2）──
        phase["n"] = "存草稿"
        pg.click("#pf-save-btn")
        ok_save = wait_for(lambda: len(saved["posts"]) == 1, timeout=20000)
        card = (saved["posts"][0].get("card") if saved["posts"] else {}) or {}
        files = {f.get("path"): f.get("text") for f in (card.get("files") or [])}
        im = card.get("images") or {}
        op = card.get("opening") or {}
        check("⑦ payload 用了契约形状（files[]/images{}/opening{}）",
              ok_save and isinstance(card.get("files"), list)
              and isinstance(card.get("images"), dict) and isinstance(op, dict))
        check("⑦b files 里固定三件 + 扁平知识库 + 分组知识库都在",
              {"core.md", "identity.md", "sms_samples.md",
               "knowledge/世界观.md", "knowledge/factions/势力志.md"} <= set(files),
              str(sorted(files)))
        check("⑦c images 只带契约槽位（avatar/thumb/display/stickers）",
              set(im.keys()) == {"avatar", "thumb", "display", "stickers"}, str(sorted(im)))
        stk_payload = im.get("stickers") or []
        check("⑦d 表情包 8 条、形状是 {data,label}、缩略图是 data URL",
              len(stk_payload) == 8
              and all(set(x.keys()) <= {"data", "label"} and str(x.get("data", "")).startswith("data:image/")
                      for x in stk_payload)
              and str(im.get("thumb", "")).startswith("data:image/"))
        check("⑦d2 填过的标签原样进了 payload（第 1/2 张）",
              stk_payload[0].get("label") == "开心" and stk_payload[1].get("label") == "疑惑")
        check("⑦e 开场白是对象（narrations/first_messages），条目对得上",
              len(op.get("first_messages") or []) == 1 and len(op.get("narrations") or []) == 1)

        # ── ⑧ 继续编辑：往返一致 ──
        phase["n"] = "继续编辑"
        wait_for(lambda: pg.locator("#pf-drafts-box .pf-draft").count() >= 1)
        click_soft("#pf-drafts-box .pf-draft button:has-text('继续编辑')")
        ok_back = wait_for(lambda: "核心设定正文" in pg.eval_on_selector(
            "#pf-core", "el => el.value || ''"), timeout=15000)
        check("⑧ 继续编辑把文本带回来了（核心设定）", ok_back)
        kb_names = pg.eval_on_selector_all(".pff-kname", "els => els.map(e => e.value)")
        kb_dirs = pg.eval_on_selector_all(".pff-kdir", "els => els.map(e => e.value)")
        kb_texts = pg.eval_on_selector_all(".pff-ktext", "els => els.map(e => e.value)")
        check("⑧b 知识库（含分组）文件名/域/正文一致（往返）",
              kb_names == ["世界观", "势力志"] and kb_dirs == ["", "factions"]
              and kb_texts == ["这个世界有一套自己的物理。", "北境有三大势力。"],
              f"{kb_dirs} / {kb_names}")
        check("⑧c 开场白/旁白条数一致（往返）",
              pg.locator(".pff-first").count() == 1 and pg.locator(".pff-narr").count() == 1)
        check("⑧d 8 张表情包都回来了（往返）", pg.locator(".pfi-stk").count() == 8)
        check("⑧d2 表情包标签也往返一致（第 1/2 张）",
              pg.locator(".pfi-stk-label").first.input_value() == "开心"
              and pg.locator(".pfi-stk-label").nth(1).input_value() == "疑惑")
        check("⑧e 缩略图回来了（往返）",
              (pg.eval_on_selector("#pf-img-thumb", "el => el.getAttribute('src') || ''")
               ).startswith("data:image/"))

        # ── ⑨ 超限不许提交（草稿带回超限**详情图** → 校验必须拦下且不发请求）──
        phase["n"] = "超限拦截"
        over["on"] = True
        posts_before = len(saved["posts"])
        click_soft("#pf-drafts-box .pf-draft button:has-text('继续编辑')")
        wait_for(lambda: "超过" in pg.eval_on_selector("#pf-imginfo-display", "el => el.textContent"),
                 timeout=10000)
        info2 = pg.eval_on_selector("#pf-imginfo-display", "el => el.textContent")
        check(f"⑨ 超限图就地标红（{info2.strip()[:40]}）",
              "超过上限" in info2 and "bad" in (
                  pg.eval_on_selector("#pf-imginfo-display", "el => el.className")))
        click_soft("#pf-save-btn")
        pg.wait_for_timeout(800)
        msg2 = pg.eval_on_selector("#pf-msg", "el => el.textContent")
        check(f"⑨b 超限时保存被拦并给出中文原因（{msg2.strip()[:42]}）",
              "超过" in msg2 and "300 KB" in msg2)
        check("⑨c 超限时**没有**发出存草稿请求（不是提交后才报错）",
              len(saved["posts"]) == posts_before)
        over["on"] = False

        # ── ⑨d 老草稿迁移（有 cover、无 thumb）→ 自动补 thumb 且**能存**（task-13）──
        phase["n"] = "老草稿迁移"
        legacy["mode"] = "cover"
        posts_before = len(saved["posts"])
        click_soft("#pf-drafts-box .pf-draft button:has-text('继续编辑')")
        # ⚠ 迁移是**异步**的（要跑一次压缩）：必须等"旧格式"提示真的出现，
        #   不能只看缩略图是否合格 —— 上一个用例留下的合规缩略图会让这个等待立刻为真（假通过）。
        ok_mig = wait_for(lambda: "旧格式" in pg.eval_on_selector("#pf-msg", "el => el.textContent"),
                          timeout=30000)
        info_mig = pg.eval_on_selector("#pf-imginfo-thumb", "el => el.textContent")
        check(f"⑨d 老草稿（有封面、无缩略图）自动补了一张合规 thumb（{info_mig.strip()}）",
              ok_mig and "合格" in info_mig)
        _hint = pg.eval_on_selector("#pf-msg", "el => el.textContent")
        check(f"⑨e 并且给了人话提示（不是静默改数据）：{_hint.strip()[:34]}", "旧格式" in _hint)
        click_soft("#pf-save-btn")
        ok_saved2 = wait_for(lambda: len(saved["posts"]) == posts_before + 1, timeout=20000)
        _th = str((((saved["posts"][-1].get("card") or {}).get("images") or {}).get("thumb")
                   if ok_saved2 else ""))
        check("⑨f 老草稿改完**能存成功**，且带上的 thumb ≤12KB",
              ok_saved2 and _th.startswith("data:image/")
              and 0 < len(_th) * 3 // 4 <= 12 * 1024, f"{len(_th) * 3 // 4} B")
        legacy["mode"] = ""

        # ── ⑨g 老草稿（一张图都没有）→ 明确原因 + **不发请求**（task-13 第二条路径）──
        legacy["mode"] = "none"
        posts_before = len(saved["posts"])
        click_soft("#pf-drafts-box .pf-draft button:has-text('继续编辑')")
        pg.wait_for_timeout(800)
        click_soft("#pf-save-btn")
        pg.wait_for_timeout(800)
        msg_none = pg.eval_on_selector("#pf-msg", "el => el.textContent")
        check(f"⑨g 无图老草稿给出明确原因（{msg_none.strip()[:38]}）",
              "列表缩略图" in msg_none and ("必填" in msg_none or "必须" in msg_none))
        check("⑨h 无图老草稿**不发请求**（不拿后端 400 当唯一反馈）",
              len(saved["posts"]) == posts_before)
        legacy["mode"] = ""

        # ── ⑩ 列表只拉 thumb（网络层证据）──
        phase["n"] = "列表/详情分流"
        # ⚠ 读的是**列表阶段累计**的请求（页面加载 → 打开详情之前），不是"清空后重渲一次"：
        #   广场模块按 URL 缓存 objectURL（3Mbps 省流量），第二次渲染**不会**再发请求——
        #   本脚本第一版就是清空后再断言，抓到 0 条，假失败。
        click_soft("#plaza-forge .cv-back")            # 回广场列表
        wait_for(lambda: pg.locator(".pz-card").count() >= 1, timeout=15000)
        got_thumb = wait_for(lambda: any("slot=thumb" in u for u in asset_reqs), timeout=8000)
        big_in_list = [u for u in asset_reqs
                       if any(k in u for k in ("slot=display", "slot=avatar", "slot=sticker"))]
        check(f"⑩ 列表阶段请求了 thumb（{len(asset_reqs)} 条 asset 请求，含 slot=thumb={got_thumb}）",
              got_thumb)
        check("⑩b ★ 列表**没有**请求 display/avatar/sticker（几 KB 缩略图之外的图一条都没拉）",
              not big_in_list, str([u.split("?")[-1] for u in big_in_list][:3]))
        pg.screenshot(path=str(SHOTS / "03_list.png"))

        # ── ⑪ 详情才拉大图 ──
        asset_reqs.clear()
        click_soft(".pz-card:has-text('自由编辑器验证卡')")
        pg.wait_for_selector("#plaza-view.plaza-detail-on", timeout=10000)
        ok_detail = wait_for(lambda: any("slot=display" in u for u in asset_reqs), timeout=15000)
        got_stk = any("slot=sticker" in u for u in asset_reqs)
        check("⑪ 详情页才请求 display（大图）", ok_detail,
              str([u.split("?")[-1] for u in asset_reqs][:4]))
        check("⑪b 详情页渲染了表情包并请求 sticker-N", got_stk
              and pg.eval_on_selector_all(".pz-dstk-cell img", "els => els.length") == 2)
        check("⑪c 表情包标签在详情页用 textContent 显示（有标签的那张）",
              pg.eval_on_selector_all(".pz-dstk-label", "els => els.map(e => e.textContent)") == ["开心"])
        pg.screenshot(path=str(SHOTS / "04_detail.png"))

        # ── 收口 ──
        phase["n"] = "收尾"
        js_errs = [e for e in errs if "console.error: Failed to load resource" not in e]
        if js_errs:
            for e in js_errs[:5]:
                print("     · " + e[:200], flush=True)
        check(f"⑫ 全程无 JS 异常（{len(js_errs)} 条）", not js_errs)
        b.close()
finally:
    _kill_server()
    _log.close()

print(f"\n截图目录：{SHOTS}", flush=True)
print(f"结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
