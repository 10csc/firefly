# -*- coding: utf-8 -*-
"""网关流量**字节可观测**回归 —— 共创平台 M4 第二层（docs/设计/角色卡共创平台/04）

为什么值得做：M4 第一层只做到「限速」，`/download/stats` **只记次数不记字节**，
于是"到底发了多少流量""续传省了多少""哪个通道在吃带宽"全靠猜。
本卡把**实际发出的字节**落库，并给 `/download/stats` 增加只增不改的字节口径。

钉死的事：
- `downloads` 表新增 `bytes` 列是**幂等迁移**：先用旧 schema 建库、灌老数据 → 跑新代码，
  自动补列、**老数据一行不丢**（老行 bytes=0），重复执行无副作用；
- `_record_download(name, channel)` 两参调用仍可用（`nbytes` 默认 0，向后兼容）；
- `_send_file` 记的是**实际发出的字节**，不是 `Content-Length` 声称值：
  全量下载记文件大小；`Range` 续传只记那一段（**不复用整文件大小**）；
- `/download/stats` 的 `bytes_total` / `bytes_by_channel` / `bytes_app_total` 自洽
  （bytes_total = 各通道之和；bytes_app_total ≤ bytes_total），老字段（total/by_file/by_channel）仍在。

沙箱：临时 BASE 目录 + 临时端口，**不碰真实下载目录**；测试期间关闭限速与 IP 配额以跑得快。
"""

import json
import os
import shutil
import socket as _sk
import sqlite3
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
sys.path.insert(0, str(SERVER))

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


def free_port(lo=8841, hi=8870):
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


TMP = Path(tempfile.mkdtemp(prefix="ff_bytes_"))
BLOB = bytes(range(256)) * 40          # 10240 字节，内容可校验（每个位置唯一）
(TMP / "firefly.apk").write_bytes(BLOB)
DB = TMP / "downloads.db"

# ── 先造一个**旧 schema** 的 downloads 表并灌入老数据（模拟线上运行中的老库）──
OLD_ROWS = [(1700000000, "firefly.apk", "legacy-old"),
            (1700000001, "firefly-setup.exe", "legacy-old")]
_conn = sqlite3.connect(str(DB))
_conn.execute("CREATE TABLE downloads ("
              "id INTEGER PRIMARY KEY AUTOINCREMENT,"
              "ts INTEGER NOT NULL,"
              "file TEXT NOT NULL,"
              "channel TEXT NOT NULL)")
_conn.executemany("INSERT INTO downloads(ts, file, channel) VALUES(?, ?, ?)", OLD_ROWS)
_conn.commit()
_conn.close()
check("沙箱老库已就绪（无 bytes 列 + 2 行老数据）",
      "bytes" not in [r[1] for r in sqlite3.connect(str(DB)).execute(
          "PRAGMA table_info(downloads)").fetchall()])


def db_query(sql, args=()):
    """只读/独立连接查库（服务进程另有连接，读写都是短事务）。"""
    conn = sqlite3.connect(str(DB), timeout=8)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def cols():
    return [r[1] for r in db_query("PRAGMA table_info(downloads)")]


def chan_bytes(channel):
    return int(db_query("SELECT COALESCE(SUM(bytes),0) FROM downloads WHERE channel=?",
                        (channel,))[0][0])


def wait_chan_bytes(channel, expect, timeout=10.0):
    """等服务端把「下载结束」的记账提交（记账发生在流结束之后，客户端可能先读到最后一个字节）。"""
    deadline = time.time() + timeout
    got = chan_bytes(channel)
    while time.time() < deadline and got != expect:
        time.sleep(0.2)
        got = chan_bytes(channel)
    return got


PORT = free_port()
check(f"找到空闲端口 {PORT}", PORT > 0)

