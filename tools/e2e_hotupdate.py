# -*- coding: utf-8 -*-
"""热更新 + 公告 · **端到端**真跑（本地源，真签名、真 HTTP、真覆盖层）。

    python tools/e2e_hotupdate.py        # 发布机/开发机跑；没有私钥会自动跳过

与 `tests/test_hotupdate.py` 的区别：那个用**假验签**（注入 `set_verifier`）跑语义；
这里用**真私钥签名 + 真 RSA 验签（Python 自实现路径）**跑"从发布到生效"的整条链：

    build_hotupdate.py --init/产出 → 起静态源 → 客户端 check()
      → 验签 → download_zip → extract_verified → 覆盖层落盘 → reload_pending
      → （模拟重启）boot_ok → 覆盖层真的被 server 的 _static_file 解析到

再覆盖三条**运维语义**：
    · 安全模式：连签两版坏补丁 → 第二次启动判定后自动回滚
    · kill switch：新清单宣布旧 serial 作废 → 客户端自动回滚
    · 双源降级：主源 404 时自动用兜底源

★ 它**临时改一个前端文件**再还原（为了造出"真实差异"），所以别在别的构建同时跑。
"""
import base64
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
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
PY = sys.executable
KEY = Path.home() / ".firefly" / "hotupdate_key.pem"
TMP = Path(tempfile.mkdtemp(prefix="firefly_e2e_hu_"))
sys.path.insert(0, str(ROOT / "app"))

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


if not KEY.is_file():
    print(f"! 没有发布私钥（{KEY}），跳过端到端（这台机器不是发布机）")
    sys.exit(0)

# 沙箱数据根：hotupdate_root() = USER_DIR.parent/hotupdate
os.environ["FIREFLY_DATA_DIR"] = str(TMP)
import modules.app_config as cfg          # noqa: E402
cfg.USER_DIR = TMP / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

import hotupdate as HU                    # noqa: E402
from hotupdate import web_dir             # noqa: E402
HU._start_background = lambda: None       # 不要后台轮询线程
HU.DEFAULT_URL_ROOTS = ()

DIST = TMP / "dist"
BUILD = TMP / "build"

print("=== A. 用真工具产出补丁（基线 + serial 1）===")
# 1) 造一个"基线"：把当前 app/static 复制一份当 base（--init 就在这一步）
r = subprocess.run([PY, str(ROOT / "tools" / "build_hotupdate.py"), "--init",
                    "--base", cfg.APP_VERSION, "--dist", str(DIST), "--build-dir", str(BUILD),
                    "--key", str(KEY)], capture_output=True, encoding="utf-8", errors="replace")
check("A1 基线建立成功", r.returncode == 0, (r.stdout or "").strip().splitlines()[-1:] and
      (r.stdout or "").strip().splitlines()[-1] or (r.stderr or "")[-200:])

# 2) 改一个前端文件（模拟一次"修 bug"），产出 serial 1
target = ROOT / "app" / "static" / "js" / "notice.js"
orig = target.read_bytes()
MARK = b"\n// E2E-MARK-" + str(int(time.time())).encode() + b"\n"
try:
    target.write_bytes(orig + MARK)
    r = subprocess.run([PY, str(ROOT / "tools" / "build_hotupdate.py"),
                        "--base", cfg.APP_VERSION, "--dist", str(DIST), "--build-dir", str(BUILD),
                        "--key", str(KEY), "--note", "E2E 冒烟"],
                       capture_output=True, encoding="utf-8", errors="replace")
    check("A2 产出 patch-1", (DIST / cfg.APP_VERSION / "patch-1.zip").is_file(),
          (r.stderr or "")[-200:] if r.returncode else "ok")
finally:
    target.write_bytes(orig)

BASE_DIR = DIST / cfg.APP_VERSION
check("A3 latest 指向 serial 1",
      json.loads((BASE_DIR / "latest").read_text(encoding="utf-8"))["serial"] == 1)

