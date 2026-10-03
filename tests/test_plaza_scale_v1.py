# -*- coding: utf-8 -*-
"""广场规模压测 · **V1 新格式卡**版（480 张）—— 补齐 task-11 的规模盲区

为什么需要单独一份：`tests/test_plaza_scale.py` 的 480 张压测用的是**旧格式卡**（只有 name/desc +
一张 cover），而 V1 之后每张卡多了：**一层分组知识库**（`knowledge/<域>/x.md`）、
`thumb`/`display`、0–3 张表情包 + `stickers.json`，持久索引也从 v2 升到 **v3**（槽位 2 → 13）。
列表/详情投影与 `store._find_assets()`（一次列举 13 槽位）在新形状下**没有规模验证**。

本文件在真实 `server_app.py` 进程上种 **480 张 V1 卡**，回答三件事：
  ① 8 类查询的热路径 p95 < 300ms、冷启动 < 300ms（与既有口径一致）；
  ② 列表**只下发 thumb 地址**（无 display/stickers 键、cover 为 None）、详情才有大图与表情包；
  ③ 新形状下索引仍自愈：跨进程**新增卡目录**、跨进程**原地重写 meta**（task-17 的 utime 路径）
     都必须"下一次读立刻可见"，并带一条**变异对照**（把 utime 打成 no-op 时必须仍陈旧，防假绿）。

卫生门禁：跑前检查 **8765 空闲**，被占则**退出码 3**（本机 Windows 有保留端口段，
见协作记忆 #18；本地版 `app/server.py` 默认就占 8765）——**绝不杀进程**。
退出码：0 全绿 / 1 有 FAIL / 2 服务器没起来 / 3 8765 被占。
"""
import json
import os
import re
import shutil
import socket as _sk
import statistics
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


# ── 0. 卫生门禁：8765 被占就退出 3（绝不杀进程）──
# 门禁目的：别与**用户本机的本地版实例**（`app/server.py`，端口写死 8765）抢资源导致假失败。
# 逃生开关（2026-10-01 Lead 批准，三条约束）：
#   ① **默认行为不变**：8765 被占 ⇒ 仍退出 3；
#   ② 只在显式 `FIREFLY_ALLOW_PORT_BUSY=1` 时放行，且**必须打印一行醒目声明**（含占用者 pid/cmdline）；
#   ③ 仅当"占用者已核实无关（本地版实例，与本测试无端口/数据交集）+ 本次走沙箱数据根与动态端口"
#      时才用；**绝不杀进程、绝不写非沙箱路径**。
BUSY_PORT = 8765
BUSY_ALLOW_ENV = "FIREFLY_ALLOW_PORT_BUSY"


def _port_busy(port: int) -> bool:
    s = _sk.socket()
    s.settimeout(0.5)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _occupant(port: int) -> str:
    """尽力查出占用者 `pid/cmdline`（只读；查不到给占位串）。"""
    import subprocess as _sp
    try:
        if os.name == "nt":
            out = _sp.run(["powershell", "-NoProfile", "-Command",
                           f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                           "-ErrorAction SilentlyContinue | "
                           "Select-Object -First 1 -ExpandProperty OwningProcess"],
                          capture_output=True, text=True, timeout=10)
            pid = (out.stdout or "").strip()
            if pid:
                cl = _sp.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                             capture_output=True, text=True, timeout=10)
                return f"{pid}/{(cl.stdout or '').strip() or '?'}"
        else:
            out = _sp.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True,
                          text=True, timeout=10)
            pid = (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else ""
            if pid:
                cl = _sp.run(["ps", "-o", "args=", "-p", pid], capture_output=True,
                             text=True, timeout=10)
                return f"{pid}/{(cl.stdout or '').strip() or '?'}"
    except Exception:
        pass
    return "(占用者信息不可读)"


if _port_busy(BUSY_PORT):
    if os.environ.get(BUSY_ALLOW_ENV, "") not in ("", "0"):
        print(f"[!] {BUSY_PORT} 被占但已**显式允许**（{BUSY_ALLOW_ENV}=1）："
              f"占用者={_occupant(BUSY_PORT)}；占用者与本测试无端口/数据交集；"
              f"本次用**沙箱数据根 + 动态端口**，只读不写非沙箱路径。", flush=True)
    else:
        print(f"{BUSY_PORT} 被占用 ⇒ 按纪律退出 3（**绝不杀进程**）。先停掉占用者再跑本测试。",
              flush=True)
        print(f"  占用者：{_occupant(BUSY_PORT)}", flush=True)
        print(f"  自查：Get-NetTCPConnection -LocalPort {BUSY_PORT} -State Listen → 看 OwningProcess；"
              f"本地版 app/server.py 默认就占 {BUSY_PORT}。", flush=True)
        print(f"  如确认占用者无关（且本次走沙箱+动态端口），可显式放行："
              f"$env:{BUSY_ALLOW_ENV}=\"1\"", flush=True)
        sys.exit(3)

SANDBOX = Path(tempfile.mkdtemp(prefix="ff_plaza_scale_v1_"))
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
BUDGET_MS = 300.0
N = 480


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}", flush=True)


