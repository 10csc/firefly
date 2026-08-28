# -*- coding: utf-8 -*-
"""自动更新路由（routes.py 拆分产物，纯重构无行为变化）：双源检测 + 加固下载。"""

import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from modules import app_config as cfg

from routes_common import _read_json, _is_server


# ══ 自动更新 ════════════════════════════════════
# 规范（见 docs/版本更新规范.md）：
# - 检测源双源：GitHub 优先（语义严格），失败降级 Gitee
# - 下载 URL 固定 Gitee 优先（国内用户下载快），GitHub 降级——检测与下载解耦
# - 资产名固定 firefly-setup.exe / firefly.apk（按扩展名匹配，不依赖版本号，跳版本天然兼容）
# - 版本号只认 x.y.z 纯数字；前后端版本对比统一以 APP_VERSION 为权威
_UPDATE_SOURCES = (
    ("https://api.github.com/repos/10csc/firefly/releases/latest",
     "https://github.com/10csc/firefly/releases"),
    ("https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest",
     "https://gitee.com/cpt-asymmetry/firefly/releases"),
)


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


def get_latest_release():
    """返回 (tag, html_url)。检测源：GitHub 优先，Gitee 降级。"""
    for api, html in _UPDATE_SOURCES:
        try:
            data = _fetch_json(api)
            tag = str(data.get("tag_name") or "").lstrip("v")
            if tag:
                return tag, html
        except Exception:
            continue
    return None


def _get_asset_url(kind: str) -> str:
    """按下载源顺序找资产 URL（Gitee 优先，GitHub 降级）。"""
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


def check_update(h):
    info = get_latest_release()
    if not info:
        h._json({"ok": False, "error": "检查失败（网络或仓库不可达）"})
        return
    tag, html = info
    h._json({
        "ok": True, "tag": tag, "current": cfg.APP_VERSION,
        "html_url": html,
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
    try:
        import tempfile
        local = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Firefly/" + cfg.APP_VERSION})
            max_size = _DOWNLOAD_MAX_BYTES[kind]
            with urllib.request.urlopen(req, timeout=600) as resp, tempfile.NamedTemporaryFile(
                    suffix=".apk" if kind == "apk" else ".exe", delete=False, dir=tempfile.gettempdir()) as out:
                local = out.name
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
            h._json({"ok": True, "path": local, "installing": True})
            return
        h._json({"ok": True, "path": local})
    except Exception as e:
        h._json({"ok": False, "error": f"下载失败: {e}"})
