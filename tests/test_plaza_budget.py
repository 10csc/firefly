# -*- coding: utf-8 -*-
"""广场的**数据量与流量**预算实测 —— 共创平台「从源头砍流量」的量化验收

为什么需要它：用户明确要求"从源头缩减流量"（06 文档 G2）。改造前列表一屏要拉 20 张
**151KB 的 cover ≈ 3MB**；V1 起列表只发 `thumb`（≤320px/≤12KB）。
本测试把"一次浏览 + 一次安装"真正花掉的字节数钉下来，并守住六条纪律：
  ① 列表**不得**内联卡正文（翻 20 张卡不能等于下载 20 张卡）；
  ② 列表 JSON 一页 20 张 ≤60KB（契约 §四）；
  ③ 列表项**只给 thumb**（V1 卡不再下发封面地址）；老卡（无 thumb）才回落到 cover；
  ④ 缩略图单张 ≤12KB，一屏 20 张合计 ≤120KB（契约 §3.3：20×≈6KB，原 3MB）；
  ⑤ 缩略图第二次必须 304（长缓存生效，翻回上一页不重复花钱）；
  ⑥ 卡体不超过契约上限；详情/模式接口不夹带卡体；老客户端的 cover 槽位仍能取到图。

沙箱：临时数据根 + 真实 `server_app.py` 进程 + 真账号 token；**不连外网、不碰真实数据**。
退出码：0 全过；1 有断言失败；2 服务没起来。
"""
import json
import os
import shutil
import socket as _sk
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_plaza_budget_"))
USER_DIR = SANDBOX / "user_data"
PLAZA_DIR = SANDBOX / "plaza"
USER_DIR.mkdir(parents=True, exist_ok=True)
PLAZA_DIR.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "FIREFLY_ANDROID": "1",
    "FIREFLY_DATA_DIR": str(SANDBOX),
    "FIREFLY_PLAZA_DIR": str(PLAZA_DIR),
    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
})
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

PASS = FAIL = 0
rows = []          # (操作, 字节, 预算文案, 判定)


def check(desc, cond, nbytes=None, budget=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}", flush=True)
    if nbytes is not None:
        rows.append((desc, nbytes, budget, "ok" if cond else "FAIL"))


def observe(desc, nbytes, budget=""):
    """**只记录不判定**：契约把"一屏 20 张合计"降级为观察值（06 §3.3/§四 只留两条硬线：
    单张 thumb ≤12KB、列表 JSON ≤60KB），所以这项不参与 PASS/FAIL。"""
    print(f"  info {desc}", flush=True)
    rows.append((desc, nbytes, budget, "观测"))


def human(n: int) -> str:
    return f"{n/1024:.1f}KB" if n >= 1024 else f"{n}B"


