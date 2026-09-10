# -*- coding: utf-8 -*-
"""路由共享状态与工具（routes.py 拆分产物，纯重构无行为变化）。

会话表（按 (sid, mode, 用户作用域) 隔离）、请求体/模式解析、平台标记、
回复写盘、图片目录与配额辅助——供 routes.py 主文件与 routes_* 子模块共用。
"""

import json
import logging
import os
import re
import threading
from urllib.parse import urlparse, parse_qs

from modules import app_config as cfg
from modules.context_manager import ContextManager
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


def _body_mode_ex(body: dict) -> tuple[str, bool]:
    """从请求体取模式，返回 (mode, fell_back)。

    fell_back=True 表示客户端请求了一个**本端未注册**的 mode（例如 PC 本地版自建的角色包，
    在服务器版上没有对应包）→ 调用方应显式告知用户，而不是静默按默认包继续
    （R-07，2026-09-10：静默回退会让用户以为"角色坏了"，且同步链路会把 story 数据
    灌进自建包目录）。"""
    m = body.get("mode", DEFAULT_MODE)
    if m in cfg.MODES:
        return m, False
    logger.warning("未知 mode=%r（本端未注册），回退 %s", m, DEFAULT_MODE)
    return DEFAULT_MODE, bool(m and m != DEFAULT_MODE)


def _body_mode(body: dict) -> str:
    """从请求体取模式，非法回退默认（审查约束）。兼容旧调用点，丢弃 fell_back。"""
    return _body_mode_ex(body)[0]


def _query_mode_ex(h) -> tuple[str, bool]:
    """从 query string 取模式（GET 接口用），返回 (mode, fell_back)。"""
    qs = parse_qs(urlparse(h.path).query)
    m = qs.get("mode", [DEFAULT_MODE])[0]
    if m in cfg.MODES:
        return m, False
    logger.warning("未知 mode=%r（本端未注册），回退 %s", m, DEFAULT_MODE)
    return DEFAULT_MODE, bool(m and m != DEFAULT_MODE)


def _query_mode(h) -> str:
    """从 query string 取模式（GET 接口用）。兼容旧调用点，丢弃 fell_back。"""
    return _query_mode_ex(h)[0]


def _is_server() -> bool:
    """服务器平台标记（FIREFLY_SERVER=1，server_app.py 启动时设置）。
    服务器版禁用本地版专属端点（Key 落盘/安装包下载）用。"""
    return bool(os.environ.get("FIREFLY_SERVER"))


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


# img_id 白名单：合法值由 upload_image 生成（img_ + 12 位 hex）。严格白名单而非黑名单
# （.. / 分隔符），因为 glob pattern 还存在其它危险形态：绝对路径 pattern
# （C:/x、UNC）会让 Path.glob 抛未捕获的 NotImplementedError 打断整个 chat 请求；
# * / [ 等 glob 通配符会意外匹配多文件。安全审查 2026-08-25：pathlib 的 .. 是字面量
# 匹配（实测 3.12 不可穿越），但校验缺失仍违反模块铁律且对未来 Python 行为变化脆弱。
_IMG_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _load_image_data_url(mode: str, img_id: str) -> str | None:
    """本地版首轮识图：按 img_id 找本地文件 → data URL（A9；字节只内存，不落日志）。"""
    from modules.vision import to_data_url
    if not _IMG_ID_RE.fullmatch(img_id or ""):
        return None
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
