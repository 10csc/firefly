# -*- coding: utf-8 -*-
"""API 路由 — 每个端点一个函数，POST_ROUTES / GET_ROUTES 分发

server 拆分产物：server.py 只留 HTTP 骨架（分发/响应工具/启动），
业务路由全部在这里。路由函数签名统一为 fn(h)，h 为 handler 实例，
通过 h._json(...) / h._serve_file(...) 回写响应。
"""

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
import hashlib
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from modules import app_config as cfg
from modules.context_manager import ContextManager
from modules.multipart import parse_multipart
from orchestrator import handle_chat
from modules.app_config import DEFAULT_MODE

logger = logging.getLogger(__name__)


# ── 会话状态（按 (sid, mode) 隔离）─────────────────
sessions: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_MAX = 30   # 会话上限：防任意 session_id 无限撑内存（超过删除最早创建的）


def _session_key(sid: str, mode: str) -> str:
    # 服务器版多用户：会话 key 必须带上用户作用域，否则同 session_id（如 "default"）
    # 的两个账号会命中同一份内存上下文/记忆头（跨用户串数据）
    return f"{sid}::{mode}::{cfg.user_scope_key()}"


def get_session(sid: str, mode: str = DEFAULT_MODE) -> dict:
    # ThreadingHTTPServer 下并发首访同一 sid 会重复创建并互相覆盖 context，必须加锁
    key = _session_key(sid, mode)
    with _SESSIONS_LOCK:
        if key not in sessions:
            # 首次创建会话：加载记忆头部（无记忆/中断/异常都降级为空串，不阻塞会话）
            from modules.memory_manager import wake as memory_wake
            from modules.conversation_store import hydrate_context
            from modules.proactive import _restore_active_semaphore
            client = cfg.get_client()
            memory_head = memory_wake(client, cfg.MODEL, mode) if client else ""
            ctx = ContextManager()
            try:
                n = hydrate_context(ctx, mode=mode)
                if n:
                    logger.info("会话 %s[%s] 回灌 %d 轮历史", sid, mode, n)
            except Exception as e:
                logger.warning("历史回灌失败（空上下文启动）: %s", e)
            # 重启兜底：ACTIVE 信号量从 proactive_log 重建（防退出重进刷主动）
            _restore_active_semaphore(mode)
            sessions[key] = {
                "context": ctx,
                "memory_head": memory_head,
                "mode": mode,
                # 会话级锁：chat/rest/undo/clear-history 串行化，防并发读写竞态
                "lock": threading.Lock(),
            }
            # 超限清理（dict 保持插入序 = 创建序，删最早的一个）
            while len(sessions) > _SESSION_MAX:
                oldest = next(k for k in sessions if k != key)
                del sessions[oldest]
        return sessions[key]


# JSON 请求体上限：防异常大 body 吃内存（本地单用户，1MB 足够）
_MAX_BODY = 1_048_576
# 写盘内容上限（手账/用户记忆）：防超大文本占满磁盘（服务器版多用户放大面）
_CONTENT_MAX = 200_000


def _read_json(h) -> dict:
    try:
        length = int(h.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        return {}
    if length <= 0:
        return {}
    if length > _MAX_BODY:
        return {}
    try:
        body = json.loads(h.rfile.read(length))
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _body_mode(body: dict) -> str:
    """从请求体取模式，非法回退默认（审查约束）。"""
    m = body.get("mode", DEFAULT_MODE)
    return m if m in cfg.MODES else DEFAULT_MODE


def _query_mode(h) -> str:
    """从 query string 取模式（GET 接口用）。"""
    qs = parse_qs(urlparse(h.path).query)
    m = qs.get("mode", [DEFAULT_MODE])[0]
    return m if m in cfg.MODES else DEFAULT_MODE


# ══ POST 路由 ═══════════════════════════════════

def _is_server() -> bool:
    """服务器平台标记（FIREFLY_SERVER=1，server_app.py 启动时设置）。
    服务器版禁用本地版专属端点（Key 落盘/安装包下载）用。"""
    return bool(os.environ.get("FIREFLY_SERVER"))


def set_key(h):
    # 服务器版：Key 只存用户浏览器（X-API-Key 头），禁写服务器全局配置（防落盘 + 全局串 Key）
    if _is_server():
        h._json({"ok": False, "error": "服务器版请在设置面板填写 Key（仅存本机浏览器）"}, 403)
        return
    body = _read_json(h)
    # A8：Key 写入激活供应商（providers 结构，不再写顶层字段）
    _p = cfg.active_provider()
    _p["api_key"] = (body.get("api_key") or "").strip()
    cfg._sync_derived()
    cfg.save_config()
    h._json({"ok": bool(cfg.config["api_key"])})


def set_config(h):
    body = _read_json(h)
    is_proxy = (h.headers.get("X-API-Mode", "") or "").strip().lower() == "proxy"
    if _is_server():
        # A2：服务器版 per-user 覆盖——只写 user_data/{uid}/settings.json（全站默认不被污染）
        _USER_KEYS = ("analyzer_model", "organizer_model", "polisher_model", "retriever_model",
                      "retriever_effort", "analyzer_effort", "polisher_effort", "organizer_effort",
                      "retriever_temperature", "polisher_temperature",
                      "proactive_enabled", "proactive_hard", "proactive_soft",
                      "prob_reply_enabled", "prob_reply_value", "hidden_reply_enabled")
        overlay = dict(cfg.get_user_overlay())
        for key in _USER_KEYS:
            if key not in body:
                continue
            if key.endswith("_model"):
                overlay[key] = "mimo-v2.5" if is_proxy else cfg._clean_model(body[key], "deepseek-v4-flash")
            elif key.endswith("_temperature"):
                try:
                    overlay[key] = max(0.0, min(2.0, float(body[key])))
                except (TypeError, ValueError):
                    pass
            elif key.endswith("_effort"):
                if body[key] in cfg.VALID_EFFORTS:
                    overlay[key] = body[key]
            elif key.startswith("proactive_hard"):
                try:
                    overlay[key] = max(1, min(10, int(body[key])))
                except (TypeError, ValueError):
                    pass
            elif key in ("proactive_soft", "prob_reply_value"):
                try:
                    overlay[key] = max(0.0, min(1.0, float(body[key])))
                except (TypeError, ValueError):
                    pass
            else:
                overlay[key] = bool(body[key])
        try:
            from modules.storage import atomic_write_json
            ov_path = cfg.USER_DIR / str(cfg.user_dir_id()) / "settings.json"
            atomic_write_json(ov_path, overlay)
            cfg.set_user_overlay(overlay)
        except Exception as e:
            logger.warning("用户设置覆盖保存失败: %s", e)
        h._json({
            "ok": True,
            "active_provider": cfg.config.get("active_provider", "deepseek"),
            "analyzer_model": overlay.get("analyzer_model", cfg.config["analyzer_model"]),
            "organizer_model": overlay.get("organizer_model", cfg.config["organizer_model"]),
            "polisher_model": overlay.get("polisher_model", cfg.config["polisher_model"]),
            "retriever_model": overlay.get("retriever_model", cfg.config["retriever_model"]),
            "retriever_effort": overlay.get("retriever_effort", cfg.config["retriever_effort"]),
            "analyzer_effort": overlay.get("analyzer_effort", cfg.config["analyzer_effort"]),
            "polisher_effort": overlay.get("polisher_effort", cfg.config["polisher_effort"]),
            "organizer_effort": overlay.get("organizer_effort", cfg.config["organizer_effort"]),
            "retriever_temperature": overlay.get("retriever_temperature", cfg.config["retriever_temperature"]),
            "polisher_temperature": overlay.get("polisher_temperature", cfg.config["polisher_temperature"]),
            "proactive_enabled": bool(overlay.get("proactive_enabled", cfg.config.get("proactive_enabled", True))),
            "proactive_hard": overlay.get("proactive_hard", cfg.config.get("proactive_hard", 4)),
            "proactive_soft": overlay.get("proactive_soft", cfg.config.get("proactive_soft", 0.5)),
            "prob_reply_enabled": bool(overlay.get("prob_reply_enabled", cfg.config.get("prob_reply_enabled", True))),
            "prob_reply_value": overlay.get("prob_reply_value", cfg.config.get("prob_reply_value", 0.3)),
            "hidden_reply_enabled": bool(overlay.get("hidden_reply_enabled", cfg.config.get("hidden_reply_enabled", True))),
        })
        return
    # ── 本地版（单用户写全站配置，行为同 0.8.0）──
    # 服务器版：剥离 api_key 字段（Key 不落服务器全局配置；模型/主动性等全局参数照常）
    new_key = "" if _is_server() else (body.get("api_key") or "").strip()
    # A8：模型名自由输入（官方英文名，无白名单）；仅服务器托管（proxy）模式锁 mimo-v2.5
    for key in ("analyzer_model", "organizer_model", "polisher_model", "retriever_model"):
        val = body.get(key, cfg.config[key])
        if _is_server() and is_proxy:
            cfg.config[key] = "mimo-v2.5"
        else:
            cfg.config[key] = cfg._clean_model(val, cfg.config[key])
    # 供应商管理（本地版：整表替换 + 激活切换；服务器版供应商配置在浏览器 localStorage，不走这里）
    if not _is_server():
        if "providers" in body:
            validated = cfg.normalize_providers(body.get("providers"))
            # 空 Key 视为「保留原 Key」（前端不回传全量 Key，只能看到 has_key 状态）
            for p in validated:
                if not p["api_key"]:
                    old = cfg.provider_by_id(p["id"])
                    if old:
                        p["api_key"] = old.get("api_key", "")
            if validated:
                cfg.config["providers"] = validated
        if "active_provider" in body:
            cfg.set_active_provider(str(body.get("active_provider") or "").strip())
        # 兼容旧接口：api_base → 当前激活供应商 base_url；api_key → 激活供应商 Key
        if "api_base" in body:
            _base = str(body.get("api_base") or "").strip().rstrip("/")
            _p = cfg.active_provider()
            if cfg._valid_http_base(_base):
                _p["base_url"] = _base
                cfg._sync_derived()
    for key in ("retriever_effort", "analyzer_effort", "polisher_effort", "organizer_effort"):
        val = body.get(key, cfg.config[key])
        if val in cfg.VALID_EFFORTS:
            cfg.config[key] = val
    try:
        t = float(body.get("retriever_temperature", cfg.config["retriever_temperature"]))
        cfg.config["retriever_temperature"] = max(0.0, min(2.0, t))
    except (TypeError, ValueError):
        pass
    try:
        t = float(body.get("polisher_temperature", cfg.config["polisher_temperature"]))
        cfg.config["polisher_temperature"] = max(0.0, min(2.0, t))
    except (TypeError, ValueError):
        pass
    eff = body.get("polisher_effort", cfg.config["polisher_effort"])
    if eff in cfg.VALID_EFFORTS:
        cfg.config["polisher_effort"] = eff
    # 主动性插件配置（v2：轮次硬约束 + 概率软约束，替代 v1 时间制）
    if "proactive_enabled" in body:
        cfg.config["proactive_enabled"] = bool(body.get("proactive_enabled"))
    if "proactive_hard" in body:
        try:
            ph = int(body.get("proactive_hard", 4))
            cfg.config["proactive_hard"] = max(1, min(10, ph))
        except (TypeError, ValueError):
            pass
    if "proactive_soft" in body:
        try:
            ps = float(body.get("proactive_soft", 0.5))
            cfg.config["proactive_soft"] = max(0.0, min(1.0, ps))
        except (TypeError, ValueError):
            pass
    # 概率式回复配置
    if "prob_reply_enabled" in body:
        cfg.config["prob_reply_enabled"] = bool(body.get("prob_reply_enabled"))
    if "prob_reply_value" in body:
        try:
            pv = float(body.get("prob_reply_value", 0.3))
            cfg.config["prob_reply_value"] = max(0.0, min(1.0, pv))
        except (TypeError, ValueError):
            pass
    # 隐藏式回复配置（独立开关，关前台概率式不影响隐藏式）
    if "hidden_reply_enabled" in body:
        cfg.config["hidden_reply_enabled"] = bool(body.get("hidden_reply_enabled"))
    if new_key:
        # 本地版：Key 写入激活供应商（providers 结构）
        _p = cfg.active_provider()
        _p["api_key"] = new_key
        cfg._sync_derived()
    cfg.save_config()
    h._json({
        "ok": bool(cfg.config["api_key"]),
        "active_provider": cfg.config.get("active_provider", "deepseek"),
        "api_base": cfg.config.get("api_base", cfg.API_BASE),
        "analyzer_model": cfg.eff_cfg("analyzer_model"),
        "organizer_model": cfg.eff_cfg("organizer_model"),
        "polisher_model": cfg.eff_cfg("polisher_model"),
        "retriever_model": cfg.eff_cfg("retriever_model"),
        "retriever_effort": cfg.eff_cfg("retriever_effort"),
        "analyzer_effort": cfg.eff_cfg("analyzer_effort"),
        "polisher_effort": cfg.eff_cfg("polisher_effort"),
        "organizer_effort": cfg.eff_cfg("organizer_effort"),
        "retriever_temperature": cfg.eff_cfg("retriever_temperature"),
        "polisher_temperature": cfg.eff_cfg("polisher_temperature"),
        "proactive_enabled": bool(cfg.eff_cfg("proactive_enabled", True)),
        "proactive_hard": cfg.eff_cfg("proactive_hard", 4),
        "proactive_soft": cfg.eff_cfg("proactive_soft", 0.5),
        "prob_reply_enabled": bool(cfg.eff_cfg("prob_reply_enabled", True)),
        "prob_reply_value": cfg.eff_cfg("prob_reply_value", 0.3),
        "hidden_reply_enabled": bool(cfg.eff_cfg("hidden_reply_enabled", True)),
    })


def save_journal(h):
    body = _read_json(h)
    mode = _body_mode(body)
    content = body.get("content", "")
    # 审查约束：类型 + 大小上限（防磁盘滥用/非 str 写盘崩 500）
    if not isinstance(content, str):
        h._json({"ok": False, "error": "内容必须为文本"}); return
    if len(content) > _CONTENT_MAX:
        h._json({"ok": False, "error": f"内容过长（上限 {_CONTENT_MAX} 字符）"}); return
    # 路径与 load_journal 同源（llm_base 内按模式公式），避免两处各写一遍公式再次分裂
    from modules.llm_base import reload_journal
    from modules.app_config import mode_journal_dir
    fp = mode_journal_dir(mode) / "手账.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    reload_journal(mode)
    h._json({"ok": True})


def check_key(h):
    # 服务器版：has_key 反映当前用户请求头的 Key（relay 模式 get_client 恒非 None，
    # 不能用它判断）；本地版：等价于原 bool(get_client())
    h._json({"has_key": cfg.user_has_key()})


# ══ A7：本地版认证代理（登录前置化；账号体系仍在服务器）══
# 本地后端把 /auth/* 转发到认证服务器（无 CORS 问题；安卓本地模式同一引擎同享）。
# token 由本地后端持有（user_data/auth.json，与 config.json 分离，服务器不落盘）。
_AUTH_PROXY_TIMEOUT = 15

_AUTH_PROXY_MAP = {
    "/auth/mail-send": "mail-send",
    "/auth/register": "register",
    "/auth/login": "login",
    "/auth/reset-send": "reset-send",
    "/auth/reset-password": "reset-password",
    "/auth/logout": "logout",
    "/auth/verify": "verify",
}


def _auth_server_base() -> str:
    base = str(cfg.config.get("auth_server_url", "") or "").strip().rstrip("/")
    return base if base.startswith(("http://", "https://")) else cfg.AUTH_SERVER_DEFAULT


def _auth_forward(e: str, body: dict, token: str):
    """转发认证请求到服务器。返回 (status, data)；网络不可达返回 (None, error_text)。"""
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        _auth_server_base() + "/auth/" + e,
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_AUTH_PROXY_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        try:
            return ex.code, json.loads(ex.read().decode("utf-8"))
        except Exception:
            return ex.code, {"error": f"认证服务异常（{ex.code}）"}
    except Exception as ex:
        return None, f"无法连接认证服务器（{_auth_server_base()}）: {ex}"


def auth_proxy(h, e: str):
    """POST /auth/<e> 代理（本地版）。登录成功 → 落 auth.json（token 由后端持有）。"""
    if _is_server():
        h._json({"error": "服务器版认证请直接访问本站"}, 403)
        return
    body = _read_json(h)
    token = ""
    auth_hdr = h.headers.get("Authorization", "") or ""
    if auth_hdr.startswith("Bearer "):
        token = auth_hdr[7:].strip()
    status, data = _auth_forward(e, body, token)
    if status is None:
        h._json({"error": data}, 502)
        return
    # 登录成功：本地落盘凭证（不回传 token 给前端——本地后端持有）
    if e == "login" and isinstance(data, dict) and data.get("ok") and data.get("token"):
        from modules.auth_store import save_login
        email = body.get("email", "") or ""
        save_login(str(data.get("token", "")), email,
                   str(data.get("expires_at", "") or ""))
        data = {"ok": True, "email": email, "role": data.get("role", "user"),
                "server": _auth_server_base()}
    # 登出：本地清除凭证
    elif e == "logout" and isinstance(data, dict) and data.get("ok"):
        from modules.auth_store import clear_login
        clear_login()
    h._json(data, status if status and status < 1000 else 400)


def auth_state(h):
    """GET /auth/state（本地版）：登录态；联网时隐式 verify（服务器滚动续期）。"""
    if _is_server():
        h._json({"error": "服务器版请用 /auth/me"}, 403)
        return
    from modules.auth_store import get_state, get_token, clear_login, refresh_expires
    st = get_state()
    if st.get("logged_in"):
        status, data = _auth_forward("verify", {}, get_token())
        if status == 200 and isinstance(data, dict):
            if data.get("expires_at"):
                refresh_expires(str(data["expires_at"]))
            st = get_state()
            st["verified"] = True
        elif status == 401:
            clear_login()      # 服务器判定失效：清凭证（前端引导重新登录）
            st = get_state()
            st["verified"] = False
        else:
            st["verified"] = False   # 网络不可达：保持离线宽限判定
    h._json(st)


# ── 聊天合并窗口（发送即达后端 + 后端 5 秒滑动窗口合并）────────
# 前端 send() 消息实时 POST 后端；后端按 session 合并窗口：
#   主请求（该 session 首个到达）挂起等待窗口结束 → 合并全部消息 → 流水线 → 返回回复；
#   副请求（窗口内到达）消息已入队 → 立即返回 {"queued": True}（回复由主请求带回）。
#   打字中（/chat/hint）重置窗口 deadline 继续等；提交窗口到期（/chat/flush）立即结束。
#   前端切后台冻结不发 flush → 窗口 5 秒自然到期兜底处理（消息已实时在后端，不丢）。
# key 含用户作用域：服务器版多用户各自独立窗口。
_CHAT_WINDOW_SEC = 5.0
_CHAT_WINDOW_IDLE = 600.0   # 窗口无活动超时（秒）：防止 session 废弃后窗口残留撑内存
_CHAT_WINDOW_MAX = 100      # 窗口字典硬上限：异常 session_id 可在无后续请求时残留，超限删最空闲的
_CHAT_WINDOW_LOCK = threading.Lock()
_CHAT_WINDOWS: dict[tuple, dict] = {}   # key -> {"msgs": [...], "deadline": float, "cond": Condition}


def _chat_window_key(session_id: str, mode: str) -> tuple:
    from modules.app_config import user_scope_key
    return (session_id, mode, user_scope_key())


def _chat_window_cleanup(now: float):
    """清理长时间无活动的窗口（session 刷新/用户离开后防内存泄漏）。
    ponytail: 超限时 sorted O(n log n)（n≤100+，可接受）；正常路径只依赖 deadline 过期。"""
    stale = [k for k, w in _CHAT_WINDOWS.items() if now - w["deadline"] > _CHAT_WINDOW_IDLE]
    for k in stale:
        _CHAT_WINDOWS.pop(k, None)
    if len(_CHAT_WINDOWS) > _CHAT_WINDOW_MAX:
        oldest = sorted(_CHAT_WINDOWS,
                        key=lambda k: _CHAT_WINDOWS[k]["deadline"])[:len(_CHAT_WINDOWS) - _CHAT_WINDOW_MAX]
        for k in oldest:
            _CHAT_WINDOWS.pop(k, None)


def _write_replies(result, mode: str) -> list:
    """流萤回复写盘并返回 enriched（带 time 回传前端）。"""
    from modules.conversation_store import append_message as _append_msg
    enriched = []
    for m in result.messages:
        record = {"type": m.get("type")}
        if m.get("type") == "text":
            record["content"] = m.get("content", "")
            seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]}, mode=mode)
        elif m.get("type") == "sticker":
            record["path"] = m.get("path", "")
            record["label"] = m.get("label", "")
            seq, t = _append_msg("firefly", {"type": "sticker", "path": record["path"], "label": record["label"]}, mode=mode)
        elif m.get("type") == "narration":
            # 视觉小说式旁白：scene=居中小字（环境/事件），action=居中括号（动作）
            record["text"] = m.get("text", "")
            record["style"] = m.get("style", "action")
            seq, t = _append_msg("firefly", {"type": "narration", "text": record["text"], "style": record["style"]}, mode=mode)
        else:
            record["content"] = str(m)
            seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]}, mode=mode)
        record["time"] = t
        enriched.append(record)
    return enriched


