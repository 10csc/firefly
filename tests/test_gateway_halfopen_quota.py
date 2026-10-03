# -*- coding: utf-8 -*-
"""网关「全局慢连接/半开配额」回归（R-01 / 安全收尾 A1）

问题（`docs/交接/PC端-交接执行计划/08_安全收尾与用户可见文本.md` §A1）：
每 IP 有软 16 / 硬 64，但**全局**只有 `MAX_THREADS=160` ⇒ 按硬上限算 **3 个源 IP**
（3×64=192）即可打满全局池 ⇒ **全站** 503（含经反代的全部业务 API）。

修法：给"正在读请求头的连接"单独设全局上限 `FIREFLY_MAX_HALFOPEN`（默认 32，远小于线程池）；
超限**只拒多余那一条**（503 + Retry-After），**不拉黑 IP**、不断开已建立连接、不影响其正常请求。

本测试钉死三半：
  ① **单元**：全局计数语义（到上限才拒、释放可再用、关闭开关时恒放行）；
  ② **端到端（多源 IP）**：用 **127.0.0.3~127.0.0.20** 多源发半开连接（数量远超线程池），
     期间从**另一个来源 IP 127.0.0.99** 发的控制请求与 `/health` **必须仍 200**；
     同 IP 20 条正常完整请求 **20/20**；
  ③ **鉴别力（旧行为对照）**：把该配额关掉（`FIREFLY_MAX_HALFOPEN=0`，单一变量回退）后
     同样负载 ⇒ 控制请求**必须出现 503** —— 证明上面②的通过不是"环境恰好不触发"。

沙箱：临时 BASE + 临时端口 + 桩上游（`FIREFLY_PROXY_TARGET`），只打 loopback，自清理。
★ 绝不碰 `E:\\Firefly`，绝不碰生产。
"""
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
PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


# ── ① 单元：全局半开计数语义（import 前设好环境，模块级读入）──────────────────
print("== ① 单元：全局「慢连接/半开」配额语义 ==")
os.environ["FIREFLY_MAX_HALFOPEN"] = "16"
os.environ["FIREFLY_MAX_IP_CONNS"] = "64"
os.environ["FIREFLY_MAX_IP_CONNS_HARD"] = "64"
_argv = sys.argv
sys.argv = ["download_server.py"]
sys.path.insert(0, str(SERVER))
try:
    import download_server as ds
finally:
    sys.argv = _argv

check("①a 名义预算与硬顶都远小于线程池",
      0 < ds.MAX_HALFOPEN < ds.MAX_THREADS and 0 < ds.MAX_HALFOPEN_HARD <= ds.MAX_THREADS // 2,
      f"budget={ds.MAX_HALFOPEN} hard={ds.MAX_HALFOPEN_HARD} threads={ds.MAX_THREADS}")
_boundary = max(0, ds.MAX_HALFOPEN - ds.HALFOPEN_RESERVE)
_handles = [object() for _ in range(400)]
_n = 0
for h in _handles:
    if ds._halfopen_acquire_for(h, "9.9.9.9"):
        _n += 1
    else:
        break
check(f"①b 单 IP 最多能占到预留区边界（{_boundary}），再多被拒",
      _n == _boundary, f"got={_n} boundary={_boundary}")
check("①c 超限后继续尝试仍被拒", ds._halfopen_acquire_for(object(), "9.9.9.9") is False)
check("①d 超限计数已累加", ds._halfopen_stats()["rejected_total"] >= 1)
check("①e 同一连接重复 acquire 幂等（不重复占名额）",
      ds._halfopen_acquire_for(_handles[0], "9.9.9.9") is True
      and ds._halfopen_stats()["now"] == _n, str(ds._halfopen_stats()))
check("①f ★ **新来的 IP 不被饿死**（预留区对 mine<MIN_SHARE 的 IP 开放）",
      ds._halfopen_acquire_for(_handles[-1], "8.8.8.8") is True)
ds._halfopen_release_for(_handles[0])
check("①g 释放后计数下降", ds._halfopen_stats()["now"] == _n, str(ds._halfopen_stats()))
check("①h 重复 release 幂等（不会把计数打成负）",
      (ds._halfopen_release_for(_handles[0]), ds._halfopen_stats()["now"]) == (None, _n),
      str(ds._halfopen_stats()))


