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
        ".json": "application/json",
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


def setup_stdio_utf8():
    """编码兜底：Windows 下输出重定向到文件（cmd > log.txt）时控制台编码变 GBK，
    中文 print 会 UnicodeEncodeError 崩溃——强制 UTF-8 + errors=replace。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def preload_knowledge() -> None:
    """预加载知识库文本（避免首条消息等几秒拼接）。失败仅告警不阻塞启动。"""
    print("  预加载知识库...", flush=True)
    try:
        from modules.llm_retriever import _load_knowledge, get_knowledge_stats
        _load_knowledge()
        s = get_knowledge_stats()
        print(f"  [OK] 知识库 {s['files']} 文件 {s['chars']} 字符", flush=True)
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
