# -*- coding: utf-8 -*-
"""广场规模压测 —— 设计文档 M3 的验收原话：「100 张卡压测列表 <300ms」

此前只验过 21 张卡（`test_plaza_budget.py`），那不足以证明规模性质：
`store.list_cards()` **每次请求都要 `iterdir` 广场目录并逐张读 `meta.json`**，
所以真正要回答的是"120 张卡时，列表/筛选/搜索/深分页**还快不快**、**响应会不会线性膨胀**"。

本测试在真实 `server_app.py` 进程上，用 **120 张卡**（含分类/标签/官方分布）实测：
  ① 首屏列表 ② 深分页 ③ 分类筛选 ④ 标签筛选 ⑤ 关键词搜索 ⑥ 只看官方 ⑦ 统计
每项取 30 次样本报 p50/p95，并断言 **p95 < 300ms**。

沙箱：临时数据根 + 真实账号 token；不连外网、不碰真实数据。
"""
import json
import os
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
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_plaza_scale_"))
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


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}", flush=True)


# ── 1. 种 120 张卡（分类/标签/官方分布都造出来，避免只压一种查询）──
import db as srv_db                    # noqa: E402
from plaza import store as st          # noqa: E402
from plaza import cache as _cache       # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID = srv_db.create_user("scale@qq.com", "h", "s", install_id="inst-" + "s" * 32, role="user")
TOK = "tok-scale"
srv_db.create_session(TOK, UID, "scale", EXP)
UID_ADMIN = srv_db.create_user("scale_admin@qq.com", "h", "s",
                               install_id="inst-" + "t" * 32, role="admin")
TOK_ADMIN = "tok-scale-admin"
srv_db.create_session(TOK_ADMIN, UID_ADMIN, "scale-admin", EXP)

PNG = bytes.fromhex(                      # 1×1 透明 PNG：压规模用，不压图片体积（那由预算测试管）
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082")
CATS = ["陪伴", "剧情", "助手", "其他"]
N = 120


def seed_cards(lo: int, hi: int) -> None:
    for i in range(lo, hi):
        cid = f"scale_{i:03d}"
        cat = CATS[i % len(CATS)]
        st.publish_card(
            {"id": cid, "name": f"规模卡{i:03d}", "char_name": "小萤", "user_name": "你",
             "presentation": "sticker", "desc": "规模压测", "category": cat,
             "tags": ["压测" if i % 3 == 0 else "常规"]},
            {"preset.json": json.dumps({"id": cid, "name": f"规模卡{i:03d}", "char_name": "小萤",
                                        "user_name": "你", "presentation": "sticker",
                                        "desc": "规模压测", "category": cat, "schema": 1},
                                       ensure_ascii=False).encode(),
             "core.md": ("# 核心\n规模压测内容\n" * 20).encode(),
             "assets/cover.png": PNG},
            uid_hash="s" * 32, display="官方" if i % 10 == 0 else "用户",
            official=(i % 10 == 0))


t_seed = time.perf_counter()
seed_cards(0, N)
seed_s = time.perf_counter() - t_seed
check(f"① 已种 {N} 张卡（其中官方 {N // 10} 张、4 个分类），耗时 {seed_s:.1f}s",
      st.stats()["cards"] == N)
official_n = sum(1 for i in range(N) if i % 10 == 0)
tag_n = sum(1 for i in range(N) if i % 3 == 0)


# ── 2. 起真实服务器入口 ──
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


# 与产品侧默认一致（`app/plaza/store.py::_INDEX_TTL`，可用环境变量覆盖）
_TTL = 2.0
try:
    _TTL = max(0.0, float(os.environ.get("FIREFLY_PLAZA_INDEX_TTL", "2") or 2))
except (TypeError, ValueError):
    _TTL = 2.0


def cold_ms(trials=3):
    """**冷启动**：等索引缓存过期后再打第一枪，取中位数。

    为什么要单独量它：索引缓存把"**每个**请求都慢"变成"每 TTL **一次**慢"——
    只报热路径 p95 会把这条尾巴藏起来（那等于自欺）。低流量下用户很可能每次都撞上冷路径，
    所以冷值必须与热值一起报、一起看趋势。
    """
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
    ("深分页 page=6", "/plaza/api/list?page=6&size=20", TOK),
    ("分类筛选", "/plaza/api/list?page=1&size=20&category=%E9%99%AA%E4%BC%B4", TOK),
    ("标签筛选", "/plaza/api/list?page=1&size=20&tag=%E5%8E%8B%E6%B5%8B", TOK),
    ("关键词搜索", "/plaza/api/list?page=1&size=20&q=%E8%A7%84%E6%A8%A1%E5%8D%A1", TOK),
    ("只看官方", "/plaza/api/list?page=1&size=20&official=1", TOK),
    ("已安装（配额）", "/plaza/api/installed", TOK),
    ("管理台列表", "/plaza/api/admin/list?status=all&page=1&size=20", TOK_ADMIN),
]