def _notify_reply_if_background(enriched: list):
    """后台回复完成通知（安卓）：App 不在前台则状态栏提醒（复用隐藏式通知通道）。
    PC/服务器版无 com.firefly.android 模块，try/except 静默跳过。"""
    try:
        from com.firefly.android import KeepAliveService
        if not KeepAliveService.isAppForeground():
            texts = [r.get("content", "") for r in enriched if r.get("type") == "text"]
            if texts:
                # 通知标题带 AI 标识（防"半夜收到真人消息"误解；角色扮演合规）
                KeepAliveService.notify("流萤 · AI", "\n".join(texts)[:200])
    except Exception:
        pass


def _image_dir(mode: str):
    root = cfg.mode_root(mode)
    d = root / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _image_quota_bytes() -> int:
    """每用户图片配额（字节）：环境变量 FIREFLY_IMAGE_QUOTA_MB，默认 200MB。非法值回退默认。"""
    try:
        mb = float(os.environ.get("FIREFLY_IMAGE_QUOTA_MB", "") or 200)
        if mb <= 0:
            mb = 200.0
    except (TypeError, ValueError):
        mb = 200.0
    return int(mb * 1024 * 1024)


def _image_used_bytes() -> int:
    """当前用户所有模式 images/ 已用总字节（配额统计用；服务器版经 _user_ctx 自动隔离）。"""
    total = 0
    for m in cfg.MODES:
        d = cfg.mode_root(m) / "images"
        try:
            if not d.is_dir():
                continue
            for fp in d.iterdir():
                try:
                    if fp.is_file():
                        total += fp.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return total


def _load_image_data_url(mode: str, img_id: str) -> str | None:
    """本地版首轮识图：按 img_id 找本地文件 → data URL（A9；字节只内存，不落日志）。"""
    from modules.vision import to_data_url
    d = _image_dir(mode)
    candidates = sorted(d.glob(img_id + ".*"))
    for fp in candidates:
        try:
            data = fp.read_bytes()
            url = to_data_url(data, fp.suffix)
            if url and len(data) <= 10 * 1024 * 1024:
                return url
        except OSError:
            continue
    return None


def upload_image(h):
    """POST /upload-image（A9）：接收压缩图字节 → 落盘 {mode}/images/ + desc 生成。
    铁律修订：原图不出设备，服务器只存压缩图——服务器版同样落盘
    （cfg.mode_root 经 _user_ctx 按用户目录隔离）。配额：每用户 FIREFLY_IMAGE_QUOTA_MB
    （默认 200MB），统计该用户所有模式 images/ 总字节，超限拒绝。"""
    from modules.vision import to_data_url, describe_image
    import uuid as _uuid
    fields, files = parse_multipart(h, max_bytes=11 * 1024 * 1024)
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少图片文件"}); return
    data = file_info["data"]
    ext = Path(str(file_info.get("filename") or "")).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp/gif 图片格式"}); return
    if not isinstance(data, bytes) or len(data) > 10 * 1024 * 1024:
        h._json({"ok": False, "error": "图片过大（上限 10MB）"}); return
    if _image_used_bytes() + len(data) > _image_quota_bytes():
        h._json({"ok": False, "error": "图片空间已满，请在数据面板清理"}); return
    img_id = "img_" + _uuid.uuid4().hex[:12]
    fp = _image_dir(mode) / (img_id + ext)
    from modules.storage import atomic_write_bytes
    if not atomic_write_bytes(fp, data):
        h._json({"ok": False, "error": "图片保存失败"}); return
    # desc 生成（vision 模型）：失败/不支持 → 空串（前端可让用户手填）。
    # 对 QuotaClient/RelayClient 通用：describe_image 走 client.chat.completions.create，
    # proxy 托管自动锁 vision 模型并记账。
    desc = ""
    client = cfg.get_client()
    try:
        vision_model = (cfg.config.get("vision_model", "deepseek-v4-flash-vision-exp")
                        or "deepseek-v4-flash-vision-exp")
        caps_ok = bool(getattr(client, "_caps", {}).get("vision", True))
        if caps_ok and client is not None:
            url = to_data_url(data, ext)
            if url:
                desc = describe_image(client, vision_model, url)
    except Exception as e:
        logger.warning("图片描述生成跳过: %s", e)
    h._json({"ok": True, "img_id": img_id, "file": fp.name, "desc": desc,
             "need_desc": not desc, "server_side": _is_server()})


