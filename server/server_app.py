# -*- coding: utf-8 -*-
"""流萤服务器版入口 — 多用户 + 用户自带 Key（本地版 app/ 零改动）

架构：
- 复用 app/modules 全部流水线逻辑 + app/routes 全部路由函数（端点与本地版相同）
- 差异只在请求入口：每请求解析 X-User-Id / X-API-Key / X-API-Base 建立用户上下文
  （app_config 的 contextvars，Flask 同款模式），路由函数与模块内部零感知
- 用户 API Key 只存用户浏览器（localStorage），服务器用后即弃不落盘
- 用户数据按匿名 UUID 隔离：user_data/{uid}/{mode}/（对话/记忆/手账/流水线全隔离）

启动：python server/server_app.py（监听 0.0.0.0:8765，端口用 FIREFLY_PORT 覆盖）
"""

import json, sys, os, logging, re, warnings
# 屏蔽 requests 依赖版本不匹配的警告（不影响功能）
warnings.filterwarnings("ignore", message=".*urllib3.*", module="requests")
warnings.filterwarnings("ignore", message=".*chardet.*", module="requests")
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s', stream=sys.stderr)

# 将 app/（modules、routes）与仓库根（knowledge 等）加入 sys.path
# 注意：本地版 server.py 靠"脚本目录=app/"自动生效；服务器版脚本在 server/，需显式加
_APP_DIR = str(Path(__file__).resolve().parent.parent / "app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from modules import app_config as cfg
import routes
from shared_http import ResponseMixin, setup_stdio_utf8, preload_knowledge, shutdown_server, _SERVER_REF

PORT = int(os.environ.get("FIREFLY_PORT", "8765"))
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
STATIC_DIR = cfg.BASE_DIR / "static"   # 图片等资源复用本地版 app/static（只读）

# X-User-Id 白名单：字母数字下划线连字符，≤64（防路径穿越：user_dir = USER_DIR / uid）
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _host_allowed(host: str) -> bool:
    """Host 头非空校验（HTTP/1.1 必须有 Host）。服务器版数据按 UUID 隔离 +
    Key 在用户浏览器侧（跨域读不到），DNS rebinding 攻击者只能读写自己 UUID 的数据，危害有限。"""
    return bool((host or "").strip())


def _shutdown_allowed(client_addr: str) -> bool:
    """/shutdown 仅限本机来源（运维场景）；公网 403，防任何人远程关闭服务器。"""
    return client_addr in ("127.0.0.1", "::1")


# ── HTTP 服务器 ──────────────────────────────────
class FireflyHandler(ResponseMixin, SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    # ── 用户上下文（每请求建立/恢复）──────────────
    def _setup_user_context(self) -> bool:
        """从请求头建立用户上下文。返回 False = 缺少合法 X-User-Id。"""
        uid = self.headers.get("X-User-Id", "")
        if not _UID_RE.fullmatch(uid):
            return False
        user_dir = cfg.USER_DIR / uid
        user_dir.mkdir(parents=True, exist_ok=True)
        api_key = (self.headers.get("X-API-Key", "") or "").strip()[:128]
        api_base = (self.headers.get("X-API-Base", "") or "").strip()
        if api_base not in (cfg.API_BASE, cfg.GO_BASE):
            api_base = ""
        self._ctx_token = cfg.set_user_context(
            user_dir=user_dir, api_key=api_key or None, api_base=api_base or None)
        return True

    def _finish_user_context(self):
        try:
            cfg.reset_user_context(self._ctx_token)
        except Exception:
            pass

    # ── 请求分发 ──────────────────────────────────
    def do_POST(self):
        if not _host_allowed(self.headers.get("Host", "")):
            self.send_error(403)
            return
        if not self._setup_user_context():
            self.send_error(400, "missing X-User-Id")   # reason phrase 须 latin-1 可编码，不能用中文
            return
        try:
            fn = routes.POST_ROUTES.get(urlparse(self.path).path)
            if fn:
                fn(self)
            else:
                self.send_error(404)
        finally:
            self._finish_user_context()

    def do_GET(self):
        if not _host_allowed(self.headers.get("Host", "")):
            self.send_error(403)
            return
        path = urlparse(self.path).path
        # 路径穿越防护（同本地版）
        if ".." in unquote(path):
            self.send_error(404)
            return
        if path == "/health":
            self._json({"ok": True, "alive": True})
            return
        if path == "/shutdown":
            # 仅本机来源生效（运维用）；公网 403
            if not _shutdown_allowed(self.client_address[0]):
                self.send_error(403)
                return
            self._json({"ok": True, "shutting_down": True})
            import threading as _t
            _t.Timer(0.3, lambda: shutdown_server()).start()
            return
        # 静态资源（页面/样式/图片/version.json）不需要用户上下文
        if path in ("/", "/index.html"):
            self._serve_file(FRONTEND_DIR / "index.html")
            return
        if path == "/app.js":
            self._serve_file(FRONTEND_DIR / "app.js")
            return
        # style.css 不设独立副本：/static/style.css 直接复用本地版 app/static/style.css
        if path == "/version.json":
            self._serve_file(FRONTEND_DIR.parent / "version.json")
            return
        if path.startswith("/static/"):
            self._serve_file(STATIC_DIR / unquote(path[8:]))
            return
        if path.startswith("/assets/"):
            self._serve_file(cfg.resolve_asset("assets/" + unquote(path[8:])))
            return
        fn = routes.GET_ROUTES.get(path)
        if fn:
            if not self._setup_user_context():
                self.send_error(400, "missing X-User-Id")   # reason phrase 须 latin-1 可编码，不能用中文
                return
            try:
                fn(self)
            finally:
                self._finish_user_context()
            return
        # 根目录静态文件（轮播图等，复用本地版 app/static）
        name = unquote(path)
        fp = STATIC_DIR / name.lstrip("/")
        if fp.exists() and fp.is_file():
            self._serve_file(fp)
        else:
            self.send_error(404)

    # ── 响应工具（供 routes 调用）──────────────────
    # _MIME/_serve_file/_json/log_message 来自 shared_http.ResponseMixin
    # /style.css 由 /static/style.css 提供（server/frontend 不保留 style.css 副本）


def main():
    setup_stdio_utf8()
    preload_knowledge()

    # 监听 0.0.0.0：公网可访问（用户自带 Key + UUID 隔离，无需服务器 token）
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), FireflyHandler)
    except OSError as e:
        print(f"  [ERROR] 端口 {PORT} 绑定失败: {e}", flush=True)
        sys.exit(1)
    _SERVER_REF["server"] = server
    print(f"\n  流萤服务器版启动中（0.0.0.0:{PORT}）")
    print(f"  公网访问: http://<公网IP>:{PORT}")
    print(f"  用户自带 API Key（存浏览器 localStorage，服务器不落盘）；数据按匿名 UUID 隔离")
    print(f"  检查更新: 放置 server/version.json（格式: " + '{"tag":"0.7.1","exe":"...","apk":"..."}' + "）\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  关闭服务器")
    finally:
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
