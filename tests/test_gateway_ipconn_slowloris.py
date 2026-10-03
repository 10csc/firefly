# -*- coding: utf-8 -*-
"""网关「单 IP 半开连接打停全站」回归（安全审计 S-1）

漏洞（`_harden/audit091_sec/_audit_t3b_slowloris.py` 复现）：单 IP 只要拿 ~70 条
**只发半截请求头**（不发结束空行）的连接，就能占满线程池 ⇒ 后续**所有**请求（含下载页、
公告、热更，以及经反代的全部业务 API）拿到 503 ⇒ 全站不可用。现有监测设施对此**完全静默**。

本测试钉死两半：
  ① **单元**：每 IP 并发连接计数的语义 —— 到硬上限才拒、按 IP 隔离、释放后可再用
     （绝不把 IP 拉黑、绝不影响其正常请求）；
  ② **端到端**：自起沙箱网关，仿审计脚本开一批半开连接，期间控制请求**不再 503**；
     连接全部关闭后立刻恢复。

沙箱：临时 BASE 目录 + 临时端口，`FIREFLY_BIND=127.0.0.1`，只打 loopback，自清理。
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
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}")


# ── ① 单元：每 IP 连接计数语义（先在 import 之前设好环境，模块级读入）──────────
print("== ① 单元：每 IP 并发连接上限的语义 ==")
os.environ["FIREFLY_MAX_IP_CONNS"] = "4"
os.environ["FIREFLY_MAX_IP_CONNS_HARD"] = "8"
_argv = sys.argv
sys.argv = ["download_server.py"]          # 防 argv 污染 BASE/PORT
sys.path.insert(0, str(SERVER))
try:
    import download_server as ds
finally:
    sys.argv = _argv

seq = [ds._ip_conn_acquire("1.2.3.4") for _ in range(8)]
check("①a 到硬上限（8）为止都放行", seq == [True] * 8, f"seq={seq}")
check("①b 第 9 条被拒（快速断开那一条）", ds._ip_conn_acquire("1.2.3.4") is False)
check("①c 计数到达硬上限", ds._ip_conn_count("1.2.3.4") == 8)
check("①d **另一个 IP 不受影响**（NAT 场景：不搞一刀切封禁）",
      ds._ip_conn_acquire("5.6.7.8") is True)
ds._ip_conn_release("1.2.3.4")
check("①e 释放一条后可再接入", ds._ip_conn_acquire("1.2.3.4") is True)
check("①f 软上限常量已定义（< 硬上限）",
      ds.MAX_IP_CONNS > 0 and ds.MAX_IP_CONNS < ds.MAX_IP_CONNS_HARD)
check("①g 请求头超时远小于旧值 120s", ds.HEADER_TIMEOUT < 120 and ds.HEADER_TIMEOUT > 0)


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


TMP = Path(tempfile.mkdtemp(prefix="ff_ipconn_"))
os.makedirs(os.path.join(TMP, "notice"), exist_ok=True)
(TMP / "notice" / "latest").write_bytes(b'{"v":1}')
(TMP / "firefly.apk").write_bytes(b"A" * 1024)

PORT = free_port()
check(f"找到空闲端口 {PORT}", PORT > 0)

env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BIND": "127.0.0.1", "FIREFLY_BW_KBPS": "0", "FIREFLY_BULK_IP_BYTES": "0",
            "FIREFLY_NOTICE_DIR": str(TMP / "notice")})
# 端到端用**生产默认**的上限（不复用①里为单测设的小值，否则控制请求可能撞硬上限）
env["FIREFLY_MAX_IP_CONNS"] = "16"
env["FIREFLY_MAX_IP_CONNS_HARD"] = "64"
env["FIREFLY_HEADER_TIMEOUT"] = "8"
env["FIREFLY_OVERFLOW_HEADER_TIMEOUT"] = "2.5"
log = open(TMP / "gw.log", "wb")
gw = subprocess.Popen([sys.executable, "download_server.py", str(PORT), str(TMP)],
                      cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)


def control(path="/notice/latest"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__


try:
    ready = False
    for _ in range(60):
        if control("/download/stats") == 200:
            ready = True
            break
        time.sleep(0.4)
    check("② 沙箱网关就绪", ready)
    if not ready:
        print((TMP / "gw.log").read_text("utf-8", "replace")[-1500:], flush=True)

    print("== ② 端到端：半开连接（slowloris 形态）期间控制请求不再 503 ==")
    check("②a 攻击前控制请求正常", control() == 200)
    socks = []
    for i in range(70):
        try:
            s = _sk.create_connection(("127.0.0.1", PORT), timeout=5)
            s.sendall(b"GET /notice/latest HTTP/1.1\r\nHost: x\r\n")   # 无结束空行 = 半开
            socks.append(s)
        except Exception:
            break
    print(f"     已保持 {len(socks)} 条慢头部连接，等待 3s")
    time.sleep(3)
    codes = [control() for _ in range(6)]
    check(f"②b 攻击中 6 次控制请求均非 503（实际 {codes}）",
          all(c in (200, 404) for c in codes), f"codes={codes}")
    check("②c 攻击中 /download/stats 仍可用",
          control("/download/stats") in (200, 404))

    for s in socks:
        try:
            s.close()
        except Exception:
            pass
    time.sleep(1.0)
    check("②d 连接关闭后立即恢复", control() in (200, 404))
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
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
