# -*- coding: utf-8 -*-
"""中转客户端（RelayClient）与 relay 队列（阶段 2.8 自 api_client 拆出，纯移动）

后端代理模式：服务器构建请求体（含资产占位符）→ APP 代发 DeepSeek（用户 Key）→ 回传。
服务器不持有用户 Key；APP 本地填充资产（知识库/设定）后调用。
实现：create() 把 payload 入队（按用户隔离）并阻塞等待 APP 回传，超时抛 ApiError。

对 api_client 共享件（ApiError/冷却/重试/响应构建）的引用一律经 `_ac.` 模块属性访问：
api_client 用 PEP 562 `__getattr__` 惰性转发本模块名字，测试
`patch("modules.api_client.relay_submit")` 的打桩语义因此保持不变（运行时按属性解析）。
"""

import logging
import secrets
import threading
import time

from modules import api_client as _ac

logger = logging.getLogger(__name__)

_RELAY_TIMEOUT = 120.0   # APP 代发超时（秒），超时降级（阶段预算会覆盖成更小值）
_RELAY_HEARTBEAT_SEC = 30.0   # 超过该秒数无 /relay/pending 轮询 → 判定 APP 离线（快速失败）
_recent_retry_delay = 0.0     # 兼容旧测试引用（新逻辑用 _jitter_delay）
_relay_lock = threading.Lock()
_relay_queues: dict[str, list] = {}     # user_key -> [{"call_id","payload","cond","result"}]
_relay_heartbeats: dict[str, float] = {}  # user_key -> 最近轮询时刻（心跳）


def relay_submit(user_key: str, payload: dict, api_base: str, timeout: float = _RELAY_TIMEOUT):
    """请求体入队并阻塞等待 APP 回传。返回兼容响应结构（SimpleNamespace）。
    A3 活性快速失败：APP 从未心跳或心跳停滞 >30s（掉线/冻结）→ 立即失败降级，
    不干等超时（旧行为每阶段最多 120s，story 单轮最坏 480s）。"""
    now = time.time()
    with _relay_lock:
        last_hb = _relay_heartbeats.get(user_key)
    if last_hb is not None and (now - last_hb) > _RELAY_HEARTBEAT_SEC:
        logger.warning("relay 心跳停滞 %.0fs（>30s），判定 APP 离线，快速失败", now - last_hb)
        raise _ac.ApiError("APP 已离线（心跳停滞），请重新打开应用", code="relay_timeout")
    with _relay_lock:
        # call_id 密码学随机（曾为全局自增 r1/r2，可被同用户预测伪造回传）
        call_id = secrets.token_hex(8)
        item = {"call_id": call_id, "payload": payload, "api_base": api_base,
                "cond": threading.Condition(), "result": None}
        _relay_queues.setdefault(user_key, []).append(item)
        q = _relay_queues[user_key]
    with item["cond"]:
        # 等待结果（或超时）
        if not item["cond"].wait(timeout=timeout):
            # 超时：从队列移除（可能已被取走，防误删新项——只删自己）
            with _relay_lock:
                try:
                    if item in _relay_queues.get(user_key, []):
                        _relay_queues[user_key].remove(item)
                except Exception:
                    pass
            raise _ac.ApiError("APP 代发超时", code="relay_timeout")
        result = item["result"]
    if isinstance(result, Exception):
        raise result
    return result


def relay_pending(user_key: str) -> dict | None:
    """取队首待转发请求体（APP 轮询）。返回 {call_id, payload, api_base} 或 None。
    A3：/relay/pending 的 1s 轮询本身就是心跳——每次调用刷新用户最近活动时刻。"""
    with _relay_lock:
        _relay_heartbeats[user_key] = time.time()
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
        except _ac.ApiError as e:
            result = e
        except Exception as e:
            result = _ac.ApiError(f"APP 回传数据无效: {e}")
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
        raise _ac.ApiError("APP 回传数据无效", code="bad_response")
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
            raise _ac.ApiError(f"API 错误: {_msg}" if _msg else "API 返回错误", code=_code)
        raise _ac.ApiError("API 返回错误（无 choices）", code=_code)
    return _ac._Completions._build_response(data)


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
        if _ac._in_cooldown(self._client._api_base):
            raise _ac.ApiError("上游服务波动中（冷却期），请稍后再试", code="cooldown")
        # A3：relay 链路补重试——代发超时/网络类失败重试最多 2 次。
        # relay 失败不触发端点冷却：APP 离线/超时 ≠ 上游端点故障，
        # 服务器多用户共用同一 api_base，冷却会连坐无辜用户（入口冷却检查仍保留）。
        last_error = None
        for attempt in range(3):
            try:
                # 经 api_client 模块属性解析 relay_submit：patch("modules.api_client.relay_submit")
                # 的测试打桩在拆分后照常生效（test_fault_tolerance）
                return _ac.relay_submit(self._client._user_key, payload, self._client._api_base,
                                        timeout=self._client._timeout)
            except _ac.ApiError as e:
                last_error = e
                if e.code not in ("relay_timeout", "network") or attempt >= 2:
                    raise
                delay = _ac._jitter_delay(attempt)
                remaining = _ac._check_deadline(self._client)   # 总预算耗尽 → 抛 timeout
                if remaining is not None:
                    delay = min(delay, remaining)   # sleep 不拖过单轮硬顶
                    if delay <= 0:
                        raise _ac.ApiError("单轮总预算已耗尽", code="timeout") from e
                logger.warning("[RELAY] 代发失败（%s，重试 %d/2，%.1fs 后）",
                               e.code, attempt + 1, delay)
                time.sleep(delay)
        raise last_error or _ac.ApiError("代发失败", code="relay_timeout")


class _RelayChat:
    def __init__(self, client: "RelayClient"):
        self.completions = _RelayCompletions(client)


class RelayClient:
    """中转客户端：请求体入队 → APP 代发（用户 Key，本地资产填充）→ 回传。
    用法与 _CompatClient 一致（chat.completions.create），模块零改动。"""

    def __init__(self, user_key: str, api_base: str = "https://api.deepseek.com/v1",
                 timeout: float = _RELAY_TIMEOUT):
        self._user_key = user_key
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self.chat = _RelayChat(self)
