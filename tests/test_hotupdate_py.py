# -*- coding: utf-8 -*-
"""热更新 py 层（v2）—— 离线全链路测试。

与 `tests/test_hotupdate.py`（web 层）互补，本文专测 **Python 覆盖层**：
  两层同包 / 双覆盖层落盘 / `restart_pending` 与 `reload_pending` 的分工 /
  py 层生效性判定（`_overlay_effective`） / 两层换入的原子性（一层坏则都不换） /
  注入点时序（start_server.py 与 server.py 的 sys.path 顺序） /
  门禁与产出器的层口径（layer 字段、py/ 不许夹带非 .py、不许打包 hotupdate/）。

全部**离线**：假更新源 + 假验签，沙箱 USER_DIR（绝不碰真实数据）。
"""
import base64
import hashlib
import http.server
import importlib.util
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
sys.path.insert(0, str(ROOT / "app"))

# 沙箱：hotupdate_root() = USER_DIR.parent/hotupdate → 临时目录（绝不碰真实数据）
_TMP = Path(tempfile.mkdtemp(prefix="firefly_test_hotupdate_py_"))
os.environ["FIREFLY_DATA_DIR"] = str(_TMP)

import modules.app_config as cfg          # noqa: E402
cfg.USER_DIR = _TMP / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

import hotupdate as HU                    # noqa: E402
from hotupdate import net as HN           # noqa: E402
from hotupdate import verify as HV        # noqa: E402

HU.DEFAULT_URL_ROOTS = ()
HU._start_background = lambda: None       # 单测不起后台轮询线程（flaky 源，见 web 层测试说明）

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
WEB = {"index.html": b"<html>v2</html>", "js/bundle.js": b"console.log('patched')"}
# ★ py 层用**独立顶层模块名**（hu_probe_*），避免 importlib 去动真实包的父包
PY = {"py/hu_probe_mod.py": b"VALUE = 'patched'\n",
      "py/hu_probe_pkg/__init__.py": b"",
      "py/hu_probe_pkg/inner.py": b"INNER = 2\n"}
STATE = {"serve_serial": 1, "bad_sig": False, "base": None, "files": None,
         "layer": None, "min_safe": 1, "revoked": []}


def build_patch(serial=1):
    files = STATE["files"] if STATE["files"] is not None else {**WEB, **PY}
    buf = Path(tempfile.mkdtemp()) / f"patch-{serial}.zip"
    metas = []
    with zipfile.ZipFile(buf, "w") as z:
        for rel, data in files.items():
            # 允许两种写法：带层前缀（"py/x.py"）或按扩展名推断（"x.py" → py 层）
            arc = rel if rel.split("/", 1)[0] in ("web", "py") else (
                f"py/{rel}" if rel.endswith(".py") else f"web/{rel}")
            z.writestr(arc, data)
            metas.append({"path": arc, "sha256": hashlib.sha256(data).hexdigest(),
                          "size": len(data)})
    zsha = hashlib.sha256(buf.read_bytes()).hexdigest()
    have = [l for l in ("web", "py")
            if any(f["path"].startswith(l + "/") for f in metas)]
    m = {"format": 1, "base_version": STATE["base"] or cfg.APP_VERSION,
         "base_version_code": 806,
         "serial": serial, "issued_at": int(time.time()), "cumulative": True,
         "layer": STATE["layer"] or "+".join(have), "note": f"测试补丁 {serial}",
         "zip": {"name": f"patch-{serial}.zip", "sha256": zsha,
                 "size": buf.stat().st_size},
         "files": metas, "rollout_percent": 100,
         "min_safe_serial": STATE["min_safe"], "revoked_serials": STATE["revoked"]}
    return HN.canonical_bytes(m), buf


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
            sig = b"BAD" if STATE["bad_sig"] else b"GOOD"
            self._send(json.dumps({"v": 1,
                                   "manifest_b64": base64.b64encode(raw).decode(),
                                   "sig": base64.b64encode(sig).decode()}).encode())
            return
        if "/patch-" in self.path and self.path.endswith(".zip"):
            _, z = build_patch(STATE["serve_serial"])
            self._send(z.read_bytes(), "application/zip")
            return
        self.send_response(404)
        self.end_headers()


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

HV.set_verifier(lambda data, sig_b64: base64.b64decode(sig_b64) == b"GOOD")
HU.set_url_roots([f"http://127.0.0.1:{PORT}"])


