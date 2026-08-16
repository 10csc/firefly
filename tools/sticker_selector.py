# -*- coding: utf-8 -*-
"""表情包默认启用选择器（本地小工具，零依赖）

用法：
    python tools/sticker_selector.py
    → 浏览器打开 http://127.0.0.1:8767

功能：
    - 勾选哪些表情包默认启用
    - 编辑每个表情包的描述词（会写入 registry.json 的 label）
    - 保存时自动备份 app/assets/stickers/registry.json
"""
import json
import shutil
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parent.parent
HTML = Path(__file__).resolve().parent / "sticker_selector.html"
REGISTRY = ROOT / "app" / "assets" / "stickers" / "registry.json"
PORT = 8767

# 代码内置 5 个默认项（也允许在这里一起选择启用/停用并改写描述词）
_BUILTINS = [
    {"id": "strong_01", "file": "stickers/流萤_出击.webp", "category": "帅气", "label": "出击", "builtin": True},
    {"id": "neutral_02", "file": "stickers/流萤_也挺好(表示无奈).webp", "category": "可爱", "label": "无奈接受", "builtin": True},
    {"id": "weak_02", "file": "stickers/流萤_没钱了.webp", "category": "可爱", "label": "没钱了", "builtin": True},
    {"id": "neutral_01", "file": "stickers/流萤_比心.webp", "category": "可爱", "label": "比心", "builtin": True},
    {"id": "like_01", "file": "stickers/呜呜伯_期待.webp", "category": "可爱", "label": "呜呜伯期待", "builtin": True},
]


def _load_registry() -> dict:
    if not REGISTRY.exists():
        return {"stickers": []}
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"stickers": []}
    except Exception:
        return {"stickers": []}


def _registry_items() -> list[dict]:
    data = _load_registry()
    items = data.get("stickers", []) if isinstance(data, dict) else []
    out = []
    known = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", "")).strip()
        if not sid or sid in known:
            continue
        known.add(sid)
        out.append({
            "id": sid,
            "file": str(item.get("file", "")),
            "category": str(item.get("category", "可爱")),
            "label": str(item.get("label", "")),
            "enabled": bool(item.get("enabled", True)),
            "builtin": bool(item.get("builtin", False)),
        })
    # 内置默认项（若 registry 已包含同 id 的覆盖版本，则不重复插入）
    for b in _BUILTINS:
        if b["id"] not in known:
            out.append(dict(b))
            known.add(b["id"])
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _safe_asset(self, path: str) -> Path | None:
        raw = unquote(path)
        if raw.startswith("/"):
            raw = raw[1:]
        if not raw.startswith("assets/"):
            return None
        asset_root = (ROOT / "app" / "assets").resolve()
        fp = (asset_root / raw[len("assets/"):]).resolve()
        try:
            if not str(fp).startswith(str(asset_root)):
                return None
        except OSError:
            return None
        return fp if fp.is_file() else None

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/registry":
            self._json({"ok": True, "stickers": _registry_items()})
            return
        if path.startswith("/assets/"):
            fp = self._safe_asset(path)
            if fp is None:
                self.send_error(404)
                return
            body = fp.read_bytes()
            self.send_response(200)
            if fp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                self.send_header("Content-Type", "image/" + ("jpeg" if fp.suffix.lower() in (".jpg", ".jpeg") else fp.suffix.lower().lstrip(".")))
            else:
                self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/save":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > 2 * 1024 * 1024:
                return self._json({"ok": False, "error": "请求体非法"}, 400)
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            items = data.get("stickers") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return self._json({"ok": False, "error": "缺少 stickers 列表"}, 400)
            clean = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                sid = str(it.get("id", "")).strip()
                file = str(it.get("file", "")).strip()
                if not sid or not file.startswith("stickers/") or ".." in file or "\\" in file:
                    return self._json({"ok": False, "error": f"非法条目: {sid or '(空)'}"}, 400)
                cat = str(it.get("cat") or it.get("category") or "可爱").strip()
                if cat not in ("可爱", "帅气"):
                    cat = "可爱"
                label = str(it.get("label") or "").strip()[:120] or sid
                clean.append({"id": sid, "file": file, "category": cat,
                              "label": label, "enabled": bool(it.get("enabled"))})
            if not clean:
                return self._json({"ok": False, "error": "列表为空"}, 400)
            # 备份旧文件
            backup = ""
            if REGISTRY.exists():
                backup = REGISTRY.with_name(REGISTRY.stem + ".bak-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
                shutil.copy2(REGISTRY, backup)
                backup = backup.name
            tmp = REGISTRY.with_name(REGISTRY.name + ".tmp")
            tmp.write_text(json.dumps({"stickers": clean}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(REGISTRY)
            self._json({"ok": True, "saved": len(clean), "backup": backup})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)


if __name__ == "__main__":
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"端口 {PORT} 被占用: {e}")
        sys.exit(1)
    print(f"表情包选择器已启动: http://127.0.0.1:{PORT}")
    print("保存会写回 app/assets/stickers/registry.json（自动备份）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
