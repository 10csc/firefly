# -*- coding: utf-8 -*-
"""全包快照回归测试（2026-09-08 三处必崩 bug 的守卫）：

1. /snapshot/create 缺 _is_server 导入 → NameError
2. /snapshot/restore 引用不存在的 routes_data._restore_full_snapshot → ImportError
3. _sync_one_mode 缺 urllib 导入 → 上传分支 NameError
另含两条前端静态守卫：/check-update 必须 POST；首页 .home-start 不得双断点隐藏。
"""
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_snapshot_"))
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


import routes
import routes_data
import routes_snapshot


class FakeH:
    """最小 handler：_json 捕获 + zip 响应写入 wfile（同 test_backup）。"""

    def __init__(self, body=None, path="/", raw=None, content_type=None):
        self.data = None
        self.status = 200
        self.path = path
        self.headers = {}
        self.sent_headers = {}
        self.wfile = io.BytesIO()
        if raw is not None:
            self.headers = {"Content-Length": str(len(raw)),
                            "Content-Type": content_type or "application/octet-stream"}
            self.rfile = io.BytesIO(raw)
        elif body is not None:
            payload = json.dumps(body).encode("utf-8")
            self.headers = {"Content-Length": str(len(payload))}
            self.rfile = io.BytesIO(payload)

    def _json(self, data, status=200):
        self.data = data
        self.status = status

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass


def _multipart(fields: dict, files: dict, boundary="FFTESTBOUNDARY"):
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fname, data) in files.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                     f"Content-Type: application/zip\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _names(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return set(zf.namelist())


# ── 造数据：story / haruno 两个包 + 用户表情包 + 内部目录 ──
story = cfg.mode_root("story")
(story / "data").mkdir(parents=True, exist_ok=True)
(story / "data" / "conversation.jsonl").write_text(
    '{"seq": 1, "who": "user", "type": "text", "content": "原始对话"}\n', encoding="utf-8")
(story / ".setting_fix").mkdir(exist_ok=True)
(story / ".setting_fix" / "pending.json").write_text("{}", encoding="utf-8")
haruno = cfg.mode_root("haruno")
(haruno / "data").mkdir(parents=True, exist_ok=True)
(haruno / "data" / "memory.md").write_text("春日手信记忆", encoding="utf-8")
sdir = cfg.USER_DIR / "stickers"
sdir.mkdir(parents=True, exist_ok=True)
(sdir / "user_test_比心.webp").write_bytes(b"WEBP-TEST")
(sdir / "registry.json").write_text('{"stickers": []}', encoding="utf-8")

print("=== A. build_full_snapshot_zip 打包口径 ===")
snap = routes_snapshot.build_full_snapshot_zip()
names = _names(snap)
check("A1 含所有注册包的顶层目录", "story/data/conversation.jsonl" in names and "haruno/data/memory.md" in names)
check("A2 含用户表情包", "stickers/user_test_比心.webp" in names)
check("A3 排除内部目录", not any(".setting_fix" in n for n in names))
check("A4 附 _config.json", "_config.json" in names)
cfg_json = json.loads(zipfile.ZipFile(io.BytesIO(snap)).read("_config.json").decode("utf-8"))
check("A5 _config.json 剥离 api_key", "api_key" not in cfg_json
      and all("api_key" not in p for p in cfg_json.get("providers") or []))

print("=== B. /snapshot/create（bug1 守卫：_is_server 必须可解析） ===")
check("B0 routes_snapshot 有 _is_server（回归守卫）", hasattr(routes_snapshot, "_is_server"))
h = FakeH()
routes.snapshot_create(h)
check("B1 create 成功", h.data and h.data.get("ok") is True)
sname = h.data.get("name", "")
check("B2 快照名格式", sname.startswith("snapshot-") and sname.endswith(".zip"))
check("B3 未登录 → 本地保存 + pushed False", h.data.get("pushed") is False
      and "未登录" in str(h.data.get("push_error", "")))

print("=== C. /snapshot/list + download + delete ===")
h = FakeH()
routes.snapshot_list(h)
items = h.data.get("snapshots", [])
check("C1 list 含新建快照", h.data.get("ok") and any(i["name"] == sname for i in items))
check("C2 list 字段完整（name/size/time）",
      all(k in items[0] for k in ("name", "size", "time")) if items else False)
h = FakeH(path=f"/snapshot/download?name={sname}")
routes.snapshot_download(h)
check("C3 download 返回 zip 流", h.wfile.getvalue().startswith(b"PK"))
h = FakeH({"name": "../evil.zip"})
routes.snapshot_delete(h)
check("C4 delete 拒绝穿越名", h.data.get("ok") is False)

print("=== D. /snapshot/restore（bug2 守卫：_restore_full_snapshot 必须存在） ===")
check("D0 routes_data 有 _restore_full_snapshot（回归守卫）",
      hasattr(routes_data, "_restore_full_snapshot"))
# 改动两个包的数据 + 删表情包，再恢复
(story / "data" / "conversation.jsonl").write_text(
    '{"seq": 9, "who": "user", "type": "text", "content": "被改过的"}\n', encoding="utf-8")