def reset_env():
    HU.reset_for_test()
    STATE.update({"serve_serial": 1, "bad_sig": False, "base": None,
                  "files": None, "layer": None, "min_safe": 1, "revoked": []})
    HU._clear_overlay()
    d = HU.load_state()
    d.update({"enabled": True, "applied_serial": 0, "applied_hash": "",
              "applied_layer": "", "pending_serial": 0, "boot_fail_count": 0,
              "base_version": cfg.APP_VERSION, "rolled_back_reason": "",
              "last_error": ""})
    HU.save_state(d)
    HU._RT["available"] = None
    HU._RT["last_busy_at"] = 0.0


def _fresh_modules():
    """把探针模块从 sys.modules 里清掉（模拟"新进程"）。"""
    for name in list(sys.modules):
        if name.startswith("hu_probe"):
            del sys.modules[name]


print("=== A. 两层同包：web + py 一起应用 ===")
reset_env()
r = HU.check()
check("A1 check 拿到 serial 1", r.get("ok") and r.get("serial") == 1, str(r.get("error", "")))
check("A2 available 报出 layer=web+py", HU.status()["available"]["layer"] == "web+py",
      str(HU.status()["available"]))
r = HU.apply_available()
check("A3 apply 成功", r.get("ok"), str(r.get("error", "")))
check("A4 报出 layer=web+py", r.get("layer") == "web+py", str(r.get("layer")))
check("A5 web 覆盖层 2 个文件", HU.status()["web_files"] == 2, str(HU.status()["web_files"]))
check("A6 py 覆盖层 3 个文件", HU.status()["py_files"] == 3, str(HU.status()["py_files"]))
check("A7 两层计数 = overlay_files", HU.status()["overlay_files"] == 5)
check("A8 py 覆盖层路径正确（无多一层）",
      (HU.py_dir() / "hu_probe_mod.py").is_file()
      and (HU.py_dir() / "hu_probe_pkg/inner.py").is_file())
check("A9 py 覆盖层内容正确",
      (HU.py_dir() / "hu_probe_mod.py").read_bytes() == PY["py/hu_probe_mod.py"])
check("A10 state 记录 applied_layer", HU.status()["applied_layer"] == "web+py",
      HU.status()["applied_layer"])
check("A11 暂存目录已清（web.new / py.new 都不在）",
      not (HU.root() / "web.new").exists() and not (HU.root() / "py.new").exists())
check("A12 换入后无 .old 残留",
      not (HU.root() / "web.old").exists() and not (HU.root() / "py.old").exists())

print("=== B. ★ 生效方式分工：含 py ⇒ restart_pending，reload_pending 不再为真 ===")
st = HU.status()
check("B1 restart_pending 为真（含 py 必须重启进程）", st["restart_pending"] is True)
check("B2 reload_pending 为真（前端层也变了，但前端不该自己 reload）",
      st["reload_pending"] is True)
check("B3 pending 已置位（等新进程 boot_ok）", st["pending_serial"] == 1)
r = HU.boot_ok()
st = HU.status()
# ★ 2026-09-25 真机实测改语义：同进程的 boot-ok（页面 reload 触发）**不构成 py 层的启动确认**，
#   否则"pending 被自己伪造清掉"会让安全模式（坏补丁自动回滚）失效。清 pending 的判据见 M 组。
check("B4 ★ 同进程 boot-ok 不清 py 层的 pending（防伪造启动确认）",
      st["pending_serial"] == 1 and st["restart_pending"] is True, str(r.get("same_process")))
check("B5 但前端那条 reload 标志确实清了（它本来就该清）",
      st["reload_pending"] is False)

print("=== C. ★ 只有 web 层时不报 restart（别让壳白重启用户）===")
reset_env()
STATE["files"] = dict(WEB)
HU.check()
r = HU.apply_available()
st = HU.status()
check("C1 layer=web", r.get("layer") == "web", str(r.get("layer")))
check("C2 restart_pending 为假", st["restart_pending"] is False)
check("C3 reload_pending 为真", st["reload_pending"] is True)
check("C4 没有 py 覆盖层", st["py_files"] == 0 and not HU.py_dir().exists())
HU.boot_ok()

