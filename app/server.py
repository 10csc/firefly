#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流萤聊天 App — HTTP 骨架（分发 + 响应工具 + 启动）

拆分结构：
- modules/app_config.py  路径引导 + 配置状态（全局唯一 user_data 公式）
- modules/multipart.py   multipart/form-data 解析
- routes.py              全部 API 路由（POST_ROUTES / GET_ROUTES 分发表）
- server.py（本文件）    FireflyHandler + 启动块
"""

import json, sys, os, logging, warnings
# 屏蔽 requests 依赖版本不匹配的警告（不影响功能）
warnings.filterwarnings("ignore", message=".*urllib3.*", module="requests")
warnings.filterwarnings("ignore", message=".*chardet.*", module="requests")
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

# 配置日志输出到终端
logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s', stream=sys.stderr)

# 将项目根目录加入 sys.path，使 memory/ knowledge/ 可导入
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from modules import app_config as cfg
import routes


# ── HTTP 服务器 ──────────────────────────────────
class FireflyHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(cfg.STATIC_DIR), **kwargs)

    def do_POST(self):
        fn = routes.POST_ROUTES.get(urlparse(self.path).path)
        if fn:
            fn(self)
        else:
            self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path
        # 路径穿越防护：_serve_file 是手写的，/assets/../config.json 能读走 API Key
        if ".." in unquote(path):
            self.send_error(404)
            return
        # 多开限制：新实例探测旧实例存活性/触发优雅关闭
        if path == "/health":
            self._json({"ok": True, "alive": True})
            return
        if path == "/shutdown":
            # 优雅关闭：新实例启动时调用，保存文件后退出
            self._json({"ok": True, "shutting_down": True})
            import threading as _t
            _t.Timer(0.3, lambda: _shutdown_server()).start()
            return
        fn = routes.GET_ROUTES.get(path)
        if fn:
            fn(self)
        # 静态文件路由（unquote 解码中文路径，否则表情包等中文文件名 404）
        elif path.startswith("/assets/"):
            self._serve_file(cfg.resolve_asset("assets/" + unquote(path[8:])))
        elif path.startswith("/static/"):
            self._serve_file(cfg.STATIC_DIR / unquote(path[8:]))
        elif path == "/" or path == "/index.html":
            self._serve_file(cfg.STATIC_DIR / "index.html")
        else:
            # 根目录静态文件（轮播图等）：先按编码路径，再按原始中文路径尝试
            # （SimpleHTTPRequestHandler 对未编码中文返回 404，这里统一用 unquote 处理）
            name = unquote(path)
            if name and not name.startswith("/"):
                name = "/" + name
            fp = cfg.STATIC_DIR / name.lstrip("/")
            if fp.exists() and fp.is_file():
                self._serve_file(fp)
            else:
                super().do_GET()

    # ── 响应工具（供 routes 调用）──────────────────
    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css", ".js": "application/javascript",
        ".ttf": "font/ttf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
    }

    def _serve_file(self, filepath: Path):
        try:
            content = filepath.read_bytes()
            self.send_response(200)
            mime = self._MIME.get(filepath.suffix.lower())
            if mime:
                self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except (FileNotFoundError, OSError):
            self.send_error(404)

    def _json(self, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默日志


_SERVER_REF = {"server": None}


def _shutdown_server():
    """优雅关闭：HTTP 服务停止 + 进程退出（新实例启动时调用）。"""
    srv = _SERVER_REF.get("server")
    if srv:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    os._exit(0)


def main():
    # 编码兜底：Windows 下输出重定向到文件（cmd > log.txt）时控制台编码变 GBK，
    # 中文 print 会 UnicodeEncodeError 崩溃——强制 UTF-8 + errors=replace
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 预加载知识库文本（避免首条消息等几秒拼接）
    print("  预加载知识库...", flush=True)
    try:
        from modules.llm_retriever import _load_knowledge, get_knowledge_stats
        _load_knowledge()
        s = get_knowledge_stats()
        print(f"  [OK] 知识库 {s['files']} 文件 {s['chars']} 字符", flush=True)
    except Exception as e:
        print(f"  [WARN] 知识库加载失败: {e}", flush=True)

    # 端口占用检查——防止旧进程残留导致请求路由到旧代码。
    # 新实例检测到旧实例：先探测 /shutdown 优雅关闭（保存文件），
    # 旧实例无响应（卡死）才兜底强杀。避免 taskkill /F 丢数据。
    import socket as _sock
    _probe = _sock.socket()
    _probe.settimeout(1)
    try:
        _probe.connect(("127.0.0.1", cfg.PORT))
        # 端口被占：旧实例存在
        import urllib.request as _ur
        try:
            with _ur.urlopen(f"http://127.0.0.1:{cfg.PORT}/health", timeout=2) as r:
                if r.status == 200:
                    # 旧实例活着 → 优雅关闭
                    print("  检测到旧实例，正在优雅关闭...", flush=True)
                    try:
                        with _ur.urlopen(f"http://127.0.0.1:{cfg.PORT}/shutdown", timeout=5):
                            pass
                    except Exception:
                        pass
                    # 等端口释放（最多 5s）
                    import time as _tm
                    for _ in range(25):
                        _tm.sleep(0.2)
                        _probe2 = _sock.socket()
                        _probe2.settimeout(0.5)
                        try:
                            _probe2.connect(("127.0.0.1", cfg.PORT))
                        except (ConnectionRefusedError, OSError):
                            _probe2.close()
                            break
                        _probe2.close()
        except Exception:
            pass
        # 端口仍未释放 → 兜底强杀（找 PID）
        try:
            _probe3 = _sock.socket()
            _probe3.settimeout(0.5)
            _probe3.connect(("127.0.0.1", cfg.PORT))
            print("  [WARN] 旧实例未响应优雅关闭，尝试强杀", flush=True)
            _probe3.close()
            _out = os.popen(f'netstat -ano | findstr ":{cfg.PORT}" | findstr LISTENING').read()
            for _line in _out.splitlines():
                _parts = _line.split()
                if len(_parts) >= 5:
                    try:
                        os.system(f"taskkill /F /PID {_parts[-1]} >nul 2>&1")
                    except Exception:
                        pass
                    break
            _tm.sleep(1)
        except (ConnectionRefusedError, OSError):
            pass  # 端口已释放
        finally:
            _probe.close()
    except (ConnectionRefusedError, OSError):
        pass  # 端口空闲
    finally:
        _probe.close()

    # 只绑本机：局域网暴露会让任何人用你的 API Key 聊天/改设定
    server = ThreadingHTTPServer(("127.0.0.1", cfg.PORT), FireflyHandler)
    _SERVER_REF["server"] = server
    print("\n  流萤聊天 App 启动中...")
    _key = cfg.config.get("api_key", "")
    if _key:
        print(f"  Key OK: {_key[:12]}...", flush=True)
    else:
        print(f"  [WARN] 未检测到 API Key ({cfg.CONFIG_FILE})，请在浏览器中配置", flush=True)
    print(f"  打开浏览器访问: http://localhost:{cfg.PORT}\n")
    # 自动打开浏览器（安卓内嵌模式不打开；环境变量 FIREFLY_NO_BROWSER=1 关闭）
    try:
        if os.environ.get("FIREFLY_NO_BROWSER") != "1" and not os.environ.get("FIREFLY_ANDROID"):
            import webbrowser, threading as _t
            _t.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{cfg.PORT}")).start()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  关闭服务器")
    finally:
        server.shutdown()
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