print("\n=== B. 起真·静态源（两个源：主源 + 兜底）===")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0].lstrip("/")
        # 客户端请求 {root}/{APP_VERSION}/<file> ⇒ 从 DIST 根提供（DIST 下有 <base>/ 一层）
        fp = DIST / p
        if not fp.is_file():
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(fp.stat().st_size))
        self.end_headers()
        self.wfile.write(fp.read_bytes())


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
srv.allow_reuse_address = True
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

print("\n=== C. 客户端：真验签 → 下载 → 校验 → 覆盖层 ===")
check("C0 验签走自实现（无 Kotlin 桥）", HU.verify.backend() == "python", HU.verify.backend())
HU.set_url_roots([f"http://127.0.0.1:{PORT}"])
# 只保留一个源（先测单源）
HU.set_url_roots([f"http://127.0.0.1:{PORT}"])
HU.on_startup()
r = HU.check(manual=True)
check("C1 check 拉到最后并验签通过", r.get("ok") and r.get("serial") == 1, str(r.get("error") or ""))
a = HU.apply_available()
check("C2 apply 成功", a.get("ok"), str(a.get("error") or ""))
check("C3 覆盖层文件数 > 0", HU.status()["overlay_files"] > 0,
      f"{HU.status()['overlay_files']} 个")
check("C4 reload_pending 已置位（前端据此重启）", HU.status()["reload_pending"] is True)
ov = web_dir() / "js" / "notice.js"
check("C5 覆盖层里是**打过补丁的**内容（带 E2E 标记）",
      ov.is_file() and MARK.strip() in ov.read_bytes(),
      f"{ov.stat().st_size if ov.is_file() else 0} 字节")
check("C6 覆盖层路径不带重复 web/ 前缀（A6 回归）",
      not (web_dir() / "web").exists())

print("\n=== D. 覆盖层真的被 server 解析到（静态文件优先级）===")
os.environ["FIREFLY_DATA_DIR"] = str(TMP)
import server as SRV                      # noqa: E402
p = SRV._static_file("js/notice.js")
check("D1 server._static_file 命中覆盖层", p == ov, str(p))
check("D2 未在补丁里的文件回落 base",
      SRV._static_file("index.html") == cfg.STATIC_DIR / "index.html",
      str(SRV._static_file("index.html")))

print("\n=== E. 启动确认（boot_ok）与原子替换 ===")
b = HU.boot_ok()
check("E1 boot_ok 清 pending 与 reload_pending",
      HU.status()["pending_serial"] == 0 and HU.status()["reload_pending"] is False)
check("E2 applied_serial 保持 1", HU.status()["applied_serial"] == 1)

print("\n=== F. 防回滚：同 serial 不重复应用 ===")
HU.check(manual=True)
a2 = HU.apply_available()
check("F1 已应用的 serial 拒绝再装", (not a2.get("ok")) and ("已应用" in str(a2.get("error")) or
      "没有已就绪" in str(a2.get("error"))), str(a2.get("error")))

print("\n=== G. 安全模式：坏补丁连签两版 → 自动回滚 ===")
# 手搓一版"能验签、但启动就会挂"的补丁：把 index.html 换成会抛异常的脚本
import base64 as _b64                     # noqa: E402
from hotupdate import net as HN           # noqa: E402


def make_bad(serial, zipname):
    buf = TMP / zipname
    bad = b"<html><script>throw new Error('e2e-bad')</script></html>"
    metas = []
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("web/index.html", bad)
        metas.append({"path": "web/index.html",
                      "sha256": hashlib.sha256(bad).hexdigest(), "size": len(bad)})
    m = {"format": 1, "base_version": cfg.APP_VERSION, "base_version_code": 900,
         "serial": serial, "issued_at": int(time.time()), "cumulative": True,
         "layer": "web", "note": "E2E 坏补丁",
         "zip": {"name": zipname, "sha256": hashlib.sha256(buf.read_bytes()).hexdigest(),
                 "size": buf.stat().st_size},
         "files": metas, "rollout_percent": 100, "min_safe_serial": 1,
         "revoked_serials": []}
    raw = HN.canonical_bytes(m)
    sig = subprocess.run([str(Path(r"C:\Program Files\Git\usr\bin\openssl.exe")),
                          "dgst", "-sha256", "-sign", str(KEY)], input=raw,
                         capture_output=True).stdout
    (BASE_DIR / zipname).write_bytes(buf.read_bytes())
    (BASE_DIR / f"patch-{serial}.json").write_text(json.dumps(
        {"v": 1, "manifest_b64": _b64.b64encode(raw).decode(),
         "sig": _b64.b64encode(sig).decode()}), encoding="utf-8")
    (BASE_DIR / "latest").write_text(json.dumps({"base": cfg.APP_VERSION, "serial": serial}),
                                     encoding="utf-8")


