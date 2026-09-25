# -*- coding: utf-8 -*-
"""自动更新路由（routes.py 拆分产物）：**服务器主导**的版本检测 + 加固下载。

★ 2026-09-24 改造（用户口径："APP 内的检查更新直接连服务器走 Gitee 下载通道，
本身 GitHub 就不稳定，版本管理由服务器处理"）：

旧实现让客户端**自己**问 GitHub/Gitee 的 `releases/latest`，有两个硬伤：
  1. **GitHub 国内不稳** —— 用户侧超时/被墙，检测时好时坏；
  2. **"成功即返回"吞掉更新** —— 双源顺序是 GitHub 优先，GitHub 成功返回
     （哪怕它停留在旧版）就直接 return，**Gitee 上更新的版本被静默丢弃**。
     实测：Gitee 有 v0.9.0、GitHub 忘发（只有 v0.8.1）→ 0.8.1 客户端算出
     "0.8.1 == 0.8.1" → 永远显示「已是最新版本」。
     "降级"只处理网络失败，**不处理源过期**——这是旧设计的根本缺陷。

新实现：客户端只问**我们自己的服务器** `GET /update-manifest`，
服务器统一管理版本号与各平台下载直链（走 Gitee 下载通道），
客户端按平台挑链接、经 `/update-download` 下载（本地版）或跳下载页（安卓）。

降级链保持不变（宁可慢，不可断）：
  /update-manifest（服务器）→ GitHub/Gitee 双源取**版本最高**（最后兜底）
"""

import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from modules import app_config as cfg

from routes_common import _read_json, _is_server


# ══ 自动更新 ════════════════════════════════════
# 规范（见 docs/版本更新规范.md）：
# - **主通道**：服务器 `/update-manifest`（版本管理由服务器负责，出网这一跳在服务器）
# - 兜底通道：GitHub/Gitee `releases/latest`（服务器不可达时才用；只作最后兜底）
# - 下载 URL 固定 Gitee 优先（国内用户下载快），GitHub 降级——检测与下载解耦
# - 资产名固定 firefly-setup.exe / firefly.apk（按扩展名匹配，不依赖版本号，跳版本天然兼容）
# - 版本号只认 x.y.z 纯数字；前后端版本对比统一以 APP_VERSION 为权威
_UPDATE_SOURCES = (
    ("https://api.github.com/repos/10csc/firefly/releases/latest",
     "https://github.com/10csc/firefly/releases"),
    ("https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest",
     "https://gitee.com/cpt-asymmetry/firefly/releases"),
)


# ── 服务器清单（主通道）─────────────────────────
# 服务器地址与认证服务器同源（服务器版就是本进程所在机器；本地版即认证服务器地址）。
_SERVER_MANIFEST_TIMEOUT = 6.0


def _server_base() -> str:
    """服务器基址。服务器版：本进程即服务器，直连 127.0.0.1；
    本地版：走认证服务器地址（与 /time、登录同源 —— 见 routes_auth._auth_server_base）。"""
    if _is_server():
        return "http://127.0.0.1:%d" % cfg.PORT
    try:
        from routes_auth import _auth_server_base
        base = str(_auth_server_base() or "").strip().rstrip("/")
        if base.startswith(("http://", "https://")):
            return base
    except Exception as e:
        _log("取认证服务器地址失败: %s", e)
    return ""


def _valid_tag(tag: str) -> bool:
    """x.y.z 纯数字（与 tools/check_version.py 同口径）。"""
    import re
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", str(tag or "")))


def _log(fmt: str, *args):
    import logging
    logging.getLogger(__name__).info(fmt, *args)


def fetch_manifest() -> dict:
    """向服务器要版本清单（主通道）。返回已校验的 dict；不可用返回 {}（调用方降级）。

    清单形状（服务器侧由 server/update_manifest.py 保证）：
        {schema, tag, channel, released_at, min_supported, notes_url,
         assets: {apk: {url, sha256, size}, exe: {...}}, download_page}
    """
    base = _server_base()
    if not base:
        return {}
    url = base + "/update-manifest"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Firefly/" + cfg.APP_VERSION, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_SERVER_MANIFEST_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        _log("服务器清单不可用（降级双源）: %s", e)
        return {}
    if not isinstance(data, dict) or not data.get("ok"):
        _log("服务器清单 ok=false: %r", str(data)[:160])
        return {}
    tag = str(data.get("tag") or "").strip().lstrip("v")
    if not _valid_tag(tag):
        _log("服务器清单 tag 非法: %r", data.get("tag"))
        return {}
    return data


# 下载源顺序：Gitee 资产优先（国内直连快），GitHub 降级
_DOWNLOAD_SOURCES = (
    "https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest",
    "https://api.github.com/repos/10csc/firefly/releases/latest",
)


# ── 下载加固（轻量：无 sha256 链路，防 URL 投毒与无节制下载）──
_DOWNLOAD_KINDS = ("exe", "apk")


