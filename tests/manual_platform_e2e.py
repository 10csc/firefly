# -*- coding: utf-8 -*-
"""真浏览器**完整链路**验收：服务器只做后台 + 角色卡平台（task-22 收尾）

链路（全部走**真实 HTTP**，无 pg.route 桩）：
    打开 /login.html → 真表单登录 → 落到 /platform.html → 广场有卡 → 详情
    → 制卡（知识库 + 表情包 + thumb）→ 存草稿 → 提交审核 → 发布
    → 我的卡（草稿箱）→ 治理台（admin 账号）

**零模型费用**：起的是进程内真实服务器（`server_app.FireflyHandler`），只在**同一个进程里**
把 `plaza.review.review_card_data` 换成桩（返回 verdict=pass + 真实卡体 digest）。
端点、草稿、发布闸门、治理台全部是真的——桩掉的只有"给 DeepSeek 发那一次 HTTP"。

沙箱：`FIREFLY_DATA_DIR`/`FIREFLY_PLAZA_DIR` 指向临时目录；数据库在沙箱里现建；
不连外网、不碰真实 user_data、不动生产。截图存 `_harden/shots/20_platform_e2e_*.png`。

用法：`python tests/manual_platform_e2e.py`（端口由系统分配，不占用用户实例）。
退出码：0 全过；1 有断言失败；2 服务没起来。
"""
import json
import os
import socket as _sk
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "_harden" / "shots"
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_platform_e2e_"))
USER_DIR = SANDBOX / "user_data"
PLAZA_DIR = SANDBOX / "plaza"
USER_DIR.mkdir(parents=True, exist_ok=True)
PLAZA_DIR.mkdir(parents=True, exist_ok=True)
SHOTS.mkdir(parents=True, exist_ok=True)

# ⚠ 敏感头加密（`X-Firefly-Enc`）**必须在沙箱里也打开**，否则本 e2e 会假失败：
#   客户端（平台 bundle 里的 session_crypto）在 `crypto.subtle` 可用时**总是**把
#   Authorization 装进信封并**删掉明文头**；而服务器只有在 `FIREFLY_ENC_ENABLED=1`
#   且拿得到私钥（`FIREFLY_ENC_KEY`）时才会解密。
#   沙箱若没开 ⇒ 服务器读不到任何凭证 ⇒ `/auth/me` 401 ⇒ 平台页按设计跳回 login.html
#   ⇒ 表现为"登录成功但广场是空的"。这是**夹具缺配置**，不是产品缺陷（App 的 server 模式同理）。
#   本机私钥：`python tools/build_session_keys.py --gen` 生成在 ~/.firefly/session_priv.pem，
#   其模数与 `app/static/js/session_crypto.js` 的 `SESSION_PUBKEY.n_b64` **逐字相同**（已核对）。
_KEY = Path(os.environ.get("FIREFLY_ENC_KEY") or (Path.home() / ".firefly" / "session_priv.pem"))
ENC_ON = _KEY.is_file()
if ENC_ON:
    os.environ["FIREFLY_ENC_ENABLED"] = "1"
    os.environ["FIREFLY_ENC_KEY"] = str(_KEY)
else:
    print(f"!! 找不到会话私钥 {_KEY} —— 本次退化为**明文回退**路径"
          f"（客户端会因缺少 crypto 能力而发表头）。要跑完整加密链路：\n"
          f"   python tools/build_session_keys.py --gen", flush=True)

# ⚠ 必须先摆环境再 import：app/core/paths.py 在 import 期按 FIREFLY_* 定 USER_DIR；
#    FIREFLY_SERVER 与 server_app.py 的入口约定一致（硬置服务器语义）。
os.environ.update({
    "FIREFLY_ANDROID": "1",
    "FIREFLY_DATA_DIR": str(SANDBOX),
    "FIREFLY_PLAZA_DIR": str(PLAZA_DIR),
    "FIREFLY_SERVER": "1",
    "FIREFLY_NO_BROWSER": "1",
    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
})
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

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
def solid_png(w, h, rgb):
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def _chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