# ── 桩上游（让 /health 这类反代路径可达）────────────────────────────────
class _Up(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, *a):
        pass


def free_port(lo=20000, hi=44000):
    import random
    s0 = random.randint(lo, max(lo, hi - 80))
    for p in range(s0, min(hi, s0 + 80)):
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


UP_PORT = free_port()
up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), _Up)
up.daemon_threads = True
threading.Thread(target=up.serve_forever, daemon=True).start()

TMP = Path(tempfile.mkdtemp(prefix="ff_halfopen_"))
os.makedirs(TMP / "notice", exist_ok=True)
(TMP / "notice" / "latest").write_bytes(b'{"v":1}')


def start_gw(max_halfopen: int):
    port = free_port()
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "FIREFLY_BIND": "127.0.0.1", "FIREFLY_BW_KBPS": "0",
                "FIREFLY_NOTICE_DIR": str(TMP / "notice"),
                "FIREFLY_PROXY_TARGET": "127.0.0.1:%d" % UP_PORT,
                "FIREFLY_MAX_IP_CONNS": "16", "FIREFLY_MAX_IP_CONNS_HARD": "64",
                "FIREFLY_HEADER_TIMEOUT": "8", "FIREFLY_OVERFLOW_HEADER_TIMEOUT": "2.5",
                "FIREFLY_MAX_HALFOPEN": str(max_halfopen)})
    lg = open(TMP / f"gw_{max_halfopen}.log", "wb")
    p = subprocess.Popen([sys.executable, "download_server.py", str(port), str(TMP)],
                         cwd=str(SERVER), env=env, stdout=lg, stderr=subprocess.STDOUT)
    return p, port, lg


def get(port, path, src=None, timeout=8):
    """从指定来源 IP 发一次请求（src=None 用系统默认源）。"""
    try:
        if src:
            s = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
            s.settimeout(timeout)
            s.bind((src, 0))
            s.connect(("127.0.0.1", port))
            s.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
            data = b""
            while True:
                b = s.recv(65536)
                if not b:
                    break
                data += b
            s.close()
            return int(data.split(b" ")[1]) if b" " in data[:16] else 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__


def get_raw(port, path, src, timeout=8):
    """从指定来源 IP 发一次请求，返回 (状态码, 响应体文本)。"""
    try:
        s = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
        s.settimeout(timeout)
        s.bind((src, 0))
        s.connect(("127.0.0.1", port))
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
        data = b""
        while True:
            b = s.recv(65536)
            if not b:
                break
            data += b
        s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        code = int(head.split(b" ")[1]) if b" " in head[:16] else 0
        return code, body.decode("utf-8", "replace")
    except Exception as e:
        return type(e).__name__, ""


def open_halfopen(port, src, n):
    """从 src 开 n 条'半截请求头'连接（不发结束空行）。返回成功建立的 socket 列表。"""
    out = []
    for _ in range(n):
        try:
            s = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
            s.settimeout(5)
            s.bind((src, 0))
            s.connect(("127.0.0.1", port))
            s.sendall(b"GET /notice/latest HTTP/1.1\r\nHost: x\r\n")   # 无结束空行 = 半开
            out.append(s)
        except Exception:
            pass
    return out


def close_all(socks):
    for s in socks:
        try:
            s.close()
        except Exception:
            pass


SRCS = [f"127.0.0.{i}" for i in range(3, 21)]      # 18 个源 IP
CTL = "127.0.0.99"                                  # 控制请求来源（另一个 IP）
N_PER_IP = 12                                       # 18×12 = 216 > MAX_THREADS(160)