print("=== D. ★ 只有 py 层时：可 reload 标志为假（reload 换不掉 Python 代码）===")
reset_env()
STATE["files"] = dict(PY)
HU.check()
r = HU.apply_available()
st = HU.status()
check("D1 layer=py", r.get("layer") == "py", str(r.get("layer")))
check("D2 restart_pending 为真", st["restart_pending"] is True)
check("D3 ★ reload_pending 为假（含 py 时 reload 无意义）", st["reload_pending"] is False)
check("D4 web 覆盖层不存在", st["web_files"] == 0)
HU.boot_ok()

print("=== E. ★ 覆盖层真的会盖住底座代码（find_spec 落点）===")
reset_env()
STATE["files"] = {"py/hu_probe_mod.py": PY["py/hu_probe_mod.py"]}
HU.check()
HU.apply_available()
HU.boot_ok()
_fresh_modules()
sys.path.insert(0, str(HU.py_dir()))
try:
    spec = importlib.util.find_spec("hu_probe_mod")
    check("E1 find_spec 落在覆盖层", str(spec.origin).startswith(str(HU.py_dir())),
          str(spec.origin))
    import hu_probe_mod                            # noqa: E402
    check("E2 导入到的是补丁内容", hu_probe_mod.VALUE == "patched",
          str(getattr(hu_probe_mod, "VALUE", None)))
finally:
    try:
        sys.path.remove(str(HU.py_dir()))
    except ValueError:
        pass
    _fresh_modules()
check("E3 _overlay_effective 对刚装的补丁判为有效",
      HU._overlay_effective([{"path": "py/hu_probe_mod.py"}])[0] is True)

print("=== F. ★ 已 import 的模块：重启换不掉 ⇒ 不报 restart，但要留痕 ===")
# 用一个**当前进程已经 import 过**的真实模块（modules.app_config 必然已加载），
# 模拟"补丁改的正是启动链路上的老模块"。
reset_env()
STATE["files"] = {"py/modules/app_config.py": b"# patched\n"}
HU.check()
r = HU.apply_available()
st = HU.status()
check("F1 应用仍然成功（补丁本身合法）", r.get("ok"), str(r.get("error", "")))
check("F2 ★ restart_pending 为假（重启也换不掉已 import 的模块）",
      st["restart_pending"] is False)
check("F3 留痕说明原因", "重启无效" in (st["restart_note"] or ""), st["restart_note"])
check("F4 覆盖层文件确实落盘了（装上但不生效，等下次整包）", st["py_files"] == 1)
HU.boot_ok()

print("=== G. ★ 两层换入的原子性：一层坏 ⇒ 另一层也不许换 ===")
reset_env()
# web 层合法、py 层声明一个包里没有的文件（sha 对不上/缺文件 ⇒ 解压阶段就该拒绝）
_ok_web = {f"web/{k}": v for k, v in WEB.items()}
STATE["files"] = {**_ok_web, "py/hu_probe_mod.py": PY["py/hu_probe_mod.py"]}
HU.check()
_orig_extract = HN.extract_verified
_calls = {"n": 0}


def _fail_second(zip_path, files_meta, dst_dir, strip_prefix="web/", expect_names=None):
    _calls["n"] += 1
    if strip_prefix == "py/":
        raise ValueError("模拟 py 层解压失败")
    return _orig_extract(zip_path, files_meta, dst_dir, strip_prefix, expect_names)


HN.extract_verified = _fail_second
try:
    r = HU.apply_available()
finally:
    HN.extract_verified = _orig_extract
check("G1 apply 失败", r.get("ok") is False, str(r.get("error"))[:70])
check("G2 ★ web 覆盖层没有被换入（两层要么都换、要么都不换）",
      HU.status()["web_files"] == 0, str(HU.status()["web_files"]))
check("G3 py 覆盖层也没有", HU.status()["py_files"] == 0)
check("G4 暂存目录已清干净",
      not (HU.root() / "web.new").exists() and not (HU.root() / "py.new").exists())
check("G5 applied_serial 仍为 0", HU.status()["applied_serial"] == 0)

print("=== H. 回滚清两层 + 手动回滚标志 ===")
reset_env()
STATE["files"] = {**WEB, **PY}
HU.check()
HU.apply_available()
HU.boot_ok()
check("H1 回滚前两层共 5 个文件", HU.status()["overlay_files"] == 5)
r = HU.rollback("测试回滚")
check("H2 两层都被清掉", HU.status()["overlay_files"] == 0 and r["removed"] == 5)
check("H3 applied_layer 复位", HU.status()["applied_layer"] == "")
check("H4 reload_pending 为真（前端要刷新回底座）", HU.status()["reload_pending"] is True)
check("H5 restart_pending 为假（回滚不重启）", HU.status()["restart_pending"] is False)