import db as srv_db                          # noqa: E402
from plaza import cache as _cache            # noqa: E402
from plaza import card_format as cf          # noqa: E402
from plaza import store as st                # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID = srv_db.create_user("scalev1@qq.com", "h", "s", install_id="inst-" + "v" * 32, role="user")
TOK = "tok-scalev1"
srv_db.create_session(TOK, UID, "scalev1", EXP)
UID_ADMIN = srv_db.create_user("scalev1_admin@qq.com", "h", "s",
                               install_id="inst-" + "w" * 32, role="admin")
TOK_ADMIN = "tok-scalev1-admin"
srv_db.create_session(TOK_ADMIN, UID_ADMIN, "scalev1-admin", EXP)

CATS = ["陪伴", "剧情", "助手", "其他"]


def png(w, h):
    """头部合法的最小 PNG（服务端只解析 IHDR；压规模时不需要真像素）。"""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def make_thumb():
    """真缩略图（Pillow 平滑渐变 ≈2KB，≤12KB）；没装 Pillow 退回最小合法 PNG 头。"""
    try:
        import io

        from PIL import Image
        img = Image.new("RGB", (1000, 667))
        px = img.load()
        for y in range(667):
            for x in range(1000):
                px[x, y] = (x * 255 // 999, y * 255 // 666, (x + y) * 255 // 1665)
        buf = io.BytesIO()
        img.resize((320, 213), Image.LANCZOS).save(buf, "JPEG", quality=50, optimize=True)
        return buf.getvalue(), "jpg"
    except Exception:
        return png(320, 200), "png"


THUMB, THUMB_EXT = make_thumb()
PNG_DISPLAY = png(1024, 768)
PNG_AVATAR = png(256, 256)
PNG_STICKER = png(160, 160)
print(f"（缩略图样本 {len(THUMB)}B .{THUMB_EXT}；display/avatar/sticker 用头部样本压规模）", flush=True)


def card_files(cid, i: int) -> dict:
    """一张 V1 卡：1–2 份知识库（扁平/分组各半）+ thumb + display + 0–3 表情包 + stickers.json。"""
    files = {
        "preset.json": json.dumps({"id": cid, "name": f"V1规模卡{i:03d}", "char_name": "小萤",
                                   "user_name": "你", "presentation": "sticker",
                                   "desc": "V1 新格式规模压测", "schema": 1},
                                  ensure_ascii=False).encode(),
        "core.md": ("# 核心\nV1 规模压测内容\n" * 20).encode(),
        f"assets/thumb.{THUMB_EXT}": THUMB,
        "assets/display.png": PNG_DISPLAY,
    }
    if i % 2 == 0:
        files["knowledge/世界观.md"] = "扁平知识库".encode()
    else:
        files["knowledge/world/地理.md"] = "一层分组知识库".encode()
    if i % 3 == 0:                       # 1–2 份知识库
        files["knowledge/characters/小萤.md"] = "角色设定".encode()
    labels = {}
    for k in range(1, (i % 4) + 1):      # 0–3 张表情包
        name = f"sticker-{k}.png"
        files[f"assets/{name}"] = PNG_STICKER
        labels[name] = f"标签{k}"
    if labels:
        files["stickers.json"] = json.dumps(labels, ensure_ascii=False).encode()
    if i % 5 == 0:                       # 每 5 张带一个 avatar（用来核对列表口径，见 §② 注释）
        files["assets/avatar.png"] = PNG_AVATAR
    return files


def seed_cards(lo: int, hi: int) -> None:
    for i in range(lo, hi):
        cid = f"v1scale_{i:03d}"
        cat = CATS[i % len(CATS)]
        st.publish_card(
            {"id": cid, "name": f"V1规模卡{i:03d}", "char_name": "小萤", "user_name": "你",
             "presentation": "sticker", "desc": "V1 新格式规模压测", "category": cat,
             "tags": ["压测" if i % 3 == 0 else "常规"]},
            card_files(cid, i),
            uid_hash="v" * 32, display="官方" if i % 10 == 0 else "用户", official=(i % 10 == 0))


t_seed = time.perf_counter()
seed_cards(0, N)
seed_s = time.perf_counter() - t_seed
check(f"① 已种 {N} 张 **V1 新格式**卡（官方 {N // 10} 张、4 分类、含分组知识库/表情包）"
      f"耗时 {seed_s:.1f}s", st.stats()["cards"] == N)
_z = st.card_zip_path("v1scale_479").stat().st_size
check(f"①b 单张卡体 {_z} B（含知识库/两图/3 表情包，≤1.5MB）", _z <= 1536 * 1024, f"{_z}B")


# ── 起真实服务器入口（动态端口；8765 已确认空闲但本测试不用它）──
def pick_port():
    s = _sk.socket()
    s.bind(("127.0.0.1", 0))              # 让系统分配：本机有 Windows 保留端口段（协作记忆 #18）
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


def get(path, timeout=20, tok=TOK):
    r = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + tok})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
        return resp.status, body, (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.perf_counter() - t0) * 1000.0