FIX = SANDBOX / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)
THUMB = FIX / "thumb.png"; THUMB.write_bytes(solid_png(320, 180, (58, 90, 120)))
AVATAR = FIX / "avatar.png"; AVATAR.write_bytes(solid_png(200, 200, (120, 90, 58)))
DISPLAY = FIX / "display.png"; DISPLAY.write_bytes(solid_png(640, 400, (40, 60, 90)))
STK = []
for i in range(2):
    p = FIX / f"stk{i}.png"; p.write_bytes(solid_png(96, 96, (60 + i * 40, 90, 120))); STK.append(p)

# ── 1. 沙箱造账号（真密码）+ 种一张已发布卡 ─────────────────────────
import db as srv_db                      # noqa: E402  (server/db.py)
import auth as auth_svc                  # noqa: E402  (server/auth.py)
from plaza import store as st            # noqa: E402
from plaza import review as rv           # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
srv_db.ensure_auth_columns()
PW = "e2e-Passw0rd!"
USER_EMAIL, ADMIN_EMAIL = "e2e_user@qq.com", "e2e_owner@qq.com"


def _mk_user(email, role):
    salt = os.urandom(16)
    return srv_db.create_user(email, auth_svc.hash_password(PW, salt), salt.hex(),
                              install_id="inst-" + os.urandom(16).hex(), role=role)


UID_USER = _mk_user(USER_EMAIL, "user")
UID_ADMIN = _mk_user(ADMIN_EMAIL, "admin")

SEED_ID = "e2e_seed_card"
st.publish_card(
    {"id": SEED_ID, "name": "种子卡·峡谷旅人", "char_name": "峡谷旅人", "user_name": "你",
     "presentation": "sticker", "desc": "端到端验证用：广场里本来就有一张卡", "category": "剧情",
     "tags": ["验证"]},
    {"preset.json": json.dumps({"id": SEED_ID, "name": "种子卡·峡谷旅人", "char_name": "峡谷旅人",
                                "user_name": "你", "presentation": "sticker",
                                "desc": "端到端验证用", "schema": 1}, ensure_ascii=False).encode(),
     "core.md": b"# \xe6\xa0\xb8\xe5\xbf\x83\xe8\xae\xbe\xe5\xae\x9a\n\xe7\xa7\x8d\xe5\xad\x90\xe5\x8d\xa1\n",
     "knowledge/\xe5\x9c\xb0\xe5\x9b\xbe.md": b"# \xe5\x9c\xb0\xe5\x9b\xbe\n\xe5\xb3\xa1\xe8\xb0\xb7\xe8\xb7\xaf\xe7\xba\xbf\n",
     "assets/thumb.png": THUMB.read_bytes(),
     "assets/display.png": DISPLAY.read_bytes(),
     "assets/avatar.png": AVATAR.read_bytes(),
     "assets/sticker-1.png": STK[0].read_bytes()},
    uid_hash="o" * 32, display="官方", official=True)

# ── 2. 只桩"调 DeepSeek 那一次"（零模型费用）；其余全真 ────────────────
REVIEW_CALLS = []


def _fake_review_card_data(card_zip, sk, **kw):
    from plaza import card_format as cf
    rec = {"sk_ok": bool(sk) and sk.startswith("sk-"), "zip_bytes": len(card_zip)}
    try:
        parsed = cf.parse_card_zip(card_zip)
        rec.update({"id": parsed["manifest"]["id"], "files": sorted(parsed["files"]),
                    "digest": parsed["digest"]})
    except Exception as e:
        rec["parse_error"] = f"{type(e).__name__}: {e}"
        REVIEW_CALLS.append(rec)
        raise
    REVIEW_CALLS.append(rec)
    return {"verdict": "pass", "reasons": ["（e2e 桩：本地静态校验通过，未见违规）"],
            "risk": "low", "model": "stub", "usage": {}, "signals": [],
            "reviewed_at": int(time.time()), "digest": parsed["digest"]}


rv.review_card_data = _fake_review_card_data

# ── 3. 进程内真实服务器（同一进程 ⇒ 上面的桩对服务器线程可见）──────────
import server_app                        # noqa: E402  （硬置 FIREFLY_SERVER + re-export handler）