def chat(h):
    client = cfg.get_client()
    if not client:
        h._json({"reply": None, "error": "请先设置 API Key", "need_key": True})
        return
    # 服务器版 relay 模式：用户未带 Key → 立即返回 need_key（前端弹设置引导），
    # 否则流水线每个 LLM 阶段 relay 等待 120s 超时（story 模式约 4 分钟）才报错
    if cfg.relay_needs_key():
        h._json({"reply": None, "error": "请先设置 API Key", "need_key": True})
        return

    body = _read_json(h)
    session_id = body.get("session_id", "default")
    hint = (body.get("hint") or "").strip()
    mode = _body_mode(body)

    # 即时写盘：用户消息一发就记。
    # 前端分条发送（messages 数组）→ 分条写盘（刷新后显示多条），
    # LLM 侧用合并文本（\n 连接，保持一轮处理）。
    # 表情包消息：{"type":"sticker","label":...} → 写盘 sticker 类型 + LLM 提示。
    # 引用消息：{"type":"text","content":...,"quote":{...}} → 引用快照随消息写盘，
    # LLM 侧以 [引用流萤：「…」] 前缀体现（长按消息 → 引用功能）。
    from modules.conversation_store import append_message as _append_msg
    from modules.conversation_store import sanitize_quote as _sanitize_quote
    from modules.conversation_store import compose_user_text as _compose_user_text
    msgs = body.get("messages")
    llm_parts = []
    vision_urls = []   # A9：本批图片的 data URL（本地版 direct 首轮识图用；空=不生效）
    if isinstance(msgs, list):
        for m in msgs:
            # 统一消息对象类型：{"type":"text","content":...} / {"type":"sticker","label":...}
            if isinstance(m, dict) and m.get("type") == "text" and m.get("content"):
                text = str(m["content"]).strip()
                if text:
                    q = _sanitize_quote(m.get("quote"))
                    rec = {"type": "text", "content": text}
                    if q:
                        rec["quote"] = q
                    _append_msg("user", rec, mode=mode)
                    llm_parts.append(_compose_user_text(text, q))
            elif isinstance(m, dict) and m.get("type") == "sticker" and m.get("label"):
                label = m["label"]
                path = m.get("path") or m.get("file") or ""
                q = None
                if not path:
                    try:
                        from tools.sticker_picker import pick_sticker_by_label
                        entry = pick_sticker_by_label(label)
                        path = entry.file if entry else ""
                    except Exception:
                        path = ""
                if path:
                    q = _sanitize_quote(m.get("quote"))
                    rec = {"type": "sticker", "label": label, "path": path}
                    if q:
                        rec["quote"] = q
                    _append_msg("user", rec, mode=mode)
                llm_parts.append(_compose_user_text(f"[表情包：{label}]", q))
            elif isinstance(m, dict) and m.get("type") == "image" and m.get("img_id"):
                # A9：jsonl 只存 img_id + desc（图片字节本地持有，不进任何历史/日志）
                img_id = str(m["img_id"]).strip()[:_CONTENT_MAX]
                desc = str(m.get("desc") or "").strip()[:_CONTENT_MAX]
                q = _sanitize_quote(m.get("quote"))
                rec = {"type": "image", "img_id": img_id}
                if desc:
                    rec["desc"] = desc
                if q:
                    rec["quote"] = q
                _append_msg("user", rec, mode=mode)
                llm_parts.append(_compose_user_text(f"[图片：{desc or '（无描述）'}]", q))
                # 首轮识图（决策 12 全链路：本地 direct / proxy / relay 统一——
                # 图片已落盘（服务器版=压缩图），由此读字节转 data URL 进 vision_urls；
                # orchestrator 侧再按客户端能力+模型门控决定是否真的注入 blocks）
                if len(vision_urls) < 4:
                    url = _load_image_data_url(mode, img_id)
                    if url:
                        vision_urls.append(url)
            elif isinstance(m, str) and m.strip():
                # 兼容旧格式（纯字符串）
                _append_msg("user", {"type": "text", "content": m.strip()}, mode=mode)
                llm_parts.append(m.strip())
    if llm_parts:
        user_input = "\n".join(llm_parts)
    else:
        user_input = (body.get("message") or "").strip()
        if user_input:
            _append_msg("user", {"type": "text", "content": user_input}, mode=mode)

    # 用户回应 → 主动性信号量复位（用户发送即解锁主动通道，接上响应式回复）
    from modules.proactive import _active_reset
    _active_reset(mode)

    # 合并窗口入队：消息实时到达后端即安全；窗口按 (session, mode, 用户) 隔离
    if not user_input:
        # 空消息且无 hint：直接降级话术返回，不走 LLM 流水线（防无输入刷完整推理链）；
        # 有 hint（打字中提示）继续走下方流程——typing 场景的产品功能
        if not hint:
            from orchestrator import _handle_direct
            h._json({"messages": [{"type": "text", "content": m}
                                  for m in _handle_direct("input:empty")]})
            return
        # 有 hint 的空消息：不进窗口，直接降级快速返回
        from modules.proactive import reply_lock, reply_unlock
        reply_lock(mode)
        try:
            session = get_session(session_id, mode)
            with session["lock"]:
                result = handle_chat("", session, client,
                                     analyzer_model=cfg.eff_cfg("analyzer_model"),
                                     organizer_model=cfg.eff_cfg("organizer_model"),
                                     polisher_model=cfg.eff_cfg("polisher_model"),
                                     retriever_model=cfg.eff_cfg("retriever_model"),
                                     retriever_effort=cfg.eff_cfg("retriever_effort"),
                                     analyzer_effort=cfg.eff_cfg("analyzer_effort"),
                                     polisher_effort=cfg.eff_cfg("polisher_effort"),
                                     organizer_effort=cfg.eff_cfg("organizer_effort"),
                                     retriever_temperature=cfg.eff_cfg("retriever_temperature"),
                                     polisher_temperature=cfg.eff_cfg("polisher_temperature"),
                                     memory_head=session.get("memory_head", ""),
                                     hint=hint,
                                     mode=mode,
                                     )
            enriched = _write_replies(result, mode)
            resp = {"messages": enriched}
            if result.error_code:
                resp["error_code"] = result.error_code
            h._json(resp)
        finally:
            reply_unlock(mode)
        return

    key = _chat_window_key(session_id, mode)
    with _CHAT_WINDOW_LOCK:
        _chat_window_cleanup(time.time())
        win = _CHAT_WINDOWS.get(key)
        if win is None:
            win = {"msgs": [], "deadline": 0.0, "active": False,
                   "cond": threading.Condition()}
            _CHAT_WINDOWS[key] = win
        with win["cond"]:
            if not win["active"]:
                win["active"] = True          # 本请求成为主请求（窗口首个/上一批已结束）
                is_primary = True
            else:
                is_primary = False
            win["msgs"].append(user_input)          # 入队（消息已写盘，不会丢）
            win["deadline"] = time.time() + _CHAT_WINDOW_SEC   # 新消息重置 5 秒窗口
            if vision_urls:
                win["vision"] = list(win.get("vision") or [])
                win["vision"].extend(vision_urls[:4 - len(win.get("vision") or [])])
            win["cond"].notify_all()

    if not is_primary:
        # 副请求：消息已入队，回复由主请求带回；立即返回，不挂起
        h._json({"queued": True})
        return

    # 主请求：等待窗口结束（滑动 deadline；/chat/hint 重置延长，/chat/flush 立即结束）
    with win["cond"]:
        while True:
            remaining = win["deadline"] - time.time()
            if remaining <= 0:
                merged_msgs = list(win["msgs"])
                win["msgs"] = []
                vision_merge = list(win.get("vision") or [])
                win["vision"] = []
                win["active"] = False   # 释放主请求权：后续消息开新一批
                break
            win["cond"].wait(timeout=min(remaining, 1.0))
    user_input = "\n".join(merged_msgs)
    vision_images = vision_merge  # A9：本批首轮识图 base64 列表（orchestrator 按 caps.vision 决定）

    # 回复通道锁（阻塞）：用户消息不可丢，等待本模式任何主动生成完成后再处理
    # （按模式分锁：不阻塞其他模式的回复通道）
    from modules.proactive import reply_lock, reply_unlock
    reply_lock(mode)
    try:
        session = get_session(session_id, mode)
        # 会话级锁：同会话操作串行（chat 耗时长，防 undo/rest 并发读写 ctx）
        with session["lock"]:
            result = handle_chat(
                user_input, session, client,
                analyzer_model=cfg.eff_cfg("analyzer_model"),
                organizer_model=cfg.eff_cfg("organizer_model"),
                polisher_model=cfg.eff_cfg("polisher_model"),
                retriever_model=cfg.eff_cfg("retriever_model"),
                retriever_effort=cfg.eff_cfg("retriever_effort"),
                analyzer_effort=cfg.eff_cfg("analyzer_effort"),
                polisher_effort=cfg.eff_cfg("polisher_effort"),
                organizer_effort=cfg.eff_cfg("organizer_effort"),
                retriever_temperature=cfg.eff_cfg("retriever_temperature"),
                polisher_temperature=cfg.eff_cfg("polisher_temperature"),
                memory_head=session.get("memory_head", ""),
                hint=hint,
                mode=mode,
                vision_images=vision_images,
            )
        # 即时写盘：流萤回复每条立刻记，并把 time 回传给前端
        enriched = _write_replies(result, mode)
        # 后台回复完成 → 状态栏通知（安卓；PC/服务器版静默跳过）
        _notify_reply_if_background(enriched)
        resp = {"messages": enriched}
        if result.error_code:
            resp["error_code"] = result.error_code   # 错误分类（前端人话提示）
        h._json(resp)
    finally:
        reply_unlock(mode)


def chat_hint(h):
    """用户打字中：重置该 session 合并窗口 deadline（窗口不存在则无操作）。
    语义：输入框有内容（用户在打下一条）→ 流萤继续等，不提前提交。"""
    body = _read_json(h)
    session_id = body.get("session_id", "default")
    mode = _body_mode(body)
    key = _chat_window_key(session_id, mode)
    with _CHAT_WINDOW_LOCK:
        _chat_window_cleanup(time.time())
        win = _CHAT_WINDOWS.get(key)
        if win is not None:
            with win["cond"]:
                win["deadline"] = time.time() + _CHAT_WINDOW_SEC
                win["cond"].notify_all()
    h._json({"ok": True})


def chat_flush(h):
    """前端提交窗口到期：立即结束该 session 合并窗口（主请求从等待中醒来处理）。
    切后台前端冻结不发 flush → 窗口 5 秒自然到期兜底，消息不丢。"""
    body = _read_json(h)
    session_id = body.get("session_id", "default")
    mode = _body_mode(body)
    key = _chat_window_key(session_id, mode)
    with _CHAT_WINDOW_LOCK:
        _chat_window_cleanup(time.time())
        win = _CHAT_WINDOWS.get(key)
        if win is not None:
            with win["cond"]:
                win["deadline"] = time.time()   # 立即到期
                win["cond"].notify_all()
    h._json({"ok": True})


def rest(h):
    client = cfg.get_client()
    if not client:
        h._json({"ok": False, "error": "未设置 API Key"})
        return
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        from modules.memory_manager import MemoryManager
        mm = MemoryManager(client, cfg.MODEL, mode=mode)
        full_history = session["context"].get_full()
        result = mm.rest(full_history, session["context"].turn_count)
        # 休息成功后也更新手账
        if result.success:
            mm.update_journal(full_history[-100:])
            from modules.llm_base import reload_journal
            reload_journal(mode)
            # 立即刷新当前会话的 memory_head：新头部随下一条消息生效，
            # 不再等进程重启 / 30 会话淘汰（真 bug 修复）
            session["memory_head"] = mm.load_head()
    h._json({"ok": result.success, "added": len(result.added_entries),
             "resolved": len(result.resolved_entries), "error": result.error})


def add_sticker_route(h):
    # multipart/form-data 解析：本地版保存图片到 user_data/stickers/；
    # 服务器版（A2 媒体策略）只传输不保存：校验+哈希 → 仅注册文字元数据（file=local:<sha256>.<ext>，
    # 图片本体由前端存 WebView IndexedDB；其它设备无图 → 占位提示「（表情包已失效）」）
    from tools.sticker_picker import add_sticker, StickerAddError
    try:
        fields, files = parse_multipart(h)
        category = fields.get("category", "")
        label = fields.get("label", "")
        file_info = files.get("file")
        if not file_info:
            h._json({"ok": False, "error": "缺少图片文件"}); return
        if category not in ("可爱", "帅气"):
            h._json({"ok": False, "error": "分类必须为 可爱/帅气"}); return
        if not label:
            h._json({"ok": False, "error": "缺少含义描述"}); return

        if _is_server():
            import hashlib
            original = file_info["filename"] or "sticker"
            ext = Path(original).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp/gif 图片格式"}); return
            data = file_info["data"]
            if not isinstance(data, bytes) or len(data) > 10 * 1024 * 1024:
                h._json({"ok": False, "error": "图片过大（上限 10MB）"}); return
            # 服务器对图片字节只传输不保存：仅登记元数据（内容哈希供客户端索引
            # IndexedDB；label 即图片文字描述，已入 LLM 上下文）
            digest = hashlib.sha256(data).hexdigest()
            entry = add_sticker(f"local:{digest}{ext}", category, label)
            h._json({"ok": True, "id": entry.id, "label": entry.label,
                     "file": entry.file, "local": True,
                     "note": "图片已存于本机（服务器不保存图片）"})
            return

        # 保存图片：扩展名白名单（防 .html/.svg 落盘后被静态服务按 MIME 回吐成存储型 XSS），
        # 随机后缀防同秒同名碰撞覆盖
        import secrets
        original = file_info["filename"]
        ext = Path(original).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            h._json({"ok": False, "error": "仅支持 png/jpg/jpeg/webp/gif 图片格式"}); return
        safe_name = f"user_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        # 图片文件与注册表同作用域：服务器版写入 user_data/{uid}/stickers/（账号隔离），
        # 本地版退回 USER_DIR/stickers（行为不变）
        from tools.sticker_picker import _user_registry_file
        save_dir = _user_registry_file().parent
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / safe_name).write_bytes(file_info["data"])

        # 写入注册表
        entry = add_sticker(f"stickers/{safe_name}", category, label)
        h._json({"ok": True, "sticker_id": entry.id, "label": entry.label})
    except StickerAddError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"上传失败: {e}"})


