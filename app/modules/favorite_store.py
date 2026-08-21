# -*- coding: utf-8 -*-
"""收藏夹存储 — 消息快照式收藏（长按消息 → 收藏）

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出

设计要点：
- 快照自包含（who/type/content 等），与对话历史解耦——清空历史、撤回都不影响收藏。
- 存储位置：{mode}/data/favorites.json（mode_data_dir 自动按用户隔离：服务器版落 user_data/{uid}/）。
- 自动随导出/账号同步 zip 打包（export_data 打包整个模式目录，无需额外代码）。
- 上限 _MAX_FAVORITES 条，超出丢最旧；按 seq 去重（同一条消息重复收藏 = 更新不重复）。
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from modules.app_config import mode_data_dir

logger = logging.getLogger(__name__)
_lock = threading.Lock()

_MAX_FAVORITES = 300
_MAX_CONTENT = 2000
_VALID_TYPES = ("text", "sticker", "narration")


class FavoriteError(Exception):
    pass


def _fav_file(mode: str) -> Path:
    return mode_data_dir(mode) / "favorites.json"


def _load(mode: str) -> list:
    """读文件。损坏/非列表一律视为空（审查约束：不因坏文件崩接口）。"""
    fp = _fav_file(mode)
    if not fp.exists():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        logger.warning("favorites.json 读取失败，按空处理: %s", fp)
        return []


def _save(mode: str, items: list) -> None:
    fp = _fav_file(mode)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(fp)


def _snapshot(msg) -> dict:
    """审查约束 + 快照构建：只取白名单字段，越界直接拒绝。"""
    if not isinstance(msg, dict):
        raise FavoriteError("消息格式错误")
    who = msg.get("who")
    mtype = msg.get("type")
    if who not in ("user", "firefly"):
        raise FavoriteError("消息来源无效")
    if mtype not in _VALID_TYPES:
        raise FavoriteError("消息类型无效")
    if mtype == "text":
        body = str(msg.get("content") or "").strip()
        if not body:
            raise FavoriteError("缺少消息内容")
    elif mtype == "sticker":
        body = str(msg.get("label") or msg.get("path") or "").strip()
        if not body:
            raise FavoriteError("缺少表情包信息")
    else:
        body = str(msg.get("text") or "").strip()
        if not body:
            raise FavoriteError("缺少旁白内容")
    if len(body) > _MAX_CONTENT:
        raise FavoriteError(f"内容过长（上限 {_MAX_CONTENT} 字符）")

    record = {"who": who, "type": mtype}
    if mtype == "text":
        record["content"] = body
    elif mtype == "sticker":
        record["label"] = body
        path = str(msg.get("path") or "").strip()[:_MAX_CONTENT]
        if path:
            record["path"] = path
    else:
        record["text"] = body
        style = str(msg.get("style") or "").strip()[:_MAX_CONTENT]
        if style:
            record["style"] = style
    try:
        seq = int(msg.get("seq"))
    except (TypeError, ValueError):
        seq = None
    if seq is not None:
        record["seq"] = seq
    return record


def list_favorites(mode: str) -> list:
    """返回收藏列表（最新在前）。"""
    with _lock:
        items = _load(mode)
    return list(reversed(items))


def add_favorite(mode: str, msg: dict) -> dict:
    """新增收藏。返回写入的记录（含 id/time）。"""
    rec = _snapshot(msg)
    with _lock:
        items = _load(mode)
        # 去重：带 seq 的按 seq 唯一（重复收藏 = 更新时间，不重复插入）
        if rec.get("seq") is not None:
            items = [it for it in items if it.get("seq") != rec["seq"]]
        if rec.get("seq") is not None:
            rec["id"] = str(rec["seq"])
        else:
            rec["id"] = f"f{int(time.time() * 1000)}"
        rec["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        items.append(rec)
        # 上限：丢最旧（文件顺序 = 收藏时间正序）
        if len(items) > _MAX_FAVORITES:
            items = items[-_MAX_FAVORITES:]
        _save(mode, items)
    return rec


def delete_favorite(mode: str, fav_id) -> bool:
    """按 id 删除收藏。返回是否真的删掉了。"""
    key = str(fav_id or "").strip()
    if not key:
        return False
    with _lock:
        items = _load(mode)
        kept = [it for it in items if str(it.get("id", "")) != key]
        if len(kept) == len(items):
            return False
        _save(mode, kept)
    return True