make_bad(2, "patch-2.zip")
HU.check(manual=True)
a3 = HU.apply_available()
check("G1 坏补丁 serial 2 应用成功（这是「没启动起来」的前提）", a3.get("ok"), str(a3.get("error")))
st = HU.status()
check("G2 处于 pending 状态（等前端确认启动）", st["pending_serial"] == 2 and st["boot_fail_count"] == 0)

HU.reset_for_test()
HU.on_startup()          # 第 1 次"启动失败"
check("G3 第一次未确认 → boot_fail_count=1，覆盖层仍在",
      HU.status()["boot_fail_count"] == 1 and HU.status()["overlay_files"] > 0,
      f"count={HU.status()['boot_fail_count']}")
HU.reset_for_test()
HU.on_startup()          # 第 2 次 → 触发自动回滚
st = HU.status()
check("G4 连续 2 次未确认 → 自动回滚（安全模式）",
      st["applied_serial"] == 0 and st["overlay_files"] == 0,
      f"serial={st['applied_serial']} files={st['overlay_files']}")
check("G5 回滚原因可查", "未能完成启动" in (st["rolled_back_reason"] or ""),
      st["rolled_back_reason"][:40])
check("G6 回滚后 base 内容完好（回落 STATIC_DIR）",
      SRV._static_file("index.html") == cfg.STATIC_DIR / "index.html")

print("\n=== H. kill switch：新清单宣布旧 serial 作废 ===")
HU.reset_for_test()
(BASE_DIR / "latest").write_text(json.dumps({"base": cfg.APP_VERSION, "serial": 1}),
                                encoding="utf-8")
HU.check(manual=True)
HU.apply_available()
HU.boot_ok()
check("H1 先正常装上 serial 1", HU.status()["applied_serial"] == 1)
# 发一版 serial 2，其清单里声明 serial 1 已作废
make_bad(2, "patch-2.zip")     # 内容仍是坏 html，但这次它只是用来携带 revoked
raw2 = json.loads((BASE_DIR / "patch-2.json").read_text(encoding="utf-8"))
man = json.loads(_b64.b64decode(raw2["manifest_b64"]))
man["note"] = "E2E kill switch"
man["revoked_serials"] = [1]
man["min_safe_serial"] = 2
newraw = HN.canonical_bytes(man)
sig = subprocess.run([str(Path(r"C:\Program Files\Git\usr\bin\openssl.exe")),
                      "dgst", "-sha256", "-sign", str(KEY)], input=newraw,
                     capture_output=True).stdout
(BASE_DIR / "patch-2.json").write_text(json.dumps(
    {"v": 1, "manifest_b64": _b64.b64encode(newraw).decode(),
     "sig": _b64.b64encode(sig).decode()}), encoding="utf-8")
r = HU.check(manual=True)
check("H2 check 后旧补丁被吊销并自动回滚",
      HU.status()["applied_serial"] == 0 and HU.status()["overlay_files"] == 0,
      f"serial={HU.status()['applied_serial']} reason={HU.status()['rolled_back_reason'][:34]}")

print("\n=== I. 双源降级（主源 404 → 兜底源顶上）===")
HU.reset_for_test()
(BASE_DIR / "latest").write_text(json.dumps({"base": cfg.APP_VERSION, "serial": 1}),
                                encoding="utf-8")
