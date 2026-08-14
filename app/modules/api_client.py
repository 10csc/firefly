# -*- coding: utf-8 -*-
"""DeepSeek API 兼容客户端 — requests 实现，无 openai 依赖

背景：openai 库依赖 jiter（Rust 编译产物），安卓 Chaquopy 环境下无可用 wheel；
按项目"内化外部依赖"原则，用 requests 直连 DeepSeek API，返回与 openai
响应结构兼容的对象（choices[0].message.content / reasoning_content / usage），
现有 4 个模块（analyzer/polisher/organizer/llm_retriever）与统计层零改动。
"""

import json
import logging
import secrets
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
    """API 调用失败（网络/鉴权/限流/返回异常）。
    code 用于前端人话提示分类：
      key_invalid=Key 无效/过期；no_balance=余额不足；rate_limit=限流；
      network=网络不通；server_error=服务端错误；bad_response=返回异常；
      relay_timeout=APP 代发超时；unknown=未分类。
    """

    def __init__(self, message: str = "", code: str = "unknown"):
        super().__init__(message)
        self.code = code


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
                last_error = ApiError(f"网络请求失败: {e}", code="network")
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
                # 错误码分类（前端人话提示）
                _code = "unknown"
                if resp.status_code == 401:
                    _code = "key_invalid"
                elif resp.status_code == 402:
                    _code = "no_balance"
                elif resp.status_code == 429:
                    _code = "rate_limit"
                elif 500 <= resp.status_code < 600:
                    _code = "server_error"
                # 5xx 服务端错误可重试
                if 500 <= resp.status_code < 600 and attempt < _MAX_RETRIES:
                    logger.warning("[API] 服务端错误 HTTP %d（尝试 %d/%d），%ss 后重试 — model=%s",
                                   resp.status_code, attempt + 1, _MAX_RETRIES + 1,
                                   _RETRY_DELAY, model)
                    time.sleep(_RETRY_DELAY)
                    continue
                last_error = ApiError(f"HTTP {resp.status_code}: {detail}", code=_code)
                logger.error("[API] HTTP 错误 — model=%s status=%d detail=%s",
                             model, resp.status_code, detail[:100])
                raise last_error

            try:
                data = resp.json()
            except ValueError as e:
                last_error = ApiError(f"响应解析失败: {e}", code="bad_response")
                logger.error("[API] JSON 解析失败 — model=%s %s", model, e)
                raise last_error from e

            if "choices" not in data or not data["choices"]:
                last_error = ApiError(f"响应缺少 choices: {json.dumps(data, ensure_ascii=False)[:300]}",
                                      code="bad_response")
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


class _QuotaCompletions(_Completions):
    """配额版 completions：每次调用前执行配额检查+记账（服务器托管模式）。"""

    def __init__(self, client: "_CompatClient", quota_fn):
        super().__init__(client)
        self._quota_fn = quota_fn

    def create(self, **kwargs):
        if self._quota_fn is not None:
            err = self._quota_fn()
            if err:
                raise ApiError(err, code="quota_exhausted")
        return super().create(**kwargs)


class QuotaClient(_CompatClient):
    """托管模式客户端：运营者 Key 直发，每 LLM 调用前经 quota_fn 检查+记账。
    quota_fn 由服务器注册（见 app_config.set_proxy_quota_checker）。"""

    def __init__(self, api_key: str, base_url: str, quota_fn, timeout: float = 120.0):
        super().__init__(api_key, base_url=base_url, timeout=timeout)
        self.chat.completions = _QuotaCompletions(self, quota_fn)


# ══ 中转客户端（RelayClient）═════════════════════
# 后端代理模式：服务器构建请求体（含资产占位符）→ APP 代发 DeepSeek（用户 Key）→ 回传
# 服务器不持有用户 Key；APP 本地填充资产（知识库/设定）后调用。
# 实现：create() 把 payload 入队（按用户隔离）并阻塞等待 APP 回传，超时抛 ApiError。
_RELAY_TIMEOUT = 120.0   # APP 代发超时（秒），超时降级
_relay_lock = threading.Lock()
_relay_queues: dict[str, list] = {}     # user_key -> [{"call_id","payload","cond","result"}]