print("=== I. 整包升级：base 变了清掉两层 ===")
reset_env()
HU.check()
HU.apply_available()
HU.boot_ok()
_old = cfg.APP_VERSION
cfg.APP_VERSION = "9.9.9"
try:
    note = HU.on_startup()["note"]
finally:
    cfg.APP_VERSION = _old
    HU._thread = None
check("I1 两层覆盖层全清", HU.status()["overlay_files"] == 0, note)
check("I2 状态复位", HU.status()["applied_serial"] == 0
      and HU.status()["applied_layer"] == "")

print("=== J. ★ 注入点时序（源码不变量 + 真 subprocess 验证）===")
ss = (ROOT / "android" / "app" / "src" / "main" / "python" / "start_server.py").read_text(encoding="utf-8")
# ⚠️ 用**整行**定位，不能用 `index("import server")` —— 注释里也写着这几个字
#   （本项目注释大量引用代码形态），substring 匹配会定位到注释上，断言就变成永真/永假。
_ss_lines = [l.strip() for l in ss.splitlines()]
_i_insert = next(i for i, l in enumerate(_ss_lines) if l.startswith("sys.path.insert(0, _overlay_py)"))
_i_apppath = next(i for i, l in enumerate(_ss_lines) if l == 'sys.path.insert(0, join(_BACKEND, "app"))')
_i_import = next(i for i, l in enumerate(_ss_lines) if l == "import server")
check("J1 start_server.py 在 import server 之前挂载覆盖层", _i_insert < _i_import,
      f"insert@{_i_insert} import@{_i_import}")
check("J2 ★ 覆盖层插在 _BACKEND/app 之后（insert(0) ⇒ 覆盖层才在最前）",
      _i_apppath < _i_insert, f"app@{_i_apppath} insert@{_i_insert}")
check("J3 用的是 FIREFLY_DATA_DIR（与 Chaquopy 的数据根同源）",
      "FIREFLY_DATA_DIR" in ss)
sv = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
check("J4 PC 入口 server.py 也挂载了 py 覆盖层",
      "_hu_py" in sv and "sys.path.insert(0, str(_hu_py))" in sv)
check("J5 PC 入口用 FIREFLY_ANDROID 守卫（避免与安卓那条重复插）",
      'os.environ.get("FIREFLY_ANDROID")' in sv)

# 真跑一遍：造一个"覆盖层目录 + 同名模块"，按 start_server 的顺序插 sys.path，
# 看导入到的是哪一份。这是"顺序写反 ⇒ 静默用底座代码"的唯一有效防线。
_probe = Path(tempfile.mkdtemp(prefix="hu_order_"))
(_probe / "base" / "dupmod").mkdir(parents=True)
(_probe / "base" / "dupmod" / "__init__.py").write_text("SRC = 'base'\n", encoding="utf-8")
(_probe / "overlay" / "dupmod").mkdir(parents=True)
(_probe / "overlay" / "dupmod" / "__init__.py").write_text("SRC = 'overlay'\n", encoding="utf-8")
_code = (
    "import sys\n"
    f"sys.path.insert(0, r'{_probe / 'base'}')\n"
    f"sys.path.insert(0, r'{_probe / 'overlay'}')\n"
    "import dupmod\n"
    "print('SRC=' + dupmod.SRC)\n"
)
_r = subprocess.run([sys.executable, "-c", _code], capture_output=True, text=True)
check("J6 ★ 覆盖层优先于底座（真 subprocess 实测）", "SRC=overlay" in (_r.stdout or ""),
      (_r.stdout or _r.stderr or "").strip()[:80])
shutil.rmtree(_probe, ignore_errors=True)

print("=== K. 门禁与产出器的层口径 ===")
sys.path.insert(0, str(ROOT / "tools"))
import build_hotupdate as BH               # noqa: E402
import check_hotupdate as CH               # noqa: E402
check("K1 产出器不打包 hotupdate/（它是补丁自己的加载器）",
      "hotupdate" in BH.PY_SKIP_DIRS)
