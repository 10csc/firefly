# -*- coding: utf-8 -*-
"""公告通道 —— 离线全链路测试。

全部离线：起本地 http.server 假冒公告源；`verify.set_verifier()` 注入假验签
（真验签在 Kotlin 壳，真机 E2E 另测）。

覆盖的是**安全与降级语义**，不是"能不能跑通"：
  验签失败必须拒绝 / schema 不符必须拒绝 / serial 不一致必须拒绝（防回放）/
  版本定向生效 / 非法块被丢弃而不是渲染 / 图片 sha256 不符时图块被丢 /
  kill switch 清缓存 / 缓存不重复联网 / 已读与撤回 / 上限截断 / 任何异常都不外抛。
"""
import base64
import hashlib
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# 沙箱：notice_root() = USER_DIR.parent/notice → 临时目录（绝不碰真实数据）
_TMP = Path(tempfile.mkdtemp(prefix="firefly_test_notice_"))
os.environ["FIREFLY_DATA_DIR"] = str(_TMP)

import modules.app_config as cfg          # noqa: E402
cfg.USER_DIR = _TMP / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

import notice as NT                       # noqa: E402
from hotupdate import net as HN           # noqa: E402
from hotupdate import verify as HV        # noqa: E402

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


# ── 假公告源 ───────────────────────────────────────
IMG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 64
STATE = {"serial": 1, "bad_sig": False, "img_bytes": IMG_A, "serve": True}


def manifest(serial=None, entries=None, schema=1, revoked=None, min_safe=0,
             img_sha=None):
    serial = STATE["serial"] if serial is None else serial
    if entries is None:
        entries = [{
            "id": "e1", "title": "测试公告", "date": "2026-09-19", "level": "info",
            "pinned": True, "min_app_version": "", "max_app_version": "",
            "blocks": [{"t": "p", "text": "正文"},
                       {"t": "img", "name": "img-a.png",
                        "sha256": img_sha or hashlib.sha256(IMG_A).hexdigest(),
                        "size": len(IMG_A), "alt": "图"}],
        }]
    return {"schema": schema, "serial": serial, "generated_at": int(time.time()),
            "min_safe_serial": min_safe, "revoked_serials": revoked or [],
            "entries": entries}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not STATE["serve"]:
            self.send_response(404)
            self.end_headers()
            return
        p = self.path.split("?")[0]
        if p.endswith("/latest"):
            self._send(json.dumps({"serial": STATE["serial"]}).encode())
            return
        if p.endswith(".json") and "notice-" in p:
            raw = HN.canonical_bytes(manifest())
            sig = b"BAD" if STATE["bad_sig"] else b"GOOD"
            self._send(json.dumps({"v": 1,
                                   "manifest_b64": base64.b64encode(raw).decode(),
                                   "sig": base64.b64encode(sig).decode()}).encode())
            return
        if p.endswith(".png"):
            self._send(STATE["img_bytes"], "image/png")
            return
        self.send_response(404)
        self.end_headers()


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

HV.set_verifier(lambda data, sig_b64: base64.b64decode(sig_b64) == b"GOOD")

# 公告源由热更源翻译而来 → 直接改 hotupdate 的 url_roots 即可
import hotupdate as HU                    # noqa: E402
HU.DEFAULT_URL_ROOTS = ()
HU.set_url_roots([f"http://127.0.0.1:{PORT}"])
HU._start_background = lambda: None


def reset_env():
    NT.reset_for_test()
    STATE.update({"serial": 1, "bad_sig": False, "img_bytes": IMG_A, "serve": True})
    for p in (NT.root() / "img", NT._state_file()):
        try:
            if p.is_dir():
                for f in p.iterdir():
                    f.unlink()
            else:
                p.unlink()
        except OSError:
            pass


print("=== A. 更新源翻译（一个配置，两条通道）===")
check("A1 服务器形态：/hotupdate → /notice",
      NT._to_notice_root("http://h:8787/hotupdate") == "http://h:8787/notice",
      NT._to_notice_root("http://h:8787/hotupdate"))
check("A2 末尾斜杠不影响", NT._to_notice_root("http://h:8787/hotupdate/") == "http://h:8787/notice")
check("A3 Gitee 形态（含 {base}）原样使用（扁平资产目录）",
      NT._to_notice_root("https://gitee.com/o/r/releases/download/v{base}")
      == "https://gitee.com/o/r/releases/download/v{base}")
check("A4 空串 → 空", NT._to_notice_root("") == "")
check("A5 base_urls 已把 {base} 替换成本端版本",
      all("{base}" not in u for u in NT.base_urls()), str(NT.base_urls()))

print("\n=== B. 拉取 + 验签 ===")
reset_env()
r = NT.check()
check("B1 正常拉取成功", r.get("ok") and r.get("changed"), str(r)[:120])
check("B2 serial 记录正确", NT.status()["serial"] == 1)
check("B3 图片已缓存并通过 sha256", (NT.img_dir() / "img-a.png").read_bytes() == IMG_A)
check("B4 image_path 能取到", NT.image_path("img-a.png") is not None)

