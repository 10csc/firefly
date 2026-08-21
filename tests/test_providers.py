# -*- coding: utf-8 -*-
"""A8 多供应商测试：旧结构迁移（幂等/旧字段清除）、供应商校验、caps 能力探测回写、/models 转发"""
import json
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

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_providers_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

print("=== A. 旧结构迁移（api_key/api_base → providers） ===")
cfg.CONFIG_FILE.write_text(
    json.dumps({"api_key": "sk-old-1234567890", "api_base": cfg.API_BASE,
                "analyzer_model": "deepseek-v4-pro"}),
    encoding="utf-8")
cfg.config = cfg._load_config()
check("A1 迁移出 providers 列表", len(cfg.config["providers"]) >= 1)
p = next((p for p in cfg.config["providers"] if p["base_url"] == cfg.API_BASE), None)
check("A2 deepseek provider 携带旧 Key", p is not None and p["api_key"] == "sk-old-1234567890")
check("A3 激活供应商为 deepseek", cfg.config["active_provider"] == "deepseek")
check("A4 派生 api_key 同步", cfg.config["api_key"] == "sk-old-1234567890")
check("A5 派生 api_base 同步", cfg.config["api_base"] == cfg.API_BASE)

# save 后旧字段不再落盘（删旧字段）
cfg.save_config()
saved = json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8"))
check("A6 保存后无顶层 api_key", "api_key" not in saved)
check("A7 保存后无顶层 api_base", "api_base" not in saved)
check("A8 providers 落盘含 Key", saved["providers"][0]["api_key"] == "sk-old-1234567890")

# 幂等：再加载一次（新结构）不应重复迁移/丢 Key
cfg.config = cfg._load_config()
p2 = next((p for p in cfg.config["providers"] if p["id"] == "deepseek"), None)
check("A9 二次加载幂等", p2 is not None and p2["api_key"] == "sk-old-1234567890")

print("=== B. 供应商校验（normalize_providers） ===")
ok = cfg.normalize_providers([
    {"id": "x", "base_url": "https://x.example/v1", "api_key": "k"},
    {"id": "bad", "base_url": "ftp://x", "api_key": "k"},
    {"id": "bad2", "base_url": "javascript:alert(1)", "api_key": "k"},
    {"id": "", "base_url": "https://x.example/v1"},
    {"id": "y", "base_url": "http://127.0.0.1:8765/v1"},
])
check("B1 非法协议被剔除", all(p["id"] in ("x", "y") for p in ok))
check("B2 空 id 被剔除", all(p["id"] for p in ok))
check("B3 http 也可接受（本地自建端点）", any(p["id"] == "y" for p in ok))
check("B4 去重", len(ok) == 2)

print("=== C. caps 能力探测（unknown param 剥离重试 + 回写） ===")
from modules.api_client import _CompatClient

captured = {}
_caps_write = []


def fake_post(url, **kw):
    captured["url"] = url
    captured.setdefault("payloads", []).append(kw.get("json"))
    n = len(captured["payloads"])
    if n == 1:   # 第一次：拒绝私有参数
        class R1:
            status_code = 400
            text = '{"error":{"message":"Error code: 400 - {\'error\':{\'message\':\'Unsupported parameter: \\\'thinking\\\' is not supported with this model\'}}"}}'
        return R1()
    class R2:
        status_code = 200
        def json(self):
            return {"model": "gpt-x", "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    return R2()


c = _CompatClient("k", "https://x.example/v1", caps={"thinking": True},
                  on_caps_change=lambda d: _caps_write.append(d))
with patch("modules.api_client.requests.post", side_effect=fake_post):
    resp = c.chat.completions.create(model="gpt-x", messages=[{"role": "user", "content": "hi"}],
                                     extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
check("C1 剥离后重试成功", resp.choices[0].message.content == "ok")
check("C2 第一次请求带私有参数", "thinking" in captured["payloads"][0])
check("C3 第二次请求已剥离", "thinking" not in captured["payloads"][1])
check("C4 caps.thinking 回写 False", c._caps.get("thinking") is False)
check("C5 回调收到变更", _caps_write == [{"thinking": False}])

print("=== D. caps 关闭时不发私有参数 ===")
captured2 = {}


def fake_post2(url, **kw):
    captured2["payload"] = kw.get("json")
    class R:
        status_code = 200
        def json(self):
            return {"model": "gpt-x", "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    return R()


c2 = _CompatClient("k", "https://x.example/v1", caps={"thinking": False})
with patch("modules.api_client.requests.post", side_effect=fake_post2):
    c2.chat.completions.create(model="gpt-x", messages=[{"role": "user", "content": "hi"}],
                               extra_body={"thinking": {"type": "enabled"}})
check("D1 非 thinking 供应商不发 extra_body", "thinking" not in captured2["payload"])

print("=== E. /models 转发（本地版带 Key） ===")
import routes


class FakeH:
    def __init__(self, path):
        self.path = path

    def _json(self, data, status=200):
        self.data = data
        self.status = status


cfg.config["providers"] = [{"id": "deepseek", "name": "DeepSeek",
                            "base_url": cfg.API_BASE, "api_key": "sk-t", "models": [], "caps": {}}]
cfg.config["active_provider"] = "deepseek"


def fake_urlopen(req, timeout=None):
    check("E1 /models 请求带 Bearer", req.headers.get("Authorization", "").startswith("Bearer sk-t"))
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}]}).encode()
    return R()


h = FakeH("/models?provider=deepseek")
os.environ.pop("FIREFLY_SERVER", None)
with patch("urllib.request.urlopen", side_effect=fake_urlopen):
    routes.get_models(h)
check("E2 返回模型清单", h.data.get("ok") is True and len(h.data["models"]) == 2)

h404 = FakeH("/models?provider=nope")
routes.get_models(h404)
check("E3 未知供应商 404", h404.status == 404)

print("=== F. set_config：providers 空 Key 保留（后端不回传全量 Key） ===")
cfg.config["providers"] = [_deepseek := {"id": "deepseek", "name": "DeepSeek",
                                         "base_url": cfg.API_BASE, "api_key": "sk-keep", "models": [], "caps": {}}]
cfg.config["active_provider"] = "deepseek"
cfg.config["api_key"] = "sk-keep"


class H2:
    headers = {}
    def _json(self, data, status=200):
        self.data = data
        self.status = status


with patch("routes._read_json", return_value={"providers": [
        {"id": "deepseek", "name": "DeepSeek", "base_url": cfg.API_BASE, "api_key": ""}]}) as m:
    routes.set_config(H2())
p = cfg.provider_by_id("deepseek")
check("F1 空 Key 保留原 Key", p is not None and p["api_key"] == "sk-keep")
check("F2 保存后派生 api_key 不变", cfg.config["api_key"] == "sk-keep")

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
