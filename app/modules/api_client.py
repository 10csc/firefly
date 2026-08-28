# -*- coding: utf-8 -*-
"""DeepSeek API 兼容客户端 — requests 实现，无 openai 依赖

背景：openai 库依赖 jiter（Rust 编译产物），安卓 Chaquopy 环境下无可用 wheel；
按项目"内化外部依赖"原则，用 requests 直连 DeepSeek API，返回与 openai
响应结构兼容的对象（choices[0].message.content / reasoning_content / usage），
现有 4 个模块（analyzer/polisher/organizer/llm_retriever）与统计层零改动。
"""

import json
import logging
import random
import secrets
import threading
import time
from types import SimpleNamespace

import requests

logger = logging.getLogger(__name__)

# ── 容错策略（A3：按本项目真实调用链设计，2026-08-21）────────────
# 网络/5xx/429 → Full Jitter 重试（base=1s、cap=30s、最多 3 次）；401/400/402 不重试。
# 连续失败 → 端点冷却 60s（期间快速失败走降级话术）；429 遵守 Retry-After。
# relay 链路（APP 代发）失败不触发端点冷却：APP 离线/超时 ≠ 上游端点故障，
#   服务器多用户共用同一 api_base，冷却会连坐无辜用户（入口冷却检查仍保留）。
# 单轮总预算硬顶：orchestrator 在 client 上挂 _deadline，重试 sleep 前检查，
#   已耗尽（或 delay 截断后 <=0）→ 立即抛 ApiError(code="timeout")，不再重试。
_RETRY_BASE = 1.0        # Full Jitter 基准延迟（秒）
_RETRY_CAP = 30.0        # 单次延迟上限（秒）
_RETRY_MAX = 3           # 重试次数（总尝试 = 4）
_RETRY_AFTER_CAP = 60.0  # Retry-After 上限（秒）
_COOLDOWN_SEC = 60.0     # 端点冷却时长（连续失败后）
_MAX_RETRIES = _RETRY_MAX

# 端点冷却表：base_url -> 冷却截止时间戳（进程级）
_COOLDOWNS: dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()


# ── SSRF 防护（安全审查 2026-08-25）─────────────────────
# 服务器版 relay 链路：X-API-Base 由登录用户提供，服务器会在 /relay/proxy
# 中转时代其向该地址发请求并把响应 JSON 回传——若放任内网地址
# （127.0.0.1 / 10.x / 192.168.x / 169.254.169.254 云元数据等），
# 服务器就成了"内网探测代理"。is_public_endpoint 供两处调用：
#   ① server_app._setup_user_context（X-API-Base 入口拦截，治本）
#   ② routes.relay_proxy（出队取 base 后二次校验，纵深防御）
_EP_CHECK_TTL = 300.0
_EP_CHECK_CACHE: dict[str, tuple[bool, float]] = {}
_EP_CHECK_LOCK = threading.Lock()


def _ip_public(ip: str) -> bool:
    """单个 IP 是否公网单播地址（私网/环回/链路本地/组播/保留/CGNAT/未指定均 False）。"""
    import ipaddress
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if obj.version == 6:
        mapped = obj.ipv4_mapped   # ::ffff:a.b.c.d 按 IPv4 规则判
        if mapped is not None:
            obj = mapped
    if obj.version == 4 and obj in ipaddress.ip_network("100.64.0.0/10"):
        return False               # CGNAT（Python 部分版本 is_private 不含，显式补）
    return not (obj.is_private or obj.is_loopback or obj.is_link_local
                or obj.is_multicast or obj.is_reserved or obj.is_unspecified)