reset_env()
STATE["bad_sig"] = True
r = NT.check()
check("B5 验签失败 → 拒绝且不落缓存", (not r.get("ok")) and NT.payload()["serial"] == 0,
      str(r.get("error"))[:60])
check("B6 拒绝后目录里没有图片", not (NT.img_dir() / "img-a.png").exists())

print("\n=== C. 结构校验（审查约束）===")
reset_env()
try:
    NT.validate(HN.canonical_bytes(manifest(schema=2)))
    check("C1 schema 不符 → 抛异常", False)
except ValueError:
    check("C1 schema 不符 → 抛异常", True)

big = HN.canonical_bytes(manifest())
try:
    NT.validate(big + b" " * (NT.MAX_RAW + 1))
    check("C2 超过字节上限 → 抛异常", False)
except ValueError:
    check("C2 超过字节上限 → 抛异常", True)

m = manifest(entries=[{
    "id": "e1", "title": "T",
    "blocks": [{"t": "script", "text": "<script>alert(1)</script>"},
               {"t": "p", "text": "<b>粗</b>"},
               {"t": "img", "name": "../evil.png", "sha256": "0" * 64, "size": 5}],
}])
v = NT.validate(HN.canonical_bytes(m))
blocks = v["entries"][0]["blocks"]
check("C3 非白名单块类型（script）被丢弃", all(b["t"] != "script" for b in blocks))
check("C4 HTML 只作为**纯文本**保留（不解析、不转义决策在端侧）",
      blocks and blocks[0]["text"] == "<b>粗</b>")
check("C5 非法图片名（含 ../）被丢弃", all(b["t"] != "img" for b in blocks))

m = manifest(entries=[{"id": "../bad", "title": "T", "blocks": [{"t": "p", "text": "x"}]},
                      {"id": "ok", "title": "T2", "blocks": [{"t": "p", "text": "y"}]}])
v = NT.validate(HN.canonical_bytes(m))
check("C6 非法 id 条目被丢弃、合法条目保留",
      [e["id"] for e in v["entries"]] == ["ok"])

long_text = "字" * (NT.MAX_TEXT + 500)
m = manifest(entries=[{"id": "e1", "title": "T", "blocks": [{"t": "p", "text": long_text}]}])
v = NT.validate(HN.canonical_bytes(m))
check("C7 超长正文被截断到上限", len(v["entries"][0]["blocks"][0]["text"]) == NT.MAX_TEXT)

many = [{"id": f"e{i}", "title": "T", "blocks": [{"t": "p", "text": "x"}]}
        for i in range(NT.MAX_ENTRIES + 20)]
v = NT.validate(HN.canonical_bytes(manifest(entries=many)))
check("C8 条目数超过上限被截断", len(v["entries"]) == NT.MAX_ENTRIES)

print("\n=== D. 版本定向 ===")
reset_env()
v = NT.validate(HN.canonical_bytes(manifest(entries=[
    {"id": "new", "title": "新版本专用", "min_app_version": "99.0.0",
     "blocks": [{"t": "p", "text": "x"}]},
    {"id": "old", "title": "旧版本专用", "max_app_version": "0.0.1",
     "blocks": [{"t": "p", "text": "x"}]},
    {"id": "cur", "title": "当前版本", "min_app_version": cfg.APP_VERSION,
     "blocks": [{"t": "p", "text": "x"}]},
])))
check("D1 高于本端的 min 被排除", all(e["id"] != "new" for e in v["entries"]))
check("D2 低于本端的 max 被排除", all(e["id"] != "old" for e in v["entries"]))
check("D3 命中本端（min == 当前版本）保留", [e["id"] for e in v["entries"]] == ["cur"])

print("\n=== E. 图片（独立 URL + sha256 对账）===")
reset_env()
STATE["img_bytes"] = b"TAMPERED" + b"B" * 64      # 与清单 sha256 不符
NT.check()
p = NT.payload(auto_refresh=False)
has_img = any(b["t"] == "img" for e in p["entries"] for b in e["blocks"])
check("E1 图片 sha256 不符 → 图块被丢掉（宁可不显示也不显示裂图）", not has_img)
check("E2 条目本身仍在（只有图没了）", len(p["entries"]) == 1)

reset_env()
STATE["img_bytes"] = IMG_A
NT.check()
p = NT.payload(auto_refresh=False)
has_img = any(b["t"] == "img" for e in p["entries"] for b in e["blocks"])
check("E3 图片正确 → 图块保留", has_img)
check("E4 缓存命中不会重复下载（第二次 check 走缓存）",
      NT.check().get("cached") is True)
check("E5 image_path 拒绝非法名", NT.image_path("../state.json") is None)

print("\n=== F. 缓存与降级 ===")
reset_env()
p = NT.payload(auto_refresh=False)
check("F1 从未联网时 payload 仍 ok=True（静态兜底语义）", p["ok"] is True)
check("F2 无公告时 entries 为空、不报错", p["entries"] == [])

