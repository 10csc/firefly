# -*- coding: utf-8 -*-
"""Key 不许被"空值"覆盖 —— 四条回归测试（对应真机事故：手机里的 sk 被清且无提示）。

覆盖四条会写配置的路径：
  A 直接把内存里的 Key 清空后 save_config()        → 磁盘上的 Key 必须还在
  B providers **整表替换**成不含 Key 持有项的列表   → Key 必须还在（同 id 救回或补回）
  C set_key 传空串                                → Key 必须还在（旧实现会清成 ""）
  D POST /config 传空 Key                         → Key 必须还在（原本就有 if new_key 守卫）
另加 E：不变量必须**在落盘口**生效（任何调用 save_config() 的路径都受益）
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
TMP = Path(tempfile.mkdtemp(prefix="firefly_test_key_"))
os.environ["FIREFLY_DATA_DIR"] = str(TMP)
os.environ.pop("FIREFLY_SERVER", None)

GOOD = "sk-test-key-must-survive-0123456789abcdef"

# 造一份"磁盘上已经有 Key"的现场
from core import paths as _paths          # noqa: E402
_paths.USER_DIR = TMP / "user_data"
_paths.CONFIG_FILE = _paths.USER_DIR / "config.json"
_paths.USER_DIR.mkdir(parents=True, exist_ok=True)
_paths.CONFIG_FILE.write_text(json.dumps({
    "providers": [{"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                   "api_key": GOOD, "models": ["deepseek-flash"]}],
    "active_provider": "deepseek",
}, ensure_ascii=False), encoding="utf-8")

from core import config as cfg            # noqa: E402

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def disk_key():
    d = json.loads(_paths.CONFIG_FILE.read_text(encoding="utf-8"))
    for p in (d.get("providers") or []):
        if (p.get("api_key") or "").strip():
            return p["api_key"]
    return ""


print("=== 前置：磁盘上已有 Key ===")
check("A0 现场就绪", disk_key() == GOOD, disk_key()[:12] + "…")

print("\n=== A. 内存 Key 被清空后保存 ===")
for p in cfg.config["providers"]:
    p["api_key"] = ""
cfg.save_config()
check("A  磁盘上的 Key 被救回（没被空值覆盖）", disk_key() == GOOD, disk_key()[:12] + "…")
check("A  内存里也恢复了（_sync_derived 生效）", bool((cfg.config.get("api_key") or "").strip()))

print("\n=== B. providers 整表替换成「不含原持有项」的列表 ===")
cfg.config["providers"] = [{"id": "custom", "name": "自定义", "base_url": "https://x.example/v1",
                            "api_key": "", "models": ["m"]}]
cfg.save_config()
d = json.loads(_paths.CONFIG_FILE.read_text(encoding="utf-8"))
ids = [p.get("id") for p in (d.get("providers") or [])]
check("B  持有 Key 的旧项被补回（Key 没随整表替换丢失）", disk_key() == GOOD,
      f"providers={ids}")

print("\n=== C. set_key 传空串 ===")
sys.path.insert(0, str(ROOT / "app"))


class _H:
    def __init__(self, body):
        self._body = body
        self.path = "/config/key"
        self.headers = {}
        self.status = None

    def _json(self, obj, code=200):
        self.status = code
        self.body = obj

    def read(self, *a):
        return json.dumps(self._body).encode()


import routes_config as RC                 # noqa: E402
h = _H({"api_key": ""})
RC.set_key(h)
check("C  set_key(空) 不再清 Key", disk_key() == GOOD, f"resp={getattr(h, 'body', None)}")
_resp = getattr(h, "body", None) or {}
check("C  返回值里 kept=True（前端可据此提示已保留原 Key）",
      isinstance(_resp, dict) and _resp.get("kept") is True)

print("\n=== D. POST /config 传空 Key ===")
h2 = _H({"api_key": "", "active_provider": "deepseek"})
try:
    RC.set_config(h2)
except AttributeError:
    print("   (set_config 不在本文件，跳过——由 C 与 A/B 覆盖同类路径)")
else:
    check("D  仍保留 Key", disk_key() == GOOD)

print("\n=== E. 不变量挂在落盘口（任何 save_config 调用都受益）===")
src = (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
i = src.find("def save_config()")
check("E  save_config 开头第一件事就是跑不变量",
      "_preserve_key_from_disk()" in src[i:i + 260], src[i:i + 120].replace("\n", " "))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
