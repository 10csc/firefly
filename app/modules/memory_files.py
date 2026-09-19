# -*- coding: utf-8 -*-
"""记忆文件层（阶段 2.8 自 memory_manager 拆出，纯移动无行为变化）

职责：{mode}/data/memory.md、.memory_index、journal/手账.md 的路径公式，
旧位置一次性迁移（启动链显式调用），记忆游标自愈（阶段 3.6）。
memory_manager 保留整理/生成逻辑；本模块不 import 业务模块。
"""

import json
import logging
from pathlib import Path

from modules.app_config import ROOT, mode_data_dir, mode_journal_dir, DEFAULT_MODE

logger = logging.getLogger(__name__)


# ── 文件路径（按模式隔离）──────────────────────────
# 用户记忆 = 运行时动态数据，必须放 user_data（与 conversation.jsonl 同级）：
# 仓库内位置在打包(frozen)时位于 exe 内部 _internal/，更新安装包即被覆盖。
# 统一从 app_config 取 USER_DIR/ROOT（frozen / android / 开发 三平台同一公式）。
def _memory_file(mode: str = DEFAULT_MODE) -> Path:
    return mode_data_dir(mode) / "memory.md"


def _index_file(mode: str = DEFAULT_MODE) -> Path:
    return mode_data_dir(mode) / ".memory_index"


def _journal_file(mode: str = DEFAULT_MODE) -> Path:
    # 与 llm_base 手账同一位置：{mode}/journal/手账.md
    return mode_journal_dir(mode) / "手账.md"




def _verify_today(today) -> str:
    """today 审查：合法 YYYY-MM-DD 字符串直接采用；非法/缺省 → 本机时钟（兜底）。
    routes 层负责「服务器时间优先、本地兜底」的解析（见 api/chat._resolve_today）。"""
    import re as _re
    from datetime import datetime as _dt
    if isinstance(today, str):
        t = today.strip()
        if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            return t
    return _dt.now().strftime("%Y-%m-%d")


def _migrate_legacy(mode: str = DEFAULT_MODE):
    """一次性迁移：旧位置（memory/data/）有文件且 {mode} 无 → 拷贝。
    之后只读写 {mode}，旧文件保留不删（防误删历史数据）。
    旧目录 memory/data/ 已随结构整理移除，exists 检查自然跳过。"""
    legacy_dir = ROOT / "memory" / "data"
    try:
        _memory_file(mode).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for legacy, target in ((legacy_dir / "memory.md", _memory_file(mode)),
                           (legacy_dir / ".memory_index", _index_file(mode))):
        try:
            if legacy.exists() and not target.exists():
                target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass


def migrate_legacy_memory(mode: str = DEFAULT_MODE) -> None:
    """一次性迁移（**启动链显式调用**，阶段 2.6）。

    旧行为：memory_manager import 期直接调 `_migrate_legacy()` —— 于是"import memory_manager"
    这个纯读动作会在盘上建出 `user_data/{mode}/data/`，并可能拷历史文件。
    现改为由入口启动链在 `run_startup_init()` 之后显式调用（见 app/server.py main）。
    服务器版**不调用**：各账号数据根是 per-user 目录，且 server 与本地共用同一 user_data 根，
    跑本地迁移没有意义（与 run_legacy_migration 的处置一致）。"""
    _migrate_legacy(mode)


def _verify_index_file(idx_file: Path, mode: str = "") -> int | None:
    """记忆游标自愈（阶段 3.6，按文件路径）：游标 > 真实用户轮数时压低并落盘。

    为什么需要：`.memory_index` 的 `last_integrated_turn` 是"整理到第几轮"的唯一依据。
    一旦偏大（撤回/清历史/换机恢复/任何新的写对话入口忘了同步回写），后续整理会以为
    "没有新对话"而**永久跳过**——用户表现为"记忆再也不更新"，且全程没有任何报错。
    现在 api/chat 的 undo / clear-history 仍各有即时回写（那是最快的），这里是兜底自愈。
    返回修正后的游标；无需修正时原样返回（且**不写盘**）；
    对话历史读不出来（无法验证）返回 None——调用方应跳过本轮整理（B9，2026-09-15）。"""
    try:
        from modules.conversation_store import count_user_turns
        real = int(count_user_turns(mode=mode))
    except Exception as e:
        logger.warning("游标自愈跳过（读取用户轮数失败）: %s", e)
        # B9（审计 2026-09-15）：返回 None 表示「无法验证」——调用方（rest）见 None
        # 跳过本轮整理。原先返回 -1 会被当切片下界：turn <= -1 恒假 → **全量历史**
        # 喂给 LLM（token/费用暴涨，可能直接超上下文）。审计建议的"返回 0"同样错——
        # 0 也会让第 1 轮起的全部历史通过切片。唯一保守语义 = 不整理。
        return None
    cur = 0
    try:
        if idx_file.exists():
            cur = int(json.loads(idx_file.read_text(encoding="utf-8")).get("last_integrated_turn", 0))
    except Exception:
        cur = 0
    if cur <= real:
        return cur
    logger.warning("记忆游标偏大（%d > 真实 %d 轮，mode=%s）→ 已压低，整理不再被永久跳过",
                   cur, real, mode or "?")
    try:
        idx_file.parent.mkdir(parents=True, exist_ok=True)
        idx_file.write_text(json.dumps({"last_integrated_turn": real}, ensure_ascii=False),
                            encoding="utf-8")
    except OSError as e:
        logger.warning("游标自愈写入失败（下次仍会重试）: %s", e)
    return real


def verify_index(mode: str = DEFAULT_MODE) -> int:
    """记忆游标自愈（按模式）：见 `_verify_index_file`。返回（可能的）修正后游标。"""
    return _verify_index_file(_index_file(mode), mode)
