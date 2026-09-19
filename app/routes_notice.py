# -*- coding: utf-8 -*-
"""公告 / 更新说明路由（见 docs/公告通道规范.md）。

四个端点都轻：GET /notice 是前端渲染入口（**先回缓存、后台刷新**，永不阻塞面板）；
GET /notice-image 取已校验的缓存图；POST /notice/check 手动刷新；POST /notice/read 记已读。

**本地版与服务器版都提供**（与热更新不同）：热更新是"改端侧文件"所以服务器版必须禁；
公告是"读服务端下发的内容 + 端侧缓存"，服务器网页版用户同样该看到公告，且合并到同一份
进程内缓存（`notice_root()` 与 user_data 平级 = 全站一份），不带用户数据。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from routes_common import _read_json


def notice_get(h):
    import notice
    h._json(notice.payload())


def notice_image(h):
    """GET /notice-image?name=<name>：只服务**已通过 sha256 校验**的缓存图。

    名字走 `notice.image_path()` 的正则白名单（不把用户输入拼进路径），
    所以这里没有穿越面；文件不存在一律 404。
    """
    import notice
    qs = parse_qs(urlparse(h.path).query)
    name = (qs.get("name", [""])[0] or "").strip()[:200]
    fp = notice.image_path(name)
    if fp is None:
        h._json({"ok": False, "error": "公告图不存在"}, 404)
        return
    mime = "image/png"
    low = name.lower()
    if low.endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif low.endswith(".webp"):
        mime = "image/webp"
    elif low.endswith(".gif"):
        mime = "image/gif"
    try:
        data = fp.read_bytes()
    except OSError:
        h._json({"ok": False, "error": "公告图读取失败"}, 404)
        return
    h.send_response(200)
    h.send_header("Content-Type", mime)
    # 内容寻址（文件名含摘要、内容不可变）→ 可长缓存；图本身经过 sha256 校验才落盘
    h.send_header("Cache-Control", "public, max-age=31536000, immutable")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def notice_action(h):
    """POST /notice/action：{"action":"check"|"read","ids":[...],"all":true}。"""
    body = _read_json(h) or {}
    act = str(body.get("action") or "").strip()
    import notice
    if act == "check":
        r = notice.check(force=True)
        r["payload"] = notice.payload(auto_refresh=False)
        h._json(r)
    elif act == "read":
        h._json(notice.mark_read(body.get("ids"), bool(body.get("all"))))
    else:
        h._json({"ok": False, "error": f"未知操作：{act!r}"})
