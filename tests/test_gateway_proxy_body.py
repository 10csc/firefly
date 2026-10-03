# -*- coding: utf-8 -*-
"""网关反代正文路径回归 —— 2026-10-01 抓到的真 bug 的守卫

**bug 现象**：网关对"200 + 已知 Content-Length ≤ 4MB"的响应会**先缓冲读走正文**再看值不值得 gzip。
原实现只在"压缩成功"时才使用缓冲内容；若**不值得压缩**（客户端没发 `Accept-Encoding: gzip`、
或体积 <1KB、或类型不在白名单），代码会落到**流式转发**分支——可流已被读干 ⇒
发出 `Content-Length: N` 却**写 0 字节**。客户端拿到 `IncompleteRead(0 bytes read, N more expected)`。
浏览器都发 gzip 所以线上一直没暴露；**curl（不带 --compressed）/ urllib / Go** 等客户端会拿到**空正文**。

本测试固定四条路径，任何一条回退都会当场红：
  ① 小正文 + 不支持 gzip → **原文照发**（原 bug 现场）
  ② 大正文 + 不支持 gzip → 原文照发（同样走缓冲分支）
  ③ 大正文 + 支持 gzip → `Content-Encoding: gzip` 且解压后与原文一致
  ④ 无 Content-Length（HTTP/1.0 close 语义）→ 走流式分支，正文完整
  ⑤ 上游**截断**（声称 N 只给一半）→ 网关必须给出**自洽**响应（502），
     **不许**出现"200 + 不足的正文"这种自相矛盾
"""
import gzip
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
TMP = Path(tempfile.mkdtemp(prefix="ff_proxybody_"))
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


SMALL = json.dumps({"ok": True, "n": 1}).encode()                     # 33B < 1KB
MID = json.dumps({"items": ["x" * 40] * 200}).encode()                # ~8KB ≥ 1KB
BIG_BYTES = b"PNG" + os.urandom(300 * 1024)                           # 类型非白名单
# ~300KB 的可压缩 JSON：**大于令牌桶容量**（api 类 ≈ 速率×0.25s），专治"一次性 take 整包"
BIGJSON = json.dumps({"rows": ["萤火虫" * 20] * 1200}).encode()
NOLEN = b"stream without content-length" * 100
TRUNC = b"A" * 100


class Stub(BaseHTTPRequestHandler):
    """忠实桩：**HTTP/1.0**（真实主服务就是 close 语义，而 1.1 keep-alive 会让网关流式循环等不到 EOF）。"""
    def log_message(self, *a):
        pass

    def _raw(self, body, ctype="application/json", declared=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        n = len(body) if declared is None else declared
        if n is not None and n >= 0:
            self.send_header("Content-Length", str(n))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/small":
            return self._raw(SMALL)
        if p == "/mid":
            return self._raw(MID)
        if p == "/big":
            return self._raw(BIG_BYTES, "image/png")
        if p == "/bigjson":
            return self._raw(BIGJSON)
        if p == "/hotupdate/status":
            # 模拟主服务的热更状态端点：网关**必须把它转发过来**，而不是当成补丁路径 404
            return self._raw(b'{"ok": true, "state": "idle"}')
        if p == "/nolen":
            return self._raw(NOLEN, "text/plain", declared=None)
        if p == "/truncated":
            # 声称 100 字节，只发 40 就收工（模拟上游截断）
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(TRUNC)))
            self.end_headers()
            self.wfile.write(TRUNC[:40])
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


STUB_PORT = free_port(9061, 9100)
# ⚠ 上限取 9101：本机 Windows 保留段含 **9102-9201**（`netsh int ipv4 show excludedportrange protocol=tcp`），
# 落进去的 bind 会直接失败 —— 端口段要避开保留区（见 docs/协作记忆.md #18）。
GW_PORT = free_port(9062, 9101)
check(f"端口分配（桩 {STUB_PORT} / 网关 {GW_PORT}）", STUB_PORT and GW_PORT)

stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
stub.daemon_threads = True
import threading                                                     # noqa: E402
threading.Thread(target=stub.serve_forever, daemon=True).start()

env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BW_KBPS": "0",                 # 本测试测正文路径，不测限速
            "FIREFLY_BULK_IP_BYTES": "0",
            "FIREFLY_BIND": "127.0.0.1",
            "FIREFLY_PROXY_TARGET": f"127.0.0.1:{STUB_PORT}"})
