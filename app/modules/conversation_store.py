# -*- coding: utf-8 -*-
"""会话历史持久化 — JSON Lines 追加 + 分页加载

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
单会话单文件 app/data/conversation.jsonl，每行一条消息。
即时追加：用户发送立刻记，流萤每条回复立刻记。
"""

import json
import logging
import threading
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)
_lock = threading.Lock()

_CONV_FILE = Path(__file__).resolve().parent.parent / "data" / "conversation.jsonl"
_PAGE_SIZE = 150


# ── 异常 ──────────────────────────────────────────
class ConversationStoreError(Exception):
    pass


class InputRejected(ConversationStoreError):
    pass


# ── 内部 ──────────────────────────────────────────
def _ensure_dir():
    _CONV_FILE.parent.mkdir(parents=True, exist_ok=True)


def _next_seq() -> int:
    """读最后一行的 seq + 1。文件不存在或空返回 1。"""
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


# ── 主入口 ────────────────────────────────────────
def append_message(who: str, msg: dict) -> tuple:
    """追加一条消息。msg 已含 type/content 或 type/path+label。

    Returns:
        (seq, time) —— 分配的序号和写入时间戳，供前端回显
    """
    # 1. 审查
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

    # 2. 处理：组装记录并追加写盘
    with _lock:
        _ensure_dir()
        seq = _next_seq()
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {"seq": seq, "time": time_str, "who": who}
        record.update(msg)
        with _CONV_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 4. 最终输出
    return seq, time_str


def load_recent(limit: int = _PAGE_SIZE, before_seq: int = None) -> list:
    """加载最近 limit 条消息。若给 before_seq，则取 seq < before_seq 的最近 limit 条（向上翻页）。
    返回按 seq 升序（前端从上到下渲染）。
    """
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