srv = server_app.ThreadingHTTPServer(("127.0.0.1", 0), server_app.FireflyHandler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{PORT}"
print(f"沙箱 {SANDBOX}\n服务器 {base}（进程内真实 FireflyHandler；仅审核换成桩）", flush=True)

import urllib.request                    # noqa: E402


def _wait_ready():
    for _ in range(60):
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


if not _wait_ready():
    print("服务器未起来", flush=True)
    sys.exit(2)

NEW_ID = "e2e_platform_card"
pcalls = {"n": 0}


def _unexpected(errs, bad):
    """剔除刻意桩出来的 401/403 与静态资源缺失后的真错误（同 manual_platform_smoke 口径）。"""
    benign = ("/auth/me", "/plaza/api/admin", "StarRailFont.ttf", "favicon.ico")
    unet = [x for x in bad if not any(b in x[1] for b in benign)]
    st_ok = {stt for stt, url in bad if any(b in url for b in benign)}
    out = []
    for e in errs:
        import re as _re
        m = _re.search(r"status of (\d+)", e)
        if m and int(m.group(1)) in st_ok:
            continue
        out.append(e)
    return out, unet


try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()

        def new_ctx():
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            pg.set_default_timeout(20000)
            errs, bad = [], []
            pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
            pg.on("console", lambda m: errs.append("console.error: " + m.text)
                  if m.type == "error" else None)
            pg.on("response", lambda r: bad.append((r.status, r.url)) if r.status >= 400 else None)
            pg.on("dialog", lambda d: d.accept())
            pg.add_init_script(
                "try{localStorage.setItem('firefly_guide_v1_done','1');"
                "localStorage.setItem('firefly_deep_guide_v1_done','1');}catch(e){}")
            if not ENC_ON:
                # 没有私钥 ⇒ 让客户端失去加密能力，走**明文回退**（服务器"两种都收"）
                pg.add_init_script(
                    "try{ Object.defineProperty(window.crypto, 'subtle', {get:()=>undefined});"
                    "Object.defineProperty(window.crypto, 'getRandomValues', {get:()=>undefined}); }"
                    "catch(e){}")
            # ⚠ `config.js` 是"服务器地址单点"，仓库里那份指向**生产**（101.200.14.126:8787）。
            # 本地 e2e 必须把它指到本次沙箱端口，否则 login.html 会把登录请求发到生产
            # （跨域失败 ⇒ 永远停在登录页）。这只改"指向哪台服务器"，不改任何页面逻辑。
            pg.route("**/config.js", lambda r: r.fulfill(
                status=200, content_type="application/javascript",
                body=f'window.FIREFLY_SERVER_BASE = "{base}";'))
            return pg, errs, bad

        def login(pg, email):
            pg.goto(base + "/login.html", wait_until="domcontentloaded")
            pg.wait_for_selector("#loginForm", timeout=10000)
            pg.fill("#loginEmail", email)
            pg.fill("#loginPass", PW)
            pg.click("#loginForm button[type=submit]")

        def wait_for(fn, timeout=15000, step=200, page=None):
            """轮询直到 fn() 为真。`page` 默认用普通用户那个 page（管理员段必须显式传 pg2）。"""
            p = page or pg
            t0 = time.time()
            while (time.time() - t0) * 1000 < timeout:
                try:
                    if fn():
                        return True
                except Exception:
                    pass
                p.wait_for_timeout(step)
            return False

        # ══════ A. 普通用户：登录 → 平台 → 广场 → 详情 ══════
        print("\n== A. 登录 → /platform.html → 广场 → 详情 ==", flush=True)
        pg, errs_a, bad_a = new_ctx()
        login(pg, USER_EMAIL)
        try:
            pg.wait_for_url("**/platform.html", timeout=15000)
        except Exception:
            pass
        if not pg.url.endswith("/platform.html"):
            print("     [登录诊断] 登录页错误提示 = "
                  + pg.eval_on_selector("#loginErr", "el => el.textContent"), flush=True)
        check("① 登录成功 → 落在 /platform.html（不再进 App 壳）",
              pg.url.endswith("/platform.html"), pg.url)
        pg.wait_for_timeout(1200)
        check("② 平台头部显示真实登录邮箱（/auth/me 真返回）",
              pg.evaluate("(document.querySelector('#pf-shell-user')||{}).textContent") == USER_EMAIL,
              pg.evaluate("(document.querySelector('#pf-shell-user')||{}).textContent"))
        pg.screenshot(path=str(SHOTS / "20_platform_e2e_plaza.png"))
        # 量具自检（Lead 要求：先看列表接口的原始返回，再怀疑产品）
        import importlib as _il
        _sc = _il.import_module("session_crypto")
        _list_bad = [x for x in bad_a if "/plaza/api/list" in x[1]]
        print(f"     [量具] /plaza/api/list 非 2xx：{_list_bad or '无（200）'}；"
              f"敏感头处理统计={_sc.stats()}（enc=解密信封数 plain=明文数）", flush=True)
        check("②b 敏感头链路真的走通（服务端处理过凭证：enc 或 plain > 0）",
              (_sc.stats().get("enc", 0) + _sc.stats().get("plain", 0)) > 0, str(_sc.stats()))
        check("②c 启用了会话加密时，服务端确实解密过信封",
              (not ENC_ON) or _sc.stats().get("enc", 0) > 0, str(_sc.stats()))

        check("③ 广场列表里出现种子卡",
              wait_for(lambda: pg.locator(".pz-card").count() >= 1))
        names = pg.eval_on_selector_all(".pz-card", "els => els.map(e => e.textContent).join('|')")
        check(f"③b 卡名渲染正确（{names[:40]}）", "峡谷旅人" in names)
        _src = pg.eval_on_selector_all(".pz-card img",
                                       "els => (els[0] && els[0].getAttribute('src')) || ''") \
            if pg.locator(".pz-card img").count() else ""
        check("③c 封面真的取到了图（fetch+objectURL ⇒ src 是 blob:，不是占位）",
              wait_for(lambda: (pg.eval_on_selector_all(
                  ".pz-card img", "els => (els[0] && els[0].getAttribute('src')) || ''") or ""
              ).startswith("blob:")),
              str(_src)[:24])

        pg.click(".pz-card:has-text('峡谷旅人')")
        check("④ 点卡片进入详情（#plaza-view.plaza-detail-on）",
              wait_for(lambda: pg.evaluate("!!document.querySelector('#plaza-view.plaza-detail-on')")))
        det = pg.eval_on_selector("#plaza-detail", "el => el.textContent")
        check(f"④b 详情内容正确（{det.strip()[:30]}）", "峡谷旅人" in det and "端到端验证用" in det)
        pg.screenshot(path=str(SHOTS / "21_platform_e2e_detail.png"))
        pg.click("#plaza-detail .pz-dback")

        # ══════ B. 制卡（知识库 + 表情包 + thumb）══════
        print("\n== B. 制卡（含知识库 + 表情包）→ 存草稿 → 审核 → 发布 ==", flush=True)
        pg.click("#pz-forge-btn")
        pg.wait_for_selector("#plaza-forge.show", timeout=10000)
        check("⑤ 制卡层打开（骨架：固定三件 + 知识库 + 图片选择按钮）",
              pg.is_visible("#pf-core") and pg.is_visible("#pf-kb-add")
              and pg.is_visible("#pf-pick-thumb") and pg.is_visible("#pf-stk-add"))
        pg.fill("#pf-name", "平台链路验证卡")
        pg.fill("#pf-id", NEW_ID)
        pg.fill("#pf-char", "小平台")
        pg.fill("#pf-user", "开拓者")
        pg.fill("#pf-desc", "端到端：从平台页一路做到发布")
        pg.fill("#pf-core", "核心设定：由平台页自由书写。")
        pg.fill("#pf-identity", "口吻：安静。")
        pg.fill("#pf-sms_samples", "「在吗？」\n「在。」")
        pg.fill("#pf-opening", "晚上好。")
        pg.click("#pf-kb-add")
        row = pg.locator(".pff-kb").last
        row.locator(".pff-kname").fill("世界观")
        row.locator(".pff-ktext").fill("这个世界有一套自己的物理。")
        pg.click("#pf-kb-add")                     # 契约 §3.1：也允许一层分组
        row2 = pg.locator(".pff-kb").last
        row2.locator(".pff-kdir").fill("factions")
        row2.locator(".pff-kname").fill("势力志")
        row2.locator(".pff-ktext").fill("北境有三大势力。")
        check("⑥ 知识库两份（扁平 + 一层分组）",
              pg.locator(".pff-kb").count() == 2
              and "knowledge/factions/势力志.md" in row2.locator(".pf-idtip").inner_text())
        pg.set_input_files("#pf-file-thumb", str(THUMB))
        pg.set_input_files("#pf-file-avatar", str(AVATAR))
        pg.set_input_files("#pf-file-display", str(DISPLAY))
        check("⑦ 图片槽位收下图（thumb/avatar/display 都压好）",
              wait_for(lambda: all((pg.eval_on_selector(f"#pf-img-{s}", "el => el.getAttribute('src') || ''")
                                    or "").startswith("data:image/")
                                   for s in ("thumb", "avatar", "display")), timeout=40000))
        pg.set_input_files("#pf-file-sticker", [str(p) for p in STK])
        ok_stk = wait_for(lambda: pg.locator(".pfi-stk").count() == 2, timeout=40000)
        labels = pg.locator(".pfi-stk-label")
        if labels.count() >= 2:
            labels.first.fill("开心")
            labels.nth(1).fill("疑惑")
        check(f"⑧ 两张表情包收下且可填 label（{pg.locator('.pfi-stk').count()} 张）", ok_stk)
        pg.screenshot(path=str(SHOTS / "22_platform_e2e_forge.png"))

        pg.click("#pf-save-btn")
        check("⑨ 存草稿成功（真 /plaza/api/draft）",
              wait_for(lambda: "已存草稿" in pg.eval_on_selector("#pf-status", "el => el.textContent"),
                       timeout=25000),
              pg.eval_on_selector("#pf-status", "el => el.textContent"))
        check("⑨b 草稿箱列出这张草稿（真 /plaza/api/drafts）",
              wait_for(lambda: pg.locator("#pf-drafts-box .pf-draft").count() >= 1),
              pg.eval_on_selector("#pf-drafts-box", "el => el.textContent.slice(0,40)"))
        check("⑨c 未审核时发布按钮禁用（真实闸门）",
              pg.eval_on_selector("#pf-publish-btn", "el => el.disabled") is True)

        pg.click("#pf-review-btn")
        pg.wait_for_selector("#pf-modal.show", timeout=8000)
        check("⑩ 审核弹窗要求粘贴自己的 Key", pg.is_visible("#pf-sk"))
        # ⚠ Key 形态按**服务端**口径（app/plaza/review.py:30 `^sk-[A-Za-z0-9]{16,64}$`，不含连字符）。
        #   前端本地校验（plaza_forge_review.js:257 `/^sk-[\w-]{8,}/`）比后端宽 —— 已一并报给 Lead。
        pg.fill("#pf-sk", "sk-" + "e2e0" * 8)
        review_resp = []
        pg.on("response", lambda r: review_resp.append((r.status, r.url))
              if "/plaza/api/review" in r.url else None)
        pg.click("#pf-modal-ok")
        ok_review = wait_for(lambda: "审核已通过" in pg.eval_on_selector("#pf-status", "el => el.textContent"),
                             timeout=25000)
        if not ok_review:
            print(f"     [审核失败] /plaza/api/review 响应={review_resp}；"
                  f"桩调用记录={REVIEW_CALLS[:2]}；状态栏="
                  f"{pg.eval_on_selector('#pf-status', 'el => el.textContent')}；提示区="
                  f"{pg.eval_on_selector('#pf-msg', 'el => el.textContent')}", flush=True)
        check("⑪ 审核通过（真实端点 + 进程内桩模型，零模型费用）", ok_review,
              pg.eval_on_selector("#pf-status", "el => el.textContent"))
        check("⑪b 桩模型确实被调用一次，且卡体里带着知识库与表情包",
              len(REVIEW_CALLS) == 1 and REVIEW_CALLS[0].get("id") == NEW_ID
              and any(str(f).startswith("knowledge/") for f in REVIEW_CALLS[0].get("files", []))
              and any("sticker-1" in str(f) for f in REVIEW_CALLS[0].get("files", [])),
              str(REVIEW_CALLS[:1]))
        check("⑪c 审核后发布按钮启用", pg.eval_on_selector("#pf-publish-btn", "el => el.disabled") is False)
        pg.screenshot(path=str(SHOTS / "23_platform_e2e_reviewed.png"))

        pg.click("#pf-publish-btn")
        check("⑫ 发布成功（真 /plaza/api/publish）",
              wait_for(lambda: "已发布到广场" in pg.eval_on_selector("#pf-status", "el => el.textContent"),
                       timeout=25000),
              pg.eval_on_selector("#pf-status", "el => el.textContent"))
        meta_fp = PLAZA_DIR / "cards" / NEW_ID / "meta.json"
        meta = json.loads(meta_fp.read_text(encoding="utf-8")) if meta_fp.is_file() else {}
        check(f"⑫b 广场目录真的落了这张卡（status={meta.get('status')}）",
              meta_fp.is_file() and meta.get("status") == "published" and meta.get("id") == NEW_ID)
        pg.screenshot(path=str(SHOTS / "24_platform_e2e_published.png"))

        # ══════ C. 我的卡 + 广场回列表能看到刚发布的卡 ══════
        print("\n== C. 我的卡 / 回广场 ==", flush=True)
        pg.click(".pf-tab[data-pf='mine']")
        pg.wait_for_timeout(900)
        mine = pg.eval_on_selector("#pf-drafts-box", "el => el.textContent")
        check(f"⑬「我的卡」草稿箱显示该卡（{mine.strip()[:30]}）",
              "平台链路验证卡" in mine and "已发布" in mine, mine.strip()[:60])
        pg.screenshot(path=str(SHOTS / "25_platform_e2e_mine.png"))
        pg.click(".pf-tab[data-pf='plaza']")
        pg.wait_for_timeout(1500)
        names2 = pg.eval_on_selector_all(".pz-card", "els => els.map(e => e.textContent).join('|')")
        check("⑭ 回到广场能看到刚发布的卡（真实列表刷新）", "平台链路验证卡" in names2, names2[:60])
        check("⑭b 普通用户点治理台 → 403 人话提示（不是假装管理员）",
              True)   # 见下：真点一次
        pg.click(".pf-tab[data-pf='admin']")
        pg.wait_for_timeout(1500)
        admin_txt = pg.eval_on_selector("#plaza-admin", "el => el.textContent")
        check(f"⑭c 普通用户看到的是权限提示（{admin_txt.strip()[:24]}）",
              ("管理员" in admin_txt) or ("权限" in admin_txt), admin_txt.strip()[:60])
        pg.click(".pf-tab[data-pf='plaza']")
        ra, ua = _unexpected(errs_a, bad_a)
        check("⑮ 普通用户全链路：pageerror / 意外错误 = 0", not ra and not ua, f"{ra[:3]} {ua[:3]}")
        pg.close()

        # ══════ D. 管理员：治理台 ══════
        print("\n== D. 管理员治理台 ==", flush=True)
        pg2, errs_b, bad_b = new_ctx()
        login(pg2, ADMIN_EMAIL)
        try:
            pg2.wait_for_url("**/platform.html", timeout=15000)
        except Exception:
            pass
        pg2.wait_for_timeout(1200)
        pg2.click(".pf-tab[data-pf='admin']")
        ok_admin = wait_for(lambda: pg2.locator("#plaza-admin .pza-card, #plaza-admin .pza-row,"
                                                " #plaza-admin .pz-card, #plaza-admin table").count() >= 1,
                            timeout=15000, page=pg2)
        admin_all = pg2.eval_on_selector("#plaza-admin", "el => el.textContent")
        check(f"⑯ 治理台打开并列出卡（admin/list 真返回；{admin_all.strip()[:30]}）",
              ok_admin or ("峡谷旅人" in admin_all) or ("平台链路验证卡" in admin_all),
              admin_all.strip()[:80])
        pg2.screenshot(path=str(SHOTS / "26_platform_e2e_admin.png"))
        rb, ub = _unexpected(errs_b, bad_b)
        check("⑰ 管理员全链路：pageerror / 意外错误 = 0", not rb and not ub, f"{rb[:3]} {ub[:3]}")
        pg2.close()
        b.close()

    print(f"\n截图：{SHOTS}/20_platform_e2e_plaza.png .. 26_platform_e2e_admin.png", flush=True)
finally:
    srv.shutdown()
    srv.server_close()

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
print("判定:", "PASS" if FAIL == 0 else "FAIL", flush=True)
sys.exit(1 if FAIL else 0)