def sticker_update(h):
    from tools.sticker_picker import update_sticker, StickerUpdateError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    new_label = (body.get("label") or "").strip() or None
    new_category = (body.get("category") or "").strip() or None
    new_enabled = body.get("enabled")
    if new_enabled is not None and not isinstance(new_enabled, bool):
        h._json({"ok": False, "error": "enabled 必须是 true/false"}); return
    try:
        entry = update_sticker(sid, new_label=new_label, new_category=new_category,
                               new_enabled=new_enabled)
        h._json({"ok": True, "id": entry.id, "label": entry.label,
                 "category": entry.category, "enabled": entry.enabled})
    except StickerUpdateError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"修改失败: {e}"})


def sticker_delete(h):
    from tools.sticker_picker import delete_sticker, StickerDeleteError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    try:
        delete_sticker(sid)
        h._json({"ok": True, "id": sid})
    except StickerDeleteError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"删除失败: {e}"})


def character_file_update(h):
    body = _read_json(h)
    mode = _body_mode(body)
    filename = (body.get("filename") or "").strip()
    content = (body.get("content") or "")
    # 白名单：仅允许用户维护的补充设定（核心设定 core/identity/sms_samples 隐藏且不可经 API 修改）
    allowed = {"用户设定.md"}
    if filename not in allowed:
        h._json({"ok": False, "error": f"不允许的文件: {filename}"}); return
    if not content:
        h._json({"ok": False, "error": "内容不能为空"}); return
    try:
        filepath = cfg.mode_character_dir(mode) / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        # 清除各模块的角色设定缓存
        from modules.llm_base import clear_cache
        clear_cache()
        from modules.polisher import clear_samples_cache
        clear_samples_cache()
        h._json({"ok": True, "filename": filename})
    except Exception as e:
        h._json({"ok": False, "error": f"保存失败: {e}"})


# ── 设定纠错助手（对齐 → 提案 → 批准应用 → 回滚）────────────
# 权限模型：AI 只提案（pending），用户点「应用」才生效。
# 运行位置：本地版=本地 Python；服务器版=服务器 Python（按用户上下文隔离）。

def _fix_need_key(h) -> bool:
    if not cfg.get_client():
        h._json({"ok": False, "error": "请先设置 API Key", "need_key": True})
        return True
    if cfg.relay_needs_key():
        h._json({"ok": False, "error": "请先设置 API Key", "need_key": True})
        return True
    return False


def _fix_err_code(e: Exception) -> str:
    from modules.api_client import ApiError
    return e.code if isinstance(e, ApiError) else "unknown"


def setting_fix_status(h):
    from modules.setting_fix_store import get_status
    mode = _query_mode(h)
    try:
        h._json(get_status(mode))
    except Exception as e:
        h._json({"ok": False, "error": f"读取状态失败: {e}"})