_DOWNLOAD_MAX_BYTES = {"exe": 200 * 1024 * 1024, "apk": 100 * 1024 * 1024}


_DOWNLOAD_MIN_BYTES = 100 * 1024          # 防错误页 HTML 冒充资产


# 域名白名单：Gitee/GitHub 资产域。校验初始 URL 的 host；
# urllib 自动跟随 GitHub 官方重定向（objects.githubusercontent.com），重定向链信任官方域。
_DOWNLOAD_HOSTS = ("gitee.com", "github.com")

# ★ 下载落盘的**固定文件名**（2026-09-25）：安卓壳要把这个文件交给系统安装器，
#   而安卓侧拿到的是一个**路径字符串** —— 随机文件名没法让 Kotlin 侧安全定位文件
#   （让它自己解析路径 = 把路径拼接口子开到壳里）。固定名 = 两边都写死的契约。
#   `tools/check_android_install_contract.py` 会核对 Kotlin 侧同名常量，防两边漂移。
#   · PC 侧不受影响：exe 走静默安装，路径由后端自己 Popen，前端只拿回显。
_DOWNLOAD_FILENAMES = {"apk": "firefly-update.apk", "exe": "firefly-update.exe"}


def _validate_download_url(url: str, kind: str) -> str:
    """下载 URL 审查：https + 域名白名单 + 扩展名与 kind 一致。返回错误文案（""=通过）。"""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https":
        return "下载地址必须为 https"
    if not any(host == h or host.endswith("." + h) for h in _DOWNLOAD_HOSTS):
        return "下载地址域名不在白名单"
    suffix = ".apk" if kind == "apk" else ".exe"
    if not p.path.lower().endswith(suffix):
        return "下载地址与资产类型不符"
    return ""


def _fetch_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Firefly/" + cfg.APP_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _match_asset(assets, pattern):
    for a in assets or []:
        # 审查：非 dict 资产项直接跳过（防 API 脏数据导致 AttributeError 崩掉检测链）
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        # 匹配 name 或 URL 任一（防资产 name 不带扩展名但 URL 是 .exe/.apk 的漏检）
        if pattern.search(name) or pattern.search(url):
            return url or name
    return ""


def _pick_best(candidates: list):
    """从多源候选里挑**版本最高**的一个，返回 (tag, html_url)。全失败返回 None。

    ★ 这是旧实现最关键的修复点。旧代码 `for src in sources: ... if tag: return`
    ——**先成功者胜**，与版本无关。若排在前面的源停留在旧版（实测 GitHub 只有
    v0.8.1、Gitee 已有 v0.9.0），更新的版本永远看不到，客户端显示"已是最新"。
    新版改为**遍历全部源、按版本号取最大** ⇒ "降级"同时覆盖
    「网络失败」与「源过期」两种情况。
    """
    best = None   # (v_tuple, tag, html)
    for tag, html in candidates:
        if not _valid_tag(tag):
            continue
        v = tuple(int(x) for x in tag.split("."))
        if best is None or v > best[0]:
            best = (v, tag, html)
    if best is None:
        return None
    return best[1], best[2]


def get_latest_release():
    """返回 (tag, html_url)。

    主通道：服务器 `/update-manifest`（版本管理由服务器负责）。
    兜底：GitHub/Gitee 双源里**版本最高**的（不再"先成功者胜"）。
    """
    m = fetch_manifest()
    if m:
        tag = str(m["tag"]).lstrip("v")
        notes = str(m.get("notes_url") or "")
        if not notes:
            notes = _UPDATE_SOURCES[1][1]   # Gitee releases 页
        return tag, notes
    cands = []
    for api, html in _UPDATE_SOURCES:
        try:
            data = _fetch_json(api)
            tag = str(data.get("tag_name") or "").lstrip("v")
            if tag:
                cands.append((tag, html))
        except Exception:
            continue
    return _pick_best(cands)


def _get_asset_url(kind: str) -> str:
    """找资产 URL。主通道：服务器清单 `assets[kind].url`；
    兜底：Gitee/GitHub 里对应版本的资产（按下载源顺序）。"""
    m = fetch_manifest()
    if m:
        a = (m.get("assets") or {}).get(kind) or {}
        url = str(a.get("url") or "").strip()
        if url:
            return url
        # 清单里该平台 url 为空 = 本版本未随发；落到远端找（可能远端有旧资产）
    import re
    pat = re.compile(r"\.exe$", re.I) if kind == "exe" else re.compile(r"\.apk$", re.I)
    for api in _DOWNLOAD_SOURCES:
        try:
            data = _fetch_json(api)
            url = _match_asset(data.get("assets"), pat)
            if url:
                return url
        except Exception:
            continue
    return ""


