# -*- coding: utf-8 -*-
"""真浏览器验收（**App 本地模式**）：用户自己的角色卡 → 三步发布到广场

为什么单列一个：`manual_plaza_publish_local.py` 跑的是**服务器模式**（真 server_app）——
在服务器上，用户包里的图片目前**取不到**（`/assets/` 在鉴权前分发、且不带账号作用域，
已回报 Lead）。而用户点名要的"我自己的角色卡"首先指**设备上**的卡，所以这里用本地模式
把完整链路跑一遍：真读 `/assets/character/<包>/assets/*`（本地模式这条是通的）→ 载入表单
→ 存草稿 → 提交审核 → 发布，**三步 API 全部桩在浏览器层**（零外网、零模型费用），
断言的是"前端真的按契约把三份 payload 发出去了 + 界面走完流程"。

沙箱：静态服 `app/static` + 临时资产目录；不碰真实 user_data。截图存 `_harden/shots/45_*`。
用法：`python tests/manual_plaza_publish_local_app.py`
退出码：0 全过；1 有断言失败。
"""
import functools
import http.server
import json
import shutil
import socketserver
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "_harden" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
SITE = Path(tempfile.mkdtemp(prefix="ff_pub_app_site_"))
shutil.copytree(ROOT / "app" / "static", SITE, dirs_exist_ok=True)

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}" + (f"   {extra}" if extra else ""), flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"   {extra}" if extra else ""), flush=True)


def solid_png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def _chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


# 本机角色卡资产（本地模式下就是 `/assets/character/<包>/assets/<文件>`）
PID = "local_card1"
ADIR = SITE / "assets" / "character" / PID / "assets"
ADIR.mkdir(parents=True, exist_ok=True)
(ADIR / "avatar.png").write_bytes(solid_png(200, 200, (120, 90, 58)))
(ADIR / "cover.png").write_bytes(solid_png(640, 400, (40, 60, 90)))
(ADIR / "sticker-1.png").write_bytes(solid_png(96, 96, (60, 120, 150)))
SDIR = SITE / "assets" / "stickers"
SDIR.mkdir(parents=True, exist_ok=True)
(SDIR / "mine-1.png").write_bytes(solid_png(96, 96, (90, 60, 120)))

MODES = {"ok": True, "default": "story", "modes": [
    {"id": PID, "name": "我的角色卡", "char_name": "小我", "user_name": "开拓者",
     "presentation": "sticker", "desc": "我自己的角色卡", "tagline": "标签语",
     "custom": True, "avatar": f"/assets/character/{PID}/assets/avatar.png",
     "cover": f"/assets/character/{PID}/assets/cover.png"},
]}
PACK_FILES = {"mode": PID, "name": "我的角色卡", "presentation": "sticker", "custom": True,
              "files": [{"name": "core.md", "content": "# 核心\n[事实] 我的卡。"},
                        {"name": "identity.md", "content": "# 关系\n[事实] 老相识。"},
                        {"name": "sms_samples.md", "content": "「在吗？」"}],
              "assets": {"avatar": f"/assets/character/{PID}/assets/avatar.png",
                         "cover": f"/assets/character/{PID}/assets/cover.png"},
              "knowledge": ["世界观.md"], "proactive": {}}
KB_LIST = {"mode": PID, "files": [{"path": "世界观.md", "size": 12}]}
KB_ONE = {"ok": True, "path": "世界观.md", "content": "[事实] 这个世界的规矩。"}
OPENING = {"ok": True, "path": "opening.json",
           "content": json.dumps({"narrations": ["夜色落下来。"],
                                  "first_messages": ["晚上好。"]}, ensure_ascii=False)}
STICKERS = {"stickers": [{"id": "mine_1", "file": "stickers/mine-1.png", "category": "可爱",
                          "label": "比心", "enabled": True, "pack": PID}]}

POSTS = {"draft": [], "review": [], "publish": []}
ASSET_REQS = []          # 取"本机包图"的真实请求头（证明显式带 Bearer，见 ②g）
STATE_BODY = {"logged_in": True, "offline_ok": True}   # 可变：第二段塞 role=admin


