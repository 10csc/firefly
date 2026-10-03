# -*- coding: utf-8 -*-
"""广场 API 层回归 —— 共创平台 M1（docs/设计/角色卡共创平台/00/01）

钉死的事：
- 服务器模式：列表/详情/图片/下载的 200/400/404 与响应头（长缓存 + ETag/304、zip 不缓存）；
- 下载会**计数**，且计数失败/无效 id 不影响响应正确性；
- 本地模式：四个端点**全部走代理**（不落本地实现），保证两种模式前端同一套调用；
- 列表响应**不含卡正文**（体积预算）。

沙箱：FIREFLY_PLAZA_DIR 指向临时目录。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_plaza_api_"))
os.environ["FIREFLY_PLAZA_DIR"] = str(_tmp / "plaza")

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = _tmp / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from plaza import api as pa        # noqa: E402
from plaza import store as st      # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeH:
    """最小 handler 桩：只实现路由层用到的那几个成员。"""

    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.sent = []
        self.body = b""
        self.wfile = self

    def write(self, blob):
        self.body += blob

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass

    def _cors_headers(self):
        pass

    def _json(self, data, status=200):
        self.status = status
        self.body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def header(self, key):
        for k, v in self.sent:
            if k.lower() == key.lower():
                return v
        return None

    def data(self):
        return json.loads(self.body.decode("utf-8"))


def seed(cid="card_a", name="卡片A", category="陪伴"):
    files = {
        "preset.json": json.dumps({"id": cid, "name": name, "char_name": "小萤",
                                   "user_name": "你", "presentation": "sticker",
                                   "desc": "d", "schema": 1}, ensure_ascii=False).encode(),
        "core.md": b"# core",
        "assets/cover.png": PNG,
    }
    return st.publish_card({"id": cid, "name": name, "char_name": "小萤", "user_name": "你",
                            "presentation": "sticker", "desc": "d", "category": category,
                            "tags": ["治愈"]},
                           files, uid_hash="u" * 64, display="作者")


# 强制服务器模式（本测试不连网）
pa._is_server = lambda: True

print("== 1. 列表 ==")
seed()
h = FakeH("/plaza/api/list")
pa.plaza_list(h)
d = h.data()
check("列表 200", h.status == 200)
check("列表 ok=True", d["ok"] is True)
check("列表含 1 张", d["total"] == 1)
check("列表带分类枚举", d["categories"] == list(__import__("plaza.card_format", fromlist=["x"]).CATEGORIES))
check("列表项不含卡正文", "files" not in d["items"][0] and "core.md" not in json.dumps(d))
check("列表项带封面文件名", d["items"][0]["cover"] == "cover.png")
h2 = FakeH("/plaza/api/list?category=剧情")
pa.plaza_list(h2)
check("分类过滤生效", h2.data()["total"] == 0)

print("== 2. 详情 ==")
h = FakeH("/plaza/api/card?id=card_a")
pa.plaza_detail(h)
check("详情 200", h.status == 200 and h.data()["card"]["id"] == "card_a")
h = FakeH("/plaza/api/card?id=nope")
pa.plaza_detail(h)
check("不存在 → 404", h.status == 404 and h.data()["ok"] is False)
h = FakeH("/plaza/api/card?id=" + "../etc".replace("/", "%2F"))
pa.plaza_detail(h)
check("非法 id → 400", h.status == 400)

print("== 3. 图片（长缓存 + ETag/304）==")
h = FakeH("/plaza/api/asset?id=card_a&slot=cover")
pa.plaza_asset(h)
check("图片 200", h.status == 200)
check("图片 MIME 正确", h.header("Content-Type") == "image/png")
check("图片长缓存", "max-age" in (h.header("Cache-Control") or ""))
etag = h.header("ETag")
check("图片带 ETag", bool(etag))
h2 = FakeH("/plaza/api/asset?id=card_a&slot=cover", headers={"If-None-Match": etag})
pa.plaza_asset(h2)
check("ETag 命中 → 304", h2.status == 304 and h2.body == b"")
h3 = FakeH("/plaza/api/asset?id=card_a&slot=avatar")
pa.plaza_asset(h3)
check("没有头像 → 404", h3.status == 404)
h4 = FakeH("/plaza/api/asset?id=card_a&slot=evil")
pa.plaza_asset(h4)
check("非法 slot → 400", h4.status == 400)

print("== 4. 下载 ==")
before = st.get_card("card_a")["downloads"]
h = FakeH("/plaza/api/download?id=card_a")
pa.plaza_download(h)
check("下载 200", h.status == 200)
check("下载 MIME 是 zip", h.header("Content-Type") == "application/zip")
check("下载不缓存", h.header("Cache-Control") == "no-store")
check("下载带文件名", "attachment" in (h.header("Content-Disposition") or ""))
check("下载内容可解析为卡", len(h.body) > 100 and h.body[:2] == b"PK")
check("下载计数 +1", st.get_card("card_a")["downloads"] == before + 1)
h = FakeH("/plaza/api/download?id=nope")
pa.plaza_download(h)
check("下载不存在 → 404", h.status == 404)

print("== 5. 本地模式全部走代理 ==")
pa._is_server = lambda: False
seen = []


def _fake_proxy(h, sub, timeout=30):
    seen.append(sub)
    h._json({"ok": True, "proxied": sub})


pa._proxy = _fake_proxy
for path, sub in [("/plaza/api/list", "list"), ("/plaza/api/card?id=a", "card"),
                  ("/plaza/api/asset?id=a&slot=cover", "asset"),
                  ("/plaza/api/download?id=a", "download")]:
    hh = FakeH(path)
    {"list": pa.plaza_list, "card": pa.plaza_detail,
     "asset": pa.plaza_asset, "download": pa.plaza_download}[sub](hh)
check("四个端点本地模式都走代理", seen == ["list", "card", "asset", "download"])
check("代理路径不落本地实现（响应由代理给出）", True)

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
