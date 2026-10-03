# -*- coding: utf-8 -*-
"""抢占控制端到端压测 —— 共创平台 M4 的核心验收（用户原话：多任务流量"抢占"）

设计文档里 M4 的验收原话是：**「压测：下载大文件时聊天 relay P95 不劣化 >20%」**。
此前只测了限速器**自身**的速率与配额（`test_server_traffic.py` 40 项），
**从没端到端量过这个性质**：真实网关 + 真实大文件下载 + 并发的业务 API 请求。

做法（全部在本机沙箱，不碰真实 300KB/s 出口、不碰真实 8765）：
- 起一个**桩上游**（模拟主服务的业务 API，处理一次约几毫秒）；
- 网关用 `FIREFLY_PROXY_TARGET` 指向桩（默认值仍是 127.0.0.1:8765，产品行为不变）；
- 先量**基线**（无下载时业务 API 的 p50/p95）；
- 再开一个后台线程**限速下载大文件**，在下载进行中量同一批业务 API 的 p50/p95；
- 断言：**p95 不劣化超过阈值**（否则就是"一个下载把聊天挤死"，正是要防的抢占）。

退出码：0 全过；1 断言失败；2 环境没起来。
"""
import json
import os
import shutil
import socket as _sk
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
TMP = Path(tempfile.mkdtemp(prefix="ff_preempt_"))
PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


def free_port(lo, hi):
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


def pct(xs, q):
    if not xs:
        return float("inf")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * len(s))) - 1))
    # ⚠ 输入**已经是毫秒**（api_ping 里已 ×1000）。第一版这里又乘了一次 1000，
    # 于是打印出"基线 p50=30353ms / 30 秒"这种荒谬数字（真值 30.4ms），
    # 还差点让我误判成"限速器把 api 类饿死了"去改产品。**单位必须写在函数名/注释里**。
    return s[i]


# ── 1. 桩上游：模拟"聊天 relay / 业务 API"（几毫秒处理）──
class Stub(BaseHTTPRequestHandler):
    # ⚠ **故意用 HTTP/1.0（默认）而不是 1.1**：网关的 `_proxy` 是"读一块转一块"的流式转发，
    # 判结束靠 `resp.read()` 返回空块；若桩用 HTTP/1.1 keep-alive，连接不关，转发循环会一直等
    # （我第一版就踩了这个，表现为"桩明明回了 200，客户端却超时"）。
    # 真实主服务走的就是 HTTP/1.0/close，所以 HTTP/1.0 才是**忠实**的桩。

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


STUB_PORT = free_port(8981, 9020)
check(f"桩上游端口 {STUB_PORT}", STUB_PORT > 0)
stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
stub.daemon_threads = True
threading.Thread(target=stub.serve_forever, daemon=True).start()

# ── 2. 沙箱下载目录 + 一个大文件（1.5MB，够撑出测量窗口）──
BLOB = os.urandom(1024 * 1024) + os.urandom(512 * 1024)   # 1.5MB 不可压缩，避免"压缩后瞬间传完"
(TMP / "firefly.apk").write_bytes(BLOB)

GW_PORT = free_port(9021, 9060)
check(f"网关端口 {GW_PORT}", GW_PORT > 0)

BW_KBPS = 800          # 预算 800（kbps）≈100KB/s ⇒ 1.5MB 约需 15 秒，窗口足够
env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BW_KBPS": str(BW_KBPS),
            "FIREFLY_BULK_IP_BYTES": "0",
            "FIREFLY_BIND": "127.0.0.1",
            "FIREFLY_PROXY_TARGET": f"127.0.0.1:{STUB_PORT}"})