# 一个必定 404 的"主源" + 真源作兜底
HU.set_url_roots(["http://127.0.0.1:1/nope", f"http://127.0.0.1:{PORT}"])
r = HU.check(manual=True)
check("I1 主源挂掉时自动降级到兜底源并成功", r.get("ok") and r.get("serial") == 1,
      str(r.get("error") or ""))

print("\n=== J. 公告通道（真签名 → 真验签 → 缓存）===")
HU.reset_for_test()
import notice as NT                        # noqa: E402
r = NT.check(force=True)
check("J1 本地源没有公告也算正常（不抛异常）", isinstance(r, dict) and "ok" in r)

print("\n=== K. py 层（v2）：真签名 → 覆盖层 → 导入真的换掉代码 ===")
# 用一个**当前进程没 import 过**的探针模块（关键：已 import 的模块重启也换不掉，
# 那是 v2 的已知边界，见 `hotupdate._overlay_effective`）。
#
# ⚠️ K 段用**独立的 dist/ 与更新源**：前面 A 段已经在 `DIST/<base>/` 里放了 serial 1，
#   而"已发布的 serial 永不覆盖"是硬规则 ⇒ 复用同一个发布区会直接 die（而且它把原因打在
#   stdout 上，只读 stderr 会看到一句空失败）。独立目录既避开这条，也让断言不依赖前文残留。
KDIST = TMP / "kdist"
KBUILD = TMP / "kbuild"


class KHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        fp = KDIST / self.path.split("?")[0].lstrip("/")
        if not fp.is_file():
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(fp.stat().st_size))
        self.end_headers()
        self.wfile.write(fp.read_bytes())


ksrv = socketserver.TCPServer(("127.0.0.1", 0), KHandler)
ksrv.allow_reuse_address = True
KPORT = ksrv.server_address[1]
threading.Thread(target=ksrv.serve_forever, daemon=True).start()