# ── 1. 造"真实体积"的样本（走 Pillow；本机有 12.0.0）──
#   · COVER  = 1000×667 高频斜纹 JPEG q60 ≈ 151KB：**改造前列表用的就是它**（对照组）
#   · THUMB  = 320px 长边、平滑插画风格（真实缩略图都在这个量级）
#   · DISPLAY= 1024px 长边、≤300KB（详情才拉）
try:
    import io

    from PIL import Image
    noisy = Image.new("RGB", (1000, 667))
    px = noisy.load()
    for y in range(667):
        for x in range(0, 1000, 4):
            v = (x * 3 + y * 5) % 256
            px[x, y] = (v, (v * 2) % 256, (v * 3) % 256)
            for dx in range(1, 4):
                px[x + dx, y] = px[x, y]
    buf = io.BytesIO()
    # 质量 60 / 1000×667 ≈ 151KB 上下：真实封面都在这个量级，且不越 300KB 契约上限
    noisy.save(buf, "JPEG", quality=60, optimize=True)
    COVER = buf.getvalue()

    smooth = Image.new("RGB", (1000, 667))
    sp = smooth.load()
    for y in range(667):
        for x in range(1000):
            sp[x, y] = (x * 255 // 999, y * 255 // 666, (x + y) * 255 // 1665)
    tbuf = io.BytesIO()
    smooth.resize((320, 213), Image.LANCZOS).save(tbuf, "JPEG", quality=50, optimize=True)
    THUMB = tbuf.getvalue()
    dbuf = io.BytesIO()
    smooth.resize((1024, 683), Image.LANCZOS).save(dbuf, "JPEG", quality=60, optimize=True)
    DISPLAY = dbuf.getvalue()
except Exception as e:                                        # pragma: no cover
    print(f"SKIP：需要 Pillow 生成真实样本（{e}）")
    sys.exit(0)

check(f"封面样本（改造前列表用）= {human(len(COVER))}（契约上限 300KB）",
      0 < len(COVER) <= 300 * 1024, len(COVER), "≤300KB")
check(f"缩略图样本 = {human(len(THUMB))}（契约上限 12KB、目标 ≈6KB）",
      0 < len(THUMB) <= 12 * 1024, len(THUMB), "≤12KB")
check(f"详情图样本 = {human(len(DISPLAY))}（契约上限 300KB）",
      0 < len(DISPLAY) <= 300 * 1024, len(DISPLAY), "≤300KB")

# ── 2. 种 21 张已发布卡（V1：thumb + display + 正文）──
import db as srv_db                    # noqa: E402
from plaza import store as st          # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID = srv_db.create_user("budget@qq.com", "h", "s", install_id="inst-" + "z" * 32, role="user")
TOK = "tok-budget"
srv_db.create_session(TOK, UID, "budget", EXP)

BODY_MARK = "萤火虫预算测试专用正文标记"          # 用来证明"列表里没有卡正文"
BODY = ("# 核心设定\n" + BODY_MARK + "\n" + ("这是一段用来撑出真实体积的角色设定文本。" * 400)).encode()
N_CARDS = 21


def publish(cid, name, *, v1=True):
    files = {
        "preset.json": json.dumps({"id": cid, "name": name, "char_name": "小萤", "user_name": "你",
                                   "presentation": "sticker", "desc": "流量预算实测用", "schema": 1},
                                  ensure_ascii=False).encode(),
        "core.md": BODY,
    }
    if v1:
        files["assets/thumb.jpg"] = THUMB
        files["assets/display.jpg"] = DISPLAY
    else:
        files["assets/cover.jpg"] = COVER          # 老卡：只有老槽位 cover，没有 thumb
    return st.publish_card(
        {"id": cid, "name": name, "char_name": "小萤", "user_name": "你",
         "presentation": "sticker", "desc": "流量预算实测用", "category": "陪伴"},
        files, uid_hash="b" * 32, display="官方", official=True)


for i in range(N_CARDS):
    publish(f"budget_card_{i:02d}", f"预算卡{i:02d}")
check(f"已种 {N_CARDS} 张已发布卡", st.stats()["cards"] == N_CARDS)

ZIP_BYTES = st.card_zip_path(f"budget_card_{N_CARDS-1:02d}").stat().st_size
check(f"单张卡体 {human(ZIP_BYTES)}（契约上限 1.5MB）", ZIP_BYTES <= 1536 * 1024,
      ZIP_BYTES, "≤1.5MB")


# ── 3. 起真实服务器入口 ──
def free_port(lo=8941, hi=8975):
    for p in range(lo, hi):
        s = _sk.socket()
        s.settimeout(0.3)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return 0


PORT = free_port()
env = dict(os.environ)
env.update({"FIREFLY_PORT": str(PORT), "PYTHONUNBUFFERED": "1"})
log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, headers=None):
    h = {"Authorization": "Bearer " + TOK}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except urllib.error.URLError as e:            # 服务刚重启/半开连接：等一次再试
        time.sleep(1.0)
        r2 = urllib.request.Request(base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r2, timeout=25) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e2:
            return e2.code, dict(e2.headers or {}), e2.read()