def relay_submit(user_key: str, payload: dict, api_base: str):
    """请求体入队并阻塞等待 APP 回传。返回兼容响应结构（SimpleNamespace）。"""
    with _relay_lock:
        # call_id 密码学随机（曾为全局自增 r1/r2，可被同用户预测伪造回传）
        call_id = secrets.token_hex(8)
        item = {"call_id": call_id, "payload": payload, "api_base": api_base,
                "cond": threading.Condition(), "result": None}
        _relay_queues.setdefault(user_key, []).append(item)
        q = _relay_queues[user_key]
    with item["cond"]:
        # 等待结果（或超时）
        if not item["cond"].wait(timeout=_RELAY_TIMEOUT):
            # 超时：从队列移除（可能已被取走，防误删新项——只删自己）
            with _relay_lock:
                try:
                    if item in _relay_queues.get(user_key, []):
                        _relay_queues[user_key].remove(item)
                except Exception:
                    pass
            raise ApiError("APP 代发超时", code="relay_timeout")
        result = item["result"]
    if isinstance(result, Exception):
        raise result
    return result


def relay_pending(user_key: str) -> dict | None:
    """取队首待转发请求体（APP 轮询）。返回 {call_id, payload, api_base} 或 None。"""
    with _relay_lock:
        q = _relay_queues.get(user_key)
        if not q:
            return None
        item = q[0]
        return {"call_id": item["call_id"], "payload": item["payload"],
                "api_base": item["api_base"]}


def relay_has(user_key: str, call_id: str) -> bool:
    """校验 call_id 是否在该用户队列中（服务器中转降级的防滥用门槛：
    只允许代发服务器自己入队的请求，任意请求不可借服务器转发）。"""
    with _relay_lock:
        q = _relay_queues.get(user_key) or []
        return any(i["call_id"] == call_id for i in q)


def relay_result(user_key: str, call_id: str, data: dict, status: int = 0):
    """APP 回传 DeepSeek 响应：唤醒等待线程。data 为原始响应 JSON，status 为 HTTP 状态码
    （0=未知；前端直连/服务器中转都会带真实状态码）。"""
    with _relay_lock:
        q = _relay_queues.get(user_key) or []
        item = next((i for i in q if i["call_id"] == call_id), None)
        if item is None:
            return False
        try:
            result = _build_relay_response(data, status)
        except ApiError as e:
            result = e
        except Exception as e:
            result = ApiError(f"APP 回传数据无效: {e}")
        item["result"] = result
        q.remove(item)
        if not q:
            _relay_queues.pop(user_key, None)
    with item["cond"]:
        item["cond"].notify_all()
    return True


def _build_relay_response(data: dict, status: int = 0):
    """把 APP 回传的 DeepSeek 原始 JSON 转成兼容响应结构（同 _CompatClient._build_response）。

    API 错误响应（无 choices）不再一律归 unknown：按状态码分类
    （401=key_invalid / 402=no_balance / 429=rate_limit / 5xx=server_error），
    前端据此展示人话提示（Key 无效/余额不足/限流）。"""
    if not isinstance(data, dict):
        raise ApiError("APP 回传数据无效", code="bad_response")
    if "choices" not in data or not data["choices"]:
        _code = "unknown"
        st = status or 0
        if st == 401:
            _code = "key_invalid"
        elif st == 402:
            _code = "no_balance"
        elif st == 429:
            _code = "rate_limit"
        elif st >= 500:
            _code = "server_error"
        elif st >= 400:
            _code = "bad_response"
        _err = data.get("error")
        if isinstance(_err, dict):
            _msg = str(_err.get("message") or "")
            raise ApiError(f"API 错误: {_msg}" if _msg else "API 返回错误", code=_code)
        raise ApiError("API 返回错误（无 choices）", code=_code)
    return _Completions._build_response(data)


class _RelayCompletions:
    def __init__(self, client: "RelayClient"):
        self._client = client

    def create(self, *, model, messages, max_tokens=2000, temperature=None,
               extra_body=None, response_format=None):
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format
        if extra_body:
            payload.update(extra_body)
        return relay_submit(self._client._user_key, payload, self._client._api_base)


class _RelayChat:
    def __init__(self, client: "RelayClient"):
        self.completions = _RelayCompletions(client)


class RelayClient:
    """中转客户端：请求体入队 → APP 代发（用户 Key，本地资产填充）→ 回传。
    用法与 _CompatClient 一致（chat.completions.create），模块零改动。"""

    def __init__(self, user_key: str, api_base: str = "https://api.deepseek.com/v1"):
        self._user_key = user_key
        self._api_base = api_base.rstrip("/")
        self.chat = _RelayChat(self)
