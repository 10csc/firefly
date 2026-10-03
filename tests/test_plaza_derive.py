# -*- coding: utf-8 -*-
"""`POST /plaza/api/derive` —— "以某张广场卡为模板改编"**不再整卡重传**（2026-10-01 用户要求）

用户原话：「用户如果以某角色卡为模版，那发布的时候会不会重新复制一份上传？（我希望不会，
最好只会创建副本，上传只会替换修改的部分，这样节省流量）反正服务器内部复制就一瞬间。」

本测试在真实 `server_app.py` 进程上跑（两端账号 + 真 HTTP），钉死：
  ① 只改一个文本 ⇒ `uploaded.files` 只有那一个（其余是服务端复制来的）；
  ② 未改的图片**不传**也照样出现在新草稿里（字节与基础卡一致）；
  ③ 删掉一份知识库 ⇒ 产物里确实没有、且 `stickers.json` 不悬空；
  ④ 原卡的 `card.zip` / `meta.json` **字节不变**（改编绝不动原卡）；
  ⑤ 未登录 ⇒ 401；`base_id` 不存在/未发布 ⇒ 404 人话；
  ⑥ 隐私红线：产物只含 `character/**` 白名单卡文件、**不带基础卡作者的 author/uid_hash**；
  ⑦ 流量：同一次改编，**旧做法（整卡重传）vs 新做法（只传 patch）**的实际请求体字节数对比；
  ⑧ `derived_from` 只记录在广场 meta、**不进任何客户端投影**。
"""
import base64
import hashlib
import json
import os
import shutil
import socket as _sk
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_plaza_derive_"))
USER_DIR = SANDBOX / "user_data"
PLAZA_DIR = SANDBOX / "plaza"
USER_DIR.mkdir(parents=True, exist_ok=True)
PLAZA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "FIREFLY_ANDROID": "1", "FIREFLY_DATA_DIR": str(SANDBOX),
    "FIREFLY_PLAZA_DIR": str(PLAZA_DIR), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
})
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}", flush=True)


import db as srv_db                              # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID_ADMIN = srv_db.create_user("derive_admin@qq.com", "h", "s",
                               install_id="inst-" + "x" * 32, role="admin")
UID_USER = srv_db.create_user("derive_user@qq.com", "h", "s",
                              install_id="inst-" + "y" * 32, role="user")
TOK_ADMIN, TOK_USER = "tok-derive-admin", "tok-derive-user"
srv_db.create_session(TOK_ADMIN, UID_ADMIN, "d-admin", EXP)
srv_db.create_session(TOK_USER, UID_USER, "d-user", EXP)


