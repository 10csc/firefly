# -*- coding: utf-8 -*-
"""会话历史持久化 — JSON Lines 追加 + 分页加载

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
单会话单文件 user_data/data/conversation.jsonl，每行一条消息。
即时追加：用户发送立刻记，流萤每条回复立刻记。
"""

import json
import logging
import shutil
import threading
from pathlib import Path
from datetime import datetime

from modules.app_config import USER_DIR, BASE_DIR

logger = logging.getLogger(__name__)
_lock = threading.Lock()

_CONV_FILE = USER_DIR / "data" / "conversation.jsonl"
_PAGE_SIZE = 150
# 开发期旧路径；首次读时若新路径不存在则迁移一次
_LEGACY_CONV = BASE_DIR / "data" / "conversation.jsonl"


# ── 异常 ──────────────────────────────────────────
class ConversationStoreError(Exception):
    pass


class InputRejected(ConversationStoreError):
    pass


# ── 内部 ──────────────────────────────────────────
def _migrate_legacy():
    """app/data → user_data/data，只迁一次。"""
    if _CONV_FILE.exists() or not _LEGACY_CONV.exists():
        return
    try:
        _CONV_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(_LEGACY_CONV), str(_CONV_FILE))
        logger.info("conversation.jsonl 已迁移至 user_data/data/")
    except Exception as e:
        logger.warning("conversation.jsonl 迁移失败: %s", e)


def _ensure_dir():
    _migrate_legacy()
    _CONV_FILE.parent.mkdir(parents=True, exist_ok=True)


def _next_seq() -> int:
    """读最后一行的 seq + 1。文件不存在或空返回 1。"""
    _migrate_legacy()
    if not _CONV_FILE.exists():
        return 1
    last_seq = 0
    with _CONV_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "seq" in obj:
                    last_seq = int(obj["seq"])
            except Exception:
                continue
    return last_seq + 1


def remove_last_turn() -> int:
    """移除 conversation.jsonl 中最后一轮（从最后一条 user 消息到末尾）。

    Returns:
        移除的行数
    """
    _migrate_legacy()
    if not _CONV_FILE.exists():
        return 0

    with _lock:
        with _CONV_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        if not lines:
            return 0

        i = None
        for idx in range(len(lines) - 1, -1, -1):
            line = lines[idx].strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("who") == "user":
                    i = idx
                    break
            except Exception:
                continue

        if i is None:
            return 0

        # 扩展到连续 user 块起点：一轮可能包含多条 user（分条发送），整轮撤回
        while i > 0:
            prev = lines[i - 1].strip()
            if not prev:
                break
            try:
                po = json.loads(prev)
                if isinstance(po, dict) and po.get("who") == "user":
                    i -= 1
                else:
                    break
            except Exception:
                break

        original_len = len(lines)
        lines = lines[:i]
        with _CONV_FILE.open("w", encoding="utf-8") as f:
            f.writelines(lines)

    removed = original_len - len(lines)
    logger.debug("remove_last_turn: 移除 %d 行, 剩余 %d 行", removed, len(lines))
    return removed


# ── 主入口 ────────────────────────────────────────
def append_message(who: str, msg: dict) -> tuple:
    """追加一条消息。msg 已含 type/content 或 type/path+label。

    Returns:
        (seq, time) —— 分配的序号和写入时间戳，供前端回显
    """
    if who not in ("user", "firefly"):
        raise InputRejected(f"who 必须为 user/firefly，实际: {who}")
    if not isinstance(msg, dict):
        raise InputRejected("msg 必须为 dict")
    mtype = msg.get("type")
    if mtype == "text":
        if not msg.get("content"):
            raise InputRejected("text 消息缺 content")
    elif mtype == "sticker":
        if not msg.get("path"):
            raise InputRejected("sticker 消息缺 path")
    else:
        raise InputRejected(f"type 必须为 text/sticker，实际: {mtype}")

    with _lock:
        _ensure_dir()
        seq = _next_seq()
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {"seq": seq, "time": time_str, "who": who}
        record.update(msg)
        with _CONV_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return seq, time_str


def load_recent(limit: int = _PAGE_SIZE, before_seq: int = None) -> list:
    """加载最近 limit 条消息。若给 before_seq，则取 seq < before_seq 的最近 limit 条（向上翻页）。
    返回按 seq 升序（前端从上到下渲染）。
    """
    _migrate_legacy()
    if not _CONV_FILE.exists():
        return []
    with _lock:
        all_msgs = []
        with _CONV_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "seq" in obj:
                        all_msgs.append(obj)
                except Exception:
                    continue
    all_msgs.sort(key=lambda x: int(x.get("seq", 0)))
    if before_seq is not None:
        all_msgs = [m for m in all_msgs if int(m.get("seq", 0)) < before_seq]
    page = all_msgs[-limit:] if len(all_msgs) > limit else all_msgs
    return page


def get_total_count() -> int:
    """返回消息总数（前端判断是否还有更早历史）。"""
    _migrate_legacy()
    if not _CONV_FILE.exists():
        return 0
    with _lock:
        n = 0
        with _CONV_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n


def get_min_seq() -> int:
    """返回当前最小 seq（has_more 判断用）。无文件返回 1。"""
    _migrate_legacy()
    if not _CONV_FILE.exists():
        return 1
    with _lock:
        with _CONV_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "seq" in obj:
                        return int(obj["seq"])
                except Exception:
                    continue
    return 1


def hydrate_context(ctx, max_turns: int = 40) -> int:
    """从 jsonl 回灌 ContextManager，供重启后 LLM 仍有近期历史。

    Returns:
        回灌的轮数
    """
    # 每轮约 user+若干 firefly，多取一点再按轮裁
    raw = load_recent(limit=max(max_turns * 4, 80))
    if not raw:
        return 0

    turns = 0
    i = 0
    n = len(raw)
    pending = []  # 先攒完整轮，再只取最近 max_turns

    while i < n:
        m = raw[i]
        if m.get("who") == "user" and m.get("type") == "text" and m.get("content"):
            user_texts = [m["content"]]
            texts, stickers = [], []
            i += 1
            # 连续 user 消息合并为一轮（分条写盘场景：5s 批处理的多条消息）
            while i < n and raw[i].get("who") == "user":
                if raw[i].get("type") == "text" and raw[i].get("content"):
                    user_texts.append(raw[i]["content"])
                i += 1
            while i < n and raw[i].get("who") == "firefly":
                fm = raw[i]
                if fm.get("type") == "text" and fm.get("content"):
                    texts.append(fm["content"])
                elif fm.get("type") == "sticker":
                    stickers.append(fm.get("label") or "表情")
                i += 1
            if not texts and not stickers:
                continue  # 未完成轮次（只有用户在打字）跳过
            pending.append(("\n".join(user_texts), texts, stickers))
        else:
            i += 1

    for user_text, texts, stickers in pending[-max_turns:]:
        reply = " ".join(texts) if texts else "(表情包)"
        try:
            ctx.add_turn(user_text, reply)
            for lab in stickers:
                ctx.add_action("表情包", lab)
            turns += 1
        except Exception as e:
            logger.warning("hydrate 跳过一轮: %s", e)
    return turns
