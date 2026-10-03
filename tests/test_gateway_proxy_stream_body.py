# -*- coding: utf-8 -*-
"""网关反代请求体**流式转发**回归（安全审计 S-4）

漏洞（代码审查 + 本机量级）：`_proxy` 里 `self.rfile.read(n)` 把 ≤16MB 的请求体**一次性
读进内存**（`MAX_PROXY_BODY=16MB` × 64 线程 ≈ 1GB 峰值），而服务器 1.6GB 内存、**swap=0**
⇒ OOM 风险；且攻击在**鉴权之前**就完成读入。

本测试钉死：
  ① **确实在流式转发**：客户端只发了前 128KB 时，上游**已经收到**数据（若网关先整包缓冲，
     上游在客户端发完之前一个字节都收不到）；
  ② 完整大请求体端到端**逐字节送达**（2MB POST → 上游收到的字节数精确）；
  ③ 超过 `MAX_PROXY_BODY` 的声明体量 → 在读之前就 **413** 拒绝。

沙箱：本进程内起一个忠实桩上游（HTTP/1.0 close 语义）+ 临时网关，只打 loopback，自清理。
"""
import os
import shutil
import socket as _sk
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PASS = FAIL = 0

TOTAL = 2 * 1024 * 1024
FIRST = 128 * 1024                # ≥ PROXY_STREAM_CHUNK(64KB)，保证网关第一次 read 能返回
RECV = {"n": 0}


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


def free_port(lo=20000, hi=40000):
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


class Stub(BaseHTTPRequestHandler):
    """忠实桩：读完整 body（分块读，随时可观测"已收到多少"），回一个 JSON 计数。"""
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        got = 0
        while got < n:
            chunk = self.rfile.read(min(65536, n - got))
            if not chunk:
                break
            got += len(chunk)
            RECV["n"] = got
        body = ('{"got":%d}' % got).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


STUB_PORT = free_port()
GW_PORT = free_port()
check(f"端口分配（桩 {STUB_PORT} / 网关 {GW_PORT}）", STUB_PORT > 0 and GW_PORT > 0)

stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
stub.daemon_threads = True
threading.Thread(target=stub.serve_forever, daemon=True).start()

TMP = Path(tempfile.mkdtemp(prefix="ff_stream_"))
env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BIND": "127.0.0.1", "FIREFLY_BW_KBPS": "0", "FIREFLY_BULK_IP_BYTES": "0",
            "FIREFLY_PROXY_TARGET": f"127.0.0.1:{STUB_PORT}"})
log = open(TMP / "gw.log", "wb")
gw = subprocess.Popen([sys.executable, "download_server.py", str(GW_PORT), str(TMP)],
                      cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)


def read_http_response(sock):
    """从裸 socket 读一个响应，返回 (status:int, body:bytes)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    clen = 0
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        if k.strip().lower() == b"content-length":
            clen = int(v.strip())
    body = rest
    while len(body) < clen:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return status, body[:clen]


try:
    ready = False
    for _ in range(60):
        try:
            s = _sk.create_connection(("127.0.0.1", GW_PORT), timeout=2)
            s.sendall(b"GET /download/stats HTTP/1.1\r\nHost: x\r\n\r\n")
            st, _ = read_http_response(s)
            s.close()
            if st == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.4)
    check("① 沙箱网关就绪", ready)
    if not ready:
        print((TMP / "gw.log").read_text("utf-8", "replace")[-1500:], flush=True)

    print("== ② 流式证明：客户端只发前 128KB → 上游已收到（未整包缓冲）==")
    sock = _sk.create_connection(("127.0.0.1", GW_PORT), timeout=30)
    head = ("POST /api/upload HTTP/1.1\r\nHost: x\r\n"
            "Content-Type: application/octet-stream\r\n"
            f"Content-Length: {TOTAL}\r\n\r\n").encode()
    sock.sendall(head)
    sock.sendall(b"Z" * FIRST)
    time.sleep(1.2)
    streamed = RECV["n"]
    check(f"② 客户端未发完，上游已收到 {streamed}B > 0（若整包缓冲则为 0）", streamed > 0,
          f"received={streamed}")

    rest = b"Z" * (TOTAL - FIRST)
    sock.sendall(rest)
    status, body = read_http_response(sock)
    sock.close()
    check(f"③ 2MB 请求体端到端逐字节送达（上游 got={RECV['n']}）", RECV["n"] == TOTAL,
          f"got={RECV['n']} want={TOTAL}")
    check(f"③b 网关正确回传上游响应（status={status} body={body[:40]!r}）",
          status == 200 and (b'"got":%d' % TOTAL) in body)

    print("== ④ 超过 MAX_PROXY_BODY → 读之前 413 ==")
    s2 = _sk.create_connection(("127.0.0.1", GW_PORT), timeout=10)
    big = 17 * 1024 * 1024
    s2.sendall(("POST /api/upload HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {big}\r\n\r\n").encode())
    st2, _ = read_http_response(s2)
    s2.close()
    check(f"④ 声明 17MB（>16MB）→ 413（不读 body）", st2 == 413, f"status={st2}")
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

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