NT.check()
STATE["serve"] = False
STATE["serial"] = 5
r2 = NT.check(force=True)
check("F3 源挂掉时：报错但**保留旧缓存**", (not r2.get("ok")) and NT.payload(auto_refresh=False)["serial"] == 1)
check("F4 源挂掉不影响 payload 可用", NT.payload(auto_refresh=False)["ok"] is True)

print("\n=== G. kill switch 与回放防护 ===")
reset_env()
STATE["serial"] = 1
NT.check()
check("G1 先有一份缓存", NT.status()["serial"] == 1)

# 索引说 serial=3，清单里也必须是 3 —— 造一个"索引 3 / 清单 2"的错配
mismatch_state = dict(STATE)
STATE["serial"] = 3
raw_holder = {}

_orig_manifest = manifest


def _mismatch(**kw):
    return _orig_manifest(serial=2, **kw)


globals()["manifest"] = _mismatch
r = NT.check(force=True)
check("G2 索引与清单 serial 不一致 → 拒绝（防回放）",
      (not r.get("ok")) and "不一致" in str(r.get("error")),
      str(r.get("error"))[:60])
globals()["manifest"] = _orig_manifest
STATE.update(mismatch_state)

reset_env()
STATE["serial"] = 1
NT.check()
STATE["serial"] = 2
raw_orig = NT.manifest if hasattr(NT, "manifest") else None
# 新清单独自宣布"旧 serial 1 已作废" → 缓存必须被清掉
_orig_manifest2 = manifest


def _revoking(**kw):
    return _orig_manifest2(serial=2, revoked=[1], **kw)


globals()["manifest"] = _revoking
r = NT.check(force=True)
check("G3 revoked_serials 命中旧缓存 → 旧缓存被清", r.get("ok") and NT.payload(auto_refresh=False)["serial"] == 2,
      f"serial={NT.payload(auto_refresh=False)['serial']}")
globals()["manifest"] = _orig_manifest2
STATE["serial"] = 1

print("\n=== H. 已读 / 撤回 ===")
reset_env()
STATE["serial"] = 1
NT.check()
p = NT.payload(auto_refresh=False)
check("H1 初始全部未读", p["unread"] == 1 and p["entries"][0]["unread"] is True)
NT.mark_read(["e1"])
p = NT.payload(auto_refresh=False)
check("H2 标记已读后 unread 归零", p["unread"] == 0 and p["entries"][0]["unread"] is False)
NT.mark_read(all_read=True)
check("H3 全标已读", NT.payload(auto_refresh=False)["unread"] == 0)

# 撤回 = 新一版不再包含该条目（serial 递增）→ 已读表里那条也该被清掉
STATE["serial"] = 2
_orig_manifest3 = manifest


def _no_entry(**kw):
    return _orig_manifest3(serial=2, entries=[
        {"id": "e2", "title": "另一条", "blocks": [{"t": "p", "text": "y"}]}], **kw)


globals()["manifest"] = _no_entry
NT.check(force=True)
st = NT._load()
check("H4 被撤回条目的已读记录被清理（不再是死 id）",
      "e1" not in (st.get("read_ids") or []), str(st.get("read_ids")))
check("H5 新条目默认未读", NT.payload(auto_refresh=False)["unread"] == 1)
globals()["manifest"] = _orig_manifest3

print("\n=== I. 端到端 payload 形状（前端契约）===")
reset_env()
STATE["serial"] = 1
NT.check()
p = NT.payload(auto_refresh=False)
check("I1 顶层字段齐备",
      all(k in p for k in ("ok", "serial", "generated_at", "app_version",
                           "entries", "unread", "refreshing", "last_error")))
e = p["entries"][0]
check("I2 条目字段齐备",
      all(k in e for k in ("id", "title", "date", "level", "pinned", "blocks", "unread")))
check("I3 块字段齐备（含 img 的 name/alt）",
      all(k in b for b in e["blocks"] for k in (("t", "text") if b["t"] != "img"
                                                else ("t", "name", "sha256", "size", "alt"))))
check("I4 payload 全部可 JSON 序列化", isinstance(json.dumps(p), str))

print("\n=== J. 前端注入面静态守卫（结构保证，不是编码纪律）===")
_js = (ROOT / "app" / "static" / "js" / "notice.js").read_text(encoding="utf-8")
for _t in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
    check(f"J  notice.js 不出现 {_t}（服务端文本一律 textContent）", _t not in _js)
check("J  notice.js 用 createTextNode 写文本", "createTextNode" in _js)
check("J  notice.js 的图片 URL 走独立端点 /notice-image",
      "/notice-image?name=" in _js)
_html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
check("J  公告面板保留**静态兜底**（服务端拉不到也有内容）",
      'id="notice-static"' in _html and "platform.deepseek.com" in _html)
check("J  静态兜底在使用指南之前（服务端条目在上）",
      _html.index('id="notice-server"') < _html.index('id="notice-static"'))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
