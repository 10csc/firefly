# -*- coding: utf-8 -*-
"""图片描述生成（A9）— vision 模型（默认 deepseek-v4-flash-vision-exp）→ 1-3 句客观描述

模块铁律：输入审查 → 处理（LLM）→ 验证（非空/长度）→ 降级（失败返回空串，
调用方以用户手填描述或 [图片] 占位兜底）。图片字节只在本轮函数内存在，绝不落盘。
"""

import base64
import logging
import threading

from modules.llm_base import record_usage, record_error

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_DESC_COUNT = 0
_DESC_ERRORS = 0

_DESC_PROMPT = (
    "用 1-3 句简短中文客观描述这张图片的内容（人物、场景、动作、氛围即可），"
    "不要评价、不要猜测含义、不要提及图片本身。只输出描述文字。"
)

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


def describe_image(client, model: str, data_url: str) -> str:
    """vision 模型生成图片描述。失败/空输出返回 ""（降级信号）。"""
    global _DESC_COUNT, _DESC_ERRORS
    if client is None or not model or not data_url:
        with _lock:
            _DESC_ERRORS += 1
        return ""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _DESC_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            max_tokens=300,
            extra_body={"thinking": {"type": "disabled"}},
        )
        record_usage("vision_desc", resp)
        raw = (resp.choices[0].message.content or "").strip()
        rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
        if not raw and rc:
            raw = rc
        desc = raw[:300].strip()
        if not desc:
            with _lock:
                _DESC_ERRORS += 1
            return ""
        with _lock:
            _DESC_COUNT += 1
        return desc
    except Exception as e:
        logger.warning("图片描述生成失败（降级为用户描述/占位）: %s", e)
        record_error("vision_desc", model, str(e))
        with _lock:
            _DESC_ERRORS += 1
        return ""


def get_counters() -> dict:
    with _lock:
        return {"vision_desc_count": _DESC_COUNT, "vision_desc_errors": _DESC_ERRORS}
