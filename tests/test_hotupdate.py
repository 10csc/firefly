# -*- coding: utf-8 -*-
"""热更新客户端（web 层）——离线全链路测试。

全部**离线**：起一个本地 http.server 假冒更新源，把 `state.json` 的 url_roots 指过去，
`verify.set_verifier()` 注入假验签（真验签在 Kotlin 壳，真机 E2E 另测）。

覆盖的是**安全语义**，不是"能不能跑通"：
  验签失败必须拒绝 / zip 与单文件 sha256 不符必须整包丢弃 / 路径越界必须拒绝 /
  base 不符必须拒绝 / 防回滚 / kill switch / 安全模式自动回退 / 整包升级清空覆盖层 /
  开关关闭时不应用（但 kill switch 仍生效）/ 空闲判定。
"""
import base64
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# 沙箱：hotupdate_root() = USER_DIR.parent/hotupdate → 临时目录（绝不碰真实数据）
_TMP = Path(tempfile.mkdtemp(prefix="firefly_test_hotupdate_"))
os.environ["FIREFLY_DATA_DIR"] = str(_TMP)

import modules.app_config as cfg          # noqa: E402
cfg.USER_DIR = _TMP / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

import hotupdate as HU                    # noqa: E402
from hotupdate import net as HN           # noqa: E402
from hotupdate import verify as HV        # noqa: E402

# ★ 把"出厂更新源"清空（2026-09-19）：真实默认值指向**生产服务器 + Gitee**，
#   于是任何回落到默认值的测试路径都会去连公网 ⇒ 慢、依赖网络、**偶发失败**
#   （全量跑时实测抓到过一次 flake）。单测只许连本机那个假更新源，
#   所以这里把默认值摘掉，由下面的 set_url_roots 全权决定。
HU.DEFAULT_URL_ROOTS = ()

# ★ 单测**不要真起后台轮询线程**（2026-09-19 抓到的 flake 真凶）。
#   `on_startup()` 末尾会 `_start_background()` → 守护线程 sleep 30s 后 check/apply，
#   而全量测试跑到后半段正好被它撞上 → 状态被异步改写 ⇒ 偶发失败（3 遍挂 1 遍）。
#   后台轮询本身不在这些用例的验证范围内（那是真机 E2E 的事），这里直接摘掉。
HU._start_background = lambda: None

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


# ── 假更新源 ───────────────────────────────────────
WEB = {"index.html": b"<html>v2</html>",
       "js/bundle.js": b"console.log('patched bundle')",
       "style.css": b"body{color:red}"}
STATE = {"serve_serial": 1, "bad_sig": False, "bad_zip_sha": False,
         "bad_file_sha": False, "escape_path": False, "base": None,
         "revoked": [], "min_safe": 1, "manifest": None, "zip": None}


def build_patch(serial=1, files=None, base=None):
    files = files or WEB
    buf = Path(tempfile.mkdtemp()) / f"patch-{serial}.zip"
    metas = []
    with zipfile.ZipFile(buf, "w") as z:
        for rel, data in files.items():
            arc = rel if rel.startswith("web/") else f"web/{rel}"
            z.writestr(arc, data)
            metas.append({"path": arc, "sha256": hashlib.sha256(data).hexdigest(),
                          "size": len(data)})
    zsha = hashlib.sha256(buf.read_bytes()).hexdigest()
    m = {"format": 1, "base_version": base or cfg.APP_VERSION, "base_version_code": 806,
         "serial": serial, "issued_at": int(time.time()), "cumulative": True,
         "layer": "web", "note": f"测试补丁 {serial}",
         "zip": {"name": f"patch-{serial}.zip", "sha256": zsha,
                 "size": buf.stat().st_size},
         "files": metas, "rollout_percent": 100,
         "min_safe_serial": STATE["min_safe"], "revoked_serials": STATE["revoked"]}
    if STATE["escape_path"]:
        m["files"].append({"path": "web/../../evil.txt", "sha256": "x", "size": 1})
    if STATE["bad_file_sha"]:
        m["files"][0]["sha256"] = "0" * 64
    raw = HN.canonical_bytes(m)
    return raw, buf


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
        if self.path.endswith("/latest"):
            self._send(json.dumps({"base": STATE["base"] or cfg.APP_VERSION,
                                   "serial": STATE["serve_serial"]}).encode())
            return
        if "/patch-" in self.path and self.path.endswith(".json"):
            raw, _ = build_patch(STATE["serve_serial"])
            STATE["manifest"] = raw
            sig = b"BAD-SIGNATURE" if STATE["bad_sig"] else b"GOOD"
            self._send(json.dumps({"v": 1,
                                   "manifest_b64": base64.b64encode(raw).decode(),
                                   "sig": base64.b64encode(sig).decode()}).encode())
            return
        if "/patch-" in self.path and self.path.endswith(".zip"):
            _, z = build_patch(STATE["serve_serial"])
            data = z.read_bytes()
            if STATE["bad_zip_sha"]:
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            self._send(data, "application/zip")
            return
        self.send_response(404)
        self.end_headers()


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# 假验签：只认 sig == "GOOD"
HV.set_verifier(lambda data, sig_b64: base64.b64decode(sig_b64) == b"GOOD")
HU.set_url_roots([f"http://127.0.0.1:{PORT}"])


