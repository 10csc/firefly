# -*- coding: utf-8 -*-
"""语音插件 — 目录解析（单一来源）

与主系统只有一处约定：语音文件放在 `USER_DIR/{mode}/data/voice/`。
插件本体（模型 / 语气库）放在**与 user_data 平级**的 `voice/` 下 —— 这样"清理历史"
不会误删模型，而"卸载软件"由系统连 user_data 一起清掉、覆盖升级保留。

模型查找优先级（决定测试/正式两条路共用同一套代码）：
  1. `{插件根}/models/`                    ← 正式：下载或导入后的位置
  2. `{外部私有目录}/voice/models/`         ← 测试：adb push 的位置
"""
from __future__ import annotations

import os
from pathlib import Path

from core import paths

# 语气目录名 → 展示名（目录名与 mood_lib 子目录一致，英文避免路径编码问题）
MOODS = {"happy": "开心", "sad": "悲伤"}

BERT_FILE = "firefly_bert_int8.onnx"

MODEL_FILES = (
    "firefly_t2s_encoder.onnx",
    "firefly_t2s_fsdec_int8.onnx",
    "firefly_t2s_sdec_int8.onnx",
    "firefly_t2s_weights_int8.bin",
    "firefly_vits_int8.onnx",
    "firefly_cfm_estimator_int8.onnx",
    "firefly_vocoder.onnx",
    BERT_FILE,
)


def _android_data_dir() -> Path | None:
    v = os.environ.get("FIREFLY_DATA_DIR")
    return Path(v) if v else None


def _external_files_dir() -> Path | None:
    """安卓外部私有目录（adb 可写、app 能读；卸载清空、升级保留）。非安卓返回 None。"""
    try:
        from java import jclass  # Chaquopy
        app = jclass("com.chaquo.python.Python").getPlatform().getApplication()
        f = app.getExternalFilesDir(None)
        return Path(str(f)) if f is not None else None
    except Exception:
        return None


def plugin_root() -> Path:
    """插件本体根目录（与 user_data 平级，**不参与同步/快照**）。"""
    d = _android_data_dir()
    if d is not None:
        return d / "voice"
    if getattr(__import__("sys"), "frozen", False):        # PyInstaller
        return paths.USER_DIR.parent / "voice"
    return paths.ROOT / "voice"                            # 开发：仓库根/voice


def model_search_dirs() -> list[Path]:
    out = [plugin_root() / "models"]
    ext = _external_files_dir()
    if ext is not None:
        out.append(ext / "voice" / "models")
    return out


def find_models_dir() -> Path | None:
    """按优先级找齐 8 个模型文件的目录；都缺则 None。"""
    for d in model_search_dirs():
        if d.is_dir() and all((d / f).exists() for f in MODEL_FILES):
            return d
    return None


def mood_lib_dir() -> Path:
    """语气库目录（Kotlin 侧首次会从 assets 落地到这里）。"""
    return plugin_root() / "mood_lib"


def voice_dir(mode: str) -> Path:
    """某个角色的语音文件目录（在 user_data 内 → REGISTRY 注册 exclude）。

    走 `core.paths.mode_data_dir` 而不是自己拼路径 —— 路径公式全局只有一处
    （core/paths.py 的文档头就是这么要求的）。
    """
    return paths.mode_data_dir(mode) / "voice"


def user_dir() -> Path:
    """user_data 根。

    ★ 必须**经函数**取而不是 `paths.USER_DIR` 直接引用：`USER_DIR` 是**可变全局量**
      （测试与运行期会替换数据根，core/paths.py 的文档头明确要求"一律经属性访问"）。
      另外 `voice.paths` 只是 `from core import paths`，并未 re-export `USER_DIR` ——
      在别的模块里写 `voice.paths.USER_DIR` 会 AttributeError（2026-09-18 真机实测踩到：
      插件页状态直接变成 unavailable）。
    """
    return paths.USER_DIR


def ensure_dirs(mode: str) -> None:
    voice_dir(mode).mkdir(parents=True, exist_ok=True)
    plugin_root().mkdir(parents=True, exist_ok=True)