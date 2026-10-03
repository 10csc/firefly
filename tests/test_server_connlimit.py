# -*- coding: utf-8 -*-
"""单连接限速的**端到端接线验收**（2026-10-01；用户要求"一条流不许占满整线"）

为什么单独有这个文件：`ConnLimiter` 自己有确定性单测（`test_server_traffic.py` §10），
但"限速器**到底有没有被接进大文件写出路径**"只有真起网关才验得到 ——
这类"写了没接线"的回归（例如重构 `_send_file` 时漏掉那 3 行）纯单测抓不到。

四个断言面：
  ① 大文件（>5MB）单流被压在 `FIREFLY_CONN_MAX_KBPS` 附近；
  ② 小文件（<5MB）**不受影响**（仍秒回）—— 免得"为了防大文件占线"把公告图/小卡一起拖慢；
  ③ `FIREFLY_CONN_MAX_KBPS=0` 一键回退到满速；
  ④ 上限确实来自"单连接"这一层：同一次运行里，大文件慢、小文件快。

背景数字（线上实测）：加限速前单流 344 KB/s ≈ 满 2800kbps；加 50% 上限后 172 KB/s。
"""
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
SERVER = ROOT / "server"
PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}", flush=True)


def free_port(lo=20000, hi=40000):
    """随便挑一个空闲端口。

    ⚠ 两个教训（2026-10-01 全量 sweep 实测）：
    - **别用固定小端口段**（如 8871）：并行跑测试/多个队友同时开发时会被别人抢走，
      表现是"网关起不来"而不是"端口占用"，排查成本高。这里在 20000-40000 里**随机起点**扫，
      避开 Windows 保留段（9102-9201 / 9293-9892 / 50000-50059，见协作记忆 #18）。
    - 选到的端口仍可能被抢 ⇒ 调用方要容忍"起不来"，并**打印网关日志**（见 spawn/wait_ready）。
    """
    import random
    start = random.randint(lo, max(lo, hi - 60))
    for p in range(start, min(hi, start + 60)):
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


TMP = Path(tempfile.mkdtemp(prefix="ff_connlim_"))
BIG = 6 * 1024 * 1024                      # > BIG_FILE(5MB) ⇒ 受单连接限速
SMALL = 200 * 1024
(TMP / "firefly.apk").write_bytes(b"A" * BIG)
(TMP / "firefly-setup.exe").write_bytes(b"B" * SMALL)

CAP_KBPS = 400                             # 50 KB/s
RANGE_LEN = 96 * 1024                      # 96KB：约 (96KB-12.5KB突发)/50KB/s ≈ 1.7s


def spawn(conn_kbps, port):
    env = dict(os.environ)
    env.update({"FIREFLY_BW_KBPS": "2800", "FIREFLY_CONN_MAX_KBPS": str(conn_kbps),
                "FIREFLY_BULK_IP_BYTES": "0", "PYTHONUNBUFFERED": "1"})
    log = open(TMP / f"gw{port}.log", "wb")
    p = subprocess.Popen([sys.executable, "download_server.py", str(port), str(TMP)],
                         cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)
    return p, log


def fetch(port, path, headers=None, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, body, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.perf_counter() - t0


def wait_ready(port, timeout=45.0):
    """等网关就绪。等待窗口给足（机器被并行任务压满时，起进程+import 可能要十几秒）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s, _b, _d = fetch(port, "/health", timeout=3)
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def dump_log(tag):
    """起不来时把网关日志打出来 —— 否则"没就绪"这种失败只能靠猜。"""
    try:
        for fn in sorted(TMP.glob("gw*.log")):
            txt = fn.read_text(encoding="utf-8", errors="replace").strip()
            if txt:
                print(f"     [{tag}] {fn.name} 末尾：\n       " + "\n       ".join(txt.splitlines()[-6:]))
    except Exception:
        pass


gw = gwlog = None
try:
    # ── 限速开：大文件应被压住 ──
    P1 = free_port()
    gw, gwlog = spawn(CAP_KBPS, P1)
    ready1 = wait_ready(P1)
    check(f"① 网关就绪（端口 {P1}，单连接上限 {CAP_KBPS}kbps）", ready1)
    if not ready1:
        dump_log("限速实例")
        raise SystemExit("网关未就绪，后续断言无意义（见上面日志）")

    s, body, dt = fetch(P1, "/download/firefly.apk", {"Range": f"bytes=0-{RANGE_LEN - 1}"})
    check(f"① 大文件 Range 返回 206 且字节完整（{len(body)}B）",
          s == 206 and len(body) == RANGE_LEN, f"status={s} len={len(body)}")
    kbps = len(body) * 8 / 1000 / dt
    # 期待 ≈50KB/s；允许突发与调度误差，只要求"明显被压住"（<200kbps）且不是卡死
    check(f"①b **单流被压在上限附近**（{dt:.2f}s = {kbps:.0f} kbps，上限 {CAP_KBPS}）",
          kbps < CAP_KBPS * 1.6 and kbps > CAP_KBPS * 0.4, f"{kbps:.0f} kbps")

    s, body, dt_s = fetch(P1, "/download/firefly-setup.exe")
    check(f"② **小文件不受影响**（{len(body)}B / {dt_s:.2f}s）",
          s == 200 and len(body) == SMALL and dt_s < 0.9, f"{dt_s:.2f}s")

    # ── 限速关：同一大文件应明显更快（一键回退） ──
    P2 = free_port()
    gw2, gw2log = spawn(0, P2)
    try:
        check(f"③ 第二个网关就绪（端口 {P2}，单连接上限 0=关）", wait_ready(P2))
        s2, body2, dt2 = fetch(P2, "/download/firefly.apk", {"Range": f"bytes=0-{RANGE_LEN - 1}"})
        check(f"③b 关闭上限后同一 Range 明显更快（{dt2:.2f}s vs 限速时 {dt:.2f}s）",
              s2 == 206 and dt2 < dt * 0.6, f"{dt2:.2f}s vs {dt:.2f}s")
        # 这里**不**断言"总速率 ≤2800kbps"：96KB 这种短传输可以整段由突发额度/空闲池瞬间发出，
        # 稳态速率根本来不及体现（我第一版就误判成"没限速"）。类限速由 test_server_traffic.py
        # 用注入时钟做确定性验证，这里只关心"单连接上限"这一层。
    finally:
        try:
            gw2.terminate(); gw2.wait(timeout=8)
        except Exception:
            try:
                gw2.kill()
            except Exception:
                pass
        gw2log.close()
finally:
    if gw is not None:
        try:
            gw.terminate(); gw.wait(timeout=8)
        except Exception:
            try:
                gw.kill()
            except Exception:
                pass
        gwlog.close()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