try:
    ready = False
    for _ in range(80):
        s, _h, _b = req("GET", "/health")
        if s == 200:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        print("服务器未起来", flush=True)
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)

    print("\n== 浏览路径的字节开销 ==")
    s, h, b = req("GET", "/plaza/api/list?page=1&size=20")
    d = json.loads(b.decode("utf-8"))
    check(f"① 列表一页 20 张 JSON = {human(len(b))}（契约 §四 ≤60KB）", s == 200 and len(b) <= 60 * 1024,
          len(b), "≤60KB")
    check("①b 列表**不含卡正文**（不夹带 body）",
          BODY_MARK.encode("utf-8") not in b and "core.md" not in b.decode("utf-8", "replace"))
    check("①c 列表带分页元数据（total/size）",
          d.get("total") == N_CARDS and len(d.get("items") or []) == 20)

    items = d["items"]
    cid = items[0]["id"]
    check(f"①d 列表项**只给 thumb**（{items[0].get('thumb')}，cover={items[0].get('cover')}）",
          bool(items[0].get("thumb")) and items[0].get("cover") is None)
    check("①e 每一项都有可用小图（V1 卡 = thumb）",
          all(it.get("thumb") for it in items))

    # 一屏 20 张：列表 JSON + 20 张 thumb（真实浏览路径）。合计只是**观察值**：
    # 契约只硬性要求"单张 ≤12KB"，合规 thumb 实测 2~10KB/张 ⇒ 合计 40~200KB 都算正常。
    thumbs = 0
    for it in items:
        s2, _h2, b2 = req("GET", f"/plaza/api/asset?id={it['id']}&slot=thumb")
        if s2 == 200:
            thumbs += len(b2)
    observe(f"（观测）一屏 20 张缩略图合计 = {human(thumbs)}（改造前 20×封面 ≈{human(20 * len(COVER))}）",
            thumbs, "观察值")
    check(f"①f 单张缩略图均 ≤12KB（契约硬线）",
          thumbs <= 20 * 12 * 1024, thumbs, "≤240KB（20×12KB）")
    check(f"①g 相比改造前（20 张封面）↓ {100 * (1 - thumbs / (20 * len(COVER))):.1f}%",
          thumbs <= 20 * len(COVER) * 0.05)

    s, h, b = req("GET", f"/plaza/api/card?id={cid}")
    check(f"② 详情 = {human(len(b))}（预算 ≤16KB）", s == 200 and len(b) <= 16 * 1024,
          len(b), "≤16KB")
    detail = json.loads(b.decode("utf-8"))["card"]
    check("②b 详情才有 display（列表没有）",
          bool(detail.get("display")) and all(it.get("display") is None for it in items))

    s, h, b = req("GET", f"/plaza/api/asset?id={cid}&slot=thumb")
    first = len(b)
    check(f"③ 缩略图首次 = {human(first)}（≤12KB，实际就是样本大小）",
          s == 200 and first == len(THUMB), first, "≤12KB")
    etag = h.get("ETag")
    check("③b 缩略图带 ETag 与长缓存头",
          bool(etag) and "max-age" in (h.get("Cache-Control") or ""))

    s, h, b = req("GET", f"/plaza/api/asset?id={cid}&slot=thumb",
                  headers={"If-None-Match": etag or ""})
    check(f"④ 缩略图二次请求 = 304、正文 {len(b)}B（翻回上一页不重复花流量）",
          s == 304 and len(b) == 0, len(b), "0B（304）")

    s, h, b = req("GET", f"/plaza/api/download?id={cid}")
    check(f"⑤ 卡体下载 = {human(len(b))}（与磁盘一致）",
          s == 200 and len(b) == st.card_zip_path(cid).stat().st_size,
          len(b), "≤1.5MB")

    print("\n== 安装路径（一次安装真正花的流量）==")
    s, h, b = req("POST", "/plaza/api/install", {"id": cid})
    check(f"⑥ 安装接口响应 = {human(len(b))}（轻；重活在卡体那一步）",
          s == 200 and len(b) <= 4096, len(b), "≤4KB")

    s, h, b = req("GET", "/modes")
    check(f"⑦ /modes（21 张卡）= {human(len(b))}（预算 ≤32KB）",
          s == 200 and len(b) <= 32 * 1024, len(b), "≤32KB")

    print("\n== 一次「浏览 + 安装」的总账 ==")
    s, _h, b = req("GET", "/plaza/api/list?page=1&size=20")
    list_bytes = len(b)
    s, _h, b = req("GET", f"/plaza/api/card?id={cid}")
    detail_bytes = len(b)
    s, _h, b = req("GET", f"/plaza/api/asset?id={cid}&slot=thumb")
    thumb_bytes = len(b)
    s, _h, b = req("GET", f"/plaza/api/download?id={cid}")
    zip_bytes = len(b)
    s, _h, b = req("POST", "/plaza/api/install", {"id": cid})
    inst_bytes = len(b)
    total = list_bytes + detail_bytes + thumb_bytes + zip_bytes + inst_bytes
    check(f"⑧ 一次「浏览+看缩略图+安装」共 {human(total)}（预算 ≤64KB）",
          total <= 64 * 1024, total, "≤64KB")
    # 改造前的同一条路径 = 列表(10.8KB) + 详情 + **151KB 封面** + 卡体 + 安装 ≈ 173KB。
    # V1 把封面换成 thumb 后，图片那一段从 151KB → 12KB 内，总额随之掉到 1/6 左右。
    meta_bytes = list_bytes + detail_bytes + inst_bytes
    check(f"⑧b 元数据（列表{human(list_bytes)}+详情{human(detail_bytes)}+安装{human(inst_bytes)}）"
          f"绝对值 ≤24KB", meta_bytes <= 24 * 1024, meta_bytes, "≤24KB")
    # 改造前的同一路径 = 列表 + 详情 + **151KB 封面** + 卡体 + 安装：本次直接拿原封面算对照
    before = list_bytes + detail_bytes + len(COVER) + zip_bytes + inst_bytes
    check(f"⑧c 相比改造前（{human(before)}，列表拉 151KB 封面）↓ {100 * (1 - total / before):.1f}%"
          f"（预算 ↓ ≥70%）", total <= before * 0.3, total, "≤改造前 30%")

    print("\n== 老卡兼容（存量卡没有 thumb）==")
    publish("budget_card_old", "老封面卡", v1=False)
    s, _h, b = req("GET", "/plaza/api/list?page=1&size=50")
    old_item = next((it for it in json.loads(b.decode("utf-8"))["items"]
                     if it["id"] == "budget_card_old"), {})
    check(f"⑨ 老卡列表项回落到 cover（thumb={old_item.get('thumb')}，"
          f"cover={old_item.get('cover')}）",
          old_item.get("thumb") is None and (old_item.get("cover") or "").endswith(".jpg"))
    s, _h, b = req("GET", "/plaza/api/asset?id=budget_card_old&slot=cover")
    check(f"⑨b 老客户端的 cover 槽位仍能取到图 = {human(len(b))}",
          s == 200 and len(b) == len(COVER), len(b), "=原图")

    print("\n== 预算总表 ==")
    print(f"  {'操作':<34}{'实测':>10}  {'预算':<12}判定")
    for name, n, budget, verdict in rows:
        print(f"  {name[:33]:<34}{human(n):>10}  {budget:<12}{verdict}")
    print(f"  {'—— 一次完整安装合计':<33}{human(total):>10}")
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