class _Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(_Q, directory=str(SITE)))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
PORT = httpd.server_address[1]
base = f"http://127.0.0.1:{PORT}"
time.sleep(0.3)
print(f"静态服（app/static + 沙箱资产）: {base}", flush=True)

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.set_default_timeout(20000)
        errs = []
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        pg.on("dialog", lambda d: d.accept())

        def wait_for(fn, timeout=20000, step=200):
            t0 = time.time()
            while (time.time() - t0) * 1000 < timeout:
                try:
                    if fn():
                        return True
                except Exception:
                    pass
                pg.wait_for_timeout(step)
            return False

        def _stub(route):
            u = route.request.url
            m = route.request.method

            def _j(obj):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(obj, ensure_ascii=False))

            if u.endswith("/config.js"):
                route.fulfill(status=200, content_type="application/javascript",
                              body='window.FIREFLY_MODE="local";')
            elif "/auth/state" in u:
                # ⚠ 本地模式**没有** `/auth/me` ⇒ role 只能从 `/auth/state` 来（serverops 2026-10-01 加）。
                #   用可变桩：第二段把 role 改成 admin，验证"本地模式管理员也能看到官方勾选"。
                _j(dict(STATE_BODY))
            elif "/modes" in u:
                _j(MODES)
            elif "/pack-files" in u:
                _j(PACK_FILES)
            elif "/pack-knowledge/file" in u:
                _j(KB_ONE)
            elif "/pack-knowledge" in u:
                _j(KB_LIST)
            elif "/pack-file" in u:
                _j(OPENING)
            elif "/stickers" in u:
                _j(STICKERS)
            elif m == "POST" and "/plaza/api/draft" in u:
                POSTS["draft"].append(json.loads(route.request.post_data or "{}"))
                _j({"ok": True, "draft": {"id": PID, "status": "draft", "updated_at": 1}})
            elif m == "POST" and "/plaza/api/review" in u:
                POSTS["review"].append(json.loads(route.request.post_data or "{}"))
                _j({"ok": True, "review": {"verdict": "pass", "risk": "low",
                                           "reasons": ["桩：内容合规"], "model": "stub"}})
            elif m == "POST" and "/plaza/api/publish" in u:
                POSTS["publish"].append(json.loads(route.request.post_data or "{}"))
                _j({"ok": True, "card": {"id": PID, "name": "我的角色卡"}})
            elif "/plaza/api/drafts" in u:
                _j({"ok": True, "items": [{"id": PID, "name": "我的角色卡", "status": "draft",
                                           "updated_at": 1}]})
            elif "/plaza/api/list" in u:
                _j({"ok": True, "total": 0, "items": [], "categories": []})
            elif "/plaza/api/installed" in u:
                _j({"ok": True, "items": [], "used_cards": 0, "used_bytes": 0,
                    "max_cards": 5, "max_bytes": 8 * 1024 * 1024, "server_mode": False})
            else:
                route.continue_()

        pg.route("**/*", _stub)
        # 抓"取本机包图"的真实请求头（②g 用）：必须看到显式 Authorization
        pg.on("request", lambda r: ASSET_REQS.append({k.lower(): v for k, v in r.headers.items()})
              if "/assets/character/" in r.url else None)
        # 引导遮罩会拦截点击：**用 CSS 隐藏而不是删节点**（删节点会让引导自己的代码给已移除元素挂
        # onclick 抛假异常）；同 manual_plaza_forge_free.py 的写法。
        pg.add_init_script(
            "try{localStorage.setItem('firefly_guide_v1_done','1');"
            "localStorage.setItem('firefly_deep_guide_v1_done','1');"
            "localStorage.setItem('firefly_token','tok-e2e-1');}catch(e){}\n"
            "window.__gNuked=0;\n"
            "const __nuke=()=>{if(window.__gNuked)return;const st=document.createElement('style');"
            "st.textContent='#guide-mask,.guide-block{display:none!important;pointer-events:none!important}';"
            "(document.head||document.documentElement).appendChild(st);window.__gNuked=1;};\n"
            "const __arm=()=>{if(!document.documentElement){setTimeout(__arm,0);return;}"
            "new MutationObserver(__nuke).observe(document.documentElement,{childList:true,subtree:true});"
            "__nuke();};__arm();")
        pg.goto(base + "/index.html", wait_until="domcontentloaded")
        pg.wait_for_timeout(800)

        print("\n== 1. 从包详情页「发布到广场」进入 ==", flush=True)
        pg.evaluate("window.openPackView ? window.openPackView('%s') : null" % PID)
        pg.wait_for_timeout(900)
        # 包详情页动态按钮（App 入口 ①）
        pg.evaluate("window.openCardsView ? window.openCardsView() : null")
        pg.wait_for_timeout(400)
        pg.evaluate("window.openPackView('%s')" % PID)
        ok_btn = wait_for(lambda: pg.locator("#pv-publish-btn").count() == 1, timeout=10000)
        check("① 角色卡管理页出现「发布到广场」入口", ok_btn)
        pg.screenshot(path=str(SHOTS / "45_publish_app_entry.png"))
        pg.click("#pv-publish-btn")

        print("\n== 2. 载入本地卡（图片走 /assets/character/…）==", flush=True)
        check("② 表单被填好（卡名/角色名/id）",
              wait_for(lambda: pg.input_value("#pf-name") == "我的角色卡"
                       and pg.input_value("#pf-id") == PID, timeout=15000),
              f"name={pg.input_value('#pf-name')!r}")
        check("②b 核心正文进了表单", "我的卡" in pg.input_value("#pf-core"))
        check("②c 知识库进了表单",
              wait_for(lambda: pg.locator(".pff-kb").count() == 1
                       and "规矩" in pg.eval_on_selector_all(".pff-kb .pff-ktext",
                                                            "els => els.map(e => e.value).join('')"),
                       timeout=10000))
        check("②d 本包专属表情包进了表单（label=比心）",
              wait_for(lambda: pg.locator(".pfi-stk").count() == 1, timeout=15000)
              and pg.eval_on_selector_all(".pfi-stk-label", "els => els.map(e => e.value).join('')")
              == "比心", pg.eval_on_selector_all(".pfi-stk-label", "els => els.map(e => e.value).join('')"))
        check("②e 缩略图已派生且合规（≤12KB）",
              wait_for(lambda: "12 KB" in pg.eval_on_selector("#pf-imginfo-thumb",
                                                              "el => el.textContent")
                       and "合格" in pg.eval_on_selector("#pf-imginfo-thumb", "el => el.textContent"),
                       timeout=40000),
              pg.eval_on_selector("#pf-imginfo-thumb", "el => el.textContent").strip())
        check("②f 封面（本地资产）也进了详情图槽位",
              (pg.eval_on_selector("#pf-img-display", "el => el.getAttribute('src') || ''")
               or "").startswith("data:image/"))
        # ②g `/assets/` 已是账号作用域（服务器模式）⇒ 取图必须**显式带 Bearer**（不能用 ?token=）。
        #     这里预置一个 token，抓真实请求头：证明 `_pflDataURL` 自己带了凭证。
        check("②g 取本机包图的请求**显式带了** Authorization: Bearer（不是靠 <img src>）",
              any(h.get("authorization") == "Bearer tok-e2e-1" for h in ASSET_REQS),
              str([h.get("authorization") for h in ASSET_REQS if h.get("authorization")][:3])
              + f"（本页 /assets/character 请求共 {len(ASSET_REQS)} 个，其中带凭证的 "
              + str(sum(1 for h in ASSET_REQS if h.get("authorization"))) + " 个）")
        pg.screenshot(path=str(SHOTS / "45_publish_app_loaded.png"))

        print("\n== 3. 三步：存草稿 → 审核 → 发布 ==", flush=True)
        pg.click("#pf-save-btn")
        check("③ 存草稿：draft payload 形状正确（files[] + images{}）",
              wait_for(lambda: len(POSTS["draft"]) == 1, timeout=20000), str(len(POSTS["draft"])))
        card = (POSTS["draft"][0].get("card") if POSTS["draft"] else {}) or {}
        paths = [f.get("path") for f in (card.get("files") or [])]
        check(f"③b payload 带 core.md / knowledge/世界观.md（{paths}）",
              "core.md" in paths and "knowledge/世界观.md" in paths, str(paths))
        im = card.get("images") or {}
        check("③c payload 带 avatar/thumb/display + 表情包 label",
              bool(im.get("avatar")) and bool(im.get("thumb")) and bool(im.get("display"))
              and (im.get("stickers") or [{}])[0].get("label") == "比心",
              str({k: (str(v)[:16] + "…" if isinstance(v, str) else v) for k, v in im.items()
                   if k != "stickers"}))
        # thumb 真的 ≤12KB（data URL 的 base64 长度换算）
        thumb_b64 = (im.get("thumb") or "")
        thumb_bytes = int(len(thumb_b64.split(",", 1)[1]) * 3 / 4) if "," in thumb_b64 else 0
        check(f"③d 派生 thumb ≤12KB（实际 {thumb_bytes} B）", 0 < thumb_bytes <= 12 * 1024,
              str(thumb_bytes))

        pg.click("#pf-review-btn")
        pg.wait_for_selector("#pf-modal.show", timeout=8000)
        pg.fill("#pf-sk", "sk-" + "e2e0" * 8)
        pg.click("#pf-modal-ok")
        check("④ 提交审核成功（review payload 带 sk 与 card）",
              wait_for(lambda: len(POSTS["review"]) == 1, timeout=20000)
              and POSTS["review"][0].get("sk", "").startswith("sk-")
              and (POSTS["review"][0].get("card") or {}).get("id") == PID,
              str(list(POSTS["review"][0].keys()) if POSTS["review"] else []))
        pg.click("#pf-publish-btn")
        check("⑤ 发布成功（publish payload = {id, replace}）",
              wait_for(lambda: len(POSTS["publish"]) == 1, timeout=20000),
              str(POSTS["publish"]))
        check("⑤b 界面明确提示已发布",
              wait_for(lambda: "已发布到广场" in pg.eval_on_selector("#pf-status",
                                                                    "el => el.textContent"),
                       timeout=15000),
              pg.eval_on_selector("#pf-status", "el => el.textContent"))
        pg.screenshot(path=str(SHOTS / "45_publish_app_published.png"))

        # ── 本地模式：普通用户看不到官方勾选；`/auth/state` 带 role=admin 时**看得到并可用** ──
        print("\n== 4. 本地模式的官方勾选（role 来自 /auth/state）==", flush=True)
        vis_user = pg.evaluate("(() => { const e = document.getElementById('pf-official-wrap');"
                               "return e ? getComputedStyle(e).display : 'missing'; })()")
        check(f"⑦ 非管理员（state 无 role）**看不到**官方勾选（display={vis_user}）",
              vis_user in ("none", "missing"), str(vis_user))
        STATE_BODY["role"] = "admin"                      # serverops 让 /auth/state 回带 role
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)
        pg.evaluate("window.openPlazaForge()")
        pg.wait_for_timeout(900)
        vis_admin = wait_for(lambda: pg.evaluate(
            "(() => { const e = document.getElementById('pf-official-wrap');"
            "return e ? getComputedStyle(e).display : 'missing'; })()") not in ("none", "missing"),
            timeout=15000)
        check("⑦b 本地模式管理员**看得到**官方勾选（/auth/state 的 role 生效）",
              vis_admin, pg.evaluate("(() => { const e = document.getElementById('pf-official-wrap');"
                                     "return e ? getComputedStyle(e).display : 'missing'; })()"))
        pg.check("#pf-official")
        pg.click("#pf-local-btn")
        pg.wait_for_selector(".pfl-item", timeout=8000)
        pg.click(".pfl-item:has-text('我的角色卡')")
        wait_for(lambda: pg.input_value("#pf-id") == PID, timeout=15000)
        rev_disp = pg.evaluate("(() => { const e = document.getElementById('pf-review-btn');"
                               "return e ? getComputedStyle(e).display : 'missing'; })()")
        note_disp = pg.evaluate("(() => { const e = document.getElementById('pf-admin-note');"
                                "return e ? getComputedStyle(e).display : 'missing'; })()")
        check(f"⑦b2 本地模式管理员**免审**：「提交审核」隐藏（{rev_disp}）、免审说明可见（{note_disp}）",
              rev_disp == "none" and note_disp not in ("none", "missing"), f"{rev_disp}/{note_disp}")
        pg.click("#pf-save-btn")
        wait_for(lambda: "已存草稿" in pg.eval_on_selector("#pf-status", "el => el.textContent"),
                 timeout=20000)
        if rev_disp != "none":                      # 非管理员才走审核（这里保留通用分支）
            pg.click("#pf-review-btn")
            pg.wait_for_selector("#pf-modal.show", timeout=8000)
            pg.fill("#pf-sk", "sk-" + "e2e0" * 8)
            pg.click("#pf-modal-ok")
            wait_for(lambda: "审核已通过" in pg.eval_on_selector("#pf-status", "el => el.textContent"),
                     timeout=20000)
        check("⑦b3 管理员不提交审核，发布按钮就是可用的", not pg.is_disabled("#pf-publish-btn"),
              pg.eval_on_selector("#pf-publish-btn", "el => el.title"))
        pg.click("#pf-publish-btn")
        wait_for(lambda: len(POSTS["publish"]) == 2, timeout=20000)
        check("⑦c 管理员发布时 payload 带 official: true",
              len(POSTS["publish"]) == 2 and POSTS["publish"][1].get("official") is True,
              str(POSTS["publish"][1:]))
        pg.screenshot(path=str(SHOTS / "50_app_local_admin_official.png"))
        check("⑥ 全程零 pageerror", not errs, str(errs[:3]))
        b.close()
finally:
    httpd.shutdown()
    httpd.server_close()

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
print("判定:", "PASS" if FAIL == 0 else "FAIL", flush=True)
sys.exit(1 if FAIL else 0)