def _manifest_expect(kind: str) -> tuple:
    """从服务器清单取 (sha256, size) 作为下载后的校验期望；取不到返回 ("", 0)。"""
    m = fetch_manifest()
    if not m:
        return "", 0
    a = (m.get("assets") or {}).get(kind) or {}
    sha = str(a.get("sha256") or "").strip().lower()
    try:
        size = int(a.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return sha, size


def check_update(h):
    info = get_latest_release()
    if not info:
        h._json({"ok": False, "error": "检查失败（网络或仓库不可达）"})
        return
    tag, html = info
    m = fetch_manifest()
    h._json({
        "ok": True, "tag": tag, "current": cfg.APP_VERSION,
        "html_url": html,
        # 前端展示用：清单带的随发信息（无清单时为空，前端自行降级）
        "notes_url": str((m or {}).get("notes_url") or ""),
        "min_supported": str((m or {}).get("min_supported") or ""),
        "source": "server" if m else "remote",
    })


def update_download(h):
    """下载发行版资产到临时目录（轻量加固：kind 白名单 + https/域名校验 + 大小上限）。
    PC(exe)：下载后由后端静默启动安装器（/VERYSILENT 覆盖安装，保留 user_data），
             服务器随之关闭（安装器接管）；安卓(apk)：仅下载，前端引导系统安装器。
    服务器版禁用：检查更新走 version.json；此端点会把资产下载到服务器磁盘/带宽，
    任何登录用户可反复触发（防磁盘填满与 3Mbps 带宽耗尽）。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版请从下载页获取安装包"}, 403)
        return
    body = _read_json(h)
    kind = body.get("kind", "exe")
    if kind not in _DOWNLOAD_KINDS:
        h._json({"ok": False, "error": "不支持的资产类型"})
        return
    url = _get_asset_url(kind)
    if not url:
        h._json({"ok": False, "error": "发行版未附安装包资产或仓库不可达"})
        return
    err = _validate_download_url(url, kind)
    if err:
        h._json({"ok": False, "error": err})
        return
    # 期望校验值（服务器清单里有就带上，下载完比对；兜底通道取不到则跳过）
    want_sha, want_size = _manifest_expect(kind)
    try:
        import tempfile
        local = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Firefly/" + cfg.APP_VERSION})
            max_size = _DOWNLOAD_MAX_BYTES[kind]
            # 固定文件名（见 _DOWNLOAD_FILENAMES 的说明）：同一个文件反复覆盖，
            # 既让安卓壳能定位，也避免临时目录里堆一串 85MB 的旧包。
            dst = Path(tempfile.gettempdir()) / _DOWNLOAD_FILENAMES[kind]
            with urllib.request.urlopen(req, timeout=600) as resp, open(dst, "wb") as out:
                local = str(dst)
                # Content-Length 预检 + 流式累计兜底（防无/伪造 Content-Length）
                cl = (getattr(resp, "headers", None) or {}).get("Content-Length")
                if cl:
                    try:
                        if int(cl) > max_size:
                            raise ValueError("文件过大")
                    except (TypeError, ValueError):
                        raise
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError("文件过大")
                    out.write(chunk)
            if total < _DOWNLOAD_MIN_BYTES:
                raise ValueError("文件异常过小，疑似错误页面")
            # ★ 清单提供了 sha256 时必须校验：防 CDN 缓存串包/下载被替换/断流截断
            # （"下载完成却装不上"多数就是包不完整）。兜底通道没有校验值，
            # 只做大小下限判断（保持旧行为，不阻塞）。
            if want_sha:
                import hashlib
                h256 = hashlib.sha256()
                with open(local, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h256.update(chunk)
                got = h256.hexdigest()
                if got != want_sha:
                    raise ValueError("安装包校验失败（sha256 不符），请重试或从下载页获取")
            elif want_size and abs(total - want_size) > 1024:
                raise ValueError("安装包大小与清单不符，请重试或从下载页获取")
        except Exception:
            # 下载中途失败：清理残留临时文件（防垃圾堆积）
            if local:
                try:
                    Path(local).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        if kind == "exe" and getattr(sys, "frozen", False):
            # PC 发行版：静默启动安装器（覆盖安装保留 user_data），本服务随之退出
            import subprocess
            subprocess.Popen([local, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"])
            # 优雅关闭自身：请求 /shutdown（保存文件后退出），安装器接管
            import threading as _t
            def _close():
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://127.0.0.1:{cfg.PORT}/shutdown", timeout=2)
                except Exception:
                    pass
            _t.Timer(2.0, _close).start()
            h._json({"ok": True, "path": local, "name": _DOWNLOAD_FILENAMES[kind],
                     "installing": True})
            return
        # `name` 给安卓壳用：它只认 _DOWNLOAD_FILENAMES 里的固定名，不接受任意路径
        h._json({"ok": True, "path": local, "name": _DOWNLOAD_FILENAMES[kind],
                 "verified": bool(want_sha)})
    except Exception as e:
        h._json({"ok": False, "error": f"下载失败: {e}"})
