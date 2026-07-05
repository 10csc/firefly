#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流萤聊天 App — 接入层（HTTP 收发 + 会话管理）

业务编排见 orchestrator.py。本文件只做：
- HTTP 路由（GET 静态文件 + POST /chat /set-key /check-key + GET /metrics）
- 会话存取（sessions dict）
- API Key 管理
"""

import json, os
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from openai import OpenAI

from orchestrator import handle_chat
from modules.context_manager import ContextManager

# ── 配置 ────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ASSETS_DIR = BASE_DIR / "assets"
PORT = 8765

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
API_BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"

# ── 会话状态 ─────────────────────────────────────
CONFIG_FILE = BASE_DIR / "config.json"
_VALID_REPLY_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
_VALID_EFFORTS = ("none", "low", "high", "max")

def _load_config() -> dict:
    """加载配置：{api_key, reply_model, reply_effort, reply_temperature}。缺失字段用默认值。"""
    cfg = {"api_key": "", "reply_model": "deepseek-v4-flash", "reply_effort": "high", "reply_temperature": 0.5}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg["api_key"] = data.get("api_key", "") or ""
            rm = data.get("reply_model", "deepseek-v4-flash")
            cfg["reply_model"] = rm if rm in _VALID_REPLY_MODELS else "deepseek-v4-flash"
            eff = data.get("reply_effort", "high")
            cfg["reply_effort"] = eff if eff in _VALID_EFFORTS else "high"
            try:
                t = float(data.get("reply_temperature", 0.5))
                cfg["reply_temperature"] = max(0.0, min(2.0, t))
            except (TypeError, ValueError):
                cfg["reply_temperature"] = 0.5
    except Exception:
        pass
    if not cfg["api_key"]:
        cfg["api_key"] = API_KEY
    return cfg

def _save_config(cfg: dict) -> None:
    """持久化配置到 config.json。"""
    CONFIG_FILE.write_text(
        json.dumps({"api_key": cfg.get("api_key", ""),
                    "reply_model": cfg.get("reply_model", "deepseek-v4-flash"),
                    "reply_effort": cfg.get("reply_effort", "high"),
                    "reply_temperature": cfg.get("reply_temperature", 0.5)},
                   ensure_ascii=False),
        encoding="utf-8")

_config = _load_config()
_active_api_key = _config["api_key"]
_reply_model = _config["reply_model"]
_reply_effort = _config["reply_effort"]
_reply_temperature = _config["reply_temperature"]
sessions: dict[str, dict] = {}


def _get_client():
    """获取当前 API 客户端，若未设置 Key 则返回 None"""
    key = _active_api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return OpenAI(api_key=key, base_url=API_BASE) if key else None


def get_session(sid: str) -> dict:
    if sid not in sessions:
        # 首次创建会话：加载记忆头部（无记忆/中断/异常都降级为空串，不阻塞会话）
        from modules.memory_manager import wake as memory_wake
        memory_head = memory_wake(_get_client(), MODEL) if _get_client() else ""
        sessions[sid] = {
            "context": ContextManager(),     # 替代 history 列表
            "violation_history": False,
            "state": {"mood": [{"label": "安心", "intensity": 3}], "affection": 80.0, "tension": 15.0, "initiative": 50.0},
            "memory_head": memory_head,
        }
    return sessions[sid]


# ── HTTP 服务器 ──────────────────────────────────
class FireflyHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_POST(self):
        global _active_api_key, _reply_model, _reply_effort, _reply_temperature
        if self.path == "/set-key":
            # 兼容旧前端：只更新 api_key
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            _active_api_key = (body.get("api_key") or "").strip()
            _save_config({"api_key": _active_api_key, "reply_model": _reply_model, "reply_effort": _reply_effort})
            self._json({"ok": bool(_active_api_key)})

        elif self.path == "/set-config":
            # 新端点：一次性更新 api_key + reply_model + reply_effort + temperature
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            new_key = (body.get("api_key") or "").strip()
            new_model = body.get("reply_model", _reply_model)
            new_effort = body.get("reply_effort", _reply_effort)
            new_temp = body.get("reply_temperature", _reply_temperature)
            # 校验合法性，非法值保留旧值
            if new_model not in _VALID_REPLY_MODELS:
                new_model = _reply_model
            if new_effort not in _VALID_EFFORTS:
                new_effort = _reply_effort
            try:
                new_temp = max(0.0, min(2.0, float(new_temp)))
            except (TypeError, ValueError):
                new_temp = _reply_temperature
            # api_key：前端没传（空串）时保留旧值，避免只改温度时把 key 清空
            if new_key:
                _active_api_key = new_key
            _reply_model = new_model
            _reply_effort = new_effort
            _reply_temperature = new_temp
            _save_config({"api_key": _active_api_key, "reply_model": _reply_model,
                          "reply_effort": _reply_effort, "reply_temperature": _reply_temperature})
            # ok：最终配置里有 key 就算成功（允许只改温度不改 key）
            self._json({"ok": bool(_active_api_key), "reply_model": _reply_model,
                        "reply_effort": _reply_effort, "reply_temperature": _reply_temperature})

        elif self.path == "/check-key":
            self._json({"has_key": bool(_get_client())})

        elif self.path == "/chat":
            client = _get_client()
            if not client:
                self._json({"reply": None, "error": "请先设置 API Key", "need_key": True}); return

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            user_input = (body.get("message") or "").strip()
            session_id = body.get("session_id", "default")

            # 即时写盘：用户消息一发就记
            from modules.conversation_store import append_message as _append_msg
            if user_input:
                _append_msg("user", {"type": "text", "content": user_input})

            session = get_session(session_id)
            memory_head = session.get("memory_head", "")
            result = handle_chat(
                user_input, session, client, MODEL,
                memory_head=memory_head,
                reply_model=_reply_model,
                reply_effort=_reply_effort,
                reply_temperature=_reply_temperature,
            )
            # 即时写盘：流萤回复每条立刻记，并把 time 回传给前端
            enriched = []
            for m in result.messages:
                record = {"type": m.get("type")}
                if m.get("type") == "text":
                    record["content"] = m.get("content", "")
                    seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]})
                    record["time"] = t
                elif m.get("type") == "sticker":
                    record["path"] = m.get("path", "")
                    record["label"] = m.get("label", "")
                    seq, t = _append_msg("firefly", {"type": "sticker", "path": record["path"], "label": record["label"]})
                    record["time"] = t
                else:
                    record["content"] = str(m)
                    seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]})
                    record["time"] = t
                enriched.append(record)
            self._json({"messages": enriched, "bubble": result.bubble, "state": result.state})

        elif self.path == "/rest":
            client = _get_client()
            if not client:
                self._json({"ok": False, "error": "未设置 API Key"}); return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            session_id = body.get("session_id", "default")
            session = get_session(session_id)
            from modules.memory_manager import MemoryManager
            mm = MemoryManager(client, MODEL)
            result = mm.rest(session["context"].get_full(), session["context"].turn_count)
            self._json({"ok": result.success, "added": len(result.added_entries),
                        "resolved": len(result.resolved_entries), "error": result.error})

        elif self.path == "/add-sticker":
            # multipart/form-data 解析：保存图片到 app/assets/stickers/，写入 registry.json
            from tools.sticker_picker import add_sticker, StickerAddError
            try:
                fields, files = _parse_multipart(self)
                category = fields.get("category", "")
                label = fields.get("label", "")
                file_info = files.get("file")
                if not file_info:
                    self._json({"ok": False, "error": "缺少图片文件"}); return
                if category not in ("可爱", "帅气"):
                    self._json({"ok": False, "error": "分类必须为 可爱/帅气"}); return
                if not label:
                    self._json({"ok": False, "error": "缺少含义描述"}); return

                # 保存图片：用时间戳前缀避免重名
                import time
                original = file_info["filename"]
                ext = Path(original).suffix or ".png"
                safe_name = f"user_{int(time.time())}_{Path(original).stem}{ext}"
                save_dir = ASSETS_DIR / "stickers"
                save_dir.mkdir(parents=True, exist_ok=True)
                save_path = save_dir / safe_name
                save_path.write_bytes(file_info["data"])

                # 写入注册表
                rel_path = f"stickers/{safe_name}"
                entry = add_sticker(rel_path, category, label)
                self._json({"ok": True, "sticker_id": entry.id, "label": entry.label})
            except StickerAddError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": f"上传失败: {e}"})

        elif self.path == "/sticker-update":
            # 修改表情包 label（含义说明）
            from tools.sticker_picker import update_sticker_label, StickerUpdateError
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            sid = (body.get("id") or "").strip()
            new_label = (body.get("label") or "").strip()
            try:
                entry = update_sticker_label(sid, new_label)
                self._json({"ok": True, "id": entry.id, "label": entry.label})
            except StickerUpdateError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": f"修改失败: {e}"})

        elif self.path == "/sticker-delete":
            # 删除表情包条目（默认项不允许删）
            from tools.sticker_picker import delete_sticker, StickerDeleteError
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            sid = (body.get("id") or "").strip()
            try:
                delete_sticker(sid)
                self._json({"ok": True, "id": sid})
            except StickerDeleteError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                self._json({"ok": False, "error": f"删除失败: {e}"})
        else:
            self.send_error(404)

    def do_GET(self):
        from urllib.parse import unquote
        path = urlparse(self.path).path
        # API 路由（GET 接口）
        if path == "/check-key":
            self._json({"has_key": bool(_get_client())})
        elif path == "/config":
            # 返回当前配置（api_key 脱敏只返回前12位）
            self._json({
                "has_key": bool(_active_api_key),
                "key_prefix": _active_api_key[:12] + "..." if _active_api_key else "",
                "reply_model": _reply_model,
                "reply_effort": _reply_effort,
                "reply_temperature": _reply_temperature,
                "valid_models": list(_VALID_REPLY_MODELS),
                "valid_efforts": list(_VALID_EFFORTS),
            })
        elif path == "/metrics":
            from metrics import collect
            self._json(collect())
        elif path == "/history":
            # 分页加载历史：?limit=150&before_seq=N
            from urllib.parse import parse_qs
            from modules.conversation_store import load_recent, get_total_count, get_min_seq
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = int(qs.get("limit", ["150"])[0])
            except Exception:
                limit = 150
            before_seq_raw = qs.get("before_seq", [None])[0]
            before_seq = int(before_seq_raw) if before_seq_raw else None
            msgs = load_recent(limit=limit, before_seq=before_seq)
            total = get_total_count()
            # has_more：当前页最小 seq > 全局最小 seq 时还有更早历史
            cur_min = int(msgs[0]["seq"]) if msgs else 0
            has_more = cur_min > get_min_seq() if total > 0 else False
            self._json({"messages": msgs, "total": total, "has_more": has_more})
        elif path == "/wake-status":
            from modules.memory_manager import _INDEX_FILE, _MEMORY_FILE
            interrupted = _INDEX_FILE.exists() and not _MEMORY_FILE.exists()
            self._json({"interrupted": interrupted, "has_memory": _MEMORY_FILE.exists()})
        elif path == "/stickers":
            # 返回全量表情包列表（供前端管理表展示）
            from tools.sticker_picker import list_all_stickers
            entries = list_all_stickers()
            from tools.sticker_picker import _STICKERS_DEFAULT
            self._json({
                "stickers": [
                    {"id": s.id, "file": s.file, "category": s.category,
                     "label": s.label, "is_default": s.id in _STICKERS_DEFAULT}
                    for s in entries
                ],
            })
        # 静态文件路由（unquote 解码中文路径，否则表情包等中文文件名 404）
        elif path.startswith("/assets/"):
            self._serve_file(ASSETS_DIR / unquote(path[8:]))
        elif path.startswith("/static/"):
            self._serve_file(STATIC_DIR / unquote(path[8:]))
        elif path == "/" or path == "/index.html":
            self._serve_file(STATIC_DIR / "index.html")
        else:
            super().do_GET()

    def _serve_file(self, filepath: Path):
        try:
            content = filepath.read_bytes()
            self.send_response(200)
            if filepath.suffix == ".css":
                self.send_header("Content-Type", "text/css")
            elif filepath.suffix == ".js":
                self.send_header("Content-Type", "application/javascript")
            elif filepath.suffix == ".ttf":
                self.send_header("Content-Type", "font/ttf")
            elif filepath.suffix in (".jpg", ".jpeg"):
                self.send_header("Content-Type", "image/jpeg")
            elif filepath.suffix == ".png":
                self.send_header("Content-Type", "image/png")
            elif filepath.suffix == ".svg":
                self.send_header("Content-Type", "image/svg+xml")
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


# ── multipart/form-data 解析（手动实现，不引外部库）──
def _parse_multipart(handler):
    """从 POST 请求解析 multipart/form-data。

    Returns:
        (fields, files) — fields: {name: str 值}，files: {name: {"filename": str, "data": bytes}}
    """
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        return {}, {}
    # 提取 boundary
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
            break
    if not boundary:
        return {}, {}

    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length)
    boundary_bytes = ("--" + boundary).encode("utf-8")

    fields = {}
    files = {}
    # 按 boundary 分块
    chunks = body.split(boundary_bytes)
    for chunk in chunks:
        # 去掉首尾的 \r\n
        if chunk in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        # 找 headers 和 content 的分隔（空行 \r\n\r\n）
        sep_idx = chunk.find(b"\r\n\r\n")
        if sep_idx < 0:
            continue
        header_bytes = chunk[:sep_idx].decode("utf-8", errors="replace")
        content = chunk[sep_idx + 4:]

        # 解析 Content-Disposition
        name = None
        filename = None
        for line in header_bytes.split("\r\n"):
            if "Content-Disposition" in line:
                for seg in line.split(";"):
                    seg = seg.strip()
                    if seg.startswith("name="):
                        name = seg[len("name="):].strip('"')
                    elif seg.startswith("filename="):
                        filename = seg[len("filename="):].strip('"')
        if name is None:
            continue

        if filename is not None:
            files[name] = {"filename": filename, "data": content}
        else:
            fields[name] = content.decode("utf-8", errors="replace").strip()

    return fields, files


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), FireflyHandler)
    print(f"\n  流萤聊天 App 启动中...")
    if _active_api_key:
        print(f"  ✓ Key 已加载: {_active_api_key[:12]}... (来自 {CONFIG_FILE})")
    else:
        print(f"  ⚠️  未检测到 API Key ({CONFIG_FILE})，请在浏览器中配置")
    print(f"  打开浏览器访问: http://localhost:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  关闭服务器")
        server.shutdown()