def is_public_endpoint(base_url: str) -> bool:
    """base_url 是否指向公网（防 SSRF）。格式非法 / 主机不可解析 / 任一解析记录
    非公网 → False。结果按 host 缓存（TTL 300s）——relay 模式前端 1s 轮询
    每次都带 X-API-Base，不能每请求做 DNS。DNS 失败 = False（请求本身也到不了）。"""
    import socket
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(base_url or "")
        host = (parts.hostname or "").strip().lower()
        if not host or parts.scheme not in ("http", "https"):
            return False
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            return False
        now = time.time()
        with _EP_CHECK_LOCK:
            hit = _EP_CHECK_CACHE.get(host)
            if hit and now - hit[1] < _EP_CHECK_TTL:
                return hit[0]
        import ipaddress
        try:
            ipaddress.ip_address(host)   # IP 字面量（IPv6 括号已被 urlsplit 剥离）
            ips = [host]
        except ValueError:
            infos = socket.getaddrinfo(host, None)
            ips = [i[4][0] for i in infos]
        ok = bool(ips) and all(_ip_public(ip) for ip in ips)
        with _EP_CHECK_LOCK:
            if len(_EP_CHECK_CACHE) > 1000:   # 缓存体积防护（正常供应商数 << 1000）
                _EP_CHECK_CACHE.clear()
            _EP_CHECK_CACHE[host] = (ok, now)
        return ok
    except Exception:
        return False


def _endpoint_key(base_url: str) -> str:
    return base_url.rstrip("/")


def _in_cooldown(base_url: str) -> bool:
    now = time.time()
    with _COOLDOWN_LOCK:
        until = _COOLDOWNS.get(_endpoint_key(base_url), 0)
        if until and until > now:
            return True
        if until:
            _COOLDOWNS.pop(_endpoint_key(base_url), None)
    return False


def _set_cooldown(base_url: str):
    with _COOLDOWN_LOCK:
        _COOLDOWNS[_endpoint_key(base_url)] = time.time() + _COOLDOWN_SEC
    logger.warning("[API] 端点 %s 连续失败 → 冷却 %.0fs（期间快速失败降级）", base_url, _COOLDOWN_SEC)


def _jitter_delay(attempt: int, retry_after: float | None = None) -> float:
    """Full Jitter（AWS 推荐）：0 ~ min(cap, base * 2^attempt)；Retry-After 优先（有上限）。"""
    if retry_after is not None:
        return min(max(retry_after, 0.1), _RETRY_AFTER_CAP)
    cap = min(_RETRY_CAP, _RETRY_BASE * (2 ** attempt))
    return random.uniform(0, max(cap, 0.1))


def _retry_after_from(resp) -> float | None:
    try:
        v = resp.headers.get("Retry-After")
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _retriable(code: str, status: int) -> bool:
    """哪些错误值得重试：网络不通 / 服务端 5xx / 429 限流 / 超时类。"""
    return code in ("network", "server_error", "rate_limit", "timeout")


def _check_deadline(client) -> float | None:
    """单轮总预算硬顶兜底（orchestrator._stage_timeout 把本轮 deadline 挂在 client._deadline）。
    重试 sleep 前调用：已耗尽（now > deadline-2，留 2s 收尾余量）→ 抛 ApiError(code="timeout")；
    否则返回剩余可用秒数（调用方据此截断 delay）。client 无 _deadline 属性 → 返回 None（不约束）。"""
    deadline = getattr(client, "_deadline", None)
    if deadline is None:
        return None
    remaining = deadline - 2.0 - time.time()
    if remaining <= 0:
        raise ApiError("单轮总预算已耗尽", code="timeout")
    return remaining


def error_code_of(e: Exception) -> str:
    """从异常中提取分类（ApiError 自带 code；其它异常统一 unknown）。透传前端提示用。"""
    code = getattr(e, "code", "")
    return code if isinstance(code, str) and code else "unknown"


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


def _looks_unknown_param(detail: str) -> bool:
    """判断 4xx 错误是否"未知参数"（OpenAI 兼容供应商拒绝 thinking/reasoning_effort 等
    DeepSeek 私有参数时的典型文案：unknown parameter / unexpected parameter）。"""
    low = detail.lower()
    return ("unknown" in low or "unexpected parameter" in low
            or "not a valid parameter" in low or "not supported" in low)


