# -*- coding: utf-8 -*-
"""语音插件 — 引擎后端（平台分支的唯一处）

安卓：调 Kotlin `com.firefly.voice.VoiceBridge`（ONNX 只能在 Kotlin 跑 ——
      Chaquopy 装不上 onnxruntime，实测 `No matching distribution found`）。
PC  ：本版未接入 → `available()` 返回 False 并给出原因，整条链路静默降级为纯文字。
      接入时只需在这里补一个后端（原型见 `FireflyVoiceResearch/_proto/engine_host.py` 的
      「独立执行体 + 空闲退出」形态），上层 `__init__.py` 一个字不用改。
"""
from __future__ import annotations

import json
from pathlib import Path

from voice import paths

# 语气 → 展示名由 paths.MOODS 提供；引擎侧只需目录名
_ANDROID_BRIDGE = "com.firefly.voice.VoiceBridge"

_bridge_cls = None
_init_done = False
_last = {"ok": False, "reason": "未初始化"}


def _load_bridge():
    """拿到 Kotlin VoiceBridge 类对象；非安卓环境返回 None。"""
    global _bridge_cls
    if _bridge_cls is not None:
        return _bridge_cls
    try:
        from java import jclass                      # 仅 Chaquopy 存在
    except Exception:
        _last["reason"] = ("当前平台无语音引擎：本版只接了安卓（Kotlin ONNX）后端；"
                           "PC 后端见 docs/工具/tts.md §10 修订 3")
        return None
    try:
        _bridge_cls = jclass(_ANDROID_BRIDGE)
        return _bridge_cls
    except Exception as e:
        _last["reason"] = f"取不到 Kotlin 桥 {_ANDROID_BRIDGE}: {e}"
        return None


def available() -> tuple[bool, str]:
    """插件是否可用（协议 §3 的 available() 语义）。

    ★ 查的是**资源就绪**（平台支持 + 模型文件在），**不是**引擎是否已装载 ——
      引擎是懒加载的，首次合成才装载（手机上一次要 30~60s），
      若这里查 isReady()，第一次点播必然被判"不可用"而拒绝。
    """
    b = _load_bridge()
    if b is None:
        return False, _last.get("reason") or "当前平台无语音引擎（PC 侧本版未接入）"
    md = paths.find_models_dir()
    if md is None:
        tried = "、".join(str(d) for d in paths.model_search_dirs())
        return False, f"未安装模型（找过：{tried}）"
    st = status()
    if st.get("ready"):
        return True, "ok"
    if st.get("error"):
        # 已尝试装载但失败（如缺组件）→ 把真原因透出来
        return False, str(st["error"])
    return True, "ok（引擎未装载，将在首次合成时加载）"


def ensure() -> tuple[bool, str]:
    """装载引擎（幂等）。返回 (ok, reason)。"""
    global _init_done
    b = _load_bridge()
    if b is None:
        return False, _last.get("reason") or "当前平台无语音引擎"

    md = paths.find_models_dir()
    if md is None:
        tried = "、".join(str(d) for d in paths.model_search_dirs())
        return False, f"未找到模型（找过：{tried}）"

    moods = ",".join(paths.MOODS.keys())
    try:
        raw = b.init(str(md), str(paths.mood_lib_dir()), moods, paths.BERT_FILE)
        st = json.loads(str(raw))
        _init_done = bool(st.get("ready"))
        if not _init_done:
            return False, str(st.get("error") or "引擎装载失败（原因未提供）")
        _last.update({"ok": True, "reason": ""})
        return True, "ok"
    except Exception as e:
        return False, f"调用 Kotlin 引擎失败: {type(e).__name__}: {e}"


def status() -> dict:
    b = _load_bridge()
    if b is None:
        return {"platform": "non-android", "ready": False,
                "reason": _last.get("reason") or "无引擎后端"}
    try:
        st = json.loads(str(b.statusJson()))
        st["platform"] = "android"
        return st
    except Exception as e:
        return {"platform": "android", "ready": False, "reason": f"读状态失败: {e}"}


def synthesize(text: str, mood: str) -> bytes | None:
    """文本 → wav 字节。失败返回 None（原因见 `status()['error']`）。"""
    b = _load_bridge()
    if b is None:
        return None
    try:
        raw = b.synthesize(text, mood)
    except Exception as e:
        _last["reason"] = f"Kotlin 合成异常: {type(e).__name__}: {e}"
        return None
    if raw is None:
        return None
    try:
        return bytes(raw)
    except Exception:
        return None


def release() -> None:
    global _init_done
    b = _load_bridge()
    if b is not None:
        try:
            b.release()
        except Exception:
            pass
    _init_done = False