def png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def data_url(blob, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


THUMB, DISPLAY, STICK = png(320, 200), png(1024, 768), png(160, 160)
BIG_TEXT = "知识正文" * 12000                      # ≈144KB（未改的那份，用来体现"不用重传"）
_files = [
    {"path": "core.md", "text": "# 核心设定\n模板卡"},
    {"path": "knowledge/大世界.md", "text": BIG_TEXT},
    {"path": "knowledge/待删.md", "text": "这份稍后会被删掉"},
]
_images = {"thumb": data_url(THUMB), "display": data_url(DISPLAY),
           "stickers": [{"data": data_url(STICK), "label": "开心"},
                        {"data": data_url(STICK), "label": "疑惑"}]}
BASE_CARD = {"id": "base_card", "name": "模板卡", "char_name": "小萤", "user_name": "你",
             "presentation": "sticker", "desc": "给 derive 用的模板", "category": "陪伴",
             "tags": ["模板"], "files": _files, "images": _images}


def pick_port():
    s = _sk.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = pick_port()
env = dict(os.environ)
env.update({"FIREFLY_PORT": str(PORT), "PYTHONUNBUFFERED": "1"})
log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def req(method, path, tok=None, body=None, raw=False):
    url = base + urllib.parse.quote(path, safe="/?=&%:#")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            blob = resp.read()
            return resp.status, (blob if raw else json.loads(blob.decode("utf-8") or "{}"))
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            return e.code, (blob if raw else json.loads(blob.decode("utf-8") or "{}"))
        except Exception:
            return e.code, {}


def card_zip_bytes(cid):
    return (PLAZA_DIR / "cards" / cid / "card.zip").read_bytes()


def card_files(cid):
    with zipfile.ZipFile(PLAZA_DIR / "cards" / cid / "card.zip") as zf:
        return {n[len("character/"):]: zf.read(n) for n in zf.namelist()
                if n.startswith("character/")}


def sha(blob):
    return hashlib.sha256(blob).hexdigest()[:12]


try:
    for _ in range(80):
        try:
            if req("GET", "/health")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        print("服务器未起来：", (SANDBOX / "server.log").read_text("utf-8", "replace")[-1200:])
        sys.exit(2)

    # ── 准备：admin 发一张**已发布**的模板卡（admin 免审，走既有闸门）──
    s, d = req("POST", "/plaza/api/draft", TOK_ADMIN, {"card": BASE_CARD, "display": "官方"})
    check(f"前置：admin 存基础卡草稿（status={s}）", s == 200 and d.get("draft", {}).get("id") == "base_card",
          str(d)[:200])
    s, d = req("POST", "/plaza/api/publish", TOK_ADMIN, {"id": "base_card"})
    check(f"前置：admin 发布基础卡（status={s}）", s == 200, str(d)[:200])
    base_zip0, base_meta0 = card_zip_bytes("base_card"), (PLAZA_DIR / "cards" / "base_card" / "meta.json").read_bytes()
    bf0 = card_files("base_card")
    check(f"前置：基础卡含 {len(bf0)} 个文件（knowledge 2 份 + 2 表情包 + 两图 + core）",
          "knowledge/大世界.md" in bf0 and "knowledge/待删.md" in bf0 and len(bf0) >= 7, str(sorted(bf0)))

    # ── ① 只改一个文本 ⇒ 只传那一个文件 ──
    print("\n== ① 只改一个文本：patch 只含那一个文件 ==")
    NEW_CORE = "# 核心设定（改编）\n我把 core 改了一句"
    s, d = req("POST", "/plaza/api/derive", TOK_USER, {
        "base_id": "base_card",
        "card": {"id": "derived_card", "name": "模板卡·改编", "files": [{"path": "core.md", "text": NEW_CORE}]},
        "display": "改编者"})
    check(f"derive 200 且新草稿 id={d.get('draft', {}).get('id')}（status={s}）",
          s == 200 and d.get("draft", {}).get("id") == "derived_card", str(d)[:220])
    check(f"uploaded.files 只有 core.md（实际 {d.get('uploaded', {}).get('files')}）",
          d.get("uploaded", {}).get("files") == ["core.md"])
    check(f"copied 报出服务端复制量（{d.get('copied')}）",
          (d.get("copied") or {}).get("files", 0) >= 6)

    # 产物 = 该用户的**草稿**（在 drafts 目录里，不是广场卡）
    draft_zip = None
    for p in (PLAZA_DIR / "drafts").rglob("*.zip"):
        if p.stem == "derived_card":
            draft_zip = p
            break
    check("产物落在**当前用户的草稿**里（不是广场卡）", draft_zip is not None and "base_card" not in str(draft_zip))
    with zipfile.ZipFile(draft_zip) as zf:
        nf = {n[len("character/"):]: zf.read(n) for n in zf.namelist() if n.startswith("character/")}
    check("① patch 的改动生效", nf["core.md"].decode("utf-8") == NEW_CORE)
    check("① 未改的文件**字节与基础卡一致**（服务端复制的）",
          nf["knowledge/大世界.md"] == bf0["knowledge/大世界.md"]
          and sha(nf["knowledge/大世界.md"]) == sha(bf0["knowledge/大世界.md"]))
    check(f"⑥ 产物只含卡体白名单文件（{len(nf)} 个，无账号私有文件）",
          all(not p.startswith(("memory/", "chat", "config", "user_data")) for p in nf)
          and set(nf) - set(bf0) <= {"core.md"})
    _m = json.loads([p for p in draft_zip.parent.glob("*.json") if p.stem == "derived_card"][0]
                    .read_text(encoding="utf-8"))
    check(f"⑧ 草稿 meta 记了 derived_from={_m.get('derived_from')!r}（只记录）",
          _m.get("derived_from") == "base_card")
    from plaza import card_format as cf
    _pm = cf.parse_card_zip(draft_zip.read_bytes())["manifest"]
    check("⑥ 产物 manifest **不带基础卡作者的 author/uid_hash**（隐私红线）",
          "author" not in _pm or not (_pm.get("author") or {}).get("uid_hash"))

    # ── ② 未改的图片不传也在 ──
    print("\n== ② 未改的图片不传也照样在（服务端复制的）==")
    check("① 请求里没有任何图片字段（省流量）", not any(k.startswith("assets/") for k in d.get("uploaded", {}).get("images") or []))
    check("② thumb/display/两张表情包都在且字节一致",
          all(nf.get(k) == bf0.get(k) for k in
              ("assets/thumb.png", "assets/display.png", "assets/sticker-1.png", "assets/sticker-2.png")),
          str(sorted(nf)))

    # ── ③ 删一份知识库 + 删一张表情包（含标签不悬空）──
    print("\n== ③ 删除：产物里确实没有、标签不悬空 ==")
    s, d3 = req("POST", "/plaza/api/derive", TOK_USER, {
        "base_id": "base_card",
        "card": {"id": "derived_del"},
        "remove": {"files": ["knowledge/待删.md"], "images": ["sticker-2"]}})
    check(f"derive(删除版) 200（status={s}）", s == 200, str(d3)[:200])
    dz = None
    for p in (PLAZA_DIR / "drafts").rglob("*.zip"):
        if p.stem == "derived_del":
            dz = p
            break
    with zipfile.ZipFile(dz) as zf:
        df = {n[len("character/"):]: zf.read(n) for n in zf.namelist() if n.startswith("character/")}
    check("③ 被删的知识库确实不在产物里", "knowledge/待删.md" not in df)
    check("③ 被删的表情包确实不在产物里", "assets/sticker-2.png" not in df)
    check("③ 标签不悬空（stickers.json 只剩 sticker-1）",
          json.loads(df["stickers.json"].decode("utf-8")) == {"sticker-1.png": "开心"},
          df.get("stickers.json", b"").decode("utf-8", "replace"))

    # ── ④ 原卡字节不变 ──
    print("\n== ④ 原卡（卡体 + meta）字节不变 ==")
    check(f"④ base card.zip 未变（{sha(base_zip0)} → {sha(card_zip_bytes('base_card'))}）",
          card_zip_bytes("base_card") == base_zip0)
    check(f"④ base meta.json 未变（{sha(base_meta0)} → {sha((PLAZA_DIR / 'cards' / 'base_card' / 'meta.json').read_bytes())}）",
          (PLAZA_DIR / "cards" / "base_card" / "meta.json").read_bytes() == base_meta0)
    check("④ 基础卡仍在广场列表（未被改编影响）", req("GET", "/plaza/api/card?id=base_card", TOK_USER)[0] == 200)

    # ── ⑤ 401 / 404 ──
    print("\n== ⑤ 鉴权与基础卡闸门 ==")
    s5, d5 = req("POST", "/plaza/api/derive", None, {"base_id": "base_card"})
    check(f"⑤ 未登录 ⇒ 401（实际 {s5}）", s5 == 401)
    s6, d6 = req("POST", "/plaza/api/derive", TOK_USER, {"base_id": "no_such_card"})
    check(f"⑤ 不存在的 base ⇒ 404 人话（{d6.get('error')}）", s6 == 404 and "未发布" in str(d6.get("error") or ""))
    # 非 published：admin 存一张**草稿**当 base
    req("POST", "/plaza/api/draft", TOK_ADMIN, {"card": {**BASE_CARD, "id": "draft_only"}})
    s7, d7 = req("POST", "/plaza/api/derive", TOK_USER, {"base_id": "draft_only"})
    check(f"⑤ 未发布的 base ⇒ 404（实际 {s7}：{d7.get('error')}）", s7 == 404)

    # ── ⑦ 流量对比：整卡重传 vs 只传 patch ──
    print("\n== ⑦ 流量：同一次改编，旧做法（整卡重传）vs 新做法（只传 patch）==")
    # 旧做法 = 客户端把 base 的正文与**全部图片**以 data URL 重新 POST 一遍（制卡页 payload）
    _imgs_old, _stickers_old = {}, []
    for slot in ("thumb", "display"):
        _imgs_old[slot] = data_url(bf0[f"assets/{slot}.png"])
    for i in (1, 2):
        _stickers_old.append({"data": data_url(bf0[f"assets/sticker-{i}.png"]),
                              "label": "开心" if i == 1 else "疑惑"})
    _imgs_old["stickers"] = _stickers_old
    payload_old = {"card": {**{k: v for k, v in BASE_CARD.items() if k not in ("files", "images")},
                            "files": [{"path": p, "text": bf0[p].decode("utf-8")}
                                      for p in ("core.md", "knowledge/大世界.md", "knowledge/待删.md")],
                            "images": _imgs_old}, "display": "改编者"}
    payload_new = {"base_id": "base_card",
                   "card": {"id": "derived_card2", "name": "模板卡·改编",
                            "files": [{"path": "core.md", "text": NEW_CORE}]},
                   "display": "改编者"}
    old_b = len(json.dumps(payload_old, ensure_ascii=False).encode("utf-8"))
    new_b = len(json.dumps(payload_new, ensure_ascii=False).encode("utf-8"))
    print(f"     旧做法（整卡重传）请求体 = {old_b} B（{old_b/1024:.1f} KB）")
    print(f"     新做法（derive patch）请求体 = {new_b} B（{new_b/1024:.1f} KB）")
    print(f"     省下 {old_b - new_b} B（↓{(1 - new_b / old_b) * 100:.1f}%）")
    check(f"⑦ patch 请求体比整卡重传小 10 倍以上（{new_b}B vs {old_b}B）", new_b * 10 < old_b)
    s8, d8 = req("POST", "/plaza/api/derive", TOK_USER, payload_new)
    check(f"⑦ 同一份 patch 真跑成功（{s8}，产物 {d8.get('draft', {}).get('id')}）",
          s8 == 200 and d8.get("draft", {}).get("id") == "derived_card2")

    # ── ⑧ derived_from 只记录、不进投影（发布后核对）──
    print("\n== ⑧ derived_from：只落在广场 meta，不进客户端投影 ==")
    s9, d9 = req("POST", "/plaza/api/derive", TOK_ADMIN, {"base_id": "base_card",
                                                          "card": {"id": "admin_derived"}})
    s10, _ = req("POST", "/plaza/api/publish", TOK_ADMIN, {"id": "admin_derived"})
    _am = json.loads((PLAZA_DIR / "cards" / "admin_derived" / "meta.json").read_text(encoding="utf-8"))
    check(f"⑧ 广场 meta 里记了 derived_from（{_am.get('derived_from')!r}，publish={s10}）",
          s9 == 200 and s10 == 200 and _am.get("derived_from") == "base_card")
    _sv, _dv = req("GET", "/plaza/api/card?id=admin_derived", TOK_USER)
    check("⑧ 客户端详情**看不到** derived_from（只记录不暴露）",
          "derived_from" not in json.dumps(_dv, ensure_ascii=False))
    _sv, _lv = req("GET", "/plaza/api/list?page=1&size=20", TOK_USER)
    check("⑧ 列表也不含 derived_from / sticker_labels 等 meta 内部字段",
          "derived_from" not in json.dumps(_lv, ensure_ascii=False)
          and "sticker_labels" not in json.dumps(_lv, ensure_ascii=False))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log.close()
    shutil.rmtree(SANDBOX, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