(haruno / "data" / "memory.md").unlink()
(sdir / "user_test_比心.webp").unlink()
h = FakeH({"name": sname})
routes.snapshot_restore(h)
check("D1 restore 成功", h.data.get("ok") is True and h.data.get("restored") == sname)
check("D2 story 数据已恢复",
      "原始对话" in (story / "data" / "conversation.jsonl").read_text(encoding="utf-8"))
check("D3 haruno 数据已恢复", (haruno / "data" / "memory.md").is_file())
check("D4 用户表情包已恢复", (sdir / "user_test_比心.webp").is_file())
check("D5 恢复前打了 pre-restore 快照",
      len(sorted(routes_snapshot._snapshots_dir().glob("pre-restore-*.zip"))) >= 1)
check("D6 未产生嵌套垃圾目录", not (story / "story").exists())

print("=== E. import_data 快照格式识别（按包分发，不再塞进单包） ===")
snap2 = _zip_bytes({
    "story/data/conversation.jsonl": '{"seq": 1, "who": "user", "type": "text", "content": "来自快照"}\n',
    "haruno/data/memory.md": "快照里的春日手信",
    "stickers/user_snap.webp": b"WEBP-SNAP",
    "_config.json": '{"providers": []}',
})
raw, ctype = _multipart({"mode": "story"}, {"file": ("snapshot-x.zip", snap2)})
h = FakeH(raw=raw, content_type=ctype)
routes.import_data(h)
check("E1 识别为快照并成功", h.data.get("ok") is True and h.data.get("snapshot") is True)
check("E2 按包分发到 story",
      "来自快照" in (story / "data" / "conversation.jsonl").read_text(encoding="utf-8"))
check("E3 按包分发到 haruno", (haruno / "data" / "memory.md").is_file())
check("E4 表情包已恢复", (sdir / "user_snap.webp").is_file())
check("E5 未产生 story/story 嵌套", not (story / "story").exists())

print("=== F. import_data 旧单包格式仍兼容 ===")
legacy = _zip_bytes({"data/conversation.jsonl": '{"seq": 2, "who": "user", "type": "text", "content": "旧格式"}\n'})
raw, ctype = _multipart({"mode": "story"}, {"file": ("story-old.zip", legacy)})
h = FakeH(raw=raw, content_type=ctype)
routes.import_data(h)
check("F1 旧格式按模式导入", h.data.get("ok") is True and h.data.get("mode") == "story")
check("F2 未误判为快照", not h.data.get("snapshot"))
check("F3 旧格式数据落位",
      "旧格式" in (story / "data" / "conversation.jsonl").read_text(encoding="utf-8"))

print("=== G. _sync_one_mode 上传分支（bug3 守卫：urllib 必须可解析） ===")
import urllib.request as _ur

_sent = []


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    _sent.append(req)
    return _FakeResp(json.dumps({"applied": ["data/conversation.jsonl"],
                                 "conflicts": 0, "skipped": []}).encode("utf-8"))


def _call(path, payload=None, binary=False):
    if path.startswith("/sync/manifest"):
        return {"files": {}}
    raise AssertionError("unexpected call: " + path)


_orig = _ur.urlopen
_ur.urlopen = _fake_urlopen
try:
    reports = {"uploaded": [], "downloaded": [], "merged": [], "skipped": [], "conflicts": 0}
    err = routes_data._sync_one_mode(_call, "story", reports)
finally:
    _ur.urlopen = _orig
check("G1 上传分支不再 NameError", err is None)
check("G2 上传结果回填", reports["uploaded"] == ["data/conversation.jsonl"])
check("G3 确实发出了上传请求", len(_sent) == 1 and _sent[0].method == "POST")

print("=== H. 前端静态守卫（bug4/bug5） ===")
upd = (ROOT / "app" / "static" / "js" / "update.js").read_text(encoding="utf-8")
check("H1 /check-update 走 POST", 'fetch("/check-update", {method: "POST"' in upd)
css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
check("H2 首页入口按钮不再被双断点隐藏", ".home-carousel, .home-start" not in css)
check("H3 移动端仍隐藏继续聊天（既有设计）",
      "@media (max-width: 1099px)" in css and css.count(".home-start { display: none; }") == 1)

print("=== I. 三个小 bug 的静态守卫（2026-09-08 二轮） ===")
guide = (ROOT / "app" / "static" / "js" / "guide.js").read_text(encoding="utf-8")
check("I1 教程不再指向已删除的「主动消息」设置组",
      'data-group="proactive"' not in guide)
nums = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬"]
seq = [n for n in nums if f'title: "{n}' in guide]
check("I2 教程步骤编号连续（①..⑫ 无跳号）", seq == nums[:12])
check("I3 settings.js 不再写幽灵字段 hidden_reply_enabled",
      "payload.hidden_reply_enabled" not in (ROOT / "app" / "static" / "js" / "settings.js").read_text(encoding="utf-8"))
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
check("I4 卡片样式已提为全局（#server-web-entry 不再裸渲染）",
      "\n.am-card{" in html and "#server-web-entry{margin:14px 10px 0}" in html)
check("I5 角色详情页同步后台主动门控",
      "S._hiddenEnabled = payload.hidden_enabled" in (ROOT / "app" / "static" / "js" / "panels.js").read_text(encoding="utf-8"))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