class _Completions:
    def __init__(self, client: "_CompatClient"):
        self._client = client

    def create(self, *, model, messages, max_tokens=2000, temperature=None,
               extra_body=None, response_format=None):
        caps = self._client._caps
        # 能力位分支（A8）：仅 caps.thinking 的供应商发送 DeepSeek 私有参数
        # （extra_body 的 thinking/reasoning_effort 等）；自定义供应商默认不发。
        sent_extra = bool(extra_body) and caps.get("thinking", True)
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format
        if sent_extra:
            payload.update(extra_body)   # thinking / reasoning_effort 均为顶层参数
        payload_no_extra = dict(payload)
        if sent_extra:
            for k in extra_body:
                payload_no_extra.pop(k, None)

        # 冷却期（该端点连续失败后 60s）：快速失败走降级话术，不再烧时间
        if _in_cooldown(self._client._base_url):
            raise ApiError("上游服务波动中（冷却期），请稍后再试", code="cooldown")

        last_error = None
        retry_count = 0          # 已重试次数（分类重试）
        stripped_extra = False   # 本次已剥离 extra_body（unknown-param 探针重试中）
        for attempt in range(_RETRY_MAX + 2):   # +2：未知参数探针 1 次 + 常规重试余量
            try:
                resp = requests.post(
                    self._client._base_url + "/chat/completions",
                    headers={"Authorization": f"Bearer {self._client._api_key}",
                             "Content-Type": "application/json"},
                    json=payload_no_extra if stripped_extra else payload,
                    timeout=self._client._timeout,
                )
            except requests.RequestException as e:
                last_error = ApiError(f"网络请求失败: {e}", code="network")
                if retry_count < _RETRY_MAX and not stripped_extra:
                    retry_count += 1
                    delay = _jitter_delay(retry_count - 1)
                    remaining = _check_deadline(self._client)   # 总预算耗尽 → 抛 timeout
                    if remaining is not None:
                        delay = min(delay, remaining)   # sleep 不拖过单轮硬顶
                        if delay <= 0:
                            raise ApiError("单轮总预算已耗尽", code="timeout") from e
                    logger.warning("[API] 网络失败（重试 %d/%d，%.1fs 后）— model=%s %s: %s",
                                   retry_count, _RETRY_MAX, delay, model, type(e).__name__, e)
                    time.sleep(delay)
                    continue
                logger.error("[API] 网络失败（重试耗尽）— model=%s %s: %s",
                             model, type(e).__name__, e)
                _set_cooldown(self._client._base_url)
                raise last_error from e

            if resp.status_code != 200:
                detail = resp.text[:300]
                # 能力探测：4xx「未知参数」→ 剥离 extra_body 重试一次并回写 caps（不计重试配额）
                if (not stripped_extra and sent_extra and resp.status_code in (400, 422)
                        and _looks_unknown_param(detail)):
                    stripped_extra = True
                    logger.warning("[API] 供应商拒绝私有参数（%s → %s），剥离 extra_body 重试并回写 caps.thinking=False",
                                   resp.status_code, detail[:120])
                    continue
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
                # 分类重试：429/5xx 可重试（Full Jitter + 遵守 Retry-After）；4xx 确定性错误不重试
                if _retriable(_code, resp.status_code) and retry_count < _RETRY_MAX:
                    retry_count += 1
                    delay = _jitter_delay(retry_count - 1, _retry_after_from(resp))
                    remaining = _check_deadline(self._client)   # 总预算耗尽 → 抛 timeout
                    if remaining is not None:
                        delay = min(delay, remaining)   # sleep 不拖过单轮硬顶
                        if delay <= 0:
                            raise ApiError("单轮总预算已耗尽", code="timeout")
                    logger.warning("[API] HTTP %d（重试 %d/%d，%.1fs 后）— model=%s",
                                   resp.status_code, retry_count, _RETRY_MAX, delay, model)
                    time.sleep(delay)
                    continue
                last_error = ApiError(f"HTTP {resp.status_code}: {detail}", code=_code)
                logger.error("[API] HTTP 错误 — model=%s status=%d detail=%s",
                             model, resp.status_code, detail[:100])
                if _retriable(_code, resp.status_code):
                    _set_cooldown(self._client._base_url)
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

            # 探针成功：回写 caps.thinking=False（后续请求不再发私有参数）
            if stripped_extra and caps.get("thinking", True):
                caps["thinking"] = False
                try:
                    if self._client._on_caps_change:
                        self._client._on_caps_change({"thinking": False})
                except Exception:
                    pass
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
    """兼容 openai.OpenAI 的最小实现：chat.completions.create

    A8：caps = 供应商能力位（thinking 决定是否发 extra_body 私有参数等）；
    on_caps_change = 能力探测后回调（本地版回写 config.json，服务器 relay 由前端处理）。"""

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 timeout: float = 30.0, caps: dict | None = None,
                 on_caps_change=None):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._caps = {"thinking": True, "reasoning": True, "vision": True,
                      "prompt_cache": True, "models_endpoint": True}
        self._caps.update(caps or {})
        self._on_caps_change = on_caps_change
        self.chat = _Chat(self)