def bench(path, n=30, tok=TOK):
    lat, size, status = [], 0, 0
    for _ in range(n):
        s, b, ms = get(path, tok=tok)
        status = s
        size = len(b)
        if s == 200:
            lat.append(ms)
    if not lat:
        return status, None, None, 0, size
    lat.sort()
    return status, statistics.median(lat), lat[int(0.95 * len(lat)) - 1], len(lat), size


_TTL = 2.0
try:
    _TTL = max(0.0, float(os.environ.get("FIREFLY_PLAZA_INDEX_TTL", "2") or 2))
except (TypeError, ValueError):
    _TTL = 2.0


def list_ids():
    s, b, _ = get("/plaza/api/list?page=1&size=50")
    if s != 200:
        return None
    return [it["id"] for it in json.loads(b.decode("utf-8"))["items"]]


def list_total():
    """列表 total —— 比"首页里有没有它"强得多的判据（首页只 50 条，含不到就等于没测）。"""
    s, b, _ = get("/plaza/api/list?page=1&size=5")
    if s != 200:
        return None
    return json.loads(b.decode("utf-8")).get("total")


def cold_ms(trials=3):
    """冷启动：等索引缓存过期后再打第一枪，取中位数（不吃缓存红利也须达标）。"""
    vals = []
    for _ in range(trials):
        time.sleep(_TTL + 0.35)
        _s, _b, ms = get("/plaza/api/list?page=1&size=20")
        vals.append(ms)
    print(f"     [诊断] 冷读每次: {['%.0fms' % v for v in vals]}", flush=True)
    vals.sort()
    return vals[len(vals) // 2]


CASES = [
    ("首屏列表 page=1", "/plaza/api/list?page=1&size=20", TOK),
    ("深分页 page=12", f"/plaza/api/list?page={N // 40}&size=20", TOK),
    ("分类筛选", "/plaza/api/list?page=1&size=20&category=%E9%99%AA%E4%BC%B4", TOK),
    ("标签筛选", "/plaza/api/list?page=1&size=20&tag=%E5%8E%8B%E6%B5%8B", TOK),
    ("关键词搜索", "/plaza/api/list?page=1&size=20&q=V1%E8%A7%84%E6%A8%A1%E5%8D%A1", TOK),
    ("只看官方", "/plaza/api/list?page=1&size=20&official=1", TOK),
    ("已安装（配额）", "/plaza/api/installed", TOK),
    ("管理台列表", "/plaza/api/admin/list?status=all&page=1&size=20", TOK_ADMIN),
]


def suite(label):
    print(f"\n  ── {label} ──")
    print(f"  {'用例':<20}{'p50':>9}{'p95':>9}{'响应':>10}  判定")
    worst = 0.0
    for name, path, tok in CASES:
        status, p50, p95, _samp, size = bench(path, tok=tok)
        ok = status == 200 and p95 is not None and p95 < BUDGET_MS
        worst = max(worst, p95 or 0.0)
        check(f"[{label}] {name} p95 {p95:.1f}ms" if p95 is not None
              else f"[{label}] {name} 无样本", ok, f"status={status}")
        print(f"  {name:<20}{(p50 or 0):>8.1f}ms{(p95 or 0):>8.1f}ms{size:>9}B  "
              f"{'ok' if ok else 'FAIL'}", flush=True)
    return worst


try:
    ready = False
    for _ in range(80):
        s, _b, _ms = get("/health")
        if s == 200:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        print("服务器未起来", flush=True)
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)

    print(f"\n== ① 规模压测：{N} 张 V1 卡（每项 30 样本；验收 p95 < {BUDGET_MS:.0f}ms）==")
    worst = suite(f"{N} 张 V1 卡")
    cold = cold_ms()
    check(f"①c 热路径最慢用例 p95 {worst:.0f}ms < {BUDGET_MS:.0f}ms", worst < BUDGET_MS, f"{worst:.1f}ms")
    check(f"①d **冷启动** {cold:.0f}ms < {BUDGET_MS:.0f}ms（不吃缓存红利也达标）",
          cold < BUDGET_MS, f"{cold:.1f}ms")

    print("\n== ② 新形状投影口径：列表只给 thumb，详情才有大图/表情包 ==")
    _s, lb, _ms = get("/plaza/api/list?page=1&size=20")
    items = json.loads(lb.decode("utf-8"))["items"]
    check(f"②a 列表 20 条 = {len(lb)}B（契约 ≤60KB）", len(lb) <= 60 * 1024, f"{len(lb)}B")
    check("②b 每项都有 thumb 地址", all(it.get("thumb") for it in items))
    check("②c 列表项**没有 display 键**（详情才拉大图）", all("display" not in it for it in items))
    check("②d 列表项**没有 stickers 键**", all("stickers" not in it for it in items))
    check("②e 列表项 cover 一律 None（V1 卡有 thumb ⇒ 不下发老封面地址）",
          all(it.get("cover") is None for it in items))
    # ②f/②f'：**结构性保证**（2026-10-01 task-20）—— 列表响应里连 avatar 文件名都不出现，
    # 取代"前端别请求 avatar"的 UI 纪律（否则将来任何客户端顺手取图就是一屏 20×≤30KB）。
    _av_keys = [it["id"] for it in items if "avatar" in it]
    check(f"②f 列表项**没有 avatar 键**（本页 {len(_av_keys)} 项带 avatar，应为 0）", not _av_keys)
    check("②f' 列表项只可能有两个图片字段（thumb / cover），没有第三方图片地址",
          all({k for k in it if k in ("thumb", "cover", "avatar", "display", "stickers")}
              <= {"thumb", "cover"} for it in items))
    _s, tb, _ms = get("/plaza/api/asset?id=v1scale_479&slot=thumb")
    check(f"②g 单张 thumb 实际字节 {len(tb)}B ≤12KB（= 种卡样本 {len(THUMB)}B）",
          _s == 200 and len(tb) == len(THUMB), f"{len(tb)}B")
    _s, _b, _ms = get("/plaza/api/card?id=v1scale_479")
    card = json.loads(_b.decode("utf-8"))["card"]
    check("②h 详情有 display", bool(card.get("display")), f"display={card.get('display')}")
    _stk = card.get("stickers") or []
    check("②i 详情 stickers = [{file,label}]（3 张，标签跟着来；2026-10-01 统一形状）",
          len(_stk) == 3
          and all(isinstance(x, dict) and re.fullmatch(r"sticker-\d+\.png", str(x.get("file") or ""))
                  for x in _stk)
          and [x.get("label") for x in _stk] == ["标签1", "标签2", "标签3"],
          str(_stk))
    check("②j 详情也有 thumb（详情页小图不额外请求）", bool(card.get("thumb")))
    # ②k：**详情保留 avatar**（v1scale_470 是 i%5==0 的带头像卡）—— 与列表"没有 avatar 键"对照
    _s, _b, _ms = get("/plaza/api/card?id=v1scale_470")
    _card_av = json.loads(_b.decode("utf-8"))["card"]
    check(f"②k 详情有 avatar（{_card_av.get('avatar')}）—— 列表去掉、详情保留",
          str(_card_av.get("avatar") or "").endswith(".png"))
    _s, _lb2, _ms = get("/plaza/api/list?page=1&size=5")
    check("②l 带头像的卡在列表里同样没有 avatar 键（去掉是结构性的，不是偶发）",
          all("avatar" not in it for it in json.loads(_lb2.decode("utf-8"))["items"]))

    print("\n== ③ 新形状下索引自愈：跨进程新增卡目录 ⇒ 下一次读立刻可见 ==")
    check(f"③a 预热：列表 total = {N}", list_total() == N)
    hid = "heal_v1_%d" % int(time.time())
    hd = st.card_dir(hid)
    (hd / "assets").mkdir(parents=True, exist_ok=True)
    hfiles = card_files(hid, 3)
    hfiles["preset.json"] = json.dumps({"id": hid, "name": "自愈V1卡", "char_name": "小萤",
                                        "user_name": "你", "presentation": "sticker",
                                        "desc": "自愈探测", "schema": 1},
                                       ensure_ascii=False).encode()
    hzip = cf.build_card_zip({"id": hid, "name": "自愈V1卡", "char_name": "小萤", "user_name": "你",
                              "presentation": "sticker", "desc": "自愈探测", "category": "陪伴"},
                             hfiles)
    (hd / "card.zip").write_bytes(hzip)
    (hd / "assets" / f"thumb.{THUMB_EXT}").write_bytes(THUMB)
    st.atomic_write_json(hd / "meta.json", {
        "id": hid, "name": "自愈V1卡", "char_name": "小萤", "user_name": "你",
        "status": "published", "category": "陪伴", "tags": [], "created_at": int(time.time()),
        "size_bytes": len(hzip), "official": False})
    _t = list_total()
    check(f"③b **不等待**就读到 {N + 1} 张（实际 {_t}）—— 目录数/mtime 自愈", _t == N + 1, str(_t))
    shutil.rmtree(hd, ignore_errors=True)
    _t = list_total()
    check(f"③c 删掉后**不等待**又回到 {N} 张（实际 {_t}）", _t == N, str(_t))

    print("== ③ 新形状下索引自愈：跨进程**原地重写 meta** ⇒ 下一次读立刻可见（task-17 的 utime 路径）==")
    list_total()                                   # 预热服务端缓存
    m0 = st.cards_dir().stat().st_mtime_ns
    time.sleep(0.02)
    st.set_status("v1scale_001", "archived")       # ★ 本测试进程直写，server 在另一个进程
    m1 = st.cards_dir().stat().st_mtime_ns
    _t = list_total()
    check(f"③d 原地改 status 后 **不等待**列表 total 就变 {N - 1}（实际 {_t}）", _t == N - 1, str(_t))
    _s, _b, _ms = get("/plaza/api/card?id=v1scale_001")
    check("③e 详情也立刻 404（不是只漏列表）", _s == 404, f"status={_s}")
    check(f"③f `cards/` 目录 mtime 被顶动（{m0} → {m1}）—— utime 生效的直接证据", m1 != m0)
    st.set_status("v1scale_001", "published")
    check(f"③f' 复原后 total 回到 {N}", list_total() == N)

    print("== ③ 变异对照：写者不 utime ⇒ 必须仍陈旧（防假绿）==")
    _real_touch = st._touch_cards_dir
    st._touch_cards_dir = lambda: None
    list_total()
    st.set_status("v1scale_002", "archived")
    _t = list_total()
    check(f"③g 变异写者（无 utime）后**仍是旧数据**（total 仍 {N}，实际 {_t}）", _t == N, str(_t))
    st._touch_cards_dir = _real_touch
    _real_touch()
    _t = list_total()
    check(f"③h 恢复 utime 并顶目录后立刻自愈（total {N - 1}，实际 {_t}）", _t == N - 1, str(_t))
    st.set_status("v1scale_002", "published")
    check(f"③h' 复原后 total 回到 {N}", list_total() == N)

    print("\n== ④ 直接回答「13 槽位列举在 480 张下变慢吗」：全量重扫计时 ==")
    st._invalidate_index()
    t_scan = time.perf_counter()
    rows = st._all_cards()
    scan_ms = (time.perf_counter() - t_scan) * 1000.0
    check(f"④ 全量重扫 {len(rows)} 张（读 meta + stat zip + 一次列举 13 槽位）{scan_ms:.0f}ms < 1000ms",
          scan_ms < 1000, f"{scan_ms:.1f}ms")

    print("== ④b 已知观测项（**不进预算判定**，别当成达标）：FILE_TTL 到期那一次读 = 全量重扫 ==")
    # 为什么单独观测：`_load_file_index` 的第③条是"有界陈旧"，索引超 FILE_TTL 会被拒 ⇒
    # **每 FILE_TTL 一次**的读要付全量重扫。既有规模测试只量 2s 的 MEM 边界（走索引，很快），
    # 这条尾巴此前没被量过。**实测 460–500ms > 300ms 预算**（旧格式同口径 392ms，也已超）
    # —— 2026-10-01 task-19/20 的处置是**放宽默认 FILE_TTL 60s → 300s**（把尖峰从每分钟一次
    # 变成每 5 分钟一次；即时性由 task-17 的 `os.utime` 经第②条接管），**不是**把它做达标。
    # 这里在测试进程里复现同一代码路径（改 written_at + 清内存缓存），只打印不判定。
    st._invalidate_index()
    st._all_cards()
    doc = json.loads((PLAZA_DIR / "_index.json").read_text(encoding="utf-8"))
    doc["written_at"] = 0                      # 模拟"索引已过 FILE_TTL"
    (PLAZA_DIR / "_index.json").write_text(json.dumps(doc), encoding="utf-8")
    _cache._MEM["entries"] = None              # 模拟"内存缓存也已过期"
    _cache._MEM["mtime"] = None
    t_exp = time.perf_counter()
    st._all_cards()
    expired_ms = (time.perf_counter() - t_exp) * 1000.0
    print(f"  ⚠ 索引过期后的一次读 = {expired_ms:.0f}ms（预算 {BUDGET_MS:.0f}ms；多次实测 460–520ms，"
          f"旧格式同口径 392ms）—— **已知观测项、非达标**；默认 FILE_TTL 已放宽到 300s 以降低频率",
          flush=True)

    print("\n== ④c 索引文件 TTL 的默认值与可覆盖性（task-20）==")
    check(f"④d 默认 FILE_TTL = 300s（实际 {_cache.FILE_TTL}s）", _cache.FILE_TTL == 300.0,
          f"{_cache.FILE_TTL}")
    _ov = subprocess.run(
        [sys.executable, "-c",
         "import os, sys\n"
         f"sys.path.insert(0, r'{ROOT / 'app'}')\n"
         "os.environ['FIREFLY_PLAZA_INDEX_FILE_TTL'] = '7'\n"
         "from plaza import cache as c\n"
         "print(c.FILE_TTL)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={k: v for k, v in os.environ.items() if k != "FIREFLY_PLAZA_INDEX_FILE_TTL"})
    check(f"④e 仍可用环境变量覆盖（FIREFLY_PLAZA_INDEX_FILE_TTL=7 → {_ov.stdout.strip()}）",
          _ov.stdout.strip() == "7.0", str(_ov.stderr[-120:]))
    check(f"④f MEM TTL 默认仍是 2s（未动；实际 {_cache.MEM_TTL}s）", _cache.MEM_TTL == 2.0)

    print("\n== 数字汇总 ==")
    print(f"  热路径最慢 p95      : {worst:.1f} ms（预算 {BUDGET_MS:.0f}）")
    print(f"  冷启动（缓存过期后）: {cold:.1f} ms（预算 {BUDGET_MS:.0f}）")
    print(f"  列表一页 20 条字节  : {len(lb)} B")
    print(f"  单张 thumb 字节     : {len(tb)} B（种卡样本 {len(THUMB)}B）")
    print(f"  全量重扫 480 张     : {scan_ms:.1f} ms（含 13 槽位一次列举）")
    print(f"  种 {N} 张 V1 卡耗时 : {seed_s:.1f} s")
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
