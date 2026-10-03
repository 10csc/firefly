# -*- coding: utf-8 -*-
"""语音插件 — 引擎后端（平台分支的唯一处）

安卓：调 Kotlin `com.firefly.voice.VoiceBridge`（ONNX 只能在 Kotlin 跑 ——
      Chaquopy 装不上 onnxruntime，实测 `No matching distribution found`）。
PC  ：**独立子进程 + onnxruntime CPU + 纯 Python 文本前端**（见 `pc_host.py` / `pc_engine.py`）。
      形态对齐 `FireflyVoiceResearch/_proto/engine_host.py`：空闲自退、内存真归还、崩溃隔离。
      主进程**不 import onnxruntime/numpy**（依赖只活在子进程里，不污染主进程依赖表）。

★ 五个函数签名不变，上层 `__init__.py` / `routes.py` 一个字不用改（任务单硬约束）。
"""
from __future__ import annotations

import json
import logging
import os
import threading

from voice import paths

logger = logging.getLogger(__name__)

# 语气 → 展示名由 paths.MOODS 提供；引擎侧只需目录名
_ANDROID_BRIDGE = "com.firefly.voice.VoiceBridge"

_bridge_cls = None
_init_done = False
_last = {"ok": False, "reason": "未初始化"}

# ── PC 后端状态（独立于安卓 _last）───────────────────────────────
_pc_host = None                                   # 懒建：`voice.pc_host.PcEngineHost`
_pc_lock = threading.RLock()
# load_error：装载失败（决定 available()）；synth_error：**单次**合成失败（瞬时，不影响可用性）
_pc_last = {"load_error": "", "synth_error": "", "loaded": False, "load_seconds": 0.0}


def _platform() -> str:
    """当前平台（唯一判据 = 有没有 Chaquopy 的 `java` 模块）。"""
    try:
        import java  # noqa: F401  （仅 Chaquopy 存在）
        return "android"
    except Exception:
        return "pc"


def _load_bridge():
    """拿到 Kotlin VoiceBridge 类对象；非安卓环境返回 None。"""
    global _bridge_cls
    if _bridge_cls is not None:
        return _bridge_cls
    try:
        from java import jclass                      # 仅 Chaquopy 存在
    except Exception:
        _last["reason"] = "当前系统暂不支持语音合成（本版语音在 Windows 桌面与安卓可用）"
        return None
    try:
        _bridge_cls = jclass(_ANDROID_BRIDGE)
        return _bridge_cls
    except Exception as e:
        logger.warning("取不到 Kotlin 桥 %s: %s", _ANDROID_BRIDGE, e)
        _last["reason"] = "语音引擎初始化失败，请重启程序后重试"
        return None


# ════════════════════════════════════════════════════════════════════
# PC 后端
# ════════════════════════════════════════════════════════════════════
def _pc_resources() -> tuple[object | None, str]:
    """查 PC 侧**资源就绪**（模型 + 文本前端资产 + 语气库），返回 (models_dir, reason)。"""
    md = paths.find_models_dir()
    if md is None:
        tried = "、".join(str(d) for d in paths.model_search_dirs())
        return None, f"未安装模型（找过：{tried}）"
    assets = paths.assets_dir()
    miss = [f for f in ("symbols.json", "pinyin.json", "opencpop-strict.txt", "char2id.json")
            if not (assets / f).exists()]
    if miss:
        return None, f"缺文本前端资产：{'、'.join(miss)}（在 {assets}）"
    for m in paths.MOODS:
        if not (assets / "mood_lib" / m / "meta.json").exists():
            return None, f"缺语气库：{assets / 'mood_lib' / m}"
    return md, "ok"


def _get_pc_host():
    """懒建宿主（不 import onnxruntime；依赖只由子进程解释器 import）。"""
    global _pc_host
    with _pc_lock:
        if _pc_host is None:
            from voice.pc_host import PcEngineHost
            md = paths.find_models_dir() or paths.model_search_dirs()[0]
            assets = paths.assets_dir()
            _pc_host = PcEngineHost(
                models_dir=md, assets_dir=assets, mood_lib_dir=assets / "mood_lib",
                moods=list(paths.MOODS.keys()), bert_file=paths.BERT_FILE,
                python_exe=os.environ.get("FIREFLY_ENGINE_PYTHON") or None)
        return _pc_host


def _pc_available() -> tuple[bool, str]:
    md, why = _pc_resources()
    if md is None:
        return False, why
    if _pc_last.get("load_error"):
        return False, str(_pc_last["load_error"])     # 已尝试装载但失败 → 透出真原因
    return True, "ok（引擎未装载，将在首次合成时加载）"


