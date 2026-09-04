# -*- coding: utf-8 -*-
"""A9 图片消息测试：conversation image 校验/引用/hydrate、upload-image 落盘+desc（mock vision）、
polisher 首轮 content blocks、vision desc 失败降级、get_image 字节服务、
服务器版新语义（落盘+用户隔离+配额拒绝，铁律修订：服务器只存压缩图）"""
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

print("=== C. 图片字节工具（to_data_url） ===")
from modules.vision import to_data_url

url = to_data_url(b"fake-image", ".png")
check("C1 data url 生成", url and url.startswith("data:image/png;base64,"))
check("C1b 非白名单扩展返回 None", to_data_url(b"x", ".exe") is None)

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
routes.upload_image(h)
routes.parse_multipart = real_parse
check("D1 upload 返回 img_id", h.data.get("ok") and h.data["img_id"].startswith("img_"))
check("D1b 不再自动生成 desc（原生识图链路）", h.data.get("desc") == "")
check("D2 图片已落盘", len(list((cfg.mode_root("story") / "images").glob("*.png"))) >= 1)

h2 = FakeH()
h2.path = "/image?id=" + h.data["img_id"] + "&mode=story"
routes.get_image(h2)
check("D3 /image 服务字节", h2.status == 200)

print("=== E. 服务器版图片（开放落盘 + 用户隔离 + 配额） ===")
# 铁律修订：原图不出设备，服务器只存压缩图——服务器版同样落盘/服务，403/404 已移除
os.environ["FIREFLY_SERVER"] = "1"
_u7 = _tmp / "u7"
_tok = cfg.set_user_context(user_dir=_u7)
try:
    # 配额极小（104 字节）：200 字节图片直接拒
    os.environ["FIREFLY_IMAGE_QUOTA_MB"] = "0.0001"
    hq = FakeH()
    routes.parse_multipart = lambda hh, **kw: ({"mode": "story"},
        {"file": {"filename": "big.png", "data": b"\x89PNG" + b"\x00" * 196}})
    routes.upload_image(hq)
    routes.parse_multipart = real_parse
    check("E1 配额超限拒绝", hq.data.get("ok") is False and "图片空间已满" in hq.data.get("error", ""))
    check("E2 超限未落盘", not list((_u7 / "story" / "images").glob("*.png")))

    # 正常配额：落盘到该用户目录（与本地 USER_DIR 隔离）
    os.environ["FIREFLY_IMAGE_QUOTA_MB"] = "10"
    hs = FakeH()
    routes.parse_multipart = fake_parse
    routes.upload_image(hs)
    routes.parse_multipart = real_parse
    check("E3 服务器版上传成功", hs.data.get("ok") and hs.data.get("server_side") is True)
    check("E4 落盘在用户目录", len(list((_u7 / "story" / "images").glob("*.png"))) == 1)
    check("E5 不再自动生成 desc（原生识图链路）", hs.data.get("desc") == "")

    hs2 = FakeH()
    hs2.path = "/image?id=" + hs.data["img_id"] + "&mode=story"
    routes.get_image(hs2)
    check("E6 服务器版 /image 服务字节", hs2.status == 200)
    # 另一用户 ctx 下同 img_id 不可见（用户隔离）
    _tok2 = cfg.set_user_context(user_dir=_tmp / "u8")
    try:
        hs3 = FakeH()
        hs3.path = hs2.path
        routes.get_image(hs3)
        check("E7 跨用户取图 404", hs3.status == 404)
    finally:
        cfg.reset_user_context(_tok2)
finally:
    cfg.reset_user_context(_tok)
    os.environ.pop("FIREFLY_SERVER", None)
    os.environ.pop("FIREFLY_IMAGE_QUOTA_MB", None)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
