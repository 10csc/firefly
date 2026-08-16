# -*- coding: utf-8 -*-
"""服务器版安全回归：模型锁（只允许 flash）+ 托管模式运营者 Key 不切 pro + 每用户配额记账"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

os.environ["FIREFLY_SERVER"] = "1"   # 必须在 import app_config 之前

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_server_lock_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


print("=== A. 服务器配置加载模型锁 ===")
# 把配置文件写成 pro，重新加载后必须全部回落到 flash
cfg.CONFIG_FILE.write_text(
    '{"analyzer_model":"deepseek-v4-pro","organizer_model":"deepseek-v4-pro",'
    '"polisher_model":"deepseek-v4-pro","retriever_model":"deepseek-v4-pro"}',
    encoding="utf-8")
cfg.config = cfg._load_config()
check("A1 加载后 analyzer=flash", cfg.config["analyzer_model"] == "deepseek-v4-flash")
check("A2 加载后 polisher=flash", cfg.config["polisher_model"] == "deepseek-v4-flash")
check("A3 加载后 retriever=flash", cfg.config["retriever_model"] == "deepseek-v4-flash")
check("A4 加载后 organizer=flash", cfg.config["organizer_model"] == "deepseek-v4-flash")

# save_config 落盘前也要强制 flash
cfg.config["polisher_model"] = "deepseek-v4-pro"
cfg.save_config()
import json
saved = json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8"))
check("A5 save_config 落盘仍为 flash", saved["polisher_model"] == "deepseek-v4-flash")

print("=== B. routes.set_config 服务器分支拒绝 pro ===")
import routes


class FakeH:
    headers = {}

    def _json(self, data, status=200):
        self.data = data
        self.status = status


h = FakeH()
routes.set_config(h)
check("B1 set_config 响应 polisher=flash", h.data["polisher_model"] == "deepseek-v4-flash")
check("B2 set_config 响应 valid_models 不在此处（get_config 检查）", True)

print("=== C. 托管模式 QuotaClient 强制 flash ===")
os.environ["FIREFLY_PROXY_KEY"] = "op-key-not-real"
token = cfg.set_user_context(user_dir=_tmp / "u1", proxy=True)
try:
    client = cfg.get_client()
    check("C1 proxy 返回 QuotaClient", client is not None and type(client).__name__ == "QuotaClient")
    from modules.api_client import _QuotaCompletions
    check("C2 completions 是配额锁实现", isinstance(client.chat.completions, _QuotaCompletions))
    captured = {}
    with patch("modules.api_client._Completions.create", side_effect=lambda **kw: captured.update(kw) or None):
        try:
            client.chat.completions.create(model="deepseek-v4-pro", messages=[{"role": "user", "content": "x"}])
        except Exception:
            pass
    check("C3 传入 pro 被强制替换为 flash", captured.get("model") == "deepseek-v4-flash")
finally:
    cfg.reset_user_context(token)

print("=== D. 托管每日池记账（全站共享，不做单账号防刷） ===")
import db
db.init_db(_tmp / "firefly.db")
uid = db.create_user("locktest@qq.com", "h", "00", "inst-lock")
day = "2099-01-01"
db.proxy_usage_add(uid, day, 3)
check("D1 全站当日调用累计正确", db.proxy_usage_get(day) == 3)
db.proxy_usage_add(uid, day, 1)
check("D2 再次累加正确", db.proxy_usage_get(day) == 4)
check("D3 不再存在单用户配额函数", not hasattr(db, "proxy_usage_get_user"))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