def _pc_ensure() -> tuple[bool, str]:
    global _init_done
    md, why = _pc_resources()
    if md is None:
        return False, why
    r = _get_pc_host().ensure_loaded()
    if not r.get("ok"):
        _raw = str(r.get("error") or "")
        logger.warning("语音引擎装载失败: %s", _raw or "（原因未提供）")
        err = "语音引擎装载失败，请重启程序后重试"
        _pc_last["load_error"] = err
        return False, err
    _init_done = True
    _pc_last.update({"load_error": "", "synth_error": "", "loaded": True,
                     "load_seconds": float(r.get("load_seconds") or 0.0)})
    return True, "ok"


def _pc_status() -> dict:
    """PC 状态。★ **不**拉起子进程 —— 状态查询（前端轮询 /voice/status）绝不能因"看一眼"就
    把空闲自退的 worker 又拉起来（那会让 300s 自退形同虚设）。只有 ensure()/synthesize() 才拉起。"""
    h = _pc_host
    if h is None or not h.alive:
        md = paths.find_models_dir()
        return {"platform": "pc", "ready": bool(_init_done), "loaded": bool(_pc_last.get("loaded")),
                "error": str(_pc_last.get("load_error") or ""),
                "last_error": str(_pc_last.get("synth_error") or ""),
                "load_seconds": float(_pc_last.get("load_seconds") or 0.0),
                "moods": sorted(paths.MOODS.keys()),
                "models_dir": str(md) if md else "", "reason": ""}
    st = h.status()
    st["platform"] = "pc"
    if not st.get("error"):
        st["error"] = str(_pc_last.get("load_error") or "")
    st["last_error"] = str(_pc_last.get("synth_error") or "")
    return st


def _pc_synthesize(text: str, mood: str) -> bytes | None:
    r = _get_pc_host().synthesize(text, mood)
    if not r.get("ok"):
        # ★ 单次合成失败（守卫拦下/采样跑飞属**瞬时可重试**）——**不得**影响 available()
        err = str(r.get("error") or "引擎未产出音频")
        _pc_last["synth_error"] = err
        _last["reason"] = err
        return None
    _pc_last["synth_error"] = ""
    wav = r.get("wav")
    return bytes(wav) if wav else None


def _pc_release() -> None:
    global _pc_host, _init_done
    with _pc_lock:
        if _pc_host is not None:
            try:
                _pc_host.shutdown()
            except Exception:
                pass
            _pc_host = None
    _init_done = False
    _pc_last.update({"load_error": "", "synth_error": "", "loaded": False, "load_seconds": 0.0})


# ════════════════════════════════════════════════════════════════════
# 对外五个函数（签名与安卓版完全一致）
# ════════════════════════════════════════════════════════════════════
def available() -> tuple[bool, str]:
    """插件是否可用（协议 §3 的 available() 语义）。

    ★ 查的是**资源就绪**（平台支持 + 模型文件在），**不是**引擎是否已装载 ——
      引擎是懒加载的，首次合成才装载（手机上一次要 30~60s），
      若这里查 isReady()，第一次点播必然被判"不可用"而拒绝。
    """
    if _platform() == "pc":
        return _pc_available()
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
    if _platform() == "pc":
        return _pc_ensure()

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
        logger.warning("调用 Kotlin 语音引擎失败: %s: %s", type(e).__name__, e)
        return False, "语音引擎调用失败，请重试"


def status() -> dict:
    if _platform() == "pc":
        return _pc_status()
    b = _load_bridge()
    if b is None:
        return {"platform": "non-android", "ready": False,
                "reason": _last.get("reason") or "无引擎后端"}
    try:
        st = json.loads(str(b.statusJson()))
        st["platform"] = "android"
        return st
    except Exception as e:
        logger.warning("读取语音引擎状态失败: %s", e)
        return {"platform": "android", "ready": False,
                "reason": "语音引擎状态读取失败，请重启程序后重试"}


def synthesize(text: str, mood: str) -> bytes | None:
    """文本 → wav 字节。失败返回 None（原因见 `status()['error']`）。"""
    if _platform() == "pc":
        return _pc_synthesize(text, mood)

    b = _load_bridge()
    if b is None:
        return None
    try:
        raw = b.synthesize(text, mood)
    except Exception as e:
        logger.warning("Kotlin 语音合成异常: %s: %s", type(e).__name__, e)
        _last["reason"] = "语音合成失败，请重试"
        return None
    if raw is None:
        return None
    try:
        return bytes(raw)
    except Exception:
        return None


def release() -> None:
    global _init_done
    if _platform() == "pc":
        _pc_release()
        return
    b = _load_bridge()
    if b is not None:
        try:
            b.release()
        except Exception:
            pass
    _init_done = False
