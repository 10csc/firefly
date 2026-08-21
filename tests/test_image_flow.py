# -*- coding: utf-8 -*-
"""A9 图片消息测试：conversation image 校验/引用/hydrate、upload-image 落盘+desc（mock vision）、
polisher 首轮 content blocks、vision desc 失败降级、get_image 字节服务"""
import base64
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

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_image_"))
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


from modules import conversation_store as cs

print("=== A. conversation image 消息（校验/引用/hydrate） ===")
try:
    cs.append_message("user", {"type": "image"}, mode="story")
    check("A1 缺 img_id 拒绝", False)
except cs.InputRejected:
    check("A1 缺 img_id 拒绝", True)
seq, _ = cs.append_message("user", {"type": "image", "img_id": "img_abc", "desc": "一张蛋糕照片"}, mode="story")
cs.append_message("firefly", {"type": "text", "content": "好好吃的样子！"}, mode="story")
check("A2 image 消息落盘", cs.load_recent(10, mode="story")[0]["type"] == "image")
check("A3 只存 img_id+desc", "img_id" in cs.load_recent(10, mode="story")[0]
      and "desc" in cs.load_recent(10, mode="story")[0])
# 大字节不进 jsonl：断言文件不含 base64
raw = (cfg.mode_data_dir("story") / "conversation.jsonl").read_text(encoding="utf-8")
check("A4 文件无图片字节", "data:image" not in raw and "base64" not in raw)
q = cs.sanitize_quote({"who": "firefly", "type": "image", "img_id": "img_x", "desc": "落日"})
check("A5 引用快照净化（image 类型）", q is not None and q["type"] == "image")
check("A6 引用 LLM 文本", "[图片：落日]" in cs.format_quote_for_llm(q))

print("=== B. hydrate：图片用户消息进上下文 ===")
from modules.context_manager import ContextManager
ctx = ContextManager()
n = cs.hydrate_context(ctx, max_turns=5, mode="story")
check("B1 回灌 ≥1 轮", n >= 1)
hist = ctx.get_recent(5)
check("B2 上下文含 [图片：…]", any("[图片：" in str(m.get("content", "")) for m in hist))

print("=== C. vision describe（mock：成功 / 失败降级 / caps 关闭不发） ===")
from modules.vision import describe_image, to_data_url


class R:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})})()]
        self.usage = type("U", (), {})
        self.model = "vision-x"


class _CC:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kw):
        return self._owner.create(**kw)


class ClientOK:
    _timeout = 30.0
    _caps = {"vision": True, "thinking": True}
    sent = {}

    def __init__(self):
        self.chat = type("Chat", (), {"completions": _CC(self)})()

    def create(self, **kw):
        ClientOK.sent = kw
        return R("一张生日蛋糕照片，桌上还有蜡烛。")


with patch("modules.vision.record_usage", return_value=None):
    c = ClientOK()
    url = to_data_url(b"fake-image", ".png")
    check("C1 data url 生成", url and url.startswith("data:image/png;base64,"))
    desc = describe_image(c, "vision-model", url)
    check("C2 desc 成功", "生日蛋糕" in desc)
    check("C3 content blocks 结构", isinstance(ClientOK.sent.get("messages")[0]["content"], list)
          and any(b.get("type") == "image_url" for b in ClientOK.sent["messages"][0]["content"]))


class ClientFail:
    _timeout = 30.0
    _caps = {"vision": True, "thinking": True}

    def __init__(self):
        self.chat = type("Chat", (), {"completions": _CC(self)})()

    def create(self, **kw):
        raise RuntimeError("upstream 500")


with patch("modules.vision.record_error", return_value=None):
    check("C4 失败降级为空串", describe_image(ClientFail(), "m", url) == "")
check("C5 空模型降级", describe_image(ClientOK(), "", url) == "")

print("=== D. upload-image 路由（本地版落盘 + /image 服务） ===")
import routes

real_parse = routes.parse_multipart


class FakeH:
    headers = {}
    wfile = type("W", (), {"write": lambda self, b: None})()
    def _json(self, data, status=200):
        self.data = data
        self.status = status
    def send_response(self, c, *a): self.status = c
    def send_header(self, k, v): pass
    def end_headers(self): pass


h = FakeH()


def fake_parse(hh, **kw):
    return ({"mode": "story", "category": "可爱"},
            {"file": {"filename": "pic.png", "data": b"\x89PNG\r\n\x1a\nfake"}})


routes.parse_multipart = fake_parse
with patch("modules.vision.describe_image", return_value="描述占位"):
    routes.upload_image(h)
routes.parse_multipart = real_parse
check("D1 upload 返回 img_id", h.data.get("ok") and h.data["img_id"].startswith("img_"))
check("D2 图片已落盘", len(list((cfg.mode_root("story") / "images").glob("*.png"))) >= 1)

h2 = FakeH()
h2.path = "/image?id=" + h.data["img_id"] + "&mode=story"
routes.get_image(h2)
check("D3 /image 服务字节", h2.status == 200)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