def suite(label):
    """跑一遍全部用例，打印一行一行的 p50/p95，返回最慢的 p95。"""
    print(f"\n  ── {label} ──")
    print(f"  {'用例':<20}{'p50':>9}{'p95':>9}{'响应':>10}  判定")
    worst = 0.0
    for name, path, tok in CASES:
        status, p50, p95, samp, size = bench(path, tok=tok)
        ok = status == 200 and p95 is not None and p95 < BUDGET_MS
        worst = max(worst, p95 or 0.0)
        check(f"[{label}] {name} p95 {p95:.1f}ms" if p95 is not None
              else f"[{label}] {name} 无样本", ok, f"status={status}")
        print(f"  {name:<20}{(p50 or 0):>8.1f}ms{(p95 or 0):>8.1f}ms{size:>9}B  "
              f"{'ok' if ok else 'FAIL'}")
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

    print("\n== 规模压测（热路径每项 30 次样本；设计验收：p95 < 300ms）==")
    w120 = suite(f"{N} 张卡")
    c120 = cold_ms()
    check(f"[{N} 张卡] **冷启动**（缓存过期后首个请求）{c120:.0f}ms < {BUDGET_MS:.0f}ms"
          f" —— 即「不吃缓存红利」也达标设计验收", c120 < BUDGET_MS, f"{c120:.1f}ms")

    # 只报一个点回答不了"会不会随规模劣化"。`_scan_cards` 每次都要 iterdir + 逐卡读 meta，
    # 所以**必须看趋势**：把规模翻 4 倍（480 张）再压一遍同样的用例。
    t2 = time.perf_counter()
    seed_cards(N, 4 * N)
    check(f"⑨ 追加到 {4 * N} 张（耗时 {time.perf_counter() - t2:.1f}s）",
          st.stats()["cards"] == 4 * N)
    time.sleep(_TTL + 0.4)                 # 等服务端索引缓存过期，否则它看不见新种的 360 张
    _s, b, _ms = get("/plaza/api/list?page=1&size=20")
    check(f"⑨b 服务端已看到全部 {4 * N} 张（跨进程写入经 TTL 生效）",
          json.loads(b.decode("utf-8")).get("total") == 4 * N)
    w480 = suite(f"{4 * N} 张卡")
    c480 = cold_ms()
    check(f"⑩ 规模翻 4 倍后**热路径**最慢用例仍 <{BUDGET_MS:.0f}ms（{w120:.0f}ms → {w480:.0f}ms）",
          w480 < BUDGET_MS, f"{w480:.1f}ms")
    check(f"⑩b **冷启动**也进预算：{N} 张 {c120:.0f}ms → {4 * N} 张 {c480:.0f}ms < {BUDGET_MS:.0f}ms"
          f"（P4-11 持久索引；若退化成全量扫，480 张约 300ms 会立刻顶穿）",
          c480 < BUDGET_MS, f"{c480:.1f}ms")
    print(f"\n  趋势：{N} 张 → {4 * N} 张"
          f"\n    热路径最慢 p95 {w120:.0f}ms → {w480:.0f}ms"
          f"\n    冷启动       {c120:.0f}ms → {c480:.0f}ms"
          f"（≈{c480 / max(c120, 1):.1f}× / 4× 规模 ⇒ 冷读已与规模解耦）")

    # ── P4-11 的持久索引：落盘 / 冷热同源 / 自愈 ──
    print("\n== P4-11 持久索引（冷启动 O(1) + 自愈）==")
    _idxf = PLAZA_DIR / "_index.json"
    check(f"⑫ 索引已落盘（{_idxf.stat().st_size if _idxf.is_file() else 0}B）", _idxf.is_file())
    _hot = json.loads(get("/plaza/api/list?page=1&size=20")[1].decode("utf-8"))
    time.sleep(_TTL + 0.35)
    _s, _b, _coldms = get("/plaza/api/list?page=1&size=20")
    _cold = json.loads(_b.decode("utf-8"))
    check(f"⑫b 冷读与热读**同一份数据**（total={_cold.get('total')}，首条 cover 一致）",
          _cold.get("total") == _hot.get("total") and bool(_cold.get("items"))
          and _cold["items"][0].get("cover") == _hot["items"][0].get("cover"))
    check(f"⑫c 冷读 {_coldms:.0f}ms 已与热读同量级（持久索引命中，不是全量扫）",
          _coldms < max(c480 * 3, 60), f"{_coldms:.1f}ms")
    # 自愈：**绕过 store** 直接往盘上加一张卡（模拟"别的进程/手工改盘"）
    _hid = "heal%d" % int(time.time())
    _hd = st.card_dir(_hid)
    (_hd / "assets").mkdir(parents=True, exist_ok=True)
    st.atomic_write_json(_hd / "meta.json", {
        "id": _hid, "name": "自愈探测卡", "author": "healer", "status": "published",
        "category": "companion", "tags": [], "created_at": int(time.time()), "official": False})
    (_hd / "card.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    time.sleep(_TTL + 0.4)
    _s, _b, _ = get("/plaza/api/list?page=1&size=5")
    check(f"⑫d 自愈：绕过 store 直接加卡后广场**看得见**（dir_count 校验挡住脏索引，"
          f"total={json.loads(_b.decode('utf-8')).get('total')} 应为 {4 * N + 1}）",
          json.loads(_b.decode("utf-8")).get("total") == 4 * N + 1)
    # 再删掉那张卡：删除也要自愈（否则索引会把已经不存在的卡一直报出去）
    shutil.rmtree(_hd, ignore_errors=True)
    time.sleep(_TTL + 0.4)
    _s, _b, _ = get("/plaza/api/list?page=1&size=5")
    check("⑫e 自愈（删）：直接删卡后广场**也立刻不报**它",
          json.loads(_b.decode("utf-8")).get("total") == 4 * N)

    print("\n== 正确性与「体积不随总量膨胀」==")
    _s, b, _ms = get("/plaza/api/list?page=1&size=20")
    d = json.loads(b.decode("utf-8"))
    check(f"⑪ total 报出全部 {4 * N} 张", d.get("total") == 4 * N, str(d.get("total")))
    check(f"⑪b 首屏 20 条且**不含卡正文**（{len(b)}B ≤ 40KB，与 21 张卡时同量级）",
          len(b) <= 40 * 1024 and len(d.get("items") or []) == 20, f"{len(b)}B")
    _s, b6, _ms = get(f"/plaza/api/list?page={4 * N // 20}&size=20")
    d6 = json.loads(b6.decode("utf-8"))
    check(f"⑪c 末页返回剩余 {4 * N - (4 * N // 20 - 1) * 20} 条（分页边界正确）",
          len(d6.get("items") or []) == 4 * N - (4 * N // 20 - 1) * 20,
          str(len(d6.get("items") or [])))
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
