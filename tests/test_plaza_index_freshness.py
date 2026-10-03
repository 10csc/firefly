# -*- coding: utf-8 -*-
"""广场索引**新鲜度**回归：写盘的"下一次读必须立刻反映"（task-15 排查 → task-17 修复）

背景（task-15 的定位，verifier 的探针现场）：
落盘索引 `plaza/_index.json` 靠三重校验自愈 —— ① 子目录数 ② **`cards/` 目录 mtime** ③ 有界 TTL。
**增删卡目录**会动 ①/② ⇒ 立即自愈；但**原地重写 `cards/<id>/meta.json`**（改 status / 举报 /
下载计数）两条都不动 ⇒ 若是**别的进程**（脚本/手工/探针）写的，读者进程会一直吃旧索引，
直到 `FILE_TTL`（默认 60s）过期。修复：原地重写的写路径顺手 `os.utime(cards_dir())`
（`store._touch_cards_dir()`），让第②条校验当场失效。

本文件把这件事钉成三条用例（**读侧一律是真服务器进程 + 真 HTTP**，写侧分两种：
产品路径 / 跨进程直写；按 verifier 的提醒，两条路不互相证明）：
  · A 产品路径（同进程写：HTTP `POST /admin/status`）⇒ 下一次 HTTP 读立刻可见；
  · B **盲点现场**（测试进程直接 `st.set_status()` 改盘）⇒ 下一次 HTTP 读**也必须**立刻可见；
  · C **变异对照**（写者把 `_touch_cards_dir` 打成 no-op，等价于注释掉那行 utime）
    ⇒ 必须仍然**陈旧**（证明 B 的绿是 utime 的功劳，不是别的机制），
    且**全新进程冷读也是旧的**（证明陈旧在落盘索引里，不只是内存）。

沙箱：临时数据根 + 临时端口 + 真 `server_app.py` 进程；不连外网、不碰真实数据。
退出码：0 全绿 / 1 有 FAIL / 2 服务器没起来。
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
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_idx_fresh_"))
USER_DIR = SANDBOX / "user_data"
PLAZA_DIR = SANDBOX / "plaza"
USER_DIR.mkdir(parents=True, exist_ok=True)
PLAZA_DIR.mkdir(parents=True, exist_ok=True)

ENV = dict(os.environ)
ENV.update({
    "FIREFLY_ANDROID": "1",
    "FIREFLY_DATA_DIR": str(SANDBOX),
    "FIREFLY_PLAZA_DIR": str(PLAZA_DIR),
    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    # 不设索引 TTL ⇒ 默认 MEM=2s / FILE=60s：所以"写完后**不等待**就读到新数据"不可能是 TTL 到期的功劳
})
os.environ.update({k: v for k, v in ENV.items() if k.startswith("FIREFLY_")})
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}", flush=True)


import db as srv_db                          # noqa: E402
from plaza import store as st                # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID = srv_db.create_user("fresh@qq.com", "h", "s", install_id="inst-" + "f" * 32, role="user")
ADM = srv_db.create_user("freshadmin@qq.com", "h", "s", install_id="inst-" + "g" * 32, role="admin")
srv_db.create_session("tok-fresh-user", UID, "t", EXP)
srv_db.create_session("tok-fresh-admin", ADM, "t", EXP)


def publish(cid):
    return st.publish_card(
        {"id": cid, "name": cid, "char_name": "小萤", "user_name": "你",
         "presentation": "sticker", "desc": "索引新鲜度回归", "category": "陪伴"},
        {"preset.json": json.dumps({"id": cid, "name": cid, "char_name": "小萤",
                                    "user_name": "你", "presentation": "sticker",
                                    "desc": "索引新鲜度回归", "schema": 1},
                                   ensure_ascii=False).encode(),
         "core.md": b"# x"})


publish("fresh_a")
publish("fresh_b")


def free_port(lo=8981, hi=8999):
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
srv_env = dict(ENV)
srv_env.update({"FIREFLY_PORT": str(PORT), "PYTHONUNBUFFERED": "1"})
log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=srv_env, stdout=log, stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, token="tok-fresh-user"):
    h = {"Authorization": "Bearer " + token}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError:
        return 0, b""


def list_ids(tag=""):
    """经 HTTP 读公开列表（读者 = 真服务器进程）。"""
    s, raw = req("GET", "/plaza/api/list?size=50")
    if s != 200:
        print(f"  !! 列表请求失败 status={s} {raw[:120]!r}", flush=True)
        return None
    return [it["id"] for it in json.loads(raw.decode("utf-8"))["items"]]


def warm(tag=""):
    ids = list_ids()
    print(f"  （预热 {tag}：{ids}）", flush=True)
    return ids


try:
    ready = False
    for _ in range(80):
        s, _b = req("GET", "/health")
        if s == 200:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        print("服务器未起来", flush=True)
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)

    print("== 0. 两张公开卡都能看到 ==")
    ids0 = warm("初始")
    check("初始列表含两张卡", ids0 is not None and set(ids0) == {"fresh_a", "fresh_b"})

    print("== A. 产品路径：同进程写（HTTP /admin/status）⇒ 下一次读立刻可见（对照组）==")
    s, raw = req("POST", "/plaza/api/admin/status", {"id": "fresh_a", "status": "archived"},
                 token="tok-fresh-admin")
    check(f"A1 管理端点 200（实际 {s}）", s == 200)
    idsA = list_ids()                      # ★ 不等待
    check(f"A2 下一次 HTTP 读立刻不见 fresh_a（实际 {idsA}）",
          idsA is not None and "fresh_a" not in idsA)
    req("POST", "/plaza/api/admin/status", {"id": "fresh_a", "status": "published"},
        token="tok-fresh-admin")
    warm("A 复原")
    idsA2 = list_ids()
    check("A3 复原后立刻又可见", idsA2 is not None and "fresh_a" in idsA2)

    print("== B. 盲点现场：**跨进程**直接改盘（测试进程 st.set_status）⇒ 也必须立刻可见 ==")
    cards_dir = st.cards_dir()
    m0 = cards_dir.stat().st_mtime_ns
    time.sleep(0.02)                       # 让 utime 与写入有时序区分（不依赖它通过）
    check("B1 写者确实在**别的进程**（本测试进程 ≠ server 进程）", proc.pid != os.getpid())
    st.set_status("fresh_b", "archived")   # ★ 原地重写 meta.json（等价 verifier 探针的做法）
    m1 = cards_dir.stat().st_mtime_ns
    check(f"B2 原地写后 `cards/` 目录 mtime 被顶动（{m0} → {m1}）", m1 != m0)
    idsB = list_ids()                      # ★ 不等待；60s 窗口内
    check(f"B3 下一次 HTTP 读立刻不见 fresh_b（实际 {idsB}）—— 无 utime 时这条会红",
          idsB is not None and "fresh_b" not in idsB)
    check(f"B4 fresh_a 仍在（没被误伤）", idsB is not None and "fresh_a" in idsB)
    st.set_status("fresh_b", "published")
    warm("B 复原")

    print("== C. 变异对照：写者不 utime（= 注释掉那行）⇒ 必须仍然陈旧（防假绿）==")
    _real_touch = st._touch_cards_dir
    st._touch_cards_dir = lambda: None     # 变异：等价于把 os.utime 那行注释掉
    m2 = cards_dir.stat().st_mtime_ns
    time.sleep(0.02)
    st.set_status("fresh_b", "archived")
    m3 = cards_dir.stat().st_mtime_ns
    check(f"C1 变异写者没动 `cards/` mtime（{m2} → {m3}）", m3 == m2)
    idsC = list_ids()
    check(f"C2 下一次 HTTP 读**仍是旧数据**（fresh_b 还在：{idsC}）—— 证明 B 的绿靠 utime",
          idsC is not None and "fresh_b" in idsC)
    # 全新进程冷读：陈旧在落盘索引里，不只在内存
    probe = subprocess.run(
        [sys.executable, "-c",
         "import os,sys,json\n"
         f"sys.path.insert(0, str(r'{ROOT / 'app'}'))\n"
         f"os.environ.update({{k:v for k,v in {ENV!r}.items() if k.startswith('FIREFLY_')}})\n"
         "import modules.app_config as cfg\n"
         f"cfg.USER_DIR = __import__('pathlib').Path(r'{USER_DIR}')\n"
         "cfg.CONFIG_FILE = cfg.USER_DIR / 'config.json'\n"
         "from plaza import store as st\n"
         "print(json.dumps([v['id'] for v in st.list_cards()['items']]))"],
        env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    fresh_ids = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.stdout.strip() else []
    check(f"C3 **全新进程冷读**也拿到旧索引（{fresh_ids}）⇒ 陈旧在 `_index.json` 里",
          "fresh_b" in fresh_ids)
    idx = PLAZA_DIR / "_index.json"
    check(f"C4 落盘索引年龄 {time.time() - idx.stat().st_mtime:.1f}s < FILE_TTL(60s)（所以第③条也不触发）",
          idx.is_file() and (time.time() - idx.stat().st_mtime) < 60)
    # 恢复：把 utime 装回去，并证明"顶一下目录"就能立刻自愈
    st._touch_cards_dir = _real_touch
    _real_touch()
    idsC2 = list_ids()
    check(f"C5 恢复 utime 并顶目录后立刻自愈（fresh_b 不在：{idsC2}）",
          idsC2 is not None and "fresh_b" not in idsC2)
    st.set_status("fresh_b", "published")
    warm("C 复原")
    ids_end = list_ids()
    check("D1 收尾状态正确（两张都在）", ids_end is not None and set(ids_end) == {"fresh_a", "fresh_b"})
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

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
