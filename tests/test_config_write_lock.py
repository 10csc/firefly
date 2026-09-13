# -*- coding: utf-8 -*-
"""服务器版 /set-config 并发写回归（阶段 1 · 任务 1.8 / C-6.12，P2）

缺陷：服务器版 `/set-config` 是「读 settings.json → 改 → 整份写回」，
而 ThreadingHTTPServer 每请求一个线程——同一账号的两个并发请求会互相覆盖：
后写的整份替换，前一个刚改的字段静默消失（前端 400ms 防抖 + 双设备同时改都能触发）。

修复：按 uid 的锁 + **锁内重新读盘**取基准（只加锁、仍用请求入口装载的旧快照是不够的，
那样后到的请求会拿旧快照整份覆盖回去，锁等于白加）。

本测试用"慢写"制造**确定性**的交错窗口（不靠随机运气）：
A 进入写盘窗口并停住 → B 发起同账号写 → 断言磁盘上两份改动都在。
按旧实现，B 会读到自己那份请求入口的旧快照、整份覆盖，A 的字段随后丢失。

沙箱纪律：USER_DIR 指到临时目录，绝不触碰真实 user_data/。
"""
import io
import json
import os
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
sys.path.insert(0, str(ROOT / "app"))

# 必须在导入 routes 之前置平台标记：_is_server() 走 env 判定
os.environ["FIREFLY_SERVER"] = "1"

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_config_lock_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes
import routes_config
from modules import storage as st

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


class FakeH:
    def __init__(self, body):
        raw = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.data = None
        self.status = 200

    def _json(self, data, status=200):
        self.data = data
        self.status = status


UID = 1001
UDIR = _tmp / str(UID)
UDIR.mkdir(parents=True, exist_ok=True)
OV_FILE = UDIR / "settings.json"

print("=== A. 前置：用户覆盖文件初始为空 ===")
check("A1 settings.json 尚不存在", not OV_FILE.exists())


def _overlay_on_disk() -> dict:
    if not OV_FILE.exists():
        return {}
    return json.loads(OV_FILE.read_text(encoding="utf-8"))


print("=== B. 锁对象语义 ===")
check("B1 同一 uid 拿到的锁是同一个对象",
      routes_config._overlay_lock(UID) is routes_config._overlay_lock(UID))
check("B2 不同 uid 的锁互不相干（不互相排队）",
      routes_config._overlay_lock(UID) is not routes_config._overlay_lock(UID + 1))
check("B3 锁字典已注册该 uid", UID in routes_config._OVERLAY_LOCKS)

print("=== C. 并发双写：两次改动都必须留在盘上 ===")
_real_write = st.atomic_write_json
_entered = threading.Event()


def _slow_write(fp, data, *a, **k):
    """第一次写盘时模拟慢写（真实世界=磁盘/杀毒拖慢），制造确定性交错窗口。"""
    if not _entered.is_set():
        _entered.set()
        time.sleep(0.4)
    return _real_write(fp, data, *a, **k)


st.atomic_write_json = _slow_write
_results = {}


def _worker(tag, body):
    tok = cfg.set_user_context(user_dir=UDIR)
    cfg.set_user_overlay({})      # 模拟请求入口装载的**旧快照**（空）
    try:
        h = FakeH(body)
        routes.set_config(h)
        _results[tag] = h.data
    finally:
        cfg.reset_user_context(tok)


try:
    tA = threading.Thread(target=_worker, args=("A", {"analyzer_model": "model-from-A"}))
    tA.start()
    check("C1 A 已进入写盘窗口（交错窗口成立，不是空跑）", _entered.wait(3.0) is True)
    tB = threading.Thread(target=_worker, args=("B", {"polisher_model": "model-from-B"}))
    tB.start()
    tA.join(10)
    tB.join(10)
finally:
    st.atomic_write_json = _real_write

_disk = _overlay_on_disk()
check("C2 A 的改动在盘上", _disk.get("analyzer_model") == "model-from-A")
check("C3 B 的改动也在盘上（锁生效，未被整份覆盖）",
      _disk.get("polisher_model") == "model-from-B")
check("C4 两次请求都返回 ok", _results.get("A", {}).get("ok") and _results.get("B", {}).get("ok"))

print("=== D. 串行写：后写只改自己带的字段，不抹掉先前字段 ===")
h = FakeH({"organizer_model": "model-from-C"})
tok = cfg.set_user_context(user_dir=UDIR)
try:
    routes.set_config(h)
finally:
    cfg.reset_user_context(tok)
_disk = _overlay_on_disk()
check("D1 新增字段落盘", _disk.get("organizer_model") == "model-from-C")
check("D2 先前两个字段仍在（读-改-写而非整份替换）",
      _disk.get("analyzer_model") == "model-from-A"
      and _disk.get("polisher_model") == "model-from-B")

print("=== E. 校验与边界（既有语义不变） ===")
h = FakeH({"proactive_hard": 99, "proactive_soft": 9, "retriever_effort": "bogus"})
tok = cfg.set_user_context(user_dir=UDIR)
try:
    routes.set_config(h)
finally:
    cfg.reset_user_context(tok)
_disk = _overlay_on_disk()
check("E1 proactive_hard 仍被夹到 10", _disk.get("proactive_hard") == 10)
check("E2 proactive_soft 仍被夹到 1.0", _disk.get("proactive_soft") == 1.0)
check("E3 非法 effort 被忽略（不写脏值）", "retriever_effort" not in _disk)
check("E4 文件仍是合法 JSON 对象", isinstance(_disk, dict) and bool(_disk))

print("=== F. 损坏文件不致命（按空覆盖继续） ===")
OV_FILE.write_text("{ 这不是 json", encoding="utf-8")
check("F1 读损坏文件回退空覆盖", routes_config._load_overlay_file(UID) == {})
h = FakeH({"polisher_model": "model-after-corrupt"})
tok = cfg.set_user_context(user_dir=UDIR)
try:
    routes.set_config(h)
finally:
    cfg.reset_user_context(tok)
check("F2 损坏后仍能正常写入", _overlay_on_disk().get("polisher_model") == "model-after-corrupt")

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
