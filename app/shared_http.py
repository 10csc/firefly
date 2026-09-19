# -*- coding: utf-8 -*-
"""两个 HTTP 入口（本地版 app/server.py、服务器版 server/server_app.py）的共享工具

消除入口间复制粘贴：响应工具（_MIME/_serve_file/_json/log_message）、编码兜底、
知识库预加载、优雅关闭。改共享逻辑只需改这里，两个入口零改动感知。
"""

import json, os, sys
from pathlib import Path


class ResponseMixin:
    """HTTP 响应工具 mixin（配 SimpleHTTPRequestHandler 使用，须放在其前继承）。"""

    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css", ".js": "application/javascript",
        ".ttf": "font/ttf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
        ".gif": "image/gif", ".json": "application/json",
    }

    def _serve_file(self, filepath: Path):
        try:
            content = filepath.read_bytes()
            self.send_response(200)
            mime = self._MIME.get(filepath.suffix.lower())
            if mime:
                self.send_header("Content-Type", mime)
            # 前端文件禁止缓存：WebView/浏览器强缓存旧版会导致
            # "改了前端但用户还在跑旧逻辑"的幽灵 bug（版本参数只防 js 缓存，
            # html 文档本身仍需 no-cache 兜底）
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", len(content))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except (FileNotFoundError, OSError):
            self.send_error(404)

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        """CORS。服务器版：本地打包前端（file:// 或 appassets 域）请求服务器 API
        需要跨域，Bearer token 认证 + 无 Cookie，`*` 安全。
        本地版（审计 A2，2026-09-15）：没有 Bearer 认证——`*` 等于把读 Key 前缀/
        改配置/导入数据交给任意网页。改为白名单回显：仅本机同源页面（PC exe 与
        安卓内嵌 WebView 都从本服务加载页面，不存在合法的跨源本地前端）拿到 ACAO，
        其余来源不发 → 跨域 JS 读不到响应；写端点另有 server.py do_POST 的来源
        校验兜底（CORS 拦不住"免预检请求已发出"）。"""
        if os.environ.get("FIREFLY_SERVER"):
            self.send_header("Access-Control-Allow-Origin", "*")
        else:
            origin = (self.headers.get("Origin") or "").strip()
            if origin in _local_allowed_origins():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, X-API-Key, X-API-Base, X-API-Mode")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        """CORS 预检：POST + Authorization 头会触发浏览器预检。
        本地版（审计 A2，2026-09-15）：非本机同源来源的预检一律 403——
        把跨站写请求拦在"发出"之前（预检失败则实际请求根本不会送出）。"""
        if not os.environ.get("FIREFLY_SERVER"):
            origin = (self.headers.get("Origin") or "").strip()
            if origin not in _local_allowed_origins():
                self.send_error(403)
                return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # 静默日志


# ── 本地版信任边界（审计 A1/A2，2026-09-15）──────────
def _local_allowed_origins() -> tuple:
    """本地版跨域白名单：仅本机同源。PC exe 与安卓内嵌 WebView 都从本服务
    （http://127.0.0.1:8765）加载页面，不存在合法的跨源本地前端；安卓的服务器
    回落模式（file:// 页面 + 远端 8787）走服务器版分支，与本函数无关。"""
    try:
        from core import paths as _paths
        port = int(_paths.PORT)
    except Exception:
        port = 8765
    return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def local_origin_ok(handler) -> bool:
    """浏览器来源校验（本地版专用；/shutdown 与全部写端点共用）。

    判定：Origin / Sec-Fetch-Site / Referer 三者全缺 → 非浏览器客户端
    （server.py 多开探测、routes_update 安装器、curl/测试）→ 放行；
    任一存在 → 必须全部落在本机同源白名单内（跨站 <img>/表单/fetch 一律拒）。
    服务器版直接放行（Bearer 鉴权 + 无 Cookie，信任模型不同，见 _cors_headers）。

    为什么"绑 127.0.0.1"不够：攻击页面跑在**用户自己的浏览器**里，发出的连接
    同样来自本机回环——真正的边界是"哪个页面在发起"，只能依赖浏览器自动附加、
    页面 JS 无法伪造的来源标记（Origin/Sec-Fetch-Site/Referer）。"""
    if os.environ.get("FIREFLY_SERVER"):
        return True
    origin = (handler.headers.get("Origin") or "").strip()
    sfs = (handler.headers.get("Sec-Fetch-Site") or "").strip()
    ref = (handler.headers.get("Referer") or "").strip()
    if not origin and not sfs and not ref:
        return True   # 非浏览器客户端（urllib 探测/安装器/白盒测试）
    allowed = _local_allowed_origins()
    if origin and origin not in allowed:
        return False
    if sfs and sfs not in ("same-origin", "none"):
        return False
    if ref and not any(ref == a or ref.startswith(a + "/") for a in allowed):
        return False
    return True


def setup_stdio_utf8():
    """编码兜底：Windows 下输出重定向到文件（cmd > log.txt）时控制台编码变 GBK，
    中文 print 会 UnicodeEncodeError 崩溃——强制 UTF-8 + errors=replace。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def preload_knowledge() -> None:
    """预加载知识库文本（避免首条消息等几秒拼接）。失败仅告警不阻塞启动。
    按预设包注册表预热所有声明了知识库的包（无知识库的包跳过）。"""
    try:
        from modules.app_config import PRESETS
        from modules.llm_retriever import _load_knowledge, get_knowledge_stats, has_knowledge
        for mode in PRESETS:
            if not has_knowledge(mode):
                continue
            _load_knowledge(mode)
            s = get_knowledge_stats(mode)
            print(f"  [OK] 知识库[{mode}] {s['files']} 文件 {s['chars']} 字符", flush=True)
    except Exception as e:
        print(f"  [WARN] 知识库加载失败: {e}", flush=True)


# ── 优雅关闭（多开检测用）──────────────────────
_SERVER_REF = {"server": None}


def shutdown_server():
    """优雅关闭：HTTP 服务停止 + 进程退出（新实例启动时调用）。
    os._exit(0) 立即终止进程——各入口的 /shutdown 必须做来源校验（仅本机可触发）。"""
    srv = _SERVER_REF.get("server")
    if srv:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    os._exit(0)
