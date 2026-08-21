# -*- coding: utf-8 -*-
"""服务器版安全回归：模型锁仅托管模式（QuotaClient 运行时锁 mimo-v2.5，A8 重分层）
+ 托管模式运营者 Key 不切其它模型 + 每用户配额记账"""
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


print("=== A. 配置层模型自由（A8：锁只在托管模式运行时，配置层不再强制） ===")
# 配置文件写 pro：加载后保持用户配置（BYOK 用户可用任意官方模型名）
cfg.CONFIG_FILE.write_text(
    '{"analyzer_model":"deepseek-v4-pro","organizer_model":"deepseek-v4-pro",'
    '"polisher_model":"deepseek-v4-pro","retriever_model":"deepseek-v4-pro"}',
    encoding="utf-8")
cfg.config = cfg._load_config()
check("A1 加载后 analyzer 保持用户配置", cfg.config["analyzer_model"] == "deepseek-v4-pro")
check("A2 加载后 polisher 保持用户配置", cfg.config["polisher_model"] == "deepseek-v4-pro")
check("A3 加载后 retriever 保持用户配置", cfg.config["retriever_model"] == "deepseek-v4-pro")
check("A4 加载后 organizer 保持用户配置", cfg.config["organizer_model"] == "deepseek-v4-pro")

# save_config 不再强制（按 A8 分层：QuotaClient 运行时锁）
cfg.config["polisher_model"] = "gpt-4o"
cfg.save_config()
import json
saved = json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8"))
check("A5 save_config 落盘保留用户模型", saved["polisher_model"] == "gpt-4o")

print("=== B. routes.set_config：proxy 请求头才锁 mimo-v2.5，BYOK 不锁 ===")
import routes


class FakeH:
    def __init__(self, headers=None):
        self.headers = headers or {}

    def _json(self, data, status=200):
        self.data = data
        self.status = status


# BYOK（无 X-API-Mode）：不锁，接受用户模型
h = FakeH()
routes.set_config(h)
check("B1 BYOK set_config 接受用户模型", h.data["polisher_model"] == "gpt-4o")

# proxy 托管：请求体带模型 → 强制 mimo-v2.5（空体 = 透传全局默认，不影响运行链——QuotaClient 运行时锁兜底）
h2 = FakeH({"X-API-Mode": "proxy"})
with patch("routes._read_json", return_value={"polisher_model": "deepseek-v4-pro",
                                              "analyzer_model": "deepseek-v4-pro"}):
    routes.set_config(h2)
check("B2 proxy set_config 强制 mimo-v2.5", h2.data["polisher_model"] == "mimo-v2.5")

print("=== C. 托管模式 QuotaClient 强制 mimo-v2.5 ===")
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
    check("C3 传入 pro 被强制替换为 mimo-v2.5", captured.get("model") == "mimo-v2.5")
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
