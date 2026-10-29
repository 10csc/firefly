# -*- coding: utf-8 -*-
"""角色包文本路径与读写（底层实现）。

路径公式和读回落链不能放在 `api`：`core.pack_structure` 等核心模块也要使用，
若从 `api` 反向导入会形成架构倒置。`api.pack_paths` 只保留兼容 re-export。
"""

import logging
import time
from pathlib import Path

from modules import app_config as cfg

logger = logging.getLogger(__name__)

_BACKUP_KEEP = 5


def kb_user_root(mode: str) -> Path:
    """知识库用户副本根（写路径唯一落点）。"""
    return cfg.mode_character_dir(mode) / "knowledge"


def kb_bundled_root(mode: str) -> Path:
    """知识库 bundled 根（包内；不存在则为空）。"""
    return cfg.bundled_character_dir(mode) / "knowledge"


def _paths_for(mode: str, file: str) -> tuple[Path, Path]:
    """返回 (用户副本路径, bundled 路径)。"""
    if file.startswith("knowledge/"):
        return (
            kb_user_root(mode) / file[len("knowledge/"):],
            kb_bundled_root(mode) / file[len("knowledge/"):],
        )
    if file.startswith("memory/"):
        return (
            cfg.mode_character_dir(mode) / "memory" / "default.md",
            cfg.bundled_character_dir(mode) / "memory" / "default.md",
        )
    return (
        cfg.mode_character_dir(mode) / file,
        cfg.bundled_character_dir(mode) / file,
    )


def read_pack_text(mode: str, file: str) -> str:
    """读当前生效内容：用户副本优先 → bundled → 空串。"""
    user_fp, bundled_fp = _paths_for(mode, file)
    for fp in (user_fp, bundled_fp):
        try:
            if fp.is_file():
                return fp.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def read_pack_text_for_write(mode: str, file: str) -> str:
    """写前读当前内容；单独命名用于表达调用意图。"""
    return read_pack_text(mode, file)


def _backup_current(mode: str, user_fp: Path, content: str, file: str, tag: str) -> None:
    bdir = cfg.mode_data_dir(mode) / ".assist_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    safe = file.replace("/", "__").replace("\\", "__")
    fp = bdir / f"{safe}.{tag}-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    fp.write_text(content, encoding="utf-8")
    olds = sorted(bdir.glob(f"{safe}.{tag}-*.bak"))
    for old in olds[:max(len(olds) - _BACKUP_KEEP, 0)]:
        try:
            old.unlink()
        except OSError:
            pass


def write_pack_text(mode: str, file: str, content: str, backup_tag: str = "assist") -> str:
    """写用户副本：先备份当前生效内容 → 原子写。返回空串或错误文案。"""
    user_fp, _ = _paths_for(mode, file)
    current = read_pack_text(mode, file)
    try:
        _backup_current(mode, user_fp, current, file, backup_tag)
    except Exception as exc:
        logger.warning("写前备份失败（中止写入）: %s", exc)
        return f"备份失败，已中止写入: {exc}"
    from modules.storage import atomic_write_text

    if not atomic_write_text(user_fp, content):
        return "写入失败"
    return ""