try:
    # ── ② 修复后：全局配额 32 ⇒ 多源半开期间全站仍可用 ──────────────────
    print("== ② 端到端（多源 IP 半开）：/notice 与 /health 仍 200 ==")
    gw, port, lg = start_gw(32)
    try:
        ready = any(get(port, "/download/stats") == 200 or time.sleep(0.4) for _ in range(40))
        check("②a 沙箱网关就绪", bool(ready))
        check("②b 攻击前控制请求 200", get(port, "/notice/latest", CTL) == 200)

        socks = []
        for src in SRCS:
            socks += open_halfopen(port, src, N_PER_IP)
        print(f"     已持有半开连接 {len(socks)} 条（上限 32，其余应被秒拒）")
        codes_notice = [get(port, "/notice/latest", CTL) for _ in range(5)]
        codes_health = [get(port, "/health", CTL) for _ in range(3)]
        check(f"②c 多源半开期间 /notice 控制请求仍 200（{codes_notice}）",
              all(c == 200 for c in codes_notice), f"codes={codes_notice}")
        check(f"②d 多源半开期间 /health 仍 200（{codes_health}）",
              all(c == 200 for c in codes_health), f"codes={codes_health}")
        st_code, raw = get_raw(port, "/download/stats", CTL)
        check("②e 控制 IP 可读到 /download/stats", st_code == 200 and "{" in raw,
              f"code={st_code} body={raw[:120]!r}")
        import json as _json
        try:
            ho = (_json.loads(raw) if raw else {}).get("halfopen") or {}
        except Exception:
            ho = {}
        check("②e2 /download/stats 暴露 halfopen 计数", "now" in ho, f"halfopen={ho}")
        check("②f 全局半开数被压在**硬顶**内（≤ MAX_THREADS/2，绝不占满线程池）",
              0 < int(ho.get("now", 0)) <= int(ho.get("hard_max", 0)) <= ds.MAX_THREADS // 2,
              f"now={ho.get('now')} hard_max={ho.get('hard_max')}")
        check("②g 超限被拒计数 > 0（说明确实在拦）", int(ho.get("rejected_total", 0)) > 0,
              f"rejected={ho.get('rejected_total')}")

        # 同 IP 20 条正常完整请求 → 20/20（NAT 不误伤）
        # ★ 用一个**没参与攻击**的来源 IP，否则测的是"攻击者自己的 IP 被限"（那是设计行为）。
        src_ok = "127.0.0.98"
        ok20 = [get(port, "/notice/latest", src_ok) for _ in range(20)]
        check(f"②h 未参与攻击的同 IP 20 条正常请求 20/20（{ok20.count(200)}/20）",
              ok20.count(200) == 20, f"codes={ok20}")

        close_all(socks)
        time.sleep(1.0)
        check("②i 关闭半开后立即恢复", get(port, "/notice/latest", CTL) == 200)
    finally:
        try:
            gw.terminate(); gw.wait(timeout=8)
        except Exception:
            try:
                gw.kill()
            except Exception:
                pass
        lg.close()

    # ── ③ 旧行为对照：关掉全局配额（单一变量回退）⇒ 必须能复现 503 ────────
    print("== ③ 旧行为对照：FIREFLY_MAX_HALFOPEN=0 时应能复现 503（鉴别力）==")
    gw2, port2, lg2 = start_gw(0)
    try:
        ready2 = any(get(port2, "/download/stats") == 200 or time.sleep(0.4) for _ in range(40))
        check("③a 对照网关就绪", bool(ready2))
        socks2 = []
        for src in SRCS:
            socks2 += open_halfopen(port2, src, N_PER_IP)
        print(f"     已持有半开连接 {len(socks2)} 条（无全局配额 ⇒ 只受每 IP 64 与线程池限制）")
        codes2 = [get(port2, "/notice/latest", CTL) for _ in range(6)]
        # ★ 判据放宽到"**没有被服务**"：关掉配额后全站过载，控制请求可能表现为 503
        #   （服务级拒绝）或 连接被重置/中止（accept backlog 与线程池双爆）。
        #   两者都等价于"用户拿不到服务"，都足以证明②的通过有鉴别力。
        check(f"③b 关掉配额后控制请求**不再被服务**（{codes2}）",
              all(c != 200 for c in codes2), f"codes={codes2}")
        close_all(socks2)
    finally:
        try:
            gw2.terminate(); gw2.wait(timeout=8)
        except Exception:
            try:
                gw2.kill()
            except Exception:
                pass
        lg2.close()
finally:
    try:
        up.shutdown()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
