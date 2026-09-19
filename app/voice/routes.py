# -*- coding: utf-8 -*-
"""语音插件 — HTTP 端点（三个）

  GET  /voice/status              → 插件状态（前端据此决定菜单项可点性与原因）
  POST /voice/tts                 → {"seq","mode","mood"} → 合成并落盘（幂等）
  GET  /voice-file?mode=&name=    → 返回 wav 字节（前端 <audio> 直接用）

放在本子系统内（而不是塞进 `api/`），保持"独立子系统、耦合面收窄"的形态；
与外界的接线只有 `api/router.py` 里的三行登记。
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import voice
from voice import store

# 语音文件名白名单：只允许 v<数字>.wav（防目录穿越 —— 名字由我们生成，不需要更宽的语法）
_NAME_RE = re.compile(r"^v(\d+)\.wav$")


def voice_status(h):
    """插件状态。任何异常都转成"不可用 + 原因"，不 500。"""
    try:
        h._json(voice.status())
    except Exception as e:
        h._json({"engine_ok": False, "reason": f"读取语音插件状态失败: {type(e).__name__}: {e}",
                 "moods": {}, "models_found": False}, 200)


def voice_tts(h):
    """把某条消息转成语音。永远返回 200 + {ok:false,error}（业务失败不当作 HTTP 错误，
    前端只关心"能不能播"）。"""
    from routes_common import _body_mode, _read_json
    body = _read_json(h) or {}
    mode = _body_mode(body)
    seq = body.get("seq")
    mood = (body.get("mood") or "").strip() or None
    force = bool(body.get("force"))
    try:
        res = voice.synthesize(seq, mode, mood, force=force)
    except Exception as e:                       # 兜底：绝不把异常抛给 HTTP 层
        res = {"ok": False, "error": f"合成失败: {type(e).__name__}: {e}"}
    h._json(res, 200)


def voice_file(h):
    """返回 wav 字节。名字走白名单，路径由 store 拼（不接受外部路径）。"""
    qs = parse_qs(urlparse(h.path).query)
    name = (qs.get("name", [""])[0] or "").strip()
    from routes_common import _query_mode
    mode = _query_mode(h)
    m = _NAME_RE.fullmatch(name)
    if not m:
        h._json({"error": "非法语音文件名"}, 400)
        return
    try:
        fp = store.wav_path(mode, int(m.group(1)))
    except Exception:
        h._json({"error": "非法参数"}, 400)
        return
    if not fp.exists():
        h._json({"error": "语音不存在"}, 404)
        return
    try:
        data = fp.read_bytes()
    except OSError:
        h._json({"error": "语音读取失败"}, 500)
        return
    h.send_response(200)
    h.send_header("Content-Type", "audio/wav")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


# ── 插件管理（首页「语音插件」页）──────────────────────────────
# 用「一个 GET 读状态 + 一个 POST 派发动作」两个端点，而不是给每个动作开一条路由：
# 端点越少，路由 oracle 的维护面越小；非法 action 在派发前统一被拒（审查约束）。
_ACTIONS = {"install", "cancel", "enable", "disable", "uninstall", "rescan",
            "set_mood", "set_repo", "purge_voice"}


def voice_plugin(h):
    """GET /voice/plugin → 插件状态（5 态 + 缺什么 + 下载进度）。永不 500。"""
    try:
        from voice import plugin as vp
        h._json(vp.state())
    except Exception as e:
        h._json({"id": "unavailable", "label": "状态读取失败",
                 "reason": f"{type(e).__name__}: {e}"}, 200)


def voice_plugin_action(h):
    """POST /voice/plugin {"action": "...", ...}"""
    from routes_common import _read_json
    from voice import downloader, plugin as vp
    body = _read_json(h) or {}
    action = (body.get("action") or "").strip()
    if action not in _ACTIONS:
        h._json({"ok": False, "error": f"未知操作: {action!r}",
                 "allowed": sorted(_ACTIONS)}, 400)
        return
    try:
        if action == "install":
            r = downloader.start(url=body.get("url"), files=body.get("files"))
            if not r.get("ok"):
                h._json(r, 200)
                return
        elif action == "cancel":
            downloader.cancel()
        elif action == "enable":
            vp.set_enabled(True)
        elif action == "disable":
            vp.set_enabled(False)
        elif action == "uninstall":
            vp.uninstall()
        elif action == "rescan":
            vp.rescan()
        elif action == "set_mood":
            vp.set_mood((body.get("mood") or "").strip())
        elif action == "set_repo":
            vp.set_repo_url(body.get("url") or "")
        elif action == "purge_voice":
            r = vp.purge_voice_cache()
            h._json({"ok": True, "removed": r.get("removed"), "state": r}, 200)
            return
        h._json({"ok": True, "state": vp.state()})
    except ValueError as e:                       # 参数非法（如未知语气）
        h._json({"ok": False, "error": str(e), "state": vp.state()}, 200)
    except Exception as e:                        # 兜底：不让插件把接口打成 500
        h._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 200)