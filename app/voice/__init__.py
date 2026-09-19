# -*- coding: utf-8 -*-
"""语音插件（独立子系统）— 对外唯一接口

设计要点（详见 `docs/工具/tts.md`）：
  · **语音不是一种消息类型**，而是某条消息的**附属产物**，按 `v{seq}.wav` 命名。
    因此：不改 `append_message` 的 type 白名单、不写进 `conversation.jsonl`、
    不需要 ctx（它是"用户想听这条"，不是"流萤做了个动作"）。
  · 触发 = 前端长按消息 →「转语音」。已有缓存直接播；没有才合成。
  · **同时只有一个合成**：由 `_lock` + 引擎侧串行共同保证。
  · 任何失败都静默降级为纯文字，绝不阻塞聊天、绝不抛给调用方。

对外只有 6 个函数：status / moods / has / synthesize / purge_all / purge_after_undo。
"""
from __future__ import annotations

import json
import threading

from core import paths as _paths
from voice import engine, paths as vpaths, store

__all__ = ["status", "moods", "has", "synthesize", "purge_all", "purge_after_undo"]

_lock = threading.Lock()          # 写盘与清理由此串行；推理串行在引擎侧
_last = {"seq": None, "mood": None, "ok": None, "error": "", "seconds": 0.0}


# ── 只读：状态 ────────────────────────────────────────────────
def moods() -> dict:
    """{目录名: 展示名}"""
    return dict(vpaths.MOODS)


def status() -> dict:
    """插件状态（前端据此决定菜单项是否可点、以及给什么原因）。"""
    ok, why = engine.available()
    md = vpaths.find_models_dir()
    st = engine.status()
    return {
        "engine_ok": ok,
        "reason": why,
        "moods": moods(),
        "models_dir": str(md) if md else "",
        "models_found": md is not None,
        "mood_lib_dir": str(vpaths.mood_lib_dir()),
        "plugin_root": str(vpaths.plugin_root()),
        "last": dict(_last),
        "engine": st,
    }


def has(seq, mode: str) -> bool:
    return store.has(mode, seq)


# ── 写：合成 ──────────────────────────────────────────────────
def _text_of_seq(mode: str, seq: int) -> str | None:
    """从会话文件里按 seq 取该条消息的文本（只读）。"""
    from modules.conversation_store import conv_file
    fp = conv_file(mode)
    if not fp.exists():
        return None
    want = int(seq)
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("seq") != want:
                continue
            t = rec.get("type")
            if t == "text":
                return (rec.get("content") or "").strip() or None
            if t == "narration":
                return (rec.get("text") or "").strip() or None
            return None                      # sticker/image 没有可念文本
    except OSError:
        return None
    return None


def synthesize(seq, mode: str, mood: str | None = None, force: bool = False) -> dict:
    """把某条消息转成语音（幂等：已有缓存直接返回；force=True 则强制重生成）。

    返回 {ok, src, dur, cached, error}；永不抛异常。

    force 的用途：`pinyin.json` 修好后（978 字 → 20921 字），**旧缓存里那条吞字的音频
    不会自己变好** —— 缓存按 seq 命中就直接播。用户在长按菜单选「重新生成」时走这里。
    """
    import time
    t0 = time.time()
    try:
        seq = int(seq)
    except Exception:
        return {"ok": False, "error": f"非法 seq: {seq!r}"}

    mood = mood or next(iter(vpaths.MOODS))
    if mood not in vpaths.MOODS:
        return {"ok": False, "error": f"未知语气: {mood}"}

    # 幂等：已有语音直接返回（force 时跳过，重新合成）
    if not force and store.has(mode, seq):
        p = store.wav_path(mode, seq)
        return {"ok": True, "src": _rel(p), "cached": True, "dur": 0.0,
                "seq": seq, "mood": mood}

    text = _text_of_seq(mode, seq)
    if not text:
        return {"ok": False, "error": "该消息没有可念的文本（或 seq 不存在）"}

    ok, why = engine.ensure()
    if not ok:
        _last.update({"seq": seq, "mood": mood, "ok": False, "error": why})
        return {"ok": False, "error": why}

    with _lock:                                   # 同时只有一个合成
        data = engine.synthesize(text, mood)
        if not data:
            err = (engine.status() or {}).get("error") or "引擎未产出音频"
            _last.update({"seq": seq, "mood": mood, "ok": False, "error": err})
            return {"ok": False, "error": err}
        try:
            p = store.save(mode, seq, data)
        except Exception as e:
            err = f"写盘失败: {type(e).__name__}: {e}"
            _last.update({"seq": seq, "mood": mood, "ok": False, "error": err})
            return {"ok": False, "error": err}
        store.cleanup_to_limit(mode)               # LRU 上限（200MB/包）

    secs = round(time.time() - t0, 1)
    _last.update({"seq": seq, "mood": mood, "ok": True, "error": "", "seconds": secs})
    return {"ok": True, "src": _rel(p), "cached": False,
            "dur": _wav_seconds(data), "bytes": len(data),
            "seq": seq, "mood": mood, "seconds": secs}


def _wav_seconds(data: bytes) -> float:
    """从 WAV 头算时长（不引库；48k/16bit/单声道为我们的固定输出）。"""
    try:
        import struct
        if len(data) < 44:
            return 0.0
        byte_rate = struct.unpack_from("<I", data, 28)[0]
        data_size = struct.unpack_from("<I", data, 40)[0]
        return round(data_size / byte_rate, 3) if byte_rate else 0.0
    except Exception:
        return 0.0


def _rel(p) -> str:
    """输出相对路径（前端按 /voice-file/<mode>/<name> 取；不泄露绝对路径）"""
    try:
        return f"{p.parent.parent.parent.name}/{p.name}"
    except Exception:
        return p.name


# ── 清理（供 api/chat_ops 挂钩）────────────────────────────────
def purge_all(mode: str) -> int:
    with _lock:
        return store.purge_all(mode)


def purge_after_undo(mode: str) -> int:
    """撤回上一轮后调用：删掉 seq 超过当前会话最大 seq 的语音。

    以"文件里还剩的最大 seq"为准，不依赖 remove_last_turn 的返回值口径。
    """
    mx = _max_seq(mode)
    with _lock:
        return store.purge_after(mode, mx)


def _max_seq(mode: str) -> int:
    from modules.conversation_store import conv_file
    fp = conv_file(mode)
    if not fp.exists():
        return 0
    mx = 0
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mx = max(mx, int(json.loads(line).get("seq") or 0))
            except Exception:
                continue
    except OSError:
        return 0
    return mx