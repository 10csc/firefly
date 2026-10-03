# -*- coding: utf-8 -*-
"""网关断点续传（Range）回归 —— 共创平台 M4b

为什么值得做：3Mbps 固定带宽下 81MB 安装包要下约 4 分钟，**任何一次中断（网络抖动、
限速拿不到额度而断流）都要从 0 重来**。支持 `Range` 后客户端可续传，直接省一半以上的冤枉流量。

钉死的事：
- 无 Range → 200 全量，且**声明 `Accept-Ranges: bytes`**（否则客户端不会尝试续传）；
- `bytes=a-b` / `bytes=a-` / `bytes=-N` → 206 + 正确 `Content-Range` + 正确字节数；
- **畸形/越界/多段一律退回 200 全量**（续传是优化，不能因坏请求头让下载失败）；
- 续传记 `server-range` channel（`/download/stats` 能看出续传占比）；
- 字节范围与文件内容**逐字节一致**（不是"长度对了就算过"）。

沙箱：临时 BASE 目录 + 临时端口，**不碰真实下载目录**；测试期间关闭限速以跑得快。
"""
import os
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


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


def free_port(lo=8791, hi=8810):
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


TMP = Path(tempfile.mkdtemp(prefix="ff_range_"))
BLOB = bytes(range(256)) * 40          # 10240 字节，内容可校验（每个位置唯一）
(TMP / "firefly.apk").write_bytes(BLOB)
PORT = free_port()
check(f"找到空闲端口 {PORT}", PORT > 0)

env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BW_KBPS": "0",              # 关限速：测的是 Range 语义，不是调度
            "FIREFLY_BULK_IP_BYTES": "0"})
log = open(TMP / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "download_server.py", str(PORT), str(TMP)],
                        cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def get(path, headers=None):
    """返回 (status, headers, body)。不抛 HTTPError。"""
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


try:
    ready = False
    for _ in range(60):
        try:
            s, _h, _b = get("/download/stats")
            if s == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.4)
    if not ready:
        print("网关未起来", flush=True)
        print((TMP / "server.log").read_text("utf-8", "replace")[-1200:], flush=True)
        sys.exit(2)

    print("== 1. 无 Range：全量 + 声明可续传 ==")
    s, h, b = get("/download/firefly.apk")
    check(f"① 200 全量（实际 {s}）", s == 200)
    check("①b 内容逐字节一致", b == BLOB)
    check("①c 声明 Accept-Ranges: bytes", h.get("Accept-Ranges") == "bytes")

    print("== 2. bytes=a-b ==")
    s, h, b = get("/download/firefly.apk", {"Range": "bytes=0-99"})
    check(f"② 206（实际 {s}）", s == 206)
    check("②b Content-Range 正确", h.get("Content-Range") == f"bytes 0-99/{len(BLOB)}")
    check("②c 长度 100", h.get("Content-Length") == "100")
    check("②d 内容 = 原文件 0..99", b == BLOB[0:100])

    s, h, b = get("/download/firefly.apk", {"Range": "bytes=1000-1099"})
    check("③ 中段 206 且内容正确", s == 206 and b == BLOB[1000:1100])

    print("== 3. bytes=a-（到结尾）与 bytes=-N（最后 N 字节）==")
    s, h, b = get("/download/firefly.apk", {"Range": "bytes=10000-"})
    check("④ 开区间 206", s == 206)
    check("④b Content-Range 到结尾", h.get("Content-Range") == f"bytes 10000-{len(BLOB)-1}/{len(BLOB)}")
    check("④c 内容正确", b == BLOB[10000:])

    s, h, b = get("/download/firefly.apk", {"Range": "bytes=-100"})
    check("⑤ 末尾 100 字节 206", s == 206)
    check("⑤b Content-Range 正确", h.get("Content-Range") == f"bytes {len(BLOB)-100}-{len(BLOB)-1}/{len(BLOB)}")
    check("⑤c 内容正确", b == BLOB[-100:])

    print("== 4. 越界/畸形/多段 → 一律退回 200 全量 ==")
    for desc, rng in [("起点越界", "bytes=99999-"),
                      ("非数字", "bytes=abc"),
                      ("空值", "bytes="),
                      ("倒序", "bytes=200-100")]:
        s, _h, b = get("/download/firefly.apk", {"Range": rng})
        check(f"⑥ {desc} → 200 全量", s == 200 and b == BLOB)

    s, h, b = get("/download/firefly.apk", {"Range": "bytes=0-9,20-29"})
    check("⑦ 多段只认第一段（206 且 10 字节）", s == 206 and b == BLOB[0:10])

    s, h, b = get("/download/firefly.apk", {"Range": "bytes=0-99999"})
    check("⑧ 终点越界 → 截到文件末尾", s == 206 and b == BLOB)

    s, h, b = get("/download/firefly.apk", {"Range": "bytes=10239-10239"})
    check("⑨ 最后一个字节可取", s == 206 and b == BLOB[-1:])

    print("== 5. 统计能看出续传占比 ==")
    s, _h, b = get("/download/stats")
    import json as _json
    stats = _json.loads(b.decode("utf-8"))
    chans = stats.get("by_channel") or {}
    check(f"⑩ by_channel 记录 server-range（{chans}）", int(chans.get("server-range", 0)) >= 5)
    check("⑩b 同时记录了全量 server", int(chans.get("server", 0)) >= 1)
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
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
