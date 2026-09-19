# -*- coding: utf-8 -*-
"""流水线观测（阶段 2.8 自 orchestrator 拆出，纯移动无行为变化）

每轮对话各阶段的输入/输出/思考过程：内存环形缓冲 + 落盘持久化
（pipeline.jsonl，{mode}/data/），重启后仍可查（诊断不依赖复现）。
orchestrator 与 api/debug 经 re-export 取用，消费方零改动。
"""

import json
import logging
import threading
from pathlib import Path

from modules.app_config import DEFAULT_MODE, mode_data_dir

logger = logging.getLogger(__name__)
_lock = threading.Lock()

_PIPELINE_LOG: list[dict] = []
_PIPELINE_MAX = 200
_PIPELINE_ROTATE_BYTES = 8 * 1024 * 1024   # 文件超 8MB 轮转，保留最近 200 轮


def _pipeline_file(mode: str = DEFAULT_MODE) -> Path:
    return mode_data_dir(mode) / "pipeline.jsonl"


def _record_pipeline(entry: dict, mode: str = DEFAULT_MODE):
    # 写入作用域标记（服务器版多用户隔离）：落盘文件本身已按用户分目录，
    # 此字段用于 get_pipeline_log 的读取侧复核，防旧格式记录跨界展示。
    try:
        from modules.app_config import user_scope_key
        entry["_scope"] = user_scope_key()
    except Exception:
        entry["_scope"] = ""
    with _lock:
        _PIPELINE_LOG.append(entry)
        if len(_PIPELINE_LOG) > _PIPELINE_MAX:
            _PIPELINE_LOG.pop(0)
    # 落盘（失败静默，不影响主流程）
    try:
        fp = _pipeline_file(mode)
        fp.parent.mkdir(parents=True, exist_ok=True)
        with fp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if fp.stat().st_size > _PIPELINE_ROTATE_BYTES:
            lines = fp.read_text(encoding="utf-8").splitlines()
            fp.write_text("\n".join(lines[-_PIPELINE_MAX:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def get_pipeline_log(limit: int = 20, mode: str = DEFAULT_MODE) -> list[dict]:
    """返回当前用户最近 limit 条流水线记录（/pipeline 调试面板）。

    只读本用户的落盘文件 {mode}/data/pipeline.jsonl（mode_data_dir 经用户上下文按账号
    隔离），**不读进程级全局 _PIPELINE_LOG**——该内存列表在服务器版是多账号共享的，
    直接切片返回会造成跨账号会话内容泄漏（2026-09-10 修复）。
    旧格式（无 _scope 字段）的记录一律跳过：宁可少显示，不跨界。"""
    try:
        from modules.app_config import user_scope_key
        _scope = user_scope_key()
    except Exception:
        _scope = ""
    out = []
    try:
        lines = _pipeline_file(mode).read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for ln in reversed(lines):
        if len(out) >= limit:
            break
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        # 服务器版多用户隔离：_PIPELINE_LOG 是进程级全局，落盘文件才是按用户分的。
        # 只返回属于当前用户作用域的条目（旧的无作用域记录一律跳过，宁可少显示不跨界）。
        if (rec.get("_scope") or "") != _scope:
            continue
        out.append(rec)
    return list(reversed(out))