class _QuotaCompletions(_Completions):
    """配额版 completions：每次调用前配额检查，成功后记账（服务器托管模式）。
    A3 默认拍板：失败调用不占用户额度，单独记 proxy_usage_failed（处理方注册回调）。

    托管模型锁：服务器托管 API 只允许 OpenCode Go 的 mimo-v2.5 模型（运营者套餐约束），
    调用方传入的任何 model（含全局配置被改成其它模型的情况）一律强制为 mimo-v2.5。"""

    def __init__(self, client: "_CompatClient", quota_fn, counter_fn=None, fail_fn=None):
        super().__init__(client)
        self._quota_fn = quota_fn
        self._counter_fn = counter_fn
        self._fail_fn = fail_fn

    def create(self, **kwargs):
        if self._quota_fn is not None:
            err = self._quota_fn()
            if err:
                raise ApiError(err, code="quota_exhausted")
        kwargs["model"] = "mimo-v2.5"   # 托管模式模型锁：只允许 mimo-v2.5
        try:
            result = super().create(**kwargs)
        except Exception as e:
            # 失败调用：不占用户额度，单独记录（有处理方则通知）
            try:
                if self._fail_fn is not None:
                    self._fail_fn()
            except Exception:
                pass
            raise
        try:
            if self._counter_fn is not None:
                self._counter_fn()
        except Exception:
            pass
        return result


class QuotaClient(_CompatClient):
    """托管模式客户端：运营者 Key 直发，每 LLM 调用前经 quota_fn 检查，成功后 counter_fn 记账。
    quota_fn/counter_fn/fail_fn 由服务器注册（见 app_config.set_proxy_quota_*）。"""

    def __init__(self, api_key: str, base_url: str, quota_fn, timeout: float = 120.0,
                 counter_fn=None, fail_fn=None):
        super().__init__(api_key, base_url=base_url, timeout=timeout)
        self.chat.completions = _QuotaCompletions(self, quota_fn,
                                                  counter_fn=counter_fn, fail_fn=fail_fn)


# ══ 中转客户端（RelayClient）═════════════════════
# 后端代理模式：服务器构建请求体（含资产占位符）→ APP 代发 DeepSeek（用户 Key）→ 回传
# 服务器不持有用户 Key；APP 本地填充资产（知识库/设定）后调用。
# 实现：create() 把 payload 入队（按用户隔离）并阻塞等待 APP 回传，超时抛 ApiError。
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
        raise ApiError("APP 已离线（心跳停滞），请重新打开应用", code="relay_timeout")
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
            raise ApiError("APP 代发超时", code="relay_timeout")
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
        if _in_cooldown(self._client._api_base):
            raise ApiError("上游服务波动中（冷却期），请稍后再试", code="cooldown")
        # A3：relay 链路补重试——代发超时/网络类失败重试最多 2 次。
        # relay 失败不触发端点冷却：APP 离线/超时 ≠ 上游端点故障，
        # 服务器多用户共用同一 api_base，冷却会连坐无辜用户（入口冷却检查仍保留）。
        last_error = None
        for attempt in range(3):
            try:
                return relay_submit(self._client._user_key, payload, self._client._api_base,
                                    timeout=self._client._timeout)
            except ApiError as e:
                last_error = e
                if e.code not in ("relay_timeout", "network") or attempt >= 2:
                    raise
                delay = _jitter_delay(attempt)
                remaining = _check_deadline(self._client)   # 总预算耗尽 → 抛 timeout
                if remaining is not None:
                    delay = min(delay, remaining)   # sleep 不拖过单轮硬顶
                    if delay <= 0:
                        raise ApiError("单轮总预算已耗尽", code="timeout") from e
                logger.warning("[RELAY] 代发失败（%s，重试 %d/2，%.1fs 后）",
                               e.code, attempt + 1, delay)
                time.sleep(delay)
        raise last_error or ApiError("代发失败", code="relay_timeout")


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