def reset_env():
    HU.reset_for_test()
    STATE.update({"serve_serial": 1, "bad_sig": False, "bad_zip_sha": False,
                  "bad_file_sha": False, "escape_path": False, "base": None,
                  "revoked": [], "min_safe": 1})
    HU._clear_overlay()
    d = HU.load_state()
    d.update({"enabled": True, "applied_serial": 0, "applied_hash": "",
              "pending_serial": 0, "boot_fail_count": 0, "base_version": cfg.APP_VERSION,
              "rolled_back_reason": "", "last_error": ""})
    HU.save_state(d)
    HU._RT["available"] = None
    HU._RT["last_busy_at"] = 0.0


print("=== A. 正常闭环：检查 → 应用 → 覆盖层 ===")
reset_env()
r = HU.check()
check("A1 check 成功且拿到 serial 1", r.get("ok") and r.get("serial") == 1, str(r.get("error", "")))
check("A2 状态里 patch_ready", HU.status()["patch_ready"])
check("A3 验签可用标记为真（已注入）", HU.status()["verify_ok"])
r = HU.apply_available()
check("A4 apply 成功", r.get("ok"), str(r.get("error", "")))
check("A5 覆盖层文件数 = 3", HU.status()["overlay_files"] == 3, str(HU.status()["overlay_files"]))
check("A6 覆盖层内容正确",
      (HU.web_dir() / "js/bundle.js").read_bytes() == WEB["js/bundle.js"])
check("A7 状态记录 applied_serial/applied_hash",
      HU.status()["applied_serial"] == 1 and len(HU.status()["applied_hash"]) == 16)
check("A8 pending 已置位（等前端 boot_ok）", HU.status()["pending_serial"] == 1)
check("A9 reload_pending 为真（前端据此刷新）", HU.status()["reload_pending"])
check("A10 running 三元组正确",
      HU.status()["running"] == {"base_version": cfg.APP_VERSION, "hot_serial": 1,
                                 "patch_hash": HU.status()["applied_hash"]})
r = HU.boot_ok()
check("A11 boot_ok 清掉 pending", HU.status()["pending_serial"] == 0)
check("A12 boot_ok 清掉 reload_pending", not HU.status()["reload_pending"])
r = HU.check()
check("A13 已是最新时不重复应用", r.get("ok") and r.get("up_to_date") is True)
check("A14 重复 apply 被拒", HU.apply_available().get("ok") is False)

print("=== B. 安全：验签失败必须拒绝 ===")
reset_env()
STATE["bad_sig"] = True
r = HU.check()
check("B1 验签失败 → check 失败", r.get("ok") is False, str(r.get("error")))
check("B2 失败原因指向验签", "验签" in (r.get("error") or ""), str(r.get("error")))
check("B3 不设 available（应用不了）", HU.status()["patch_ready"] is False)
check("B4 apply 被拒", HU.apply_available().get("ok") is False)
check("B5 覆盖层没被写", HU.status()["overlay_files"] == 0)

print("=== C. 安全：内容校验（zip 篡改 / 单文件篡改 / 路径越界）===")
for name, key, flag in (("zip 本体被篡改", "bad_zip_sha", "bad_zip_sha"),
                        ("单文件 sha256 不符", "bad_file_sha", "bad_file_sha"),
                        ("路径越界 ../..", "escape_path", "escape_path")):
    reset_env()
    STATE[flag] = True
    HU.check()
    r = HU.apply_available()
    check(f"C {name} → apply 失败", r.get("ok") is False, str(r.get("error"))[:70])
    check(f"C {name} → 覆盖层未落任何文件", HU.status()["overlay_files"] == 0)
    check(f"C {name} → 暂存目录已清干净", not (HU.root() / "web.new").exists())

print("=== D. 安全：base 不符 / 防回滚 ===")
reset_env()
STATE["base"] = "9.9.9"
r = HU.check()
check("D1 latest 的 base 不符 → 拒绝", r.get("ok") is False, str(r.get("error")))
reset_env()
STATE["base"] = None
HU.check()
HU.apply_available()
STATE["serve_serial"] = 1
r = HU.check()
check("D2 serial 不大于已应用 → 不重复提示", r.get("up_to_date") is True)

print("=== E. kill switch：新清单可吊销旧补丁 ===")
reset_env()
HU.check()
HU.apply_available()
HU.boot_ok()
check("E1 回滚前覆盖层有 3 个文件", HU.status()["overlay_files"] == 3)
STATE["serve_serial"] = 2
STATE["revoked"] = [1]
r = HU.check()
check("E2 check 成功（拿到 serial 2）", r.get("ok"), str(r.get("error", "")))
check("E3 ★ 已应用的 serial 1 被吊销 → 自动回滚",
      HU.status()["applied_serial"] == 0 and HU.status()["overlay_files"] == 0)