check("K2 产出器不打包角色资产 assets/", "assets" in BH.PY_SKIP_DIRS)
check("K3 layer_of 能识别三种组合",
      BH.layer_of(["web/a.js"]) == "web" and BH.layer_of(["py/m.py"]) == "py"
      and BH.layer_of(["web/a.js", "py/m.py"]) == "web+py")
_shared = tempfile.mkdtemp(prefix="hu_build_")
_sh_dist = Path(_shared) / "dist"
_sh_build = Path(_shared) / "build"

# 用**真源码**的临时副本跑一次产出器（不碰仓库的 .tmp_hotupdate / hotupdate_dist）
_app = Path(_shared) / "app"
(_app / "static").mkdir(parents=True)
(_app / "static" / "index.html").write_bytes(b"<html>base</html>")
(_app / "modules").mkdir(parents=True)
(_app / "modules" / "demo.py").write_text("X = 1\n", encoding="utf-8")
(_app / "hotupdate").mkdir(parents=True)
(_app / "hotupdate" / "skipme.py").write_text("Y = 2\n", encoding="utf-8")
(_app / "assets").mkdir(parents=True)
(_app / "assets" / "data.json").write_text("{}", encoding="utf-8")
(_o_static, _o_app) = BH.STATIC_DIR, BH.APP_DIR
BH.STATIC_DIR, BH.APP_DIR = _app / "static", _app
try:
    web = BH.collect_web()
    py = BH.collect_py()
    check("K4 collect_py 只收 .py 且排除 hotupdate/ 与 assets/",
          set(py) == {"modules/demo.py"}, str(sorted(py)))
    check("K5 collect_web 收 static 下全部文件", set(web) == {"index.html"})
    allf = BH.collect_all()
    check("K6 collect_all 的键带层前缀",
          "web/index.html" in allf and "py/modules/demo.py" in allf)
finally:
    BH.STATIC_DIR, BH.APP_DIR = _o_static, _o_app

# 门禁对 layer 字段的交叉校验：造两个假清单，一个自洽一个不自洽。
# ⚠️ `CH.check_one` 会往它自己的 PASS/FAIL 全局计数里写，也会往 stdout 打一堆行 ——
#    这里必须**隔离**：先快照再恢复，并把它的输出吞掉（否则污染本测试的统计与可读性）。
_m_dir = Path(_shared) / "m"
_m_dir.mkdir(parents=True, exist_ok=True)
_zip = _m_dir / "patch-1.zip"
with zipfile.ZipFile(_zip, "w") as z:
    z.writestr("py/m.py", b"X=1\n")
_raw = json.dumps({"format": 1, "base_version": "9.9.9", "serial": 1, "issued_at": 0,
                   "cumulative": True, "layer": "web",
                   "zip": {"name": "patch-1.zip",
                           "sha256": hashlib.sha256(_zip.read_bytes()).hexdigest(),
                           "size": _zip.stat().st_size},
                   "files": [{"path": "py/m.py",
                              "sha256": hashlib.sha256(b"X=1\n").hexdigest(),
                              "size": 4}],
                   "rollout_percent": 100, "min_safe_serial": 1,
                   "revoked_serials": []}, sort_keys=True,
                  separators=(",", ":"), ensure_ascii=False).encode("utf-8")
(_m_dir / "patch-1.json").write_text(json.dumps(
    {"v": 1, "manifest_b64": base64.b64encode(_raw).decode(), "sig": ""}), encoding="utf-8")
(_m_dir / "latest").write_text(json.dumps({"base": "9.9.9", "serial": 1}), encoding="utf-8")
import contextlib                                                   # noqa: E402
import io                                                           # noqa: E402
_gate = io.StringIO()
_p0, _f0 = CH.PASS, CH.FAIL
with contextlib.redirect_stdout(_gate):
    CH.check_one(_m_dir, 1, Path("/nonexistent-key.pem"))
gate_out, gate_fail = _gate.getvalue(), CH.FAIL - _f0
CH.PASS, CH.FAIL = _p0, _f0                                          # 恢复，不污染本测试统计
check("K7 ★ 门禁能抓出 layer 与文件前缀不一致（假 web 实 py）", gate_fail > 0,
      f"{gate_fail} 条 FAIL")
check("K8 门禁同时报出「本包含 py 层」提示", "本包含 py 层" in gate_out)

