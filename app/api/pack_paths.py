# -*- coding: utf-8 -*-
"""包文本文件读写统一口（阶段 E）：人设槽位 / 知识库 / 出厂记忆三类文件的
「读（用户副本→bundled 回落）」与「写（备份 → 原子写）」收口在这里。

边界：路径合法性由调用方（routes_pack 的 _safe_kb_rel / assistable 白名单）负责；
本模块只管"从哪读、往哪写、写前备份"。
"""

import logging
import time
from pathlib import Path

from modules import app_config as cfg

logger = logging.getLogger(__name__)

_BACKUP_KEEP = 5   # 每文件滚动备份份数


def _paths_for(mode: str, file: str) -> tuple[Path, Path]:
    """返回 (用户副本路径, bundled 路径)。知识库/出厂记忆走子目录，人设槽位走包根。"""
    if file.startswith("knowledge/"):
        from routes_pack import _kb_user_root, _kb_bundled_root
        return _kb_user_root(mode) / file[len("knowledge/"):], _kb_bundled_root(mode) / file[len("knowledge/"):]
    if file.startswith("memory/"):
        return (cfg.mode_character_dir(mode) / "memory" / "default.md",
                cfg.bundled_character_dir(mode) / "memory" / "default.md")
    return (cfg.mode_character_dir(mode) / file,
            cfg.bundled_character_dir(mode) / file)


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
    """写前读当前内容（与 read_pack_text 同语义；单独命名是供校验器表达意图）。"""
    return read_pack_text(mode, file)


def _backup_current(mode: str, user_fp: Path, content: str, file: str, tag: str) -> None:
    """写前备份：{pack}/data/.assist_backups/{文件名安全化}.{tag}-{时间戳}.bak，滚动留 5 份。"""
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
    """写用户副本：先备份当前生效内容 → 原子写。返回 "" 或错误文案。"""
    user_fp, _ = _paths_for(mode, file)
    cur = read_pack_text(mode, file)
    try:
        _backup_current(mode, user_fp, cur, file, backup_tag)
    except Exception as e:
        logger.warning("写前备份失败（中止写入）: %s", e)
        return f"备份失败，已中止写入: {e}"
    from modules.storage import atomic_write_text
    if not atomic_write_text(user_fp, content):
        return "写入失败"
    return ""
