# -*- coding: utf-8 -*-
"""语音插件 — 生命周期（状态机 / 配置 / 安装 / 启用停用 / 卸载）

配置存在**插件自己的** `{插件根}/config.json`，不动主系统配置 ——
保持"独立子系统"的形态（`docs/工具/tts.md` §10）。

状态机（`state()` 返回 `id` + `reason`）：

    downloading       下载中（带 percent / bytes）
    not_installed     未安装（模型文件不齐）
    installed_disabled 已安装·未启用
    unavailable       已启用但不可用（缺组件 / 上次装载失败）
    ready             已启用·资源就绪（引擎可能尚未装载，首次合成时懒加载）

"未安装"的判定只认**模型文件在不在**（`paths.find_models_dir()`），
不认"是否装载过引擎" —— 引擎懒加载，用后者会把第一次点播误判成不可用。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from voice import engine, paths, store

_LOCK = threading.RLock()

# 模型仓库地址（ModelScope）。留空 = 未配置 → UI 显示提示并禁用下载按钮。
# 2026-09-18 已上传并逐个 sha256 对账通过（8 个文件 871.9 MB，全部走 LFS）：
#   https://www.modelscope.cn/models/cpt0721/firefly-voice-v4-onnx
# 终端用户**零配置**：公开仓库匿名可下、支持 Range 断点续传、实测 17 MB/s。
# 上传/换仓库用 tools/upload_voice_models.py（令牌位置见 docs/工具/tts.md §12）。
DEFAULT_REPO_URL = ("https://www.modelscope.cn/api/v1/models/cpt0721/firefly-voice-v4-onnx"
                    "/repo?Revision=master&FilePath={file}")
DEFAULT_REPO_FILES = list(paths.MODEL_FILES)

_DEFAULTS = {
    "enabled": True,          # 装好后默认启用
    "mood": "happy",          # 默认语气
    "repo_url": "",           # 空 = 用 DEFAULT_REPO_URL
    "bert_file": paths.BERT_FILE,
}


def config_file() -> Path:
    return paths.plugin_root() / "config.json"


def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    fp = config_file()
    if fp.exists():
        try:
            got = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                cfg.update({k: v for k, v in got.items() if k in _DEFAULTS or k == "installed_at"})
        except Exception:
            pass          # 坏配置 → 用默认值，不让插件因此不可用
    return cfg


def save_config(cfg: dict) -> None:
    fp = config_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, fp)


def repo_url() -> str:
    return (load_config().get("repo_url") or "").strip() or DEFAULT_REPO_URL


# ── 状态 ──────────────────────────────────────────────────────
def state() -> dict:
    """5 态状态机 + 展示所需的一切。永不抛异常。"""
    cfg = load_config()
    md = paths.find_models_dir()
    dl = download_status()

    missing = []
    if md is None:
        # 逐个说清楚缺什么（诊断价值高：多半是推错目录 / 少推了一个文件）
        for d in paths.model_search_dirs():
            if d.is_dir():
                missing += [f for f in paths.MODEL_FILES if not (d / f).exists()]
    else:
        missing = []

    if dl.get("running"):
        sid, reason = "downloading", ""
    elif md is None:
        sid, reason = "not_installed", "模型文件不齐"
    elif not cfg.get("enabled", True):
        sid, reason = "installed_disabled", ""
    else:
        ok, why = engine.available()
        if ok:
            sid, reason = "ready", ""
        else:
            sid, reason = "unavailable", why

    label = {
        "downloading": "下载中",
        "not_installed": "未安装",
        "installed_disabled": "已安装 · 未启用",
        "unavailable": "已启用 · 不可用",
        "ready": "可用",
    }[sid]

    return {
        "id": sid,
        "label": label,
        "reason": reason,
        "enabled": bool(cfg.get("enabled", True)),
        "mood": cfg.get("mood", "happy"),
        "moods": dict(paths.MOODS),
        "models_found": md is not None,
        "models_dir": str(md) if md else "",
        "search_dirs": [str(d) for d in paths.model_search_dirs()],
        "missing_files": sorted(set(missing)),
        "required_files": list(paths.MODEL_FILES),
        "plugin_root": str(paths.plugin_root()),
        "model_bytes": _dir_bytes(md) if md else 0,
        "repo_url": repo_url(),
        "repo_configured": bool(repo_url()),
        "download": dl,
        "voice_cache": store.cache_info(),
        "engine": engine.status(),
    }


def _dir_bytes(d: Path | None) -> int:
    if not d:
        return 0
    try:
        return sum(f.stat().st_size for f in d.iterdir() if f.is_file())
    except OSError:
        return 0


# ── 启用 / 停用 / 卸载 ────────────────────────────────────────
def set_enabled(on: bool) -> dict:
    with _LOCK:
        cfg = load_config()
        cfg["enabled"] = bool(on)
        save_config(cfg)
        if not on:
            engine.release()          # 停用即释放 session，内存归还
    return state()


def set_mood(mood: str) -> dict:
    if mood not in paths.MOODS:
        raise ValueError(f"未知语气: {mood}")
    with _LOCK:
        cfg = load_config()
        cfg["mood"] = mood
        save_config(cfg)
    return state()


def set_repo_url(url: str) -> dict:
    with _LOCK:
        cfg = load_config()
        cfg["repo_url"] = (url or "").strip()
        save_config(cfg)
    return state()


def uninstall() -> dict:
    """卸载：删掉插件本体（模型 + 语气库 + 配置）。

    **不动**已生成的语音文件（在 user_data 里，属于对话的附属物；
    用户要清那些走「清理历史」）。这与"卸载软件"不同 —— 那是系统连 user_data 一起清。
    """
    with _LOCK:
        engine.release()
        root = paths.plugin_root()
        removed = 0
        if root.is_dir():
            for child in root.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                    removed += 1
                except OSError:
                    pass
    return {"removed": removed, **state()}


def rescan() -> dict:
    """重新探测模型（测试期把模型 adb 推进去后，用它免重启）。"""
    with _LOCK:
        engine.release()
    return state()


def purge_voice_cache() -> dict:
    """清掉**所有模式**已生成的语音（插件页「清除语音缓存」）。

    用途：`pinyin.json` 从 978 字修到 20921 字后，**旧缓存里那条吞字的音频不会自己变好**
    （缓存按 seq 命中就直接播）。清掉后重新点「转语音」即用新前端重合成。
    **不动**模型与语气库。
    """
    with _LOCK:
        n = store.purge_all_modes()
    return {"removed": n, **state()}


# ── 下载（进度由 downloader 维护，这里只读）────────────────────
# verify/verified：sha256 对账（2026-09-18 加）。verify="on" 表示本次下载拿到了仓库清单、
# 每个文件下完都会比对 sha256；"off" 表示拿不到清单（不阻断下载，只降级）。
_DL = {"running": False, "percent": 0, "done": 0, "total": 0,
       "file": "", "error": "", "started_at": 0.0, "finished_at": 0.0,
       "verify": "", "verified": 0}


def download_status() -> dict:
    d = dict(_DL)
    if d["running"]:
        d["elapsed"] = round(time.time() - (d["started_at"] or time.time()), 1)
    return d


def download_reset() -> None:
    _DL.update({"running": False, "percent": 0, "done": 0, "total": 0,
                "file": "", "error": "", "started_at": 0.0, "finished_at": 0.0,
                "verify": "", "verified": 0})