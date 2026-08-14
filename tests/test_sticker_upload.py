# -*- coding: utf-8 -*-
"""表情包上传路由测试（routes.add_sticker_route 加固：扩展名白名单/随机后缀/路径校验）

铁律：正常路径 + 边界 + 错误路径。只 mock 真实外部依赖（无网络调用）。
"""
import sys, os, json, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# 隔离 user_data：必须在 import routes 之前重定向（sticker_picker 模块级
# _REGISTRY_FILE 在 import 时绑定 USER_DIR）
import modules.app_config as cfg
from pathlib import Path as _P
_tmp = tempfile.mkdtemp(prefix="firefly_test_upload_")
cfg.USER_DIR = _P(_tmp)
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

import routes

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")


# ── 构造 multipart 请求 ─────────────────────────────
def multipart_body(boundary, fields, files):
    parts = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    for k, (fn, data) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'.encode("utf-8") + data + b"\r\n")
    parts.append(f'--{boundary}--\r\n'.encode("utf-8"))
    return b"".join(parts)


class FakeH:
    class _Hdr:
        def __init__(self, ct, length):
            self._ct, self._len = ct, length
        def get(self, k, d=None):
            if k == "Content-Type": return self._ct
            if k == "Content-Length": return str(self._len)
            return d
    def __init__(self, body: bytes, content_type: str):
        self.headers = self._Hdr(content_type, len(body))
        self.out = None
        class _R:
            def read(self, n): return body[:n]
        self.rfile = _R()
    def _json(self, d): self.out = d


def upload(filename, data, category="可爱", label="测试图"):
    boundary = "----firefly-test-boundary"
    body = multipart_body(boundary,
                          {"category": category, "label": label},
                          {"file": (filename, data)})
    h = FakeH(body, f"multipart/form-data; boundary={boundary}")
    routes.add_sticker_route(h)
    return h.out


# ══════════════════════════════════════════════════
print("=== 1. 正常路径 ===")

out = upload("test.png", b"\x89PNG fake-image-bytes")
check("png 上传→ok=True", out.get("ok") is True)
check("png 上传→返回 sticker_id", bool(out.get("sticker_id")))
saved = [f for f in (cfg.USER_DIR / "stickers").glob("*.png")] if (cfg.USER_DIR / "stickers").exists() else []
check("png 上传→文件落盘", len(saved) >= 1)
if saved:
    check("png 上传→内容一致", saved[0].read_bytes() == b"\x89PNG fake-image-bytes")

out = upload("photo.JPG", b"\xff\xd8 fake-jpg", category="帅气", label="帅气图")
check("JPG 大写扩展名→ok=True（小写化白名单）", out.get("ok") is True)

out = upload("noext.webp", b"RIFF fake-webp")
check("webp 上传→ok=True", out.get("ok") is True)

# ══════════════════════════════════════════════════
print("=== 2. 扩展名白名单（存储型 XSS 防护）===")

out = upload("evil.html", b"<script>alert(1)</script>")
check("html 上传→拒绝", out.get("ok") is False and "仅支持" in out.get("error", ""))

out = upload("x.svg", b"<svg onload=alert(1)>")
check("svg 上传→拒绝", out.get("ok") is False)

out = upload("x.js", b"alert(1)")
check("js 上传→拒绝", out.get("ok") is False)

# ══════════════════════════════════════════════════
print("=== 3. 字段边界 ===")

boundary = "----firefly-test-boundary"
body = multipart_body(boundary, {"category": "可爱", "label": "无文件"}, {})
h = FakeH(body, f"multipart/form-data; boundary={boundary}")
routes.add_sticker_route(h)
check("缺文件字段→拒绝", h.out.get("ok") is False and "缺少图片" in h.out.get("error", ""))

out = upload("a.png", b"x", category="强势")
check("非法分类→拒绝", out.get("ok") is False and "分类" in out.get("error", ""))

out = upload("a.png", b"x", label="")
check("空 label→拒绝", out.get("ok") is False and "含义" in out.get("error", ""))

# ══════════════════════════════════════════════════
print("=== 4. 注册表路径校验（防手改 registry.json 越权读取）===")

from tools import sticker_picker as sp
orig_registry = sp._REGISTRY_FILE
try:
    sp._REGISTRY_FILE = _P(tmpdir2 := tempfile.mkdtemp()) / "registry.json"
    sp._REGISTRY_FILE.write_text(json.dumps({"stickers": [
        {"id": "evil_01", "file": "../../config.json", "category": "可爱", "label": "越权"},
        {"id": "good_01", "file": "stickers/ok.png", "category": "可爱", "label": "合法"},
    ]}, ensure_ascii=False), encoding="utf-8")
    all_s = sp.get_all_stickers()
    check("registry 越权条目→被跳过", "evil_01" not in all_s)
    check("registry 合法条目→保留", "good_01" in all_s)
finally:
    sp._REGISTRY_FILE = orig_registry
    shutil.rmtree(tmpdir2, ignore_errors=True)

print(f"\n=== 表情包上传测试: PASS={PASS} FAIL={FAIL} ===")
sys.exit(1 if FAIL else 0)