print("=== M. ★ boot-ok 不许伪造 py 层的启动确认（真机实测抓到的坑）===")
# 真机现象：壳的重启被系统推迟后，前端 polling 触发了页面 reload → 页面重跑 boot() →
# 无条件上报 boot-ok ⇒ pending 被清、安全模式（坏补丁自动回滚）形同虚设。
# 真机日志里进程 PID 没变，却打印"新进程已完成启动确认"。
reset_env()
STATE["files"] = {"py/hu_probe_mod.py": PY["py/hu_probe_mod.py"]}
HU.check()
HU.apply_available()
st = HU.status()
check("M1 纯 py 层补丁应用后 pending 置位", st["pending_serial"] == 1)
check("M2 状态里记下了应用补丁的进程号", int(HU.load_state().get("applied_pid") or 0) == os.getpid())
r = HU.boot_ok()
st = HU.status()
check("M3 ★ 同进程的 boot-ok 不清 py 层的 pending", st["pending_serial"] == 1, str(r))
check("M4 ★ 此时 restart_pending 仍在（还没真重启）", st["restart_pending"] is True)
# 模拟"真·新进程"：把 applied_pid 改成别的进程号（等价于 os.getpid() 变了）
d = HU.load_state()
d["applied_pid"] = int(os.getpid()) + 12345
HU.save_state(d)
HU.boot_ok()
st = HU.status()
check("M5 ★ 新进程的 boot-ok 才清 pending", st["pending_serial"] == 0)
check("M6 并清掉 restart_pending", st["restart_pending"] is False)
# web 层补丁不受影响：它的生效方式本来就是 reload，同进程确认是正确语义
reset_env()
STATE["files"] = dict(WEB)
HU.check()
HU.apply_available()
HU.boot_ok()
check("M7 web 层补丁仍由同进程 boot-ok 确认（不该被这条修复误伤）",
      HU.status()["pending_serial"] == 0 and HU.status()["reload_pending"] is False)

print("=== L. ★ 运行版本三元组（用户报障时的唯一凭据）===")# 热更不改版本号 ⇒ "用户在跑哪份代码" 只能靠 base + hot序列 + 清单指纹 合起来说清。
# 用户 2026-09-25 明确提的就是这个需求，所以它必须有测试。
reset_env()
rid = HU.running_id()
check("L1 无补丁时报 base 且 hot_serial=0",
      rid["base_version"] == cfg.APP_VERSION and rid["hot_serial"] == 0,
      str(rid.get("id")))
check("L2 无补丁时**只显示底座版本**（不显示 _hot0）",
      rid["display_version"] == cfg.APP_VERSION and "_hot" not in rid["display_version"],
      rid["display_version"])
STATE["files"] = {**WEB, **PY}
HU.check()
HU.apply_available()
HU.boot_ok()
rid = HU.running_id()
st = HU.status()
check("L3 打过补丁后 hot_serial 与状态一致", rid["hot_serial"] == st["applied_serial"] == 1)
check("L4 patch_hash 与状态一致且为 16 位",
      rid["patch_hash"] == st["applied_hash"] and len(rid["patch_hash"]) == 16,
      rid["patch_hash"])
# ★ 展示口径（用户 2026-09-25 拍板）：0.9.0_hot1
check("L5 ★ 展示版本是 0.9.0_hot1 形态（base_hot<序号>）",
      rid["display_version"] == f"{cfg.APP_VERSION}_hot1", rid["display_version"])
check("L6 可粘贴的 id 里带指纹（两个用户都报 hot1 时靠它区分）",
      rid["id"].startswith(rid["display_version"]) and rid["patch_hash"] in rid["id"],
      rid["id"])
check("L7 layer 如实报 web+py", rid["layer"] == "web+py", rid["layer"])
# /config 必须把三元组带给前端（设置页就靠它渲染与复制）
import routes_config as RC                                          # noqa: E402


class _CfgH:
    def __init__(self):
        self.out = None

    def _json(self, d, code=200):
        self.out = d


_h = _CfgH()
RC.get_config(_h)
check("L7 ★ GET /config 返回 running 三元组",
      isinstance(_h.out, dict) and isinstance(_h.out.get("running"), dict)
      and _h.out["running"].get("base_version") == cfg.APP_VERSION,
      str((_h.out or {}).get("running")))
check("L8 /config 的 running 与 running_id() 同源",
      _h.out["running"].get("hot_serial") == rid["hot_serial"])

HU.reset_for_test()
srv.shutdown()
shutil.rmtree(_shared, ignore_errors=True)
shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