env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BW_KBPS": "0",              # 关限速：测的是字节记账，不是调度
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

    print("== 1. 旧库幂等迁移（PRAGMA 检测 → ALTER ADD COLUMN，老数据不丢）==")
    check("① bytes 列已自动补上", "bytes" in cols())
    check("①b 列定义 NOT NULL DEFAULT 0",
          any(r[1] == "bytes" and r[3] == 1 and (r[4] or "").strip() == "0"
              for r in db_query("PRAGMA table_info(downloads)")))
    legacy = db_query("SELECT ts, file, channel, bytes FROM downloads "
                      "WHERE channel='legacy-old' ORDER BY id")
    check(f"①c 老数据 2 行完好（实际 {len(legacy)} 行）", len(legacy) == 2)
    check("①d 老行的 ts/file 原样保留",
          [(r[0], r[1]) for r in legacy] == [(r[0], r[1]) for r in OLD_ROWS])
    check("①e 老行 bytes 补 0（不是 NULL、不是瞎猜）", all(int(r[3]) == 0 for r in legacy))

    print("== 2. 全量下载：按实际发送字节记账 ==")
    s, h, b = get("/download/firefly.apk")
    check(f"② 200 全量（实际 {s}）", s == 200)
    check("②b 内容逐字节一致", b == BLOB)
    got = wait_chan_bytes("server", len(BLOB))
    check(f"②c channel=server 记账 = 实际发出 {len(b)} 字节（库内 {got}）", got == len(b))

    print("== 3. Range 续传：只记实际发出的那一段（不是声称的整文件）==")
    s, h, b = get("/download/firefly.apk", {"Range": "bytes=1000-1099"})
    check(f"③ 206（实际 {s}）", s == 206)
    check("③b 内容 = 原文件 1000..1099", b == BLOB[1000:1100])
    got = wait_chan_bytes("server-range", len(b))
    check(f"③c channel=server-range 记账 = 100（库内 {got}）", got == len(b))
    check("③d **不是**整文件大小 10240（若按 Content-Length/size 记就会错）",
          chan_bytes("server-range") != len(BLOB))

    print("== 4. /download/stats 的字节汇总自洽 ==")
    s, _h, raw = get("/download/stats")
    stats = json.loads(raw.decode("utf-8"))
    check("④ 老字段仍在（total / by_file / by_channel）",
          all(k in stats for k in ("total", "by_file", "by_channel")))
    check("④b bytes_total ≥ 全量+续传实际字节",
          int(stats.get("bytes_total", -1)) >= len(BLOB) + 100)
    bbc = stats.get("bytes_by_channel") or {}
    check(f"④c bytes_by_channel 分通道（{bbc}）",
          int(bbc.get("server", 0)) >= len(BLOB) and int(bbc.get("server-range", 0)) == 100)
    check("④d bytes_total = 各通道字节之和（两个口径互校）",
          int(stats.get("bytes_total", -1)) == sum(int(v) for v in bbc.values()))
    check("④e bytes_app_total 只算安装包且 ≤ 全量（与 total 同口径）",
          0 < int(stats.get("bytes_app_total", 0)) <= int(stats.get("bytes_total", 0)))
    check("④f 老库老行也计入（legacy-old 通道，字节 0）", "legacy-old" in bbc)

    print("== 5. 迁移幂等 + 老 API 兼容（两参调用、脏字节值）==")
    import download_lib as dl            # 同库直连：验证 _record_download 的老签名
    dl.init(str(TMP))
    before = len(cols())
    dl._download_stats()
    dl._download_stats()
    check("⑤ 重复访问统计不再改表结构（列数不变）", len(cols()) == before and "bytes" in cols())
    dl._record_download("version.json", "compat-2arg")            # 老调用方：不带字节
    row = db_query("SELECT bytes FROM downloads WHERE channel='compat-2arg' ORDER BY id DESC")
    check("⑤b 两参调用可用且 bytes=0（向后兼容）", bool(row) and int(row[0][0]) == 0)
    dl._record_download("version.json", "dirty", -5)              # 审查阶段：负值归 0
    dl._record_download("version.json", "dirty", "abc")           # 审查阶段：非数字归 0
    dirty = db_query("SELECT bytes FROM downloads WHERE channel='dirty'")
    check("⑤c 负数/非数字字节一律记 0（脏值不进库）",
          len(dirty) == 2 and all(int(r[0]) == 0 for r in dirty))

    print("== 6. 全新库：nbytes 参数直通统计 ==")
    TMP2 = Path(tempfile.mkdtemp(prefix="ff_bytes_new_"))
    dl.init(str(TMP2))
    dl._record_download("firefly.apk", "server", 1234)
    dl._record_download("firefly-setup.exe", "server", 66)
    st2 = dl._download_stats()
    check("⑥ 新库建表即含 bytes 列",
          "bytes" in [r[1] for r in sqlite3.connect(str(TMP2 / "downloads.db")).execute(
              "PRAGMA table_info(downloads)").fetchall()])
    check("⑥b bytes_total = 1300", int(st2.get("bytes_total", -1)) == 1300)
    check("⑥c bytes_by_channel = {server: 1300}", st2.get("bytes_by_channel") == {"server": 1300})
    check("⑥d bytes_app_total = 1300（两个都是安装包）", int(st2.get("bytes_app_total", -1)) == 1300)
    check("⑥e total 计数 = 2", int(st2.get("total", -1)) == 2)
    shutil.rmtree(TMP2, ignore_errors=True)
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
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
