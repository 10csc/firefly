# -*- coding: utf-8 -*-
"""图片字节工具 — data URL 编码（图片理解统一由模型原生识图，本模块仅剩此函数）"""

import base64

# 图片 extension → MIME（上传白名单同源）
EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}


def to_data_url(data: bytes, ext: str) -> str | None:
    """字节 → base64 data URL（OpenAI 兼容 image_url block 用）。失败返回 None。"""
    try:
        mime = EXT_MIME.get(ext.lower())
        if not mime:
            return None
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        return None