check("E4 回退原因留痕", "吊销" in HU.status()["rolled_back_reason"],
      HU.status()["rolled_back_reason"])
STATE["revoked"] = []

print("=== F. 安全模式：pending 未确认 + 连续失败 → 自动回退 ===")
reset_env()
STATE["serve_serial"] = 1
HU.check()
HU.apply_available()
# 模拟"补丁把前端搞坏、boot_ok 永远不来"：直接跑两次启动钩子
n1 = HU.on_startup()["note"]
check("F1 第一次启动：只记失败次数，不回退",
      HU.status()["boot_fail_count"] == 1 and HU.status()["overlay_files"] == 3, n1)
n2 = HU.on_startup()["note"]
check("F2 ★ 第二次启动失败 → 自动回退到 base",
      HU.status()["overlay_files"] == 0 and HU.status()["applied_serial"] == 0, n2)
check("F3 回退原因可读", "未能完成启动" in HU.status()["rolled_back_reason"],
      HU.status()["rolled_back_reason"])
HU._thread = None

print("=== G. 整包升级：base 变了必须清空覆盖层 ===")
reset_env()
HU.check()
HU.apply_available()
HU.boot_ok()
check("G1 升级前覆盖层在", HU.status()["overlay_files"] == 3)
_old_ver = cfg.APP_VERSION
cfg.APP_VERSION = "9.9.9"          # 模拟"装了新整包"
try:
    note = HU.on_startup()["note"]
finally:
    cfg.APP_VERSION = _old_ver
    HU._thread = None
check("G2 ★ 覆盖层被清空（防旧补丁盖住新 APK）", HU.status()["overlay_files"] == 0,
      note)
check("G3 状态复位并留痕", HU.status()["applied_serial"] == 0
      and "整包" in HU.status()["rolled_back_reason"], HU.status()["rolled_back_reason"])
# ★ 换 base 会同时把 url_roots **重置回出厂默认**（正确行为：更新源地址里含版本号，
#   旧 base 的 override 对新 base 没有意义）。离线测试必须把地址拨回本地——
#   否则下面的用例会去连真实的公网更新源（那是网络依赖，不是单测该干的事）。
HU.set_url_roots([f"http://127.0.0.1:{PORT}"])

print("=== H. 开关：关闭时不应用，但 kill switch 仍生效（安全例外）===")
reset_env()
STATE["serve_serial"] = 1
HU.set_enabled(False)
r = HU.check()
check("H1 关闭时仍会联网检查（拉到了 serial）", r.get("ok") and r.get("serial") == 1)
check("H2 关闭时不应用", HU.apply_available().get("ok") is False
      and HU.status()["overlay_files"] == 0)
HU.set_enabled(True)
HU.check()
HU.apply_available()
HU.boot_ok()
HU.set_enabled(False)
STATE["serve_serial"] = 2
STATE["revoked"] = [1]
HU.check()
check("H3 ★ 关闭状态下吊销依然生效", HU.status()["overlay_files"] == 0)
STATE["revoked"] = []
HU.set_enabled(True)

print("=== I. 空闲判定（规范 §5.2.1）===")
reset_env()
check("I1 初始即空闲", HU.idle() is True)
HU.note_activity(True, "typing")
check("I2 前端忙 → 不空闲", HU.idle() is False)
check("I3 忙的原因可读", HU.status()["busy_why"] == "typing")
HU.note_activity(False)
check("I4 刚变空闲还在 3 秒静默期内 → 仍不空闲", HU.idle() is False)
HU._RT["last_busy_at"] = time.time() - 10
check("I5 静默期满 → 空闲", HU.idle() is True)

print("=== J. server.py 覆盖层优先级 ===")
reset_env()
import server as SV                       # noqa: E402
base_file = SV._static_file("index.html")
check("J1 无覆盖层时回落底座 STATIC_DIR",
      base_file == cfg.STATIC_DIR / "index.html", str(base_file))
HU.web_dir().mkdir(parents=True, exist_ok=True)
(HU.web_dir() / "index.html").write_bytes(b"<html>overlay</html>")
over = SV._static_file("index.html")
check("J2 ★ 有覆盖层时优先用覆盖层", over == HU.web_dir() / "index.html", str(over))
check("J3 未覆盖的文件仍走底座",
      SV._static_file("style.css") == cfg.STATIC_DIR / "style.css")
check("J4 路径越界被挡（返回不存在的底座路径）",
      not SV._static_file("../config.json").exists())

print("=== K. 服务器版禁用 ===")
import routes_hotupdate as RH             # noqa: E402
_sv = RH._is_server
RH._is_server = lambda: True
try:
    class _H:
        def __init__(self):
            self.code = None
            self.body = None

        def _json(self, obj, code=200):
            self.code, self.body = code, obj

    h = _H()
    RH.hotupdate_status(h)
    check("K1 服务器版 status 返回 403", h.code == 403 and h.body.get("ok") is False)
finally:
    RH._is_server = _sv

HU.reset_for_test()
srv.shutdown()
print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
