# -*- coding: utf-8 -*-
"""本地版登录态存储（A7）— user_data/auth.json

语义（与 02 方案 A7 一致）：
- 登录 = 使用前提：token 由本地后端持有（服务器登录后写盘，与 config.json 分离）；
- 离线宽限 30 天：token 过期时间（服务器下发）未到 → 允许离线使用本地后端；
- 联网时自动 verify（服务器滚动续期：剩余 <7 天续到 30 天，见 server/auth.py verify_and_renew）；
- 超期且未能 verify → 要求重新登录（前端引导）。

模块铁律：审查（类型/路径）→ 处理 → 原子写 → 降级兜底。
"""

import json
import logging
import threading
import time
from pathlib import Path

from modules.app_config import USER_DIR

logger = logging.getLogger(__name__)
_lock = threading.Lock()

AUTH_FILE = USER_DIR / "auth.json"

# 服务器会话有效期（本地判断宽限期用；与 server/auth.py SESSION_DAYS 一致）
SESSION_DAYS = 30
# 本地 expires_at 缺失（老版本）时按保存日 + 30 天兜底
_MISSING_EXPIRES_SLACK = SESSION_DAYS * 86400


def _auth_file() -> Path:
    return AUTH_FILE


def _read_auth() -> dict:
    try:
        data = json.loads(_auth_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_auth(data: dict) -> bool:
    try:
        _auth_file().parent.mkdir(parents=True, exist_ok=True)
        tmp = _auth_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_auth_file())
        return True
    except OSError as e:
        logger.warning("auth.json 写入失败: %s", e)
        return False


def save_login(token: str, email: str, expires_at: str = "") -> bool:
    """登录成功落盘（本地后端持有 token；expires_at 为空 = 服务器未返回，按 SESSION_DAYS 兜底）。"""
    if not isinstance(token, str) or not token.strip():
        return False
    with _lock:
        return _write_auth({
            "token": token.strip(),
            "email": (email or "").strip(),
            "expires_at": expires_at or time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + _MISSING_EXPIRES_SLACK)),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })


def clear_login() -> None:
    with _lock:
        try:
            _auth_file().unlink()
        except OSError:
            pass


def get_state() -> dict:
    """当前登录态（前端 /auth/state 用）。
    {logged_in, email, expires_at, offline_ok}
    offline_ok：离线宽限期内（本地记录的 expires_at 未到）允许继续使用本地功能。"""
    d = _read_auth()
    token = str(d.get("token", "") or "")
    if not token:
        return {"logged_in": False, "email": "", "expires_at": "", "offline_ok": False}
    expires = str(d.get("expires_at", "") or "")
    try:
        from datetime import datetime
        now = datetime.now()
        exp = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
        offline_ok = exp > now
    except Exception:
        # 解析失败保守判定：已过期（要求重新 verify）
        offline_ok = False
    return {"logged_in": True, "email": d.get("email", ""),
            "expires_at": expires, "offline_ok": offline_ok}


def get_token() -> str:
    return str(_read_auth().get("token", "") or "")


def refresh_expires(expires_at: str) -> None:
    """verify 续期后回写服务器返回的新过期时间。"""
    if not isinstance(expires_at, str) or not expires_at:
        return
    d = _read_auth()
    if not d.get("token"):
        return
    d["expires_at"] = expires_at
    d["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        _write_auth(d)