log = open(TMP / "gw.log", "wb")
gw = subprocess.Popen([sys.executable, "download_server.py", str(GW_PORT), str(TMP)],
                      cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{GW_PORT}"


def get(path, headers=None):
    r = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:
        return -1, {}, str(e).encode()


try:
    ready = False
    for _ in range(60):
        s, _h, b = get("/small")
        if s == 200 and b == SMALL:
            ready = True
            break
        time.sleep(0.4)
    check("① 网关就绪：小正文 + 不支持 gzip → **原文照发**（原 bug 现场）",
          ready, f"status={s} body={b[:60]!r}")

    print("== 不走压缩的三种情况，正文都必须完整 ==")
    s, h, b = get("/small", {"Accept-Encoding": "identity"})
    check(f"② identity + 33B（<1KB 不压）= {len(b)}B 原文一致",
          s == 200 and b == SMALL, f"status={s} got={len(b)} want={len(SMALL)}")
    s, h, b = get("/mid", {"Accept-Encoding": "identity"})
    check(f"③ identity + 8KB（≥1KB 但客户端不支持 gzip）正文一致",
          s == 200 and b == MID, f"status={s} got={len(b)} want={len(MID)}")
    s, h, b = get("/big", {"Accept-Encoding": "gzip"})
    check(f"④ gzip + 300KB 二进制（类型不在白名单）正文一致且未被压",
          s == 200 and b == BIG_BYTES and not h.get("Content-Encoding"),
          f"status={s} got={len(b)} want={len(BIG_BYTES)} enc={h.get('Content-Encoding')}")
    s, h, b = get("/nolen")
    check(f"⑤ 无 Content-Length → 走流式分支，正文一致（{len(b)}B）",
          s == 200 and b == NOLEN, f"status={s} got={len(b)} want={len(NOLEN)}")

    print("== 压缩路径 ==")
    s, h, b = get("/mid", {"Accept-Encoding": "gzip"})
    ok_gunzip = False
    if h.get("Content-Encoding") == "gzip":
        try:
            ok_gunzip = gzip.decompress(b) == MID
        except Exception:
            ok_gunzip = False
    check(f"⑥ gzip + 8KB JSON → Content-Encoding: gzip 且解压后一致"
          f"（{len(b)}B < {len(MID)}B）",
          s == 200 and ok_gunzip and 0 < len(b) < len(MID),
          f"status={s} enc={h.get('Content-Encoding')} got={len(b)}")

    print("== ★/hotupdate/* 的名字空间归属（网关吞掉主服务路由的回归）==")
    # 线上真浏览器冒烟抓到：前端调 `/hotupdate/status`（注册在 app 的 api/router.py 里）却拿到 404，
    # 因为网关把整个 `/hotupdate/*` 当成自己的补丁分发命名空间。修法：**只有补丁三种形状**归网关。
    s, h, b = get("/hotupdate/status", {"Accept-Encoding": "identity"})
    check(f"⑨ 非补丁形状的 /hotupdate/status 被**转发给主服务**（status={s} body={b[:40]!r}）",
          s == 200 and b'"state": "idle"' in b)
    s, h, b = get("/hotupdate/9.9.9/latest")
    check(f"⑨b 补丁形状仍归网关自己管（不存在的补丁 → {s}）", s == 404)
    s, h, b = get("/hotupdate/9.9.9/patch-1.json")
    check(f"⑨c 补丁清单形状同理（{s}）", s == 404)

    print("== 上游截断必须自洽（不许 200 + 不足正文）==")
    s, h, b = get("/truncated")
    clen = h.get("Content-Length")
    contradict = (s == 200 and clen is not None and int(clen or 0) != len(b))
    check(f"⑦ 上游声称 {len(TRUNC)}B 只给 40B → 网关不得发出自相矛盾的 200"
          f"（实际 status={s}，正文 {len(b)}B）", not contradict)
    check("⑦b 截断被如实报成 502（客户端可重试，而不是拿到半个 JSON）",
          s == 502, f"status={s}")

    print("== ★开限速时的缓冲写（线上登录页白屏的真凶）==")
    # 上面所有用例都把 FIREFLY_BW_KBPS 设成 0（关限速）⇒ **从没走过"被令牌桶限速的缓冲写"**。
    # 线上 FIREFLY_BW_KBPS 默认 2800（≈350KB/s）：api 类桶容量只有 ~55KB，而 bundle.js
    # gzip 后仍有 ~120KB ⇒ 一次性 `take(整包)` 永远拿不到额度 ⇒ 响应头声称 N 字节、实际 0 字节
    # ⇒ 浏览器 `ERR_CONTENT_LENGTH_MISMATCH`（登录页白屏，而直连 8765 一切正常）。
    GW2 = free_port(8801, 8900)
    env2 = dict(env)
    env2["FIREFLY_BW_KBPS"] = "2800"
    log2 = open(TMP / "gw2.log", "wb")
    gw2 = subprocess.Popen([sys.executable, "download_server.py", str(GW2), str(TMP)],
                           cwd=str(SERVER), env=env2, stdout=log2, stderr=subprocess.STDOUT)
    base2 = f"http://127.0.0.1:{GW2}"

    def get2(path, headers=None):
        r = urllib.request.Request(base2 + path, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()
        except Exception as e:
            return -1, {}, str(e).encode()

    try:
        ok2 = False
        for _ in range(60):
            s2, _h2, b2 = get2("/small")
            if s2 == 200 and b2 == SMALL:
                ok2 = True
                break
            time.sleep(0.4)
        check(f"⑧ 开限速的网关就绪（端口 {GW2}）", ok2)

        s2, h2, b2 = get2("/bigjson", {"Accept-Encoding": "gzip"})
        cl2 = int(h2.get("Content-Length") or -1)
        dec = b2
        if h2.get("Content-Encoding") == "gzip":
            try:
                dec = gzip.decompress(b2)
            except Exception:
                dec = b""
        check(f"⑧b gzip + {len(BIGJSON) // 1024}KB JSON（超过桶容量）：**长度自洽**且解压后一致"
              f"（CL={cl2} 实收={len(b2)} 解压={len(dec)}）",
              s2 == 200 and cl2 == len(b2) and dec == BIGJSON)

        s2, h2, b2 = get2("/bigjson", {"Accept-Encoding": "identity"})
        cl2 = int(h2.get("Content-Length") or -1)
        check(f"⑧c identity + 同一个大响应：长度自洽且正文一致（CL={cl2} 实收={len(b2)}）",
              s2 == 200 and cl2 == len(b2) and b2 == BIGJSON)
    finally:
        try:
            gw2.terminate()
            gw2.wait(timeout=8)
        except Exception:
            try:
                gw2.kill()
            except Exception:
                pass
        log2.close()
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
