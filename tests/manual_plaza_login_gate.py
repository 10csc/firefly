# -*- coding: utf-8 -*-
"""真实守护验证：**未登录时广场 API 必须 401**（共创平台 M1 的产品决策「完全登录可见」）。

为什么单独一个脚本：
- 广场端点必须落在 `server/app.py` 的登录闸门之后（不在公开白名单里），
  这只有在**服务器入口**（`server/server_app.py`，自带 `FIREFLY_SERVER=1`）上才成立；
  本地入口没有账号体系，测不到这条。
- 手工/按需运行，不在自动化套件内（要起端口）。用法：`python tests/manual_plaza_login_gate.py`
- 自建沙箱数据根，**绝不碰真实 user_data**，也不连任何外网。

退出码：0 全过；1 有断言失败；2 服务没起来；3 端口被占。
"""
import json
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
PORT = 8799                      # 避开本机 8765（用户的真实实例）；server 入口读 FIREFLY_PORT
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_plaza_gate_"))
PLAZA_DIR = SANDBOX / "plaza"
DATA_DIR = SANDBOX / "user_data"
for _d in (PLAZA_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)
print("沙箱:", SANDBOX, flush=True)

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}", flush=True)


# 种一张**已发布**的卡：证明 401 是"闸门"造成的，而不是"广场根本没内容"
os.environ["FIREFLY_ANDROID"] = "1"
os.environ["FIREFLY_DATA_DIR"] = str(SANDBOX)
os.environ["FIREFLY_PLAZA_DIR"] = str(PLAZA_DIR)
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = DATA_DIR
cfg.CONFIG_FILE = DATA_DIR / "config.json"

from plaza import store as st      # noqa: E402

st.publish_card(
    {"id": "gate_card", "name": "闸门卡", "char_name": "小闸", "user_name": "你",
     "presentation": "sticker", "desc": "用于验证登录闸门", "category": "陪伴"},
    {"preset.json": json.dumps({"id": "gate_card", "name": "闸门卡", "char_name": "小闸",
                                "user_name": "你", "presentation": "sticker",
                                "desc": "d", "schema": 1}, ensure_ascii=False).encode(),
     "core.md": b"# core"},
    uid_hash="g" * 32, display="官方", official=True)
print(f"已种卡：{st.stats()['cards']} 张（已发布）", flush=True)

_p = _sk.socket()
_p.settimeout(0.6)
try:
    _p.connect(("127.0.0.1", PORT))
    print(f"!! 端口 {PORT} 被占用 —— 放弃", flush=True)
    sys.exit(3)
except OSError:
    pass
finally:
    _p.close()

env = dict(os.environ)
env.update({"FIREFLY_PORT": str(PORT), "FIREFLY_ANDROID": "1",
            "FIREFLY_DATA_DIR": str(SANDBOX), "FIREFLY_PLAZA_DIR": str(PLAZA_DIR),
            "FIREFLY_NO_BROWSER": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1"})
_log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=env, stdout=_log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def _get(path):
    """返回 (status, body_text)；HTTP 错误也当结果返回，不抛。"""
    try:
        with urllib.request.urlopen(base + path, timeout=3) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, str(e)


try:
    ready = False
    for _ in range(80):
        s, _b = _get("/health")
        if s == 200:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        print("服务未起来", flush=True)
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)
    print("服务器入口已就绪", flush=True)

    s, body = _get("/plaza/api/list")
    check(f"① 未登录访问广场列表 → 401（实际 {s}）", s == 401)
    check("①b 响应是「未登录」而不是空列表", "未登录" in body or "登录" in body)

    s, _ = _get("/plaza/api/card?id=gate_card")
    check(f"② 未登录访问卡片详情 → 401（实际 {s}）", s == 401)

    s, _ = _get("/plaza/api/download?id=gate_card")
    check(f"③ 未登录下载卡 → 401（实际 {s}）", s == 401)

    s, _ = _get("/plaza/api/asset?id=gate_card&slot=cover")
    check(f"④ 未登录取封面 → 401（实际 {s}）", s == 401)

    s, b = _get("/plaza/api/installed")
    check(f"⑤ 未登录查已安装 → 401（实际 {s}）", s == 401)

    s, _ = _get("/health")
    check(f"⑥ 公开端点在登录闸门之前仍可用（/health → {s}）", s == 200)
finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _log.close()

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
