# -*- coding: utf-8 -*-
"""网关「响应发送超时」回归（R-02 / 安全收尾 A2 / P7-1）

问题：连接**已建立但不读响应**（慢读）时，处理线程会阻塞在 write、名额一直被占，
⇒ 与攻击者共用出口 IP 的正常用户被**临时连坐**（全站不受影响，但那个 IP 不可用）。

修法：只给**响应写出**加独立发送超时 `FIREFLY_SEND_TIMEOUT`（默认 10s）；
超时就断开**这一条**连接并计数（`/download/stats` 的 `send_timeout.aborted_total`）。
★ 不动读路径：读请求头的超时仍由 `HEADER_TIMEOUT` + 绝对截止看门狗负责。

验收（`08` §A2）：慢读连接在超时后被断开；同 IP 正常请求随后**立即恢复**；另一个 IP 全程 200。
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


# ── ① 单元：包装流的超时语义（不起网络）────────────────────────────────
print("== ① 单元：发送超时与计数 ==")
os.environ["FIREFLY_SEND_TIMEOUT"] = "1"
_argv = sys.argv
sys.argv = ["download_server.py"]
sys.path.insert(0, str(SERVER))
try:
    import download_server as ds
finally:
    sys.argv = _argv

check("①a SEND_TIMEOUT 已定义且远小于旧空闲超时（120s）",
      0 < ds.SEND_TIMEOUT < 120, f"SEND_TIMEOUT={ds.SEND_TIMEOUT}")


class _Blowup:
    def write(self, data):
        raise _sk.timeout("模拟对端不读导致写超时")

    def flush(self):
        raise _sk.timeout("模拟对端不读导致写超时")


class _Sock:
    def __init__(self):
        self.calls = []
        self.shut = False

    def gettimeout(self):
        return 120.0

    def settimeout(self, t):
        self.calls.append(t)

    def shutdown(self, how):
        self.shut = True


_s, _f = _Sock(), _Blowup()
_w = ds._SendTimeoutStream(_s, _f, 1.0)
_before = ds._send_timeout_stats()["aborted_total"]
try:
    _w.write(b"x" * 16)
    check("①b 超时应向上抛出（让上层断流）", False, "没有抛异常")
except _sk.timeout:
    check("①b 超时应向上抛出（让上层断流）", True)
check("①c 超时计数已累加", ds._send_timeout_stats()["aborted_total"] == _before + 1,
      str(ds._send_timeout_stats()))
check("①d 超时后主动 shutdown 该连接", _s.shut is True)
check("①e 写出期间把超时设成 SEND_TIMEOUT（写完再还原）",
      1.0 in _s.calls, f"settimeout 调用={_s.calls}")


# ── ② 端到端：慢读被断开、同 IP 恢复、另一 IP 全程 200 ────────────────────
class _Up(BaseHTTPRequestHandler):
    def do_GET(self):
        b = b'{"ok":true}'
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def log_message(self, *a):
        pass


def free_port(lo=20000, hi=46000):
    import random
    s0 = random.randint(lo, max(lo, hi - 80))
    for p in range(s0, min(hi, s0 + 80)):
        s = _sk.socket(); s.settimeout(0.3)
        try:
            s.bind(("127.0.0.1", p)); return p
        except OSError:
            continue
        finally:
            s.close()
    return 0


UP = free_port()
up = ThreadingHTTPServer(("127.0.0.1", UP), _Up); up.daemon_threads = True
threading.Thread(target=up.serve_forever, daemon=True).start()
TMP = Path(tempfile.mkdtemp(prefix="ff_sendto_"))
os.makedirs(TMP / "notice", exist_ok=True)
(TMP / "notice" / "latest").write_bytes(b'{"v":1}')
(TMP / "firefly.apk").write_bytes(b"Z" * (8 * 1024 * 1024))     # 8MB：足以把发送缓冲写满
PORT = free_port()
env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BIND": "127.0.0.1", "FIREFLY_BW_KBPS": "0",
            "FIREFLY_NOTICE_DIR": str(TMP / "notice"),
            "FIREFLY_PROXY_TARGET": "127.0.0.1:%d" % UP,
            "FIREFLY_SEND_TIMEOUT": "4"})
lg = open(TMP / "gw.log", "wb")
gw = subprocess.Popen([sys.executable, "download_server.py", str(PORT), str(TMP)],
                      cwd=str(SERVER), env=env, stdout=lg, stderr=subprocess.STDOUT)


def get_raw(path, src="127.0.0.1", timeout=10):
    try:
        s = _sk.socket(); s.settimeout(timeout); s.bind((src, 0))
        s.connect(("127.0.0.1", PORT))
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
        d = b""
        while True:
            b = s.recv(65536)
            if not b:
                break
            d += b
        s.close()
        head, _, body = d.partition(b"\r\n\r\n")
        return (int(head.split(b" ")[1]) if b" " in head[:16] else 0), body.decode("utf-8", "replace")
    except Exception as e:
        return type(e).__name__, ""


def stats(src="127.0.0.1"):
    c, b = get_raw("/download/stats", src)
    try:
        return json.loads(b) if c == 200 else {}
    except Exception:
        return {}


SLOW = "127.0.0.95"      # 慢读方（不读响应）
OTHER = "127.0.0.99"     # 第三方，应全程 200

try:
    for _ in range(40):
        if get_raw("/download/stats")[0] == 200:
            break
        time.sleep(0.4)
    check("②a 沙箱网关就绪", get_raw("/download/stats")[0] == 200)
    check("②b 攻击前另一 IP 可用", get_raw("/notice/latest", OTHER)[0] == 200)

    before = int(stats().get("send_timeout", {}).get("aborted_total", 0))
    socks = []
    for _ in range(4):
        try:
            s = _sk.socket(); s.settimeout(5); s.bind((SLOW, 0))
            s.connect(("127.0.0.1", PORT))
            s.sendall(b"GET /download/firefly.apk HTTP/1.1\r\nHost: x\r\n\r\n")
            socks.append(s)          # ★ 建立后**绝不读** ⇒ 服务端写出会阻塞
        except Exception:
            pass
    print(f"     已开 {len(socks)} 条慢读连接（不读响应），等待发送超时…")
    t0 = time.time()
    # 另一 IP 全程应 200
    other_codes = [get_raw("/notice/latest", OTHER)[0] for _ in range(5)]
    check(f"②c 慢读期间**另一个 IP 全程 200**（{other_codes}）",
          all(c == 200 for c in other_codes), f"codes={other_codes}")

    # 等发送超时生效：轮询统计
    aborted = 0
    while time.time() - t0 < 25:
        aborted = int(stats().get("send_timeout", {}).get("aborted_total", 0))
        if aborted > before:
            break
        time.sleep(0.5)
    check(f"②d 发送超时已触发并被计数（before={before} now={aborted}）", aborted > before,
          f"aborted={aborted}")

    # 慢读方被断开：先把客户端已缓冲的数据读干，再看是否 FIN（b''）
    # ★ 不能直接 recv 一次就判：服务端在超时前**已经写进客户端的若干 KB**（8MB 响应的一部分），
    #   第一次 recv 会返回数据而不是 b''（2026-10-03 实测踩到，属测试写法问题）。
    closed = 0
    for s in socks:
        ok_closed = False
        deadline = time.time() + 8
        try:
            s.settimeout(2)
            while time.time() < deadline:
                b = s.recv(262144)
                if b == b"":
                    ok_closed = True
                    break
        except Exception:
            ok_closed = True          # RST / 连接已被服务端销毁
        if ok_closed:
            closed += 1
    check(f"②e 慢读连接已被断开（{closed}/{len(socks)}）", len(socks) and closed == len(socks),
          f"closed={closed}")

    # 再等其它慢读也被计数（发送超时对每条独立生效）
    t1 = time.time()
    final_aborted = aborted
    while time.time() - t1 < 12 and final_aborted < before + len(socks):
        final_aborted = int(stats().get("send_timeout", {}).get("aborted_total", 0))
        time.sleep(0.5)
    check(f"②e2 全部慢读连接都被发送超时计数（{before}→{final_aborted}）",
          final_aborted >= before + 1, f"aborted={final_aborted}")

    # 同 IP（慢读那个 IP）随后应立即恢复
    rec = [get_raw("/notice/latest", SLOW)[0] for _ in range(5)]
    check(f"②f 同 IP 正常请求随后**立即恢复**（{rec}）", all(c == 200 for c in rec), f"codes={rec}")

    for s in socks:
        try:
            s.close()
        except Exception:
            pass
finally:
    try:
        gw.terminate(); gw.wait(timeout=8)
    except Exception:
        try:
            gw.kill()
        except Exception:
            pass
    lg.close()
    try:
        up.shutdown()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