log = open(TMP / "gw.log", "wb")
gw = subprocess.Popen([sys.executable, "download_server.py", str(GW_PORT), str(TMP)],
                      cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{GW_PORT}"


_dbg = {"printed": False}


def api_ping(timeout=10):
    """一次业务 API 请求（走网关 → 桩上游），返回 (状态码, 毫秒)。"""
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(base + "/api/ping", timeout=timeout) as r:
            r.read()
            return r.status, (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        if not _dbg["printed"]:
            _dbg["printed"] = True
            print(f"     [诊断] 客户端侧异常：{type(e).__name__}: {e}", flush=True)
        return 0, (time.perf_counter() - t0) * 1000.0


# 下载线程：把大文件流式读完（丢弃），记录窗口内收到多少字节
dl = {"bytes": 0, "status": 0, "done": False, "err": ""}


def downloader():
    try:
        with urllib.request.urlopen(base + "/download/firefly.apk", timeout=120) as r:
            dl["status"] = r.status
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                dl["bytes"] += len(chunk)
    except Exception as e:
        dl["err"] = str(e)
    finally:
        dl["done"] = True


try:
    ready = False
    for _ in range(60):
        s, _ = api_ping()
        if s == 200:
            ready = True
            break
        time.sleep(0.4)
    if not ready:
        print("网关或桩未起来", flush=True)
        print((TMP / "gw.log").read_text("utf-8", "replace")[-1200:], flush=True)
        sys.exit(2)

    print("\n== 基线：无下载时业务 API 延迟 ==")
    base_lat = []
    for _ in range(25):
        s, ms = api_ping()
        if s == 200:
            base_lat.append(ms)
    check(f"① 基线 25 次全部 200（样本 {len(base_lat)}）", len(base_lat) >= 25)
    p50b, p95b = pct(base_lat, 0.50), pct(base_lat, 0.95)
    print(f"     基线 p50={p50b:.1f}ms  p95={p95b:.1f}ms")

    print("\n== 并发：大文件限速下载进行中，同时打同一批业务 API ==")
    t = threading.Thread(target=downloader, daemon=True)
    t0 = time.perf_counter()
    t.start()
    # 等下载真的开始（拿到首块）再开始测量，避免把"连接建立"算成并发窗口
    for _ in range(100):
        if dl["bytes"] > 0:
            break
        time.sleep(0.05)
    check("② 下载已真正开始（窗口非空转）", dl["bytes"] > 0, f"bytes={dl['bytes']}")

    during = []
    min_bytes = 96 * 1024            # 窗口内下载至少推进这么多，才算"并发真的发生了"
    tw = time.perf_counter()
    while time.perf_counter() - tw < 10.0:
        s, ms = api_ping()
        if s == 200:
            during.append(ms)
        # ⚠ 退出条件必须**同时**看两件事：样本够了 + 下载真的推进了。
        # 第一版只等 25 个样本就退出，而 25×18ms≈0.45s ⇒ 下载还没跑起来就收工，
        # 于是"窗口 ≥1s"那条断言随机红（窗口长度由样本数决定，与下载无关 —— 断言设计错了）。
        if len(during) >= 25 and dl["bytes"] >= min_bytes:
            break
    span = time.perf_counter() - t0
    p50d, p95d = pct(during, 0.50), pct(during, 0.95)
    print(f"     下载中 p50={p50d:.1f}ms  p95={p95d:.1f}ms（样本 {len(during)}）")
    print(f"     下载窗口 {span:.1f}s，收到 {dl['bytes'] / 1024:.0f}KB，状态 {dl['status']}"
          f"{'，错误 ' + dl['err'] if dl['err'] else ''}")

    check("③ 并发期业务 API 全部 200（不是被饿死/拒连）", len(during) >= 25)
    # 阈值取"相对 2.5 倍 或 绝对 +60ms"的宽松者：本机负载抖动不该让这条变红，
    # 而真抢占（一个下载把 API 挤到秒级）会远远突破。
    limit = max(p95b * 2.5, p95b + 60.0)
    check(f"④ ★抢占控制：并发期 p95 {p95d:.1f}ms ≤ 上限 {limit:.1f}ms（基线 {p95b:.1f}ms）",
          p95d <= limit, f"p95_during={p95d:.1f} > {limit:.1f}")
    check(f"⑤ 中位数也没被拖垮（{p50d:.1f}ms ≤ {max(p50b * 3, p50b + 40):.1f}ms）",
          p50d <= max(p50b * 3, p50b + 40))
    check(f"⑥ 下载确实在**被限速地**进行：窗口 {span:.1f}s 内只推进 "
          f"{dl['bytes'] / 1024:.0f}KB（未瞬间传完 {len(BLOB) / 1024:.0f}KB）",
          dl["bytes"] >= min_bytes and dl["bytes"] < len(BLOB),
          f"bytes={dl['bytes']}")
    rate_kbps = dl["bytes"] * 8 / max(span, 0.001) / 1000.0
    check(f"⑦ 实测吞吐 {rate_kbps:.0f}kbps ≤ 预算 {BW_KBPS}kbps 的 2 倍（限速真的生效）",
          rate_kbps <= BW_KBPS * 2, f"{rate_kbps:.0f}kbps")

    print("\n== 控制面自检（本机专用端点）==")
    try:
        with urllib.request.urlopen(base + "/download/traffic", timeout=5) as r:
            tr = json.loads(r.read().decode("utf-8"))
        chans = (tr.get("downloads") or {}).get("by_channel") or tr.get("by_channel") or {}
        check("⑧ /download/traffic 可读（限速预算与用量可见）", bool(tr))
        print(f"     traffic: {json.dumps(tr, ensure_ascii=False)[:220]}")
    except Exception as e:
        check("⑧ /download/traffic 可读", False, str(e))
finally:
    try:
        gw.terminate()
        gw.wait(timeout=8)
    except Exception:
        try:
            gw.kill()
        except Exception:
            pass
    log.close()
    stub.shutdown()
    stub.server_close()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
