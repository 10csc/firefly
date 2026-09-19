# -*- coding: utf-8 -*-
"""语音插件 — 语音文件读写与清理

命名：`v{seq}.wav`（seq = 该条消息在会话里的全局序号）。
清理三条（用户 2026-09-18 确认）：
  · 卸载软件          → 系统连 user_data 一起清（零代码）
  · 清理历史记录      → `purge_all(mode)`
  · 撤回上一轮        → `purge_after(mode)`（按当前会话最大 seq 截断）
  · 更新（覆盖安装）  → **不触发**（不挂任何版本流程）
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from voice import paths

_NAME = re.compile(r"^v(\d+)\.wav$")


def wav_path(mode: str, seq: int) -> Path:
    try:
        seq = int(seq)
    except Exception:
        raise ValueError(f"非法 seq: {seq!r}")
    if seq < 0:
        raise ValueError(f"非法 seq: {seq!r}")
    return paths.voice_dir(mode) / f"v{seq}.wav"


def has(mode: str, seq: int) -> bool:
    try:
        return wav_path(mode, seq).exists()
    except ValueError:
        return False


def save(mode: str, seq: int, data: bytes) -> Path:
    """原子写：先写 .tmp 再 rename，避免半截文件被当成有效语音。"""
    dst = wav_path(mode, seq)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".wav.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)
    return dst


def scan(mode: str) -> list[tuple[int, Path]]:
    d = paths.voice_dir(mode)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = _NAME.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def purge_all(mode: str) -> int:
    """清空该角色的语音文件（清理历史记录时调用）。返回删除数。"""
    n = 0
    for _, p in scan(mode):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def purge_after(mode: str, max_seq: int) -> int:
    """删掉 seq 超过 max_seq 的语音（撤回上一轮时调用）。

    不依赖 `remove_last_turn` 的返回值口径：直接以"当前会话还剩的最大 seq"为准，
    对 seq 间隙、连续撤回、跨模式都成立。
    """
    n = 0
    for seq, p in scan(mode):
        if seq > max_seq:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


def total_bytes(mode: str) -> int:
    return sum(p.stat().st_size for _, p in scan(mode) if p.exists())


def cleanup_to_limit(mode: str, limit_mb: int = 200) -> int:
    """超过上限时按 seq 从旧到新删（`voice_cache` 的 LRU 口径：每包 200 MB）。"""
    rows = scan(mode)
    total = sum(p.stat().st_size for _, p in rows)
    if total <= limit_mb * 1024 * 1024:
        return 0
    n = 0
    for _, p in rows:                       # rows 已按 seq 升序 = 旧→新
        if total <= limit_mb * 1024 * 1024:
            break
        try:
            sz = p.stat().st_size
            p.unlink()
            total -= sz
            n += 1
        except OSError:
            pass
    return n


# ── 跨模式：插件页的「清除语音缓存」用 ──────────────────────────
def all_voice_dirs() -> list[tuple[str, Path]]:
    """扫描 user_data 下所有**确有语音目录**的 mode（不依赖 presets，避免耦合）。"""
    out: list[tuple[str, Path]] = []
    root = paths.user_dir()          # 经函数取（USER_DIR 是可变量；见 voice/paths.user_dir 的说明）
    if root.is_dir():
        for d in root.iterdir():
            try:
                vd = d / "data" / "voice"
                if d.is_dir() and vd.is_dir():
                    out.append((d.name, vd))
            except OSError:
                continue
    return sorted(out)


def wav_dur(p: Path) -> float:
    """只读 WAV 头算时长（48k/16bit 单声道是我们固定输出；头里也有 byte_rate）。"""
    try:
        with open(p, "rb") as f:
            h = f.read(44)
        if len(h) < 44 or h[0:4] != b"RIFF":
            return 0.0
        import struct
        byte_rate = struct.unpack_from("<I", h, 28)[0]
        data_size = struct.unpack_from("<I", h, 40)[0]
        return round(data_size / byte_rate, 2) if byte_rate else 0.0
    except Exception:
        return 0.0


def cache_info() -> dict:
    """语音缓存概览（插件页显示占用；前端用它恢复"消息下面的语音条"）。

    `seqs`：{mode: {seq: 时长秒}} —— 前端据此在历史渲染后把语音条贴回对应消息。
    只读 44 字节 WAV 头，几百个文件的开销可忽略。
    """
    per: dict[str, int] = {}
    seqs: dict[str, dict[str, float]] = {}
    files = 0
    total = 0
    for mode, d in all_voice_dirs():
        m: dict[str, float] = {}
        for p in d.glob("v*.wav"):
            try:
                total += p.stat().st_size
                m[p.stem[1:]] = wav_dur(p)      # v47.wav -> "47"
            except OSError:
                pass
        if m:
            per[mode] = len(m)
            seqs[mode] = m
        files += len(m)
    return {"modes": per, "files": files, "bytes": total, "seqs": seqs}


def purge_all_modes() -> int:
    """清掉**所有模式**的语音（插件页「清除语音缓存」）。返回删除数。"""
    n = 0
    for _, d in all_voice_dirs():
        for p in d.glob("v*.wav"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        for p in d.glob("*.tmp"):
            try:
                p.unlink()
            except OSError:
                pass
    return n