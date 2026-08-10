# -*- coding: utf-8 -*-
"""自动更新模块严格测试（routes.py 的 check_update/update_download/_fetch_json/_match_asset/_get_asset_url）

铁律：正常路径(≥3例,断言具体内容) + 边界 + 错误路径 + 数据一致性。
只 mock 真实外部依赖（网络 urllib.request.urlopen）。
"""
import sys, os, json, io, re, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import routes
from modules import app_config as cfg

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")

# ── Mock 外部依赖：网络（urllib.request.urlopen）─────────────────
class FakeResp:
    def __init__(self, data: bytes): self._data = data
    def read(self): return self._data
    def __enter__(self): return self
    def __exit__(self, *a): return False

def fake_urlopen(responses: dict, default=None):
    """responses: {url_substring: bytes 或 Exception 实例}"""
    def _open(req, timeout=15.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for key, val in responses.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return FakeResp(val if isinstance(val, bytes) else json.dumps(val).encode("utf-8"))
        if default is not None:
            if isinstance(default, Exception):
                raise default
            return FakeResp(default if isinstance(default, bytes) else json.dumps(default).encode("utf-8"))
        raise FileNotFoundError(f"unexpected url: {url}")
    return _open

def build_release(tag="v0.7.0", assets=None):
    return {"tag_name": tag, "assets": assets or []}

# ══════════════════════════════════════════════════
print("=== A. _fetch_json ===")

# A1 正常：解析 JSON 返回 dict（断言具体字段值）
import urllib.request
_orig_open = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen({"api.github.com": {"tag_name": "v0.8.0", "assets": []}})
try:
    d = routes._fetch_json("https://api.github.com/repos/x/releases/latest")
    check("A1 正常：解析出 tag_name=v0.8.0", d.get("tag_name") == "v0.8.0")
finally:
    urllib.request.urlopen = _orig_open

# A2 正常：UTF-8 中文内容正确解码
urllib.request.urlopen = fake_urlopen({"gitee.com": json.dumps({"name": "流萤 v0.8.0 发布"}, ensure_ascii=False).encode("utf-8")})
try:
    d = routes._fetch_json("https://gitee.com/api/v5/repos/x/releases/latest")
    check("A2 中文内容解码正确", d.get("name") == "流萤 v0.8.0 发布")
finally:
    urllib.request.urlopen = _orig_open

# A3 正常：User-Agent 携带版本号
captured = {}
def _open_capture(req, timeout=15.0):
    captured["ua"] = req.get_header("User-agent")
    return FakeResp(b'{"tag_name":"v0.9.0"}')
urllib.request.urlopen = _open_capture
try:
    routes._fetch_json("https://api.github.com/x")
    check("A3 User-Agent 携带 Firefly/{APP_VERSION}", captured.get("ua") == f"Firefly/{cfg.APP_VERSION}")
finally:
    urllib.request.urlopen = _orig_open

# A4 边界：空响应体 → json.JSONDecodeError 抛出（调用方捕获）
urllib.request.urlopen = fake_urlopen({"x": b""})
try:
    routes._fetch_json("https://x")
    check("A4 空响应→抛异常", False)
except (json.JSONDecodeError, ValueError):
    check("A4 空响应→抛异常", True)
finally:
    urllib.request.urlopen = _orig_open

# A5 边界：非法 JSON → 抛异常（防调用方静默吞垃圾）
urllib.request.urlopen = fake_urlopen({"x": b"{broken"})
try:
    routes._fetch_json("https://x")
    check("A5 非法 JSON→抛异常", False)
except (json.JSONDecodeError, ValueError):
    check("A5 非法 JSON→抛异常", True)
finally:
    urllib.request.urlopen = _orig_open

# A6 错误路径：网络错误（HTTPError）向上抛（调用方 get_latest_release 捕获降级）
urllib.request.urlopen = fake_urlopen({"x": urllib.error.HTTPError("https://x", 500, "err", None, None)})
try:
    routes._fetch_json("https://x")
    check("A6 HTTPError→向上抛", False)
except urllib.error.HTTPError:
    check("A6 HTTPError→向上抛", True)
finally:
    urllib.request.urlopen = _orig_open

# ══════════════════════════════════════════════════
print("=== B. _match_asset ===")

# B1 正常：exe 资产命中（断言返回具体 URL）
assets = [{"name": "firefly-setup.exe", "browser_download_url": "https://gitee.com/x/download/firefly-setup.exe"}]
check("B1 exe 命中返回完整 URL", routes._match_asset(assets, re.compile(r"\.exe$", re.I)) ==
      "https://gitee.com/x/download/firefly-setup.exe")

# B2 正常：apk 命中
assets2 = [{"name": "firefly.apk", "browser_download_url": "https://gitee.com/x/download/firefly.apk"}]
check("B2 apk 命中", routes._match_asset(assets2, re.compile(r"\.apk$", re.I)) ==
      "https://gitee.com/x/download/firefly.apk")

# B3 正常：大小写不敏感（.EXE / .Apk）
check("B3 .EXE 大写命中", routes._match_asset([{"name": "FIREFLY.EXE", "browser_download_url": "u1"}],
                                              re.compile(r"\.exe$", re.I)) == "u1")
check("B3 .Apk 混合大小写命中", routes._match_asset([{"name": "firefly.Apk", "browser_download_url": "u2"}],
                                                   re.compile(r"\.apk$", re.I)) == "u2")

# B4 边界：assets 为空 → 返回空串
check("B4 空 assets→''", routes._match_asset([], re.compile(r"\.exe$", re.I)) == "")
check("B4 None assets→''", routes._match_asset(None, re.compile(r"\.exe$", re.I)) == "")

# B5 边界：无匹配（zip/tar.gz 源码包不命中 exe/apk）
mixed = [{"name": "v0.7.0.zip", "browser_download_url": "z"},
         {"name": "v0.7.0.tar.gz", "browser_download_url": "g"}]
check("B5 zip/tar.gz 不命中 exe", routes._match_asset(mixed, re.compile(r"\.exe$", re.I)) == "")
check("B5 zip/tar.gz 不命中 apk", routes._match_asset(mixed, re.compile(r"\.apk$", re.I)) == "")

# B6 边界：只有 name 无 browser_download_url → 返回 name
check("B6 无 download_url→返回 name", routes._match_asset([{"name": "firefly.apk"}],
                                                         re.compile(r"\.apk$", re.I)) == "firefly.apk")

# B7 边界：重复资产（多个 exe）→ 返回第一个
dup = [{"name": "a.exe", "browser_download_url": "first"},
       {"name": "b.exe", "browser_download_url": "second"}]
check("B7 重复 exe→取第一个", routes._match_asset(dup, re.compile(r"\.exe$", re.I)) == "first")

# B8 边界：URL 里带 .exe 但 name 没有 → 仍命中（按 URL 兜底）
url_only = [{"name": "installer", "browser_download_url": "https://x/firefly-setup.exe"}]
check("B8 URL 含 .exe→命中", routes._match_asset(url_only, re.compile(r"\.exe$", re.I)) ==
      "https://x/firefly-setup.exe")

# B9 错误路径：资产字段非 dict（脏数据）→ 不崩溃
check("B9 脏数据不崩溃", routes._match_asset([None, "str", 123], re.compile(r"\.exe$", re.I)) == "")

# ══════════════════════════════════════════════════
print("=== C. get_latest_release（双源检测）===")

GH = "api.github.com"; GT = "gitee.com"
rel_gh = build_release("v0.8.0")
rel_gt = build_release("v0.7.5")

# C1 正常：GitHub 成功 → 返回 GitHub tag + html
urllib.request.urlopen = fake_urlopen({GH: rel_gh, GT: rel_gt})
try:
    r = routes.get_latest_release()
    check("C1 GitHub 优先返回 v0.8.0", r == ("0.8.0", "https://github.com/10csc/firefly/releases"))
finally:
    urllib.request.urlopen = _orig_open

# C2 正常：GitHub 失败 → 降级 Gitee
urllib.request.urlopen = fake_urlopen({GH: urllib.error.HTTPError("", 403, "", None, None), GT: rel_gt})
try:
    r = routes.get_latest_release()
    check("C2 GitHub 失败→降级 Gitee 0.7.5", r == ("0.7.5", "https://gitee.com/cpt-asymmetry/firefly/releases"))
finally:
    urllib.request.urlopen = _orig_open

# C3 边界：tag 带 v 前缀 → 去 v
urllib.request.urlopen = fake_urlopen({GH: build_release("v0.8.0"), GT: build_release("0.7.5")})
try:
    r = routes.get_latest_release()
    check("C3 v 前缀去除：v0.8.0→0.8.0", r == ("0.8.0", "https://github.com/10csc/firefly/releases"))
finally:
    urllib.request.urlopen = _orig_open

# C4 边界：tag 为空/None → 跳过该源
urllib.request.urlopen = fake_urlopen({GH: build_release(""), GT: rel_gt})
try:
    r = routes.get_latest_release()
    check("C4 空 tag 跳过 GitHub→Gitee", r == ("0.7.5", "https://gitee.com/cpt-asymmetry/firefly/releases"))
finally:
    urllib.request.urlopen = _orig_open

# C5 错误路径：双源都失败 → None
urllib.request.urlopen = fake_urlopen({GH: urllib.error.HTTPError("", 500, "", None, None),
                                       GT: urllib.error.URLError("net")})
try:
    check("C5 双源失败→None", routes.get_latest_release() is None)
finally:
    urllib.request.urlopen = _orig_open

# C6 数据一致性：检测不修改任何全局状态（幂等无副作用）
import copy
snap = dict(cfg.config)
urllib.request.urlopen = fake_urlopen({GH: rel_gh, GT: rel_gt})
try:
    routes.get_latest_release(); routes.get_latest_release()
    check("C6 检测无副作用", dict(cfg.config) == snap)
finally:
    urllib.request.urlopen = _orig_open

# ══════════════════════════════════════════════════
print("=== D. _get_asset_url（下载源：Gitee 优先）===")

# D1 正常：Gitee 有 exe → 返回 Gitee URL（即使 GitHub 也有，Gitee 优先）
gh_assets = [{"name": "firefly-setup.exe", "browser_download_url": "https://github.com/dl/setup.exe"}]
gt_assets = [{"name": "firefly-setup.exe", "browser_download_url": "https://gitee.com/dl/setup.exe"}]
urllib.request.urlopen = fake_urlopen({GH: build_release("v0.8.0", gh_assets),
                                       GT: build_release("v0.8.0", gt_assets)})
try:
    r = routes._get_asset_url("exe")
    check("D1 Gitee 优先返回 Gitee URL", r == "https://gitee.com/dl/setup.exe")
finally:
    urllib.request.urlopen = _orig_open

# D2 正常：Gitee 无 exe 但 GitHub 有 → 降级 GitHub
urllib.request.urlopen = fake_urlopen({GH: build_release("v0.8.0", gh_assets),
                                       GT: build_release("v0.8.0", [])})
try:
    r = routes._get_asset_url("exe")
    check("D2 Gitee 缺资产→GitHub 降级", r == "https://github.com/dl/setup.exe")
finally:
    urllib.request.urlopen = _orig_open

# D3 正常：apk 匹配
gt_apk = [{"name": "firefly.apk", "browser_download_url": "https://gitee.com/dl/firefly.apk"}]
urllib.request.urlopen = fake_urlopen({GH: build_release("v0.8.0", []), GT: build_release("v0.8.0", gt_apk)})
try:
    r = routes._get_asset_url("apk")
    check("D3 apk 命中 Gitee URL", r == "https://gitee.com/dl/firefly.apk")
finally:
    urllib.request.urlopen = _orig_open

# D4 错误路径：双源都失败 → 空串
urllib.request.urlopen = fake_urlopen({GH: urllib.error.HTTPError("", 500, "", None, None),
                                       GT: urllib.error.URLError("net")})
try:
    check("D4 双源失败→''", routes._get_asset_url("exe") == "")
finally:
    urllib.request.urlopen = _orig_open

# D5 边界：双源都有资产但都无 exe（只有 apk）→ 空串
only_apk = [{"name": "firefly.apk", "browser_download_url": "a"}]
urllib.request.urlopen = fake_urlopen({GH: build_release("v0.8.0", only_apk),
                                       GT: build_release("v0.8.0", only_apk)})
try:
    check("D5 无 exe→''", routes._get_asset_url("exe") == "")
finally:
    urllib.request.urlopen = _orig_open

# ══════════════════════════════════════════════════
print("=== E. check_update ===")

class FakeH:
    """模拟 HTTP handler：_json 捕获输出；headers/rfile 供 _read_json。"""
    class _Hdr:
        def get(self, k, d=None):
            if k == "Content-Length":
                return str(len(self._raw or b""))
            return d
        def set_raw(self, raw): self._raw = raw
    def __init__(self, body=None):
        self.out = None
        self.headers = self._Hdr()
        raw = json.dumps(body).encode("utf-8") if isinstance(body, dict) else (
            body.encode("utf-8") if isinstance(body, str) else (body or b""))
        self.headers.set_raw(raw)
        class _R:
            def __init__(self, b): self._b = b
            def read(self, n): return self._b[:n]
        self.rfile = _R(raw)
    def _json(self, d): self.out = d

# E1 正常：返回 ok/tag/current/html_url（断言具体值）
urllib.request.urlopen = fake_urlopen({GH: build_release("v0.9.0")})
try:
    h = FakeH(); routes.check_update(h)
    check("E1 ok=True tag=0.9.0", h.out == {"ok": True, "tag": "0.9.0",
          "current": cfg.APP_VERSION, "html_url": "https://github.com/10csc/firefly/releases"})
finally:
    urllib.request.urlopen = _orig_open

# E2 错误路径：检测失败 → ok=False + 明确错误信息
urllib.request.urlopen = fake_urlopen({GH: urllib.error.HTTPError("", 500, "", None, None),
                                       GT: urllib.error.URLError("net")})
try:
    h = FakeH(); routes.check_update(h)
    check("E2 检测失败→ok=False 错误信息正确",
          h.out == {"ok": False, "error": "检查失败（网络或仓库不可达）"})
finally:
    urllib.request.urlopen = _orig_open

# E3 数据一致性：current 恒等于 APP_VERSION（防版本号失步）
urllib.request.urlopen = fake_urlopen({GH: build_release("v0.8.0")})
try:
    h = FakeH(); routes.check_update(h)
    check("E3 current==APP_VERSION", h.out["current"] == cfg.APP_VERSION)
finally:
    urllib.request.urlopen = _orig_open

# ══════════════════════════════════════════════════
print("=== F. update_download ===")

# F1 正常（apk）：下载成功 → ok=True + path 存在 + 内容正确
gt_apk2 = [{"name": "firefly.apk", "browser_download_url": "https://gitee.com/dl/firefly.apk"}]
apk_bytes = b"APK-BINARY-CONTENT-1234567890"
class _FakeRespBytes(FakeResp):
    def __init__(self, data):
        super().__init__(data)
        self._chunks = [data[:10], data[10:20], data[20:]] if len(data) > 20 else [data]
        self._i = 0
    def read(self, n=None):
        if self._i >= len(self._chunks): return b""
        c = self._chunks[self._i]; self._i += 1
        return c

def _open_update(req, timeout=600.0):
    """区分 API 与下载：API 返回 release JSON，下载 URL 返回二进制。"""
    url = req.full_url
    if "/releases/latest" in url and "gitee.com" in url:
        return FakeResp(json.dumps(build_release("v0.8.0", gt_apk2)).encode("utf-8"))
    if "/releases/latest" in url and "github.com" in url:
        return FakeResp(json.dumps(build_release("v0.8.0", [])).encode("utf-8"))
    if "gitee.com/dl/firefly.apk" in url:
        return _FakeRespBytes(apk_bytes)
    if "gitee.com/dl/setup.exe" in url:
        return FakeResp(b"SETUP-EXE")
    raise FileNotFoundError(url)

urllib.request.urlopen = _open_update
try:
    h = FakeH({"kind": "apk"})
    routes.update_download(h)
    ok = h.out.get("ok") and h.out.get("path")
    data_ok = False
    if ok:
        with open(h.out["path"], "rb") as f:
            data_ok = f.read() == apk_bytes
    check("F1 apk 下载成功且内容完整", data_ok)
    if ok:
        os.unlink(h.out["path"])
finally:
    urllib.request.urlopen = _orig_open

# F2 错误路径：资产 URL 获取失败 → ok=False 明确错误
urllib.request.urlopen = fake_urlopen({GH: urllib.error.HTTPError("", 500, "", None, None),
                                       GT: urllib.error.URLError("net")})
try:
    h = FakeH()
    h._body = {"kind": "exe"}
    routes.update_download(h)
    check("F2 无资产→ok=False 明确错误",
          h.out == {"ok": False, "error": "发行版未附安装包资产或仓库不可达"})
finally:
    urllib.request.urlopen = _orig_open

# F3 错误路径：下载中途网络异常 → ok=False "下载失败: ..."
def _open_boom(req, timeout=600.0):
    url = req.full_url
    if "/releases/latest" in url:
        return FakeResp(json.dumps(build_release("v0.8.0", gt_apk2)).encode("utf-8"))
    if "gitee.com/dl/firefly.apk" in url:
        raise urllib.error.URLError("connection reset")
    raise FileNotFoundError(url)
urllib.request.urlopen = _open_boom
try:
    h = FakeH({"kind": "apk"})
    routes.update_download(h)
    check("F3 下载异常→ok=False 含'下载失败'",
          h.out.get("ok") is False and "下载失败" in h.out.get("error", ""))
finally:
    urllib.request.urlopen = _orig_open

# F4 边界：kind 非法值（非 exe/apk）→ 按 apk 匹配（实现行为：非 exe 均走 apk 分支）
urllib.request.urlopen = _open_update
try:
    h = FakeH({"kind": "weird"})
    routes.update_download(h)
    check("F4 非法 kind→按 apk 处理（有下载行为）", h.out.get("ok") is True)
    if h.out.get("ok") and h.out.get("path"):
        os.unlink(h.out["path"])
finally:
    urllib.request.urlopen = _orig_open

# F5 数据一致性：exe 非 frozen 环境不触发安装器（getattr(sys,'frozen')=False）
def _open_exe_update(req, timeout=600.0):
    url = req.full_url
    if "/releases/latest" in url and "gitee.com" in url:
        return FakeResp(json.dumps(build_release("v0.8.0",
            [{"name": "firefly-setup.exe", "browser_download_url": "https://gitee.com/dl/setup.exe"}])).encode("utf-8"))
    if "/releases/latest" in url and "github.com" in url:
        return FakeResp(json.dumps(build_release("v0.8.0", [])).encode("utf-8"))
    if "gitee.com/dl/setup.exe" in url:
        return FakeResp(b"SETUP-EXE")
    raise FileNotFoundError(url)
urllib.request.urlopen = _open_exe_update
try:
    h = FakeH()
    routes.update_download(h)
    check("F5 非 frozen→不 installing（无安装器启动）", h.out.get("installing") is None)
    if h.out.get("ok") and h.out.get("path"):
        os.unlink(h.out["path"])
finally:
    urllib.request.urlopen = _orig_open

print(f"\n=== 自动更新测试: PASS={PASS} FAIL={FAIL} ===")
sys.exit(1 if FAIL else 0)
