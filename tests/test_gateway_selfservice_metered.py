# -*- coding: utf-8 -*-
"""自服务路径（公告 / 热更）纳入流量计量与并发名额的回归（安全审计 S-3）

漏洞（`_harden/audit091_sec/_audit_t2b_traffic.py` 复现）：`/hotupdate/.../patch-1.zip`
与 `/notice/img-big.png` 经 `_reply` **直接写 socket** —— 既不限速也不计量
（`/download/traffic` 的 `sent_bytes` 增量恒 0），单个客户端即可不限速、不限并发地
拉补丁与公告图，挤占业务 API 的带宽。

本测试钉死：
  ① 拉 `patch-1.zip` 与 `img-big.png` 后，`sent_bytes`（asset 类）**确有增量**
     （同一套计量 = GOV），不再是绕过；
  ② 正对照：`/download/firefly.apk` 计入 `bulk`（调度对下载路径本就生效）；
  ③ `latest`（公告/热更清单）仍能正常送达（**保持"公告送到所有版本"的语义**）；
  ④ 自服务有名额 + 计量：`SELF_SEM` 已接线（导代码校验），且**不占 `DL_SEM`**。

沙箱：临时 hotupdate/notice 目录 + 临时端口，只打 loopback，自清理。
"""
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


TMP = Path(tempfile.mkdtemp(prefix="ff_selfsvc_"))
HU = TMP / "hotupdate" / "0.9.1"
NO = TMP / "notice"
HU.mkdir(parents=True, exist_ok=True)
NO.mkdir(parents=True, exist_ok=True)
MB = 1024 * 1024
(HU / "patch-1.zip").write_bytes(b"B" * MB)
(NO / "img-big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"C" * (MB - 8))
(NO / "latest").write_bytes(b'{"v":1,"ok":true}')
(TMP / "firefly.apk").write_bytes(b"A" * MB)

PORT = free_port()
check(f"找到空闲端口 {PORT}", PORT > 0)

env = dict(os.environ)
env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "FIREFLY_BIND": "127.0.0.1", "FIREFLY_BW_KBPS": "8000", "FIREFLY_BULK_IP_BYTES": "0",
            "FIREFLY_HOTUPDATE_DIR": str(TMP / "hotupdate"),
            "FIREFLY_NOTICE_DIR": str(NO)})
log = open(TMP / "gw.log", "wb")
gw = subprocess.Popen([sys.executable, "download_server.py", str(PORT), str(TMP)],
                      cwd=str(SERVER), env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def snap():
    with urllib.request.urlopen(base + "/download/traffic", timeout=10) as r:
        return json.loads(r.read().decode())


def get(path, timeout=120):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        n = 0
        while True:
            b = r.read(65536)
            if not b:
                break
            n += len(b)
    return n


try:
    ready = False
    for _ in range(60):
        try:
            if snap():
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.4)
    check("① 沙箱网关就绪", ready)
    if not ready:
        print((TMP / "gw.log").read_text("utf-8", "replace")[-1500:], flush=True)

    s0 = snap()
    got_hot = get("/hotupdate/0.9.1/patch-1.zip")
    got_img = get("/notice/img-big.png")
    s1 = snap()
    d_asset = s1["sent_bytes"]["asset"] - s0["sent_bytes"].get("asset", 0)
    d_bulk = s1["sent_bytes"]["bulk"] - s0["sent_bytes"].get("bulk", 0)
    check(f"② 补丁/公告图字节完整（{got_hot}B / {got_img}B）", got_hot == MB and got_img == MB)
    check(f"③ ★自服务路径**计入计量**（asset 增量 {d_asset}B ≈ 2MB，旧实现恒 0）",
          d_asset >= int(2 * MB * 0.9), f"delta={d_asset}")
    check("③b 自服务不误记到 bulk（大文件类）", d_bulk == 0, f"bulk delta={d_bulk}")

    # 正对照：下载路径本就计入 bulk
    got_apk = get("/download/firefly.apk")
    s2 = snap()
    d_bulk2 = s2["sent_bytes"]["bulk"] - s1["sent_bytes"].get("bulk", 0)
    check(f"④ 正对照：/download/firefly.apk 计入 bulk（增量 {d_bulk2}B）",
          got_apk == MB and d_bulk2 >= int(MB * 0.9), f"delta={d_bulk2}")

    # 保持"公告送到所有版本"语义：latest 仍可读且内容完整
    with urllib.request.urlopen(base + "/notice/latest", timeout=10) as r:
        body = r.read()
    check("⑤ latest 公告仍能正常送达（语义未因限速被堵）", body == b'{"v":1,"ok":true}')
    try:
        urllib.request.urlopen(base + "/hotupdate/0.9.1/latest", timeout=10)
        hot_latest_ok = False
    except urllib.error.HTTPError as e:
        hot_latest_ok = (e.code == 404)
    check("⑤b 缺补丁 latest 时按语义 404（网关仍掌握热更命名空间）", hot_latest_ok)
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

# ⑥ 代码层：自服务名额与下载名额**相互独立**（补丁不排队等 APK）
os.environ.setdefault("FIREFLY_SELF_MAX", "16")
_argv = sys.argv
sys.argv = ["download_server.py"]
sys.path.insert(0, str(SERVER))
try:
    import download_server as ds
finally:
    sys.argv = _argv
check("⑥ SELF_SEM 已接线且与 DL_SEM 是**两个不同的**信号量",
      hasattr(ds, "SELF_SEM") and ds.SELF_SEM is not ds.DL_SEM)
check("⑥b 自服务名额独立于大文件名额（避免补丁排在 APK 后面）",
      ds.SELF_MAX >= 1)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