def setting_fix_message(h):
    if _fix_need_key(h):
        return
    body = _read_json(h)
    mode = _body_mode(body)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        h._json({"ok": False, "error": "请描述她哪里说得不对"})
        return
    text = text.strip()

    from modules.setting_fix import run_alignment, run_proposal
    from modules.setting_fix_store import (locked, load_conversation,
                                           append_conversation, load_pending,
                                           save_pending)
    client = cfg.get_client()
    model = cfg.eff_cfg("polisher_model", "deepseek-v4-flash")
    effort = cfg.eff_cfg("polisher_effort", "high")
    with locked(mode):
        conversation = load_conversation(mode)
        pending = load_pending(mode)
        try:
            if pending is not None:
                # proposal 阶段用户继续补充 → 先记下用户的话，再重新生成方案（仍不生效）
                append_conversation(mode, "user", text, stage="proposal")
                conversation = load_conversation(mode)
                result = run_proposal(client, mode, conversation, model, effort)
            else:
                result = run_alignment(client, mode, conversation, text, model, effort)
        except Exception as e:
            h._json({"ok": False, "error": f"分析失败，请稍后再试: {e}",
                     "error_code": _fix_err_code(e)})
            return

        if "error" in result and not result.get("ok", True):
            h._json(result)
            return

        if pending is not None:
            if result.get("kind") in ("proposal", "no_fix"):
                proposal = {
                    "kind": result.get("kind"),
                    "diagnosis": result.get("diagnosis", ""),
                    "changes": result.get("changes", []),
                    "model": model,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_pending(mode, proposal)
            h._json({"ok": True, "stage": "proposal",
                     "diagnosis": result.get("diagnosis", ""),
                     "changes": result.get("changes", [])})
            return

        append_conversation(mode, "user", text, stage=result.get("stage", "aligning"))
        assistant = append_conversation(
            mode, "assistant", result.get("text", ""),
            options=result.get("options", []), stage=result.get("stage", "aligning"))
        h._json({"ok": True, "stage": result.get("stage", "aligning"),
                 "text": result.get("text", ""),
                 "options": result.get("options", []),
                 "message": assistant})


def setting_fix_start(h):
    if _fix_need_key(h):
        return
    body = _read_json(h)
    mode = _body_mode(body)

    from modules.setting_fix import run_proposal
    from modules.setting_fix_store import locked, load_conversation, save_pending
    client = cfg.get_client()
    model = cfg.eff_cfg("polisher_model", "deepseek-v4-flash")
    effort = cfg.eff_cfg("polisher_effort", "high")
    with locked(mode):
        conversation = load_conversation(mode)
        if not conversation:
            h._json({"ok": False, "error": "请先描述她哪里说得不对"})
            return
        try:
            result = run_proposal(client, mode, conversation, model, effort)
        except Exception as e:
            h._json({"ok": False, "error": f"生成修改方案失败，请稍后再试: {e}",
                     "error_code": _fix_err_code(e)})
            return
        if not result.get("ok"):
            h._json(result)
            return
        proposal = {
            "kind": result.get("kind"),
            "diagnosis": result.get("diagnosis", ""),
            "changes": result.get("changes", []),
            "model": model,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_pending(mode, proposal)
        h._json({"ok": True, "stage": "proposal",
                 "diagnosis": proposal["diagnosis"],
                 "changes": proposal["changes"]})


def setting_fix_apply(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session_id = body.get("session_id", "default")

    from modules.setting_fix_store import locked, apply_pending
    with locked(mode):
        ok, err, applied, version = apply_pending(mode)
    if not ok:
        h._json({"ok": False, "error": err})
        return

    # 若本次改了 memory.md：让当前会话立即用新记忆头部（不等待重启）
    try:
        if "memory.md" in applied:
            session = get_session(session_id, mode)
            from modules.memory_manager import wake as memory_wake
            session["memory_head"] = memory_wake(None, cfg.MODEL, mode)
    except Exception as e:
        logger.warning("应用后重载会话记忆失败: %s", e)

    h._json({"ok": True, "version": version, "applied": applied,
             "message": "修改已生效，可在「最近修正记录」中撤销"})


def setting_fix_dismiss(h):
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.setting_fix_store import locked, dismiss
    with locked(mode):
        ok, err = dismiss(mode)
    h._json({"ok": ok, "error": err})


def setting_fix_rollback(h):
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.setting_fix_store import locked, rollback
    with locked(mode):
        ok, err, version = rollback(mode)
    h._json({"ok": ok, "error": err, "version": version})


def setting_fix_reset(h):
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.setting_fix_store import locked, reset
    with locked(mode):
        ok, err = reset(mode)
    h._json({"ok": ok, "error": err})


def undo(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        result = session["context"].pop_last_turn()
        from modules.conversation_store import remove_last_turn
        removed = remove_last_turn(mode=mode)
        # 撤回后回退 .memory_index：若整合游标 > 当前轮数（说明被撤回轮次
        # 已被记过数），必须压低游标，否则后续 rest 按 turn 号切片会把新的
        # 对话全部误判为"已整理过"而永远跳过（真 bug 修复）
        try:
            from modules.memory_manager import _index_file
            from modules.conversation_store import count_user_turns
            fp = _index_file(mode)
            if fp.exists():
                idx = json.loads(fp.read_text(encoding="utf-8"))
                last_turn = int(idx.get("last_integrated_turn", 0))
                # 以文件为准：重启后内存 context 只回灌近期轮次，
                # session["context"].turn_count 小于真实进度会把游标误压低；
                # 注意必须用用户轮数（与游标同口径），不可用消息总行数
                cur_turn = count_user_turns(mode=mode)
                if last_turn > cur_turn:
                    idx["last_integrated_turn"] = cur_turn
                    fp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("撤回后记忆游标回退失败: %s", e)
    # 以文件为准：重启后内存 context 为空但文件仍有历史，文件删成功就算成功
    if removed > 0 or result is not None:
        h._json({"ok": True, "removed_turn": 1, "files_removed": removed})
    else:
        h._json({"ok": False, "error": "没有可撤回的轮次"})


def clear_history(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        session["context"] = ContextManager()
        # 清空持久化文件
        from modules.conversation_store import conv_file
        try:
            fp = conv_file(mode)
            if fp.exists():
                fp.write_text("", encoding="utf-8")
        except Exception:
            pass
        # 记忆整理进度必须同步归零：turn_count 已归零，旧 index 会让下次
        # 休息时把新对话全部误判为"已整理过"而跳过
        try:
            from modules.memory_manager import _index_file
            fp = _index_file(mode)
            if fp.exists():
                fp.write_text(
                    json.dumps({"last_integrated_turn": 0}, ensure_ascii=False),
                    encoding="utf-8")
        except Exception:
            pass
        # 会话聊天产生的数据全部随历史清理（除配置/已写进设定文件的）：
        # proactive_log（主动判断记录）、pipeline（流水线日志）、
        # 内存信号量（ACTIVE 复位）+ 忽视计数清零
        try:
            from modules.proactive import _log_file, _active_set, _IGNORED, _HIDDEN
            fp = _log_file(mode)
            if fp.exists():
                fp.unlink()
            _active_set(mode, 1)
            _IGNORED.pop(mode, None)
            _HIDDEN.pop(mode, None)   # 隐藏式冷却随历史清理重置
        except Exception:
            pass
        try:
            from modules.app_config import mode_data_dir
            fp = mode_data_dir(mode) / "pipeline.jsonl"
            if fp.exists():
                fp.unlink()
        except Exception:
            pass
    h._json({"ok": True})


# ══ GET 路由 ════════════════════════════════════

def _platform_tag() -> str:
    """当前运行平台：pc（本地版 Windows，可退出）/ android（本地版安卓）/ server（服务器版）。"""
    if os.environ.get("FIREFLY_ANDROID"):
        return "android"
    if os.environ.get("FIREFLY_SERVER"):
        return "server"
    return "pc"


def get_chat_stage(h):
    """流水线阶段进度（前端等待回复时轮询）：?sid=&mode= → {"stage": "retriever"|...|null, "waited": 秒}。
    只读不创建会话；查不到会话返回 null（前端回退默认"对方正在输入…"）。
    waited：当前阶段已等待秒数，供前端在上游卡住时显示超时预警。"""
    q = parse_qs(urlparse(h.path).query)
    sid = (q.get("sid") or [""])[0] or "default"
    mode = (q.get("mode") or [DEFAULT_MODE])[0]
    if mode not in cfg.MODES:
        mode = DEFAULT_MODE
    key = _session_key(sid, mode)
    with _SESSIONS_LOCK:
        session = sessions.get(key)
    if not session:
        h._json({"stage": None, "waited": 0})
        return
    from orchestrator import get_chat_stage as _get_stage, stage_label, get_stage_waited
    stage = _get_stage(session)
    h._json({"stage": stage, "label": stage_label(stage) if stage else None,
             "waited": round(get_stage_waited(session)) if stage else 0})


# 导出/备份打包排除的内部目录（同步冲突备份/设定纠错中间态，不是用户数据）
_EXPORT_EXCLUDE_DIRS = {".sync_backups", ".setting_fix", ".sync_conflicts"}


def _build_backup_zip(root: Path, mode: str) -> bytes:
    """打包模式数据为 zip 字节：{mode} 根全量文件（排除内部目录；images/ 压缩图是
    正式消息数据，进包）+ _config.json（剥离 Key）+ stickers-meta.json（表情包
    文字元数据，图片本体不打包）。export_data 下载与 /backup/create 本地备份共用。
    只读打包，不修改数据。"""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(root.rglob("*")):
            if not fp.is_file():
                continue
            rel = fp.relative_to(root)
            if any(part in _EXPORT_EXCLUDE_DIRS for part in rel.parts):
                continue
            zf.write(fp, rel.as_posix())
        # config.json：全量配置但剥离 API Key（换机迁移带上供应商/base 信息）
        try:
            import copy as _copy
            cfg_copy = _copy.deepcopy(cfg.config)
            cfg_copy.pop("api_key", None)
            for _p in cfg_copy.get("providers") or []:
                _p.pop("api_key", None)
            zf.writestr("_config.json",
                        json.dumps(cfg_copy, ensure_ascii=False, indent=1))
        except Exception:
            pass
        # 表情包元数据：{file名: {label, category, enabled, sha256}}——图片本体不打包。
        # 用户表情包目录与注册表在 USER_DIR/{uid}/stickers/（无模式维度，见 sticker_picker）
        try:
            from tools.sticker_picker import _user_registry_file
            meta = {}
            reg = _user_registry_file()
            sdir = reg.parent if reg is not None else None
            if reg.exists() and sdir is not None:
                data = json.loads(reg.read_text(encoding="utf-8"))
                entries = data if isinstance(data, list) else data.get("stickers", [])
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    fname = str(e.get("file", "")).rsplit("/", 1)[-1]
                    fp = sdir / fname
                    if not fp.exists() or not fp.is_file():
                        continue
                    meta[fname] = {"label": e.get("label", ""), "category": e.get("category", ""),
                                   "enabled": bool(e.get("enabled", True)),
                                   "sha256": hashlib.sha256(fp.read_bytes()).hexdigest()}
            if meta:
                zf.writestr("stickers-meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        except Exception:
            pass
    return buf.getvalue()


def export_data(h):
    """导出当前模式数据为 zip 备份（对话/记忆/手账/设定/聊天图片 + config.json[剥离 Key] +
    表情包文字元数据——换机迁移用）。打包逻辑见 _build_backup_zip。
    带 name 参数时改为下载 backups/ 目录里已有的对应备份包（备份管理列表的「下载」）。
    Content-Disposition: attachment 触发浏览器/WebView 下载。只读打包，不修改数据。"""
    q = parse_qs(urlparse(h.path).query)
    mode = (q.get("mode") or [DEFAULT_MODE])[0]
    if mode not in cfg.MODES:
        mode = DEFAULT_MODE
    name = (q.get("name") or [""])[0]
    if name:
        # 下载既有备份包（名字校验与 /backup/* 同规则，防穿越）
        name = _backup_name_ok(name)
        fp = (_backup_dir(DEFAULT_MODE) / name) if name else None
        if not fp or not fp.is_file():
            h._json({"ok": False, "error": "非法或已不存在的备份名"}, 404)
            return
        data = fp.read_bytes()
        fname = name
    else:
        data = _build_backup_zip(cfg.mode_root(mode), mode)
        fname = f"firefly-backup-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f'attachment; filename="{fname}"')
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


# ══ 数据导入 / 本地备份 ════
# 导出复用 GET /export-data（zip 下载）；导入=multipart zip 覆盖（导入前自动备份 +
# zip slip 防御，见 _import_zip_to_mode）。备份本地化：/backup/* 端点管理
# {用户目录}/backups/（每模式留最近 _BACKUP_KEEP 份），手动云端备份（/sync/upload|download）已下线。
_IMPORT_MAX_BYTES = 60 * 1024 * 1024          # zip 上传上限（含 multipart 开销）
_IMPORT_MAX_FILE_BYTES = 20 * 1024 * 1024     # 包内单文件解压上限
_IMPORT_MAX_TOTAL_BYTES = 100 * 1024 * 1024   # 包内解压总量上限
_BACKUP_KEEP = 10                             # 每模式保留最近手动备份份数


def _zip_safe_entries(zf) -> list[tuple[str, object]]:
    """zip slip 防御：拒绝绝对路径与 .. 穿越，只收普通文件；返回 [(name, info)]。
    超限抛 ValueError（调用方转人话文案）。"""
    out = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        # zip slip 防御：拒绝绝对路径（/ 或 Windows 盘符 C:/）、.. 穿越，只收普通文件
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":") or ".." in name.split("/"):
            raise ValueError("压缩包内含非法路径，已拒绝")
        if info.file_size > _IMPORT_MAX_FILE_BYTES:
            raise ValueError("压缩包内单个文件过大，已拒绝")
        total += info.file_size
        if total > _IMPORT_MAX_TOTAL_BYTES:
            raise ValueError("压缩包解压总量过大，已拒绝")
        out.append((name, info))
    return out


def _backup_dir(mode: str) -> Path:
    """本地备份目录：{用户数据根}/backups/（与模式目录平级，不进导出/同步循环）。
    服务器版经 _user_ctx 自动按用户目录隔离。"""
    return cfg.mode_root(mode).parent / "backups"


def _backup_current_mode(mode: str, prefix: str) -> None:
    """把当前模式数据打成 zip 存到 backups/（导入/恢复前自动备份，防误操作）。空目录跳过。"""
    root = cfg.mode_root(mode)
    if not any(root.rglob("*")):
        return
    import io as _io
    import zipfile as _zipfile
    _backup_dir(mode).mkdir(parents=True, exist_ok=True)
    fp = _backup_dir(mode) / f"{prefix}-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with _zipfile.ZipFile(fp, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(root).as_posix())


def _import_zip_to_mode(data: bytes, mode: str, backup_prefix: str = "auto") -> tuple[bool, str, int]:
    """zip 数据覆盖导入到指定模式（导入前把当前模式备份到 backups/，前缀 backup_prefix）。
    返回 (ok, error, 文件数)。"""
    import io as _io
    import zipfile as _zipfile
    import shutil as _sh
    if not data.startswith(b"PK"):
        return False, "不是有效的 zip 备份文件", 0
    try:
        zf = _zipfile.ZipFile(_io.BytesIO(data))
    except Exception:
        return False, "zip 解析失败（文件损坏？）", 0
    try:
        entries = _zip_safe_entries(zf)
    except ValueError as e:
        return False, str(e), 0

    # 覆盖式导入：先自动备份现有数据，再清空目标目录解压
    try:
        _backup_current_mode(mode, backup_prefix)
    except Exception:
        pass    # 备份失败不阻塞导入（导入包本身是用户拿来的数据源）
    root = cfg.mode_root(mode)
    try:
        if root.exists():
            _sh.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        for name, info in entries:
            dst = root / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dst, "wb") as out:
                _sh.copyfileobj(src, out)
    except Exception as e:
        return False, f"写入失败: {e}", 0
    # 清缓存：设定/手账/短信样本的内存缓存必须重载（旧内容会串进新数据）
    try:
        from modules.llm_base import clear_cache, reload_journal
        from modules.polisher import clear_samples_cache
        clear_cache()
        clear_samples_cache()
        reload_journal(mode)
    except Exception:
        pass
    return True, "", len(entries)


def import_data(h):
    """导入 zip 备份（覆盖当前模式数据）。multipart：mode + file。
    换机/恢复用；导入前自动备份当前模式到 backups/（见 _import_zip_to_mode）。"""
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    file_info = files.get("file")
    if not file_info:
        h._json({"ok": False, "error": "缺少 zip 文件"}); return
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    ok, err, n = _import_zip_to_mode(file_info["data"], mode)
    if not ok:
        h._json({"ok": False, "error": err}); return
    h._json({"ok": True, "files": n, "mode": mode})


# ── 本地备份管理（/backup/*）：{用户目录}/backups/，每模式留最近 _BACKUP_KEEP 份 ──
# 手动云端备份（/sync/upload|download）已下线：备份改本地目录管理，恢复复用导入链路。
_BACKUP_NAME_RE = re.compile(r"^[a-z0-9_\-\.]+\.zip$")


def _backup_name_ok(name) -> str:
    """备份文件名审查：白名单字符 + .zip 结尾，拒绝 .. 与路径分隔符。合法返回名字，否则 ""。"""
    name = str(name or "").strip()
    if not name or len(name) > 120:
        return ""
    if not _BACKUP_NAME_RE.match(name) or ".." in name or "/" in name or "\\" in name:
        return ""
    return name


def _backup_mode_of(name: str) -> str:
    """从备份名解析模式（{mode}-{时间戳}.zip）；不含合法模式返回 ""。"""
    m = name.split("-", 1)[0]
    return m if m in cfg.MODES else ""


def backup_create(h):
    """POST /backup/create {mode}：打包该模式（与 /export-data 同逻辑，见 _build_backup_zip）
    落 backups/{mode}-{yyyymmdd-HHMMSS}.zip，每模式保留最近 _BACKUP_KEEP 份（按名排序删旧）。"""
    body = _read_json(h)
    mode = _body_mode(body)
    try:
        data = _build_backup_zip(cfg.mode_root(mode), mode)
    except Exception as e:
        logger.warning("备份打包失败: %s", e)
        h._json({"ok": False, "error": f"备份打包失败: {e}"}); return
    bdir = _backup_dir(mode)
    fp = None
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        # 秒级时间戳同名碰撞（同一秒内重复创建）→ 追加 _N 后缀保证唯一
        stem = f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}"
        name = f"{stem}.zip"
        i = 1
        while (bdir / name).exists():
            i += 1
            name = f"{stem}_{i}.zip"
        fp = bdir / name
        fp.write_bytes(data)
        # 每模式只留最近 _BACKUP_KEEP 份（文件名 = 模式-时间戳，按名排序即时间序，删旧）。
        # 刚写入的一份不参与淘汰：同秒连建时基底名会被淘汰再复用，不排除会自删
        olds = sorted(p.name for p in bdir.glob(f"{mode}-*.zip") if p.name != name)
        for old in olds[:max(len(olds) + 1 - _BACKUP_KEEP, 0)]:
            try:
                (bdir / old).unlink()
            except OSError:
                pass
    except OSError as e:
        logger.warning("备份写入失败: %s", e)
        h._json({"ok": False, "error": f"备份写入失败: {e}"}); return
    # 输出验证：落盘内容与打包字节一致才算成功
    try:
        if not fp.is_file() or fp.stat().st_size != len(data):
            h._json({"ok": False, "error": "备份写入校验失败"}); return
    except OSError:
        h._json({"ok": False, "error": "备份写入校验失败"}); return
    h._json({"ok": True, "name": name, "mode": mode, "size": len(data)})


def backups_list(h):
    """GET /backups → {ok, backups:[{name,mode,size,time}]}（新→旧）。
    只列手动备份（{mode}-*.zip）；auto-/pre-restore- 自动备份不列入。"""
    out = []
    try:
        bdir = _backup_dir(DEFAULT_MODE)
        if bdir.exists():
            for fp in sorted(bdir.glob("*.zip"), key=lambda p: p.name, reverse=True):
                m = _backup_mode_of(fp.name)
                if not m or not fp.name.startswith(f"{m}-"):
                    continue
                try:
                    st = fp.stat()
                except OSError:
                    continue
                out.append({"name": fp.name, "mode": m, "size": st.st_size,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    except OSError as e:
        logger.warning("备份列表读取失败: %s", e)
    h._json({"ok": True, "backups": out})


def backup_restore(h):
    """POST /backup/restore {name}：恢复指定备份（覆盖该备份所属模式的数据）。
    恢复前先对当前模式打一份 pre-restore 自动备份；解压校验复用 import-data 链路
    （zip slip 防御/单文件与总量上限/覆盖后清缓存，见 _import_zip_to_mode）。"""
    body = _read_json(h)
    name = _backup_name_ok(body.get("name"))
    if not name:
        h._json({"ok": False, "error": "非法备份名"}); return
    mode = _backup_mode_of(name)
    if not mode:
        h._json({"ok": False, "error": "备份名不含合法模式"}); return
    fp = _backup_dir(mode) / name
    if not fp.is_file():
        h._json({"ok": False, "error": "备份不存在"}); return
    try:
        data = fp.read_bytes()
    except OSError as e:
        h._json({"ok": False, "error": f"备份读取失败: {e}"}); return
    ok, err, n = _import_zip_to_mode(data, mode, backup_prefix="pre-restore")
    if not ok:
        h._json({"ok": False, "error": err}); return
    h._json({"ok": True, "restored": name, "mode": mode, "files": n})


def backup_delete(h):
    """POST /backup/delete {name}：删除一份本地备份（含自动备份，按名审查后删除）。"""
    body = _read_json(h)
    name = _backup_name_ok(body.get("name"))
    if not name:
        h._json({"ok": False, "error": "非法备份名"}); return
    fp = _backup_dir(DEFAULT_MODE) / name
    if not fp.is_file():
        h._json({"ok": False, "error": "备份不存在"}); return
    try:
        fp.unlink()
    except OSError as e:
        h._json({"ok": False, "error": f"删除失败: {e}"}); return
    h._json({"ok": True, "deleted": name})


# ══ A1 增量同步（文件级双向：三端同一套 Python，按用户上下文隔离）══
# 协议：客户端（本地后端）直连认证服务器——
#   GET  /sync/manifest            ← 服务器侧清单（该用户 {mode} 目录）
#   POST /sync/import  (multipart) → 服务器合并写入（append 行合并 / 文档新者胜，冲突备份）
#   POST /sync/export  {"files"}   → 服务器侧差异文件 zip（客户端拉回合并）
#   POST /sync/now                 → 本地后端编排：manifest → 对比 → 上传/拉取 → 本地落盘
# images/ 压缩图是正式消息数据：blob 整份双向传输（不解码不合并，按首段目录判定）；
# stickers/ 表情包二进制仍本地策略不进清单（只同步文字元数据，见 sync_engine）。


def _sync_mode_root(mode: str):
    """同步数据根：服务器版=该用户目录（server_app 注入上下文）；本地版=USER_DIR/{mode}。"""
    m = mode if mode in cfg.MODES else DEFAULT_MODE
    return cfg.mode_root(m), m


def sync_manifest(h):
    """服务器侧清单（A1）。GET /sync/manifest?mode=story  → {files: {path: {size,sha256,mtime}}}。"""
    from modules.sync_engine import scan_manifest
    root, m = _sync_mode_root(_query_mode(h))
    h._json({"ok": True, "mode": m, "files": scan_manifest(root)})


def sync_import(h):
    """服务器侧合并写入（A1）。POST /sync/import multipart：
    fields: mode + mtimes(json {path: mtime})；parts: 每个待写文件一个 part（名字 = 相对路径）。
    裁决：jsonl → 行合并；favorites.json → 按 seq/f-id 并集；文档类 → mtime 新者胜（服务器更新则跳过）。
    冲突/覆盖前备份到 {mode}/.sync_backups/（客户端同理）。"""
    from modules.sync_engine import (in_sync_scope, merge_jsonl, merge_favorites,
                                     apply_merge_file, backup_file)
    root, m = _sync_mode_root(DEFAULT_MODE)
    fields, files = parse_multipart(h, max_bytes=_IMPORT_MAX_BYTES)
    mode = fields.get("mode", DEFAULT_MODE)
    if mode not in cfg.MODES:
        h._json({"ok": False, "error": "非法模式"}); return
    root, m = _sync_mode_root(mode)
    try:
        mtimes = json.loads(fields.get("mtimes", "{}") or "{}")
        if not isinstance(mtimes, dict):
            mtimes = {}
    except Exception:
        mtimes = {}
    backups_dir = root / ".sync_backups"
    applied, skipped, conflicts = [], [], []
    for name, info in files.items():
        try:
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or not in_sync_scope(rel, root):
                skipped.append(f"{name}（非法路径/超范围）")
                continue
            data = info["data"]
            if not isinstance(data, bytes):
                data = bytes(data)
            fp = root / rel
            if rel.parts[0] == "images":
                # 图片二进制（blob，正式消息数据）：跳过 utf-8 解码与合并，
                # 配额检查（同 upload_image）后原子落盘
                if _image_used_bytes() + len(data) > _image_quota_bytes():
                    skipped.append(f"{name}（图片空间已满）")
                    continue
                from modules.storage import atomic_write_bytes
                if atomic_write_bytes(fp, data):
                    applied.append(rel.as_posix())
                else:
                    skipped.append(f"{name}（写入失败）")
                continue
            local_text = fp.read_text(encoding="utf-8") if fp.exists() else ""
            remote_text = data.decode("utf-8", errors="replace")
            st = fp.stat() if fp.exists() else None
            remote_mtime = float(mtimes.get(rel.as_posix(), time.time()) or time.time())
            if rel.suffix == ".jsonl":
                merged, cf = merge_jsonl(local_text, remote_text)
                if cf:
                    backup_file(fp, backups_dir, "conflict")
                if merged != local_text:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(merged, encoding="utf-8")
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
                conflicts.extend(cf)
            elif rel.name == "favorites.json":
                merged, cf = merge_favorites(local_text, remote_text)
                if merged != local_text:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(merged, encoding="utf-8")
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
                conflicts.extend(cf)
            else:
                # 文档类：新者胜。服务器已有且 mtime 更新 → 跳过（客户端应 pull 服务器版）
                if st and remote_mtime < st.st_mtime:
                    skipped.append(rel.as_posix() + "（服务器更新，客户端将拉取）")
                elif remote_text != local_text:
                    if st:
                        backup_file(fp, backups_dir, "pre")
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(remote_text, encoding="utf-8")
                    applied.append(rel.as_posix())
                else:
                    skipped.append(rel.as_posix() + "（无变化）")
        except Exception as e:
            logger.warning("同步导入失败 %s: %s", name, e)
            skipped.append(f"{name}（{e}）")
    h._json({"ok": True, "applied": applied, "skipped": skipped,
             "conflicts": len(conflicts)})


def sync_export(h):
    """服务器侧差异文件 zip（A1）。POST /sync/export {"mode","files":[...]} → zip 流。"""
    import io
    import zipfile
    from modules.sync_engine import in_sync_scope
    body = _read_json(h)
    mode = _body_mode(body)
    root, _ = _sync_mode_root(mode)
    files = body.get("files") or []
    if not isinstance(files, list):
        h._json({"ok": False, "error": "files 必须为列表"}); return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files[:200]:   # 上限防超大响应
            try:
                rel = Path(str(name))
                if rel.is_absolute() or ".." in rel.parts or not in_sync_scope(rel, root):
                    continue
                fp = root / rel
                if fp.is_file():
                    zf.write(fp, rel.as_posix())
            except OSError:
                continue
    data = buf.getvalue()
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", 'attachment; filename="sync-diff.zip"')
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def sync_now(h):
    """本地端编排（A1）：本地后端直连认证服务器完成一轮双向同步。
    触发时机：登录态冷启动 / 聊天页回前台 / 设置页手动按钮（W4 前端接线在 W7）。
    范围：对话/记忆/手账/设定/收藏/pipeline 文字类 + images/ 压缩图（blob 整份，
    上传 read_bytes / 拉取 write_bytes，不解码不合并）；stickers/ 表情包二进制不进清单。"""
    if _is_server():
        h._json({"error": "服务器版数据已在云端，无需本端点"}, 403); return
    import io
    import urllib.request
    import zipfile
    from modules.auth_store import get_token
    from modules.sync_engine import (scan_manifest, plan_sync, merge_jsonl,
                                     merge_favorites, backup_file)
    body = _read_json(h)
    mode = _body_mode(body)
    token = get_token()
    if not token:
        h._json({"ok": False, "error": "未登录（请先在首页登录账号）"}); return
    root, m = _sync_mode_root(mode)
    reports = {"uploaded": [], "downloaded": [], "merged": [], "skipped": [], "conflicts": 0}
    base = _auth_server_base()

    def call(path: str, payload: dict | None = None, binary: bool = False):
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        return raw if binary else json.loads(raw.decode("utf-8"))

    try:
        # 1. 服务器清单
        remote = call(f"/sync/manifest?mode={m}").get("files", {})
    except Exception as e:
        h._json({"ok": False, "error": f"连接服务器失败: {e}"}); return

    local = scan_manifest(root)
    plan = plan_sync(local, remote)

    # 2. 上传（本地独有 + 需合并的文件；文档类新者胜在服务器侧裁决）
    upload_paths = plan["to_upload"] + plan["to_merge"]
    if upload_paths:
        # 构建 multipart: 每个文件一个 part（名字=相对路径）
        boundary = "ffsync" + time.strftime("%H%M%S") + "x"
        parts = []
        mtimes = {}
        for name in upload_paths:
            fp = root / name
            if not fp.is_file():
                continue
            try:
                data = fp.read_bytes()
            except OSError:
                continue
            mtimes[name] = round(fp.stat().st_mtime, 3)
            parts.append(
                b"--" + boundary.encode() + b"\r\n"
                + f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n'.encode("utf-8")
                + b"Content-Type: application/octet-stream\r\n\r\n" + data + b"\r\n")
        body_m = (b"--" + boundary.encode() + b"\r\n"
                  + b'Content-Disposition: form-data; name="mode"\r\n\r\n' + m.encode("utf-8") + b"\r\n")
        body_mt = (b"--" + boundary.encode() + b"\r\n"
                   + b'Content-Disposition: form-data; name="mtimes"\r\n\r\n'
                   + json.dumps(mtimes).encode("utf-8") + b"\r\n")
        body_mp = b"".join(parts) + b"--" + boundary.encode() + b"--\r\n"
        req = urllib.request.Request(base + "/sync/import",
                                     data=body_m + body_mt + body_mp,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                rp = json.loads(r.read().decode("utf-8"))
            reports["uploaded"].extend(rp.get("applied", []))
            reports["conflicts"] += int(rp.get("conflicts", 0))
            reports["skipped"].extend(rp.get("skipped", []))
        except urllib.error.HTTPError as e:
            h._json({"ok": False, "error": f"上传失败（{e.code}）"}); return

    # 3. 拉取（服务器独有 + 服务器更新的合并项）→ 本地合并落盘
    local_after = scan_manifest(root)
    need = [p for p in plan["to_download"]]
    # 服务器侧判定"服务器更新"而跳过的上传项 → 拉回
    for p in plan["to_merge"]:
        if (local_after.get(p, {}).get("sha256") or "") != (remote.get(p, {}).get("sha256") or ""):
            need.append(p)
    need = sorted(set(need))
    if need:
        try:
            zip_raw = call("/sync/export", {"mode": m, "files": need}, binary=True)
        except Exception as e:
            h._json({"ok": False, "error": f"拉取失败: {e}"}); return
        with zipfile.ZipFile(io.BytesIO(zip_raw)) as zf:
            backups_dir = root / ".sync_backups"
            for info in zf.infolist():
                name = info.filename
                try:
                    rel = Path(name)
                    if rel.is_absolute() or ".." in rel.parts:
                        continue
                    fp = root / rel
                    if rel.parts[0] == "images":
                        # 图片二进制（blob，正式消息数据）：不解码不合并，整份落盘
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        fp.write_bytes(zf.read(info))
                        reports["downloaded"].append(name)
                        continue
                    remote_text = zf.read(info).decode("utf-8", errors="replace")
                    local_text = fp.read_text(encoding="utf-8") if fp.exists() else ""
                    if rel.suffix == ".jsonl":
                        merged, cf = merge_jsonl(local_text, remote_text)
                        if cf:
                            backup_file(fp, backups_dir, "conflict")
                        reports["conflicts"] += len(cf)
                        if merged != local_text:
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(merged, encoding="utf-8")
                            reports["merged"].append(name)
                    elif rel.name == "favorites.json":
                        merged, cf = merge_favorites(local_text, remote_text)
                        if merged != local_text:
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(merged, encoding="utf-8")
                            reports["merged"].append(name)
                    else:
                        # 文档类：第 1 步抓的远端清单 mtime 与当前文件 mtime 比——
                        # 远端更新才覆盖，否则本地更新保留（防旧远端回滚覆盖本地新内容）
                        st = fp.stat().st_mtime if fp.exists() else 0.0
                        remote_mtime = float((remote.get(str(name)) or {}).get("mtime", 0) or 0)
                        if fp.exists() and remote_mtime <= st + 0.05:
                            reports["skipped"].append(name + "（本地更新，保留）")
                        elif remote_text != local_text:
                            if fp.exists():
                                backup_file(fp, backups_dir, "pre")
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(remote_text, encoding="utf-8")
                            reports["downloaded"].append(name)
                except Exception as e:
                    logger.warning("同步拉取落盘失败 %s: %s", name, e)
                    reports["skipped"].append(f"{name}（{e}）")
    h._json({"ok": True, **reports})


def get_image(h):
    """GET /image?id=<img_id>（A9）：图片字节服务（用户目录 {mode}/images/，按 ext 给 MIME）。
    服务器版同样服务（铁律修订：服务器只存压缩图；cfg.mode_root 经 _user_ctx 按用户隔离）。"""
    qs = parse_qs(urlparse(h.path).query)
    img_id = (qs.get("id", [""])[0] or "").strip()[:200]
    if not img_id or ".." in img_id or "/" in img_id or "\\" in img_id:
        h._json({"error": "非法图片 id"}, 400)
        return
    mode = (qs.get("mode", [DEFAULT_MODE])[0] or DEFAULT_MODE)
    mode = mode if mode in cfg.MODES else DEFAULT_MODE
    from modules.vision import EXT_MIME
    d = _image_dir(mode)
    for fp in sorted(d.glob(img_id + ".*")):
        mime = EXT_MIME.get(fp.suffix.lower())
        if not mime:
            continue
        try:
            data = fp.read_bytes()
            h.send_response(200)
            h.send_header("Content-Type", mime)
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return
        except OSError:
            continue
    h._json({"error": "图片不存在"}, 404)


def get_config(h):
    key = cfg.active_provider().get("api_key", "") or cfg.config.get("api_key", "")
    # 服务器版不返回 key_prefix：全局/env 兜底 Key 的前缀也不能向登录用户暴露
    key_prefix = "" if _is_server() else (key[:12] + "..." if key else "")
    # 供应商列表：Key 只回 has_key + 前缀（不回全量），服务器版供应商配置在浏览器（localStorage）
    providers = []
    for p in (cfg.config.get("providers") or []):
        pk = p.get("api_key", "")
        providers.append({
            "id": p["id"], "name": p.get("name", p["id"]),
            "base_url": p.get("base_url", ""),
            "models": p.get("models", []),
            "caps": p.get("caps", {}),
            "has_key": bool(pk) if not _is_server() else False,
            "key_prefix": (pk[:12] + "...") if (pk and not _is_server()) else "",
        })
    h._json({
        "platform": _platform_tag(),
        "has_key": bool(cfg.get_api_key()),
        "key_prefix": key_prefix,
        "active_provider": cfg.config.get("active_provider", "deepseek"),
        "providers": providers,
        "suggested_providers": cfg.SUGGESTED_PROVIDERS,
        "api_base": cfg.config.get("api_base", cfg.API_BASE),
        "api_bases": [cfg.API_BASE, cfg.GO_BASE],
        "analyzer_model": cfg.eff_cfg("analyzer_model"),
        "organizer_model": cfg.eff_cfg("organizer_model"),
        "polisher_model": cfg.eff_cfg("polisher_model"),
        "retriever_model": cfg.eff_cfg("retriever_model"),
        "retriever_effort": cfg.eff_cfg("retriever_effort"),
        "analyzer_effort": cfg.eff_cfg("analyzer_effort"),
        "polisher_effort": cfg.eff_cfg("polisher_effort"),
        "organizer_effort": cfg.eff_cfg("organizer_effort"),
        "retriever_temperature": cfg.eff_cfg("retriever_temperature"),
        "polisher_temperature": cfg.eff_cfg("polisher_temperature"),
        "proactive_enabled": bool(cfg.eff_cfg("proactive_enabled", True)),
        "proactive_hard": cfg.eff_cfg("proactive_hard", 4),
        "proactive_soft": cfg.eff_cfg("proactive_soft", 0.5),
        "prob_reply_enabled": bool(cfg.eff_cfg("prob_reply_enabled", True)),
        "prob_reply_value": cfg.eff_cfg("prob_reply_value", 0.3),
        "hidden_reply_enabled": bool(cfg.eff_cfg("hidden_reply_enabled", True)),
        "suggested_models": list(cfg.SUGGESTED_MODELS),
        "valid_models": list(cfg.SUGGESTED_MODELS),
        "valid_efforts": list(cfg.VALID_EFFORTS),
    })


def get_metrics(h):
    from modules.metrics import collect
    h._json(collect())


def get_models(h):
    """GET /models?provider=<id>：用该供应商 Key 请求其 /models 取官方模型清单（A8）。
    本地版后端带 Key 转发；服务器版 relay 由前端直连（Key 在浏览器），本端点不起作用。"""
    if _is_server():
        h._json({"error": "服务器版模型清单由浏览器直连供应商获取"}, 403); return
    qs = parse_qs(urlparse(h.path).query)
    pid = (qs.get("provider", [""])[0] or "").strip()
    p = cfg.provider_by_id(pid)
    if p is None:
        h._json({"error": "供应商不存在"}, 404); return
    if not p.get("api_key"):
        h._json({"error": "请先填写该供应商的 API Key"}, 400); return
    import urllib.request
    try:
        req = urllib.request.Request(
            p["base_url"].rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {p['api_key']}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        ids = [str(m.get("id", "")) for m in (data.get("data") or []) if isinstance(m, dict)]
        ids = [i for i in ids if i]
        h._json({"ok": True, "provider": pid, "models": ids})
    except Exception as e:
        h._json({"error": f"获取模型列表失败: {e}"})


def get_balance(h):
    # 查询 DeepSeek 账户余额（仅 DeepSeek 官方端点与激活供应商 Key；其它供应商不支持）
    import urllib.request
    api_base = cfg.config.get("api_base", cfg.API_BASE)
    if not api_base.startswith("https://api.deepseek.com"):
        h._json({"error": "仅 DeepSeek 官方支持余额查询", "supported": False}); return
    try:
        req = urllib.request.Request(
            f"{cfg.API_BASE.replace('/v1', '')}/user/balance",
            headers={"Authorization": f"Bearer {cfg.get_api_key()}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            h._json(json.loads(r.read()))
    except Exception as e:
        h._json({"error": str(e)})


def get_requests(h):
    from modules.llm_base import get_request_log
    log = get_request_log(200)
    h._json({"requests": log, "count": len(log)})


def get_pipeline(h):
    # 每轮对话各阶段的输入/输出/思考过程（调试答非所问用）
    from orchestrator import get_pipeline_log
    log = get_pipeline_log(20, mode=_query_mode(h))
    h._json({"pipeline": log, "count": len(log)})


def get_history(h):
    # 分页加载历史：?limit=150&before_seq=N&mode=story
    from modules.conversation_store import load_recent, get_total_count, get_min_seq
    mode = _query_mode(h)
    qs = parse_qs(urlparse(h.path).query)
    try:
        limit = int(qs.get("limit", ["150"])[0])
    except Exception:
        limit = 150
    # 审查约束：钳制到 [1, 500]（防 limit=10**9 全量读 jsonl 进内存放大）
    limit = max(1, min(limit, 500))
    before_seq_raw = qs.get("before_seq", [None])[0]
    # 审查约束：非法 before_seq 回退 None（防 /history?before_seq=abc 崩 500；同函数 limit 已有 try）
    try:
        before_seq = int(before_seq_raw) if before_seq_raw else None
    except (TypeError, ValueError):
        before_seq = None
    msgs = load_recent(limit=limit, before_seq=before_seq, mode=mode)
    total = get_total_count(mode=mode)
    # has_more：当前页最小 seq > 全局最小 seq 时还有更早历史
    cur_min = int(msgs[0]["seq"]) if msgs else 0
    has_more = cur_min > get_min_seq(mode=mode) if total > 0 else False
    h._json({"messages": msgs, "total": total, "has_more": has_more})


def get_wake_status(h):
    from modules.memory_manager import _index_file, _memory_file
    mode = _query_mode(h)
    interrupted = _index_file(mode).exists() and not _memory_file(mode).exists()
    h._json({"interrupted": interrupted, "has_memory": _memory_file(mode).exists()})


def get_stickers(h):
    # 返回表情包列表。?enabled=1 只返回启用项（聊天页选择面板）；
    # 不带参数返回全量（管理页，含停用项）。editable：当前用户可改/删的条目。
    from tools.sticker_picker import list_all_stickers, _STICKERS_DEFAULT, editable_ids
    qs = parse_qs(urlparse(h.path).query)
    enabled_only = qs.get("enabled", [""])[0] == "1"
    ids = editable_ids()
    editable_all = not cfg.user_scope_key()   # 本地版（无用户上下文）全部可编辑
    items = list_all_stickers()
    if enabled_only:
        items = [s for s in items if s.enabled]
    h._json({
        "stickers": [
            {"id": s.id, "file": s.file, "category": s.category,
             "label": s.label, "enabled": bool(s.enabled),
             "is_default": s.id in _STICKERS_DEFAULT,
             "editable": editable_all or s.id in ids}
            for s in items
        ],
    })


def get_character_files(h):
    from modules.llm_base import resolve_character_file
    mode = _query_mode(h)
    files = []
    # 核心设定已隐藏，仅暴露用户可维护的补充设定文件
    for fname in ("用户设定.md",):
        fp = resolve_character_file(fname, mode)
        if fp.exists():
            files.append({"name": fname, "content": fp.read_text(encoding="utf-8")})
    h._json({"files": files})


def get_user_memory(h):
    # 用户记忆 = memory.md（休息时自动整理的过往摘要），展示为可编辑
    from modules.memory_manager import _memory_file
    mode = _query_mode(h)
    fp = _memory_file(mode)
    content = fp.read_text(encoding="utf-8") if fp.exists() else ""
    h._json({"content": content})


def save_user_memory(h):
    from modules.memory_manager import _memory_file
    body = _read_json(h)
    mode = _body_mode(body)
    content = (body.get("content") or "")
    # 审查约束：类型 + 大小上限（与 save_journal 同规则）
    if not isinstance(content, str):
        h._json({"ok": False, "error": "内容必须为文本"}); return
    if len(content) > _CONTENT_MAX:
        h._json({"ok": False, "error": f"内容过长（上限 {_CONTENT_MAX} 字符）"}); return
    try:
        fp = _memory_file(mode)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        h._json({"ok": True})
    except Exception as e:
        h._json({"ok": False, "error": f"保存失败: {e}"})


def get_journal(h):
    from modules.llm_base import load_journal
    h._json({"content": load_journal(_query_mode(h))})


# ═══ 收藏夹（长按消息 → 收藏）═══
def add_favorite_route(h):
    from modules.favorite_store import add_favorite, FavoriteError
    body = _read_json(h)
    mode = _body_mode(body)
    try:
        rec = add_favorite(mode, body.get("message"))
    except FavoriteError as e:
        h._json({"ok": False, "error": str(e)})
        return
    except Exception as e:
        logger.warning("收藏失败: %s", e)
        h._json({"ok": False, "error": "收藏失败，请重试"})
        return
    h._json({"ok": True, "id": rec["id"]})


def get_favorites(h):
    from modules.favorite_store import list_favorites
    h._json({"items": list_favorites(_query_mode(h))})


def delete_favorite_route(h):
    from modules.favorite_store import delete_favorite
    body = _read_json(h)
    mode = _body_mode(body)
    fav_id = (body.get("id") or "").strip()
    if not fav_id:
        h._json({"ok": False, "error": "缺少收藏 id"})
        return
    ok = delete_favorite(mode, fav_id)
    h._json({"ok": ok, "error": "" if ok else "收藏不存在或已删除"})


def open_mode(h):
    """模式开场演出：haruno 首次进入时返回自动首条消息（旁白+流萤的话）。

    幂等保护：会话已有历史时不再重复开场（重进不重演）。
    """
    body = _read_json(h)
    mode = _body_mode(body)
    if mode == "haruno":
        from modules.conversation_store import get_total_count
        if get_total_count(mode="haruno") == 0:
            from orchestrator import haruno_opening
            msgs = haruno_opening()
            h._json({"messages": msgs, "opened": True})
            return
    h._json({"messages": [], "opened": False})


def proactive_status(h):
    """主动性检查入口：REPLY 预占用 → 主动式/概率式串联判断 → 生成 → 写盘。

    前端轮询调用（空闲时）；每次调用都是独立判断，门控不通过则零成本返回
    {"messages": []}。生成的主动消息直接写盘，返回 messages 供前端即时渲染
    （与 /chat 返回格式一致）。

    信号量：REPLY 非阻塞预占用（忙碌则放弃）；ACTIVE 在 proactive 模块内
    管理（主动式/概率式互斥 + 用户回应复位 + 超时恢复）。
    """
    client = cfg.get_client()
    if not client or cfg.relay_needs_key():
        # 服务器版 relay 模式用户未带 Key：零成本返回，避免轮询线程空等 120s relay 超时
        h._json({"messages": []}); return
    body = _read_json(h)
    mode = _body_mode(body)
    session_id = body.get("session_id", "default")

    from modules.proactive import check_and_generate, reply_try_lock, reply_unlock
    if not reply_try_lock(mode):
        h._json({"messages": []}); return   # 回复通道忙（响应式生成中/其他主动生成中）
    try:
        session = get_session(session_id, mode)
        with session["lock"]:
            result = check_and_generate(
                session, client, mode=mode,
                enabled=bool(cfg.eff_cfg("proactive_enabled", True)),
                hard=cfg.eff_cfg("proactive_hard", 4),
                soft=cfg.eff_cfg("proactive_soft", 0.5),
                prob_enabled=bool(cfg.eff_cfg("prob_reply_enabled", True)),
                prob_value=cfg.eff_cfg("prob_reply_value", 0.3),
                polisher_model=cfg.eff_cfg("polisher_model"),
                polisher_effort=cfg.eff_cfg("polisher_effort"),
                polisher_temperature=cfg.eff_cfg("polisher_temperature"),
                organizer_model=cfg.eff_cfg("organizer_model"),
                organizer_effort=cfg.eff_cfg("organizer_effort"),
                memory_head=session.get("memory_head", ""),
            )
    finally:
        reply_unlock(mode)
    if not result.messages or result.discarded:
        h._json({"messages": [], "reason": result.reason_type})
        return
    h._json({"messages": result.messages, "proactive": True})


# ══ 自动更新 ════════════════════════════════════
# 规范（见 docs/版本更新规范.md）：
# - 检测源双源：GitHub 优先（语义严格），失败降级 Gitee
# - 下载 URL 固定 Gitee 优先（国内用户下载快），GitHub 降级——检测与下载解耦
# - 资产名固定 firefly-setup.exe / firefly.apk（按扩展名匹配，不依赖版本号，跳版本天然兼容）
# - 版本号只认 x.y.z 纯数字；前后端版本对比统一以 APP_VERSION 为权威
_UPDATE_SOURCES = (
    ("https://api.github.com/repos/10csc/firefly/releases/latest",
     "https://github.com/10csc/firefly/releases"),
    ("https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest",
     "https://gitee.com/cpt-asymmetry/firefly/releases"),
)
# 下载源顺序：Gitee 资产优先（国内直连快），GitHub 降级
_DOWNLOAD_SOURCES = (
    "https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest",
    "https://api.github.com/repos/10csc/firefly/releases/latest",
)

# ── 下载加固（轻量：无 sha256 链路，防 URL 投毒与无节制下载）──
_DOWNLOAD_KINDS = ("exe", "apk")
_DOWNLOAD_MAX_BYTES = {"exe": 200 * 1024 * 1024, "apk": 100 * 1024 * 1024}
_DOWNLOAD_MIN_BYTES = 100 * 1024          # 防错误页 HTML 冒充资产
# 域名白名单：Gitee/GitHub 资产域。校验初始 URL 的 host；
# urllib 自动跟随 GitHub 官方重定向（objects.githubusercontent.com），重定向链信任官方域。
_DOWNLOAD_HOSTS = ("gitee.com", "github.com")


def _validate_download_url(url: str, kind: str) -> str:
    """下载 URL 审查：https + 域名白名单 + 扩展名与 kind 一致。返回错误文案（""=通过）。"""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https":
        return "下载地址必须为 https"
    if not any(host == h or host.endswith("." + h) for h in _DOWNLOAD_HOSTS):
        return "下载地址域名不在白名单"
    suffix = ".apk" if kind == "apk" else ".exe"
    if not p.path.lower().endswith(suffix):
        return "下载地址与资产类型不符"
    return ""


def _fetch_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Firefly/" + cfg.APP_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _match_asset(assets, pattern):
    for a in assets or []:
        # 审查：非 dict 资产项直接跳过（防 API 脏数据导致 AttributeError 崩掉检测链）
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        # 匹配 name 或 URL 任一（防资产 name 不带扩展名但 URL 是 .exe/.apk 的漏检）
        if pattern.search(name) or pattern.search(url):
            return url or name
    return ""


def get_latest_release():
    """返回 (tag, html_url)。检测源：GitHub 优先，Gitee 降级。"""
    for api, html in _UPDATE_SOURCES:
        try:
            data = _fetch_json(api)
            tag = str(data.get("tag_name") or "").lstrip("v")
            if tag:
                return tag, html
        except Exception:
            continue
    return None


def _get_asset_url(kind: str) -> str:
    """按下载源顺序找资产 URL（Gitee 优先，GitHub 降级）。"""
    import re
    pat = re.compile(r"\.exe$", re.I) if kind == "exe" else re.compile(r"\.apk$", re.I)
    for api in _DOWNLOAD_SOURCES:
        try:
            data = _fetch_json(api)
            url = _match_asset(data.get("assets"), pat)
            if url:
                return url
        except Exception:
            continue
    return ""


def check_update(h):
    info = get_latest_release()
    if not info:
        h._json({"ok": False, "error": "检查失败（网络或仓库不可达）"})
        return
    tag, html = info
    h._json({
        "ok": True, "tag": tag, "current": cfg.APP_VERSION,
        "html_url": html,
    })


def update_download(h):
    """下载发行版资产到临时目录（轻量加固：kind 白名单 + https/域名校验 + 大小上限）。
    PC(exe)：下载后由后端静默启动安装器（/VERYSILENT 覆盖安装，保留 user_data），
             服务器随之关闭（安装器接管）；安卓(apk)：仅下载，前端引导系统安装器。
    服务器版禁用：检查更新走 version.json；此端点会把资产下载到服务器磁盘/带宽，
    任何登录用户可反复触发（防磁盘填满与 3Mbps 带宽耗尽）。"""
    if _is_server():
        h._json({"ok": False, "error": "服务器版请从下载页获取安装包"}, 403)
        return
    body = _read_json(h)
    kind = body.get("kind", "exe")
    if kind not in _DOWNLOAD_KINDS:
        h._json({"ok": False, "error": "不支持的资产类型"})
        return
    url = _get_asset_url(kind)
    if not url:
        h._json({"ok": False, "error": "发行版未附安装包资产或仓库不可达"})
        return
    err = _validate_download_url(url, kind)
    if err:
        h._json({"ok": False, "error": err})
        return
    try:
        import tempfile
        local = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Firefly/" + cfg.APP_VERSION})
            max_size = _DOWNLOAD_MAX_BYTES[kind]
            with urllib.request.urlopen(req, timeout=600) as resp, tempfile.NamedTemporaryFile(
                    suffix=".apk" if kind == "apk" else ".exe", delete=False, dir=tempfile.gettempdir()) as out:
                local = out.name
                # Content-Length 预检 + 流式累计兜底（防无/伪造 Content-Length）
                cl = (getattr(resp, "headers", None) or {}).get("Content-Length")
                if cl:
                    try:
                        if int(cl) > max_size:
                            raise ValueError("文件过大")
                    except (TypeError, ValueError):
                        raise
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError("文件过大")
                    out.write(chunk)
            if total < _DOWNLOAD_MIN_BYTES:
                raise ValueError("文件异常过小，疑似错误页面")
        except Exception:
            # 下载中途失败：清理残留临时文件（防垃圾堆积）
            if local:
                try:
                    Path(local).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        if kind == "exe" and getattr(sys, "frozen", False):
            # PC 发行版：静默启动安装器（覆盖安装保留 user_data），本服务随之退出
            import subprocess
            subprocess.Popen([local, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"])
            # 优雅关闭自身：请求 /shutdown（保存文件后退出），安装器接管
            import threading as _t
            def _close():
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://127.0.0.1:{cfg.PORT}/shutdown", timeout=2)
                except Exception:
                    pass
            _t.Timer(2.0, _close).start()
            h._json({"ok": True, "path": local, "installing": True})
            return
        h._json({"ok": True, "path": local})
    except Exception as e:
        h._json({"ok": False, "error": f"下载失败: {e}"})




# ══ 后端代理（relay 中转）═════════════════════════
# 用户 Key 模式：服务器构建请求体（含资产占位符）→ APP 代发 DeepSeek（用户 Key）→ 回传。
# 服务器不持有用户 Key；资产（知识库/设定）在 APP 本地，占位符由 APP 填充。
def relay_pending(h):
    """APP 轮询：取待代发的 LLM 请求体（可能含 __KNOWLEDGE__ 等资产占位符）。"""
    from modules.api_client import relay_pending as _pending
    item = _pending(cfg.user_scope_key() or "local")
    if item:
        h._json({"pending": True, "call_id": item["call_id"],
                 "payload": item["payload"], "api_base": item["api_base"]})
    else:
        h._json({"pending": False})


def relay_result(h):
    """APP 回传 DeepSeek 响应，唤醒等待中的流水线线程（带 HTTP 状态码做错误分类）。"""
    body = _read_json(h)
    from modules.api_client import relay_result as _result
    try:
        status = int(body.get("status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    ok = _result(cfg.user_scope_key() or "local",
                 body.get("call_id", ""), body.get("response") or {}, status=status)
    h._json({"ok": ok})


def relay_proxy(h):
    """中转降级：APP 直连 api_base 被 CORS 拦截（如 OpenCode Go 端点不支持浏览器
    跨域）时，服务器用本请求的 X-API-Key 代发并回传结果。Key 仅内存即弃不落盘。

    防滥用：call_id 必须匹配该用户队列中真实 pending 项（服务器自己入队的请求），
    否则 404——中转不是开放代理。payload 用前端已填充占位符的版本（资产在 APP 本地，
    服务器只有占位符版本）；payload 只影响用户自己的 Key 调用，无越权面。"""
    body = _read_json(h)
    call_id = str(body.get("call_id") or "")
    user_key = cfg.user_scope_key() or "local"
    from modules.api_client import relay_has, relay_result as _result
    if not relay_has(user_key, call_id):
        h._json({"ok": False, "error": "无此待发请求"}, 404)
        return
    payload = body.get("payload")
    if not isinstance(payload, dict):
        h._json({"ok": False, "error": "payload 缺失"}, 400)
        return
    key = (h.headers.get("X-API-Key", "") or "").strip()
    if not key:
        h._json({"ok": False, "error": "缺少 API Key（请先设置）"}, 400)
        return
    # api_base 从队列项取（服务器入队时校验过白名单），不信任前端传值
    from modules.api_client import _relay_queues, _relay_lock
    with _relay_lock:
        q = _relay_queues.get(user_key) or []
        item = next((i for i in q if i["call_id"] == call_id), None)
        api_base = (item or {}).get("api_base", cfg.API_BASE)
    try:
        import requests as _requests
        resp = _requests.post(api_base.rstrip("/") + "/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=payload, timeout=120)
        try:
            data = resp.json()
        except ValueError:
            data = {"error": {"message": f"中转响应解析失败（HTTP {resp.status_code}）"}}
        # 带真实状态码回传：错误响应（401/402/429/5xx）由服务器转成分类错误唤醒流水线
        _result(user_key, call_id, data, status=resp.status_code)
        h._json({"ok": True, "response": data})
    except Exception as e:
        h._json({"ok": False, "error": f"中转失败: {e}"}, 502)


# ══ 资产清单（服务器告诉 APP 用哪些资产）═══════════
_ASSET_MD5_CACHE: dict[str, str] = {}
_ASSET_MD5_LOCK = threading.Lock()


def _asset_md5(text: str) -> str:
    """资产版本指纹（内容 hash 前 8 位，内容变化即版本变化）。"""
    import hashlib
    with _ASSET_MD5_LOCK:
        key = text[:64]
        if key in _ASSET_MD5_CACHE:
            return _ASSET_MD5_CACHE[key]
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
        _ASSET_MD5_CACHE[key] = digest
        return digest


def assets_index(h):
    """资产清单：服务器"告诉 APP 要用哪些资产"（版本指纹+大小）。
    APP 比对本地版本，缺失/过期则从 /assets/raw 下载。?mode=story|haruno（默认 story）。"""
    from modules.llm_retriever import _load_knowledge, get_knowledge_stats
    from modules.llm_base import resolve_character_file

    mode = _query_mode(h)
    kb = _load_knowledge(mode)
    stats = get_knowledge_stats(mode)

    def _char_asset(name):
        # resolve_character_file 不拼后缀（load_slot 才拼），这里显式拼 .md
        fp = resolve_character_file(name + ".md", mode)
        if fp.exists():
            content = fp.read_text(encoding="utf-8")
            return {"version": _asset_md5(content), "size": len(content)}
        return {"version": "0", "size": 0}

    h._json({
        "mode": mode,
        "knowledge": {"version": _asset_md5(kb), "size": len(kb),
                      "chars": stats.get("chars", 0)},
        "character": {
            "core": _char_asset("core"),
            "identity": _char_asset("identity"),
            "sms_samples": _char_asset("sms_samples"),
        },
    })


def assets_raw(h):
    """资产下载（认证后可用）：APP 首次本地化 / 更新时拉取。
    ?name=knowledge|core|identity|sms_samples&mode=story|haruno"""
    from modules.llm_retriever import _load_knowledge
    from modules.llm_base import resolve_character_file
    qs = parse_qs(urlparse(h.path).query)
    name = qs.get("name", [""])[0]
    mode = (qs.get("mode", [DEFAULT_MODE])[0] or DEFAULT_MODE)
    mode = mode if mode in cfg.MODES else DEFAULT_MODE
    if name == "knowledge":
        h._json({"name": name, "content": _load_knowledge(mode)})
        return
    if name in ("core", "identity", "sms_samples"):
        fp = resolve_character_file(name + ".md", mode)
        if fp.exists():
            h._json({"name": name, "content": fp.read_text(encoding="utf-8")})
            return
        h._json({"error": "资产不存在"}, 404)
        return
    h._json({"error": "未知资产"}, 404)


# ── 分发表 ───────────────────────────────────────
def _mk_auth_proxy(e: str):
    return lambda h: auth_proxy(h, e)


POST_ROUTES = {
    "/set-key": set_key,
    "/set-config": set_config,
    "/save-journal": save_journal,
    "/save-user-memory": save_user_memory,
    "/check-key": check_key,
    "/chat": chat,
    "/chat/hint": chat_hint,
    "/chat/flush": chat_flush,
    "/open-mode": open_mode,
    "/proactive-status": proactive_status,
    "/rest": rest,
    "/add-sticker": add_sticker_route,
    "/sticker-update": sticker_update,
    "/sticker-delete": sticker_delete,
    "/character-file-update": character_file_update,
    "/check-update": check_update,
    "/update-download": update_download,
    "/relay/pending": relay_pending,
    "/relay/result": relay_result,
    "/relay/proxy": relay_proxy,
    "/import-data": import_data,
    "/backup/create": backup_create,
    "/backup/restore": backup_restore,
    "/backup/delete": backup_delete,
    "/sync/import": sync_import,
    "/sync/export": sync_export,
    "/sync/now": sync_now,
    "/undo": undo,
    "/clear-history": clear_history,
    "/setting-fix/message": setting_fix_message,
    "/setting-fix/start": setting_fix_start,
    "/setting-fix/apply": setting_fix_apply,
    "/setting-fix/dismiss": setting_fix_dismiss,
    "/setting-fix/rollback": setting_fix_rollback,
    "/setting-fix/reset": setting_fix_reset,
    "/favorite": add_favorite_route,
    "/favorites/delete": delete_favorite_route,
    "/upload-image": upload_image,
}
# A7：本地版认证代理端点（账号体系在服务器；本地模式前端同源调用）
for _a_path, _a_ep in _AUTH_PROXY_MAP.items():
    POST_ROUTES[_a_path] = _mk_auth_proxy(_a_ep)

GET_ROUTES = {
    "/check-key": check_key,
    "/config": get_config,
    "/models": get_models,
    "/sync/manifest": sync_manifest,
    "/image": get_image,
    "/auth/state": auth_state,
    "/chat-stage": get_chat_stage,
    "/metrics": get_metrics,
    "/balance": get_balance,
    "/requests": get_requests,
    "/pipeline": get_pipeline,
    "/history": get_history,
    "/wake-status": get_wake_status,
    "/stickers": get_stickers,
    "/character-files": get_character_files,
    "/user-memory": get_user_memory,
    "/journal": get_journal,
    "/export-data": export_data,
    "/backups": backups_list,
    "/setting-fix/status": setting_fix_status,
    "/assets/index": assets_index,
    "/assets/raw": assets_raw,
    "/favorites": get_favorites,
}
