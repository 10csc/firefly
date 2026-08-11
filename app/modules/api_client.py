# -*- coding: utf-8 -*-
"""DeepSeek API 兼容客户端 — requests 实现，无 openai 依赖

背景：openai 库依赖 jiter（Rust 编译产物），安卓 Chaquopy 环境下无可用 wheel；
按项目"内化外部依赖"原则，用 requests 直连 DeepSeek API，返回与 openai
响应结构兼容的对象（choices[0].message.content / reasoning_content / usage），
现有 4 个模块（analyzer/polisher/organizer/llm_retriever）与统计层零改动。
"""

import json
import logging
import threading
import time
from types import SimpleNamespace

import requests

logger = logging.getLogger(__name__)

# 网络重试配置：后台切前台/WiFi 省电恢复场景下，首次请求可能因 TCP 连接
# 中断而失败，等待 2 秒让网络栈恢复后重试一次即可成功。
_RETRY_DELAY = 2.0        # 重试前等待秒数
_MAX_RETRIES = 1           # 最多重试 1 次（即总共 2 次尝试）


class ApiError(Exception):
    """API 调用失败（网络/鉴权/限流/返回异常）"""


def _ns(d: dict) -> SimpleNamespace:
    """dict → 嵌套 SimpleNamespace（兼容 openai 的属性访问风格），list 递归转换"""
    out = SimpleNamespace()
    for k, v in d.items():
        if isinstance(v, dict):
            v = _ns(v)
        elif isinstance(v, list):
            v = [_ns(i) if isinstance(i, dict) else i for i in v]
        setattr(out, k, v)
    return out


class _Completions:
    def __init__(self, client: "_CompatClient"):
        self._client = client

    def create(self, *, model, messages, max_tokens=2000, temperature=None,
               extra_body=None, response_format=None):
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format
        if extra_body:
            payload.update(extra_body)   # thinking / reasoning_effort 均为顶层参数

        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    self._client._base_url + "/chat/completions",
                    headers={"Authorization": f"Bearer {self._client._api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=self._client._timeout,
                )
            except requests.RequestException as e:
                last_error = ApiError(f"网络请求失败: {e}")
                if attempt < _MAX_RETRIES:
                    logger.warning("[API] 请求失败（尝试 %d/%d），%ss 后重试 — "
                                   "model=%s %s: %s",
                                   attempt + 1, _MAX_RETRIES + 1, _RETRY_DELAY,
                                   model, type(e).__name__, e)
                    time.sleep(_RETRY_DELAY)
                    continue
                logger.error("[API] 请求失败（已达最大重试）— model=%s %s: %s",
                             model, type(e).__name__, e)
                raise last_error from e

            if resp.status_code != 200:
                detail = resp.text[:300]
                # 5xx 服务端错误可重试
                if 500 <= resp.status_code < 600 and attempt < _MAX_RETRIES:
                    logger.warning("[API] 服务端错误 HTTP %d（尝试 %d/%d），%ss 后重试 — model=%s",
                                   resp.status_code, attempt + 1, _MAX_RETRIES + 1,
                                   _RETRY_DELAY, model)
                    time.sleep(_RETRY_DELAY)
                    continue
                last_error = ApiError(f"HTTP {resp.status_code}: {detail}")
                logger.error("[API] HTTP 错误 — model=%s status=%d detail=%s",
                             model, resp.status_code, detail[:100])
                raise last_error

            try:
                data = resp.json()
            except ValueError as e:
                last_error = ApiError(f"响应解析失败: {e}")
                logger.error("[API] JSON 解析失败 — model=%s %s", model, e)
                raise last_error from e

            if "choices" not in data or not data["choices"]:
                last_error = ApiError(f"响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:300]}")
                logger.error("[API] 响应缺少 choices — model=%s", model)
                raise last_error

            return self._build_response(data)

        # 不应到达此处
        raise last_error or ApiError("未知错误")

    @staticmethod
    def _build_response(data: dict) -> SimpleNamespace:
        msg = data["choices"][0].get("message", {})
        usage = data.get("usage", {})
        details = usage.get("completion_tokens_details", {})
        return _ns({
            "model": data.get("model", ""),
            "choices": [{
                "message": {
                    "content": msg.get("content", ""),
                    "reasoning_content": msg.get("reasoning_content", ""),
                },
            }],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens", 0),
                "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens", 0),
                "completion_tokens_details": {
                    "reasoning_tokens": details.get("reasoning_tokens", 0),
                },
            },
        })


class _Chat:
    def __init__(self, client: "_CompatClient"):
        self.completions = _Completions(client)


class _CompatClient:
    """兼容 openai.OpenAI 的最小实现：chat.completions.create"""

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 timeout: float = 30.0):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.chat = _Chat(self)