probe = ROOT / "app" / "modules" / "hu_e2e_probe.py"
probe.write_text("# E2E 探针（测试结束即删）\nE2E_VALUE = 'base'\n", encoding="utf-8")
try:
    HU.reset_for_test()
    HU._clear_overlay()
    st = HU.load_state()
    st.update({"enabled": True, "applied_serial": 0, "applied_hash": "",
               "applied_layer": "", "pending_serial": 0, "boot_fail_count": 0,
               "base_version": cfg.APP_VERSION, "rolled_back_reason": "", "last_error": "",
               "url_roots": [f"http://127.0.0.1:{KPORT}"]})
    HU.save_state(st)
    HU._RT["available"] = None

    # K1：先用**当前工作区**重建基线（含刚写的探针），确保"基线里已有它"
    r = subprocess.run([PY, str(ROOT / "tools" / "build_hotupdate.py"), "--init",
                        "--base", cfg.APP_VERSION, "--dist", str(KDIST),
                        "--build-dir", str(KBUILD), "--key", str(KEY)],
                       capture_output=True, encoding="utf-8", errors="replace")
    check("K1 基线重建（含 py 层）成功", r.returncode == 0,
          ((r.stdout or "")[-160:] + (r.stderr or "")[-80:]).strip())

    # K2：改探针 → 产出累积补丁（这一次是纯 py 层改动）
    probe.write_text("# E2E 探针（测试结束即删）\nE2E_VALUE = 'patched'\n", encoding="utf-8")
    r = subprocess.run([PY, str(ROOT / "tools" / "build_hotupdate.py"),
                        "--base", cfg.APP_VERSION, "--dist", str(KDIST),
                        "--build-dir", str(KBUILD), "--key", str(KEY), "--note", "E2E py 层"],
                       capture_output=True, encoding="utf-8", errors="replace")
    check("K2 产出 py 层补丁", r.returncode == 0,
          ((r.stdout or "")[-160:] + (r.stderr or "")[-80:]).strip())
    KBASE = KDIST / cfg.APP_VERSION
    man = json.loads(_b64.b64decode(json.loads(
        (KBASE / "patch-1.json").read_text(encoding="utf-8"))["manifest_b64"]))
    check("K3 清单 layer 如实报 py", man.get("layer") == "py", str(man.get("layer")))
    check("K4 清单路径带 py/ 前缀",
          [f["path"] for f in man["files"]] == ["py/modules/hu_e2e_probe.py"],
          str([f["path"] for f in man["files"]]))

    # K5：客户端应用 → 必须走"重启生效"那一路
    rr = HU.check(manual=True)
    check("K5 check 拉到 py 补丁", rr.get("ok") and rr.get("serial") == 1, str(rr.get("error") or ""))
    a = HU.apply_available()
    check("K6 apply 成功且 layer=py", a.get("ok") and a.get("layer") == "py",
          f"{a.get('error')} layer={a.get('layer')}")
    check("K7 ★ restart_pending 置位（py 层不能靠 reload）",
          HU.status()["restart_pending"] is True)
    check("K8 reload_pending 为假（纯 py 层 reload 无意义）",
          HU.status()["reload_pending"] is False)
    pv = HU.py_dir() / "modules" / "hu_e2e_probe.py"
    check("K9 py 覆盖层落盘且不带重复 py/ 前缀",
          pv.is_file() and "patched" in pv.read_text(encoding="utf-8"),
          str(pv.relative_to(HU.root()) if pv.is_file() else "缺失"))

    # K10：真导入 —— 覆盖层插到 sys.path 首位后，导入到的是**打过补丁的代码**
    sys.path.insert(0, str(HU.py_dir()))
    try:
        import importlib
        mod = importlib.import_module("modules.hu_e2e_probe")
        check("K10 ★ 导入到的是补丁内容（这是 py 层热更的最终判据）",
              getattr(mod, "E2E_VALUE", None) == "patched",
              str(getattr(mod, "E2E_VALUE", None)))
    finally:
        sys.path.remove(str(HU.py_dir()))
        sys.modules.pop("modules.hu_e2e_probe", None)

    # K11：boot_ok（新进程启动确认）清掉重启标志
    HU.boot_ok()
    check("K11 boot_ok 清掉 restart_pending",
          HU.status()["restart_pending"] is False
          and HU.status()["pending_serial"] == 0)

    # K12~K15：★ 安全模式对 **py 层**同样成立（坏 py 补丁不能把用户钉死）
    #   G 组只覆盖了 web 层；py 层更危险 —— 坏补丁可能让"服务根本起不来"，
    #   而"起不来"恰恰是它自己无法上报的状态，只有**下一次启动**的 on_startup 能判。
    #   这里把状态摆成"刚应用完、还没等到 boot_ok 就崩了/被杀了"：
    _d = HU.load_state()
    _d["pending_serial"] = 1            # = 已应用但未确认启动
    _d["boot_fail_count"] = 0
    HU.save_state(_d)
    HU.reset_for_test()                 # 内存态清空 = 模拟进程重启
    note1 = HU.on_startup()["note"]
    st = HU.status()
    check("K12 ★ 第 1 次未确认的启动：只计数、不清 py 覆盖层（补丁仍在生效中）",
          st["boot_fail_count"] == 1 and st["py_files"] > 0, f"{note1} py={st['py_files']}")
    HU.reset_for_test()
    note2 = HU.on_startup()["note"]
    st = HU.status()
    check("K13 ★ 第 2 次未确认 → 自动回退，两层覆盖层全清",
          st["applied_serial"] == 0 and st["overlay_files"] == 0
          and not HU.py_dir().exists(), f"{note2} files={st['overlay_files']}")
    check("K14 回退原因可读", "未能完成启动" in (st["rolled_back_reason"] or ""),
          (st["rolled_back_reason"] or "")[:40])

    # K15：显式回滚幂等（两层都空时再调一次不许出错）
    HU.rollback("E2E 收尾")
    check("K15 回滚后两层覆盖层都空",
          HU.status()["overlay_files"] == 0 and not HU.py_dir().exists())
finally:
    probe.unlink(missing_ok=True)
    ksrv.shutdown()

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
print(f"（沙箱：{TMP}）")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
