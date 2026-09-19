# -*- coding: utf-8 -*-
"""自动整理（睡眠期计算 / sleep-time compute）+ 活跃窗口策略

## 活跃窗口是什么（2026-09-18 用户口径）
分析器与回复器读的是**活跃窗口**——"自上次整理以来"的对话原文。
除刚开场外，这个窗口**始终保持在 30–100 轮**：

```
  0 ─────────── 30 ─────────────── 80 ────── 100 ──────► 轮数
   │  开场特例   │      正常区间            │  提醒   │ 触发整理
   │            └── 整理后仍保留 30 轮原文 ──┘        │
   └──────────── 整理：把 30 轮以外的搬进历史存档 ─────┘
```

- **触发整理**：活跃窗口 ≥ 100 轮（`memory_window_turns`）
- **整理后保留**：最近 30 轮原文仍在活跃窗口里（`memory_keep_turns`）——
  即"用户点了休息，分析器/回复器照样看得到最近 30 轮完整对话"
- **顶部提醒**：活跃窗口 ≥ 80 轮（`memory_warn_turns`）时聊天页顶部显示状态

阈值故意**放得很宽**：整理一次要花 2 次 LLM 调用（记忆 + 手账），
频繁整理就是频繁烧用户的 token，所以宁可让活跃窗口大一点。

## 为什么窗口要"增长"而不是"滑动"（缓存命中率，改动前必读）
分析器/回复器把「## 最近对话」放在 **user 消息最前面**，DeepSeek 按**前缀**打折。
窗口 30→100 的增长期里历史块"只往后追加"，老部分是稳定前缀 → 每轮只有新那一轮没命中。
若改成固定轮数滑动窗口，第一行每轮都在变 → 历史块**永远命中不了**。
所以：**任何从窗口头部裁剪的逻辑都会把增长期变成滑动期、把缓存打掉**，
`context_manager` 的 token 预算必须放宽到正常对话不触发（现在 30000，纯兜底）。

## 成本（必须知情）
每次触发 **2 次 LLM 调用**：rest（压缩进 memory.md，Think High）+ update_journal（手账）。
模型 = `cfg.MODEL`。按 100 轮触发一次估算，约等于"每 100 轮替用户点一次休息"。
**不新增任何每轮（per-turn）调用** —— 自动整理只在后台发生，失败也只记日志。

## 与其他路径的关系
- `api/chat_ops.rest`（手动「让流萤休息」）：同一套 MemoryManager.rest，同步执行并回报前端。
- `clear_history`：清完历史后存档、游标、记忆一起删 → 活跃窗口归零，不会误触发。
"""

import json
import logging
import threading

from modules.app_config import DEFAULT_MODE

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_TURNS = 100     # 活跃窗口上限：到这儿才整理
DEFAULT_KEEP_TURNS = 30        # 整理后仍留在活跃窗口的轮数
DEFAULT_WARN_TURNS = 80        # 接近上限时在聊天页顶部提醒

_RUNNING: set = set()
_lock = threading.Lock()

_AUTO_COUNT = 0
_AUTO_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {"auto_rest_count": _AUTO_COUNT, "auto_rest_errors": _AUTO_ERRORS,
                "running": sorted(_RUNNING)}


def _cfg_int(key: str, default: int) -> int:
    from modules import app_config as cfg
    try:
        return int(cfg.eff_cfg(key, default))
    except (TypeError, ValueError):
        return default


def window_max() -> int:
    """活跃窗口上限（达到即整理）。<=0 表示关闭自动整理（手动休息仍可用）。"""
    return _cfg_int("memory_window_turns", DEFAULT_WINDOW_TURNS)


def keep_turns() -> int:
    """整理后保留在活跃窗口里的轮数（用户：压缩后也要能看近 30 轮完整对话）。"""
    n = _cfg_int("memory_keep_turns", DEFAULT_KEEP_TURNS)
    return n if n > 0 else DEFAULT_KEEP_TURNS


def warn_turns() -> int:
    """顶部提醒阈值。"""
    n = _cfg_int("memory_warn_turns", DEFAULT_WARN_TURNS)
    mx = window_max()
    if mx > 0 and n >= mx:
        n = max(1, int(mx * 0.8))
    return n if n > 0 else DEFAULT_WARN_TURNS


def active_turns(mode: str = DEFAULT_MODE) -> int:
    """当前活跃窗口轮数 = 盘上用户轮数 − 上次整理到的轮号。读不出来按 0。"""
    from modules.conversation_store import count_user_turns
    from modules.memory_files import _index_file
    try:
        total = int(count_user_turns(mode=mode))
    except Exception as e:
        logger.debug("active_turns 读取轮数失败: %s", e)
        return 0
    last = 0
    try:
        fp = _index_file(mode)
        if fp.exists():
            last = int(json.loads(fp.read_text(encoding="utf-8"))
                       .get("last_integrated_turn", 0) or 0)
    except Exception:
        last = 0
    return max(0, total - max(0, last))


def status(mode: str = DEFAULT_MODE) -> dict:
    """聊天页顶部状态条的数据源（前端只读，不自己算阈值）。"""
    from modules.conversation_store import count_user_turns
    try:
        total = int(count_user_turns(mode=mode))
    except Exception:
        total = 0
    active = active_turns(mode)
    mx, keep, warn = window_max(), keep_turns(), warn_turns()
    if total == 0:
        level = "empty"
    elif mx <= 0:
        level = "off"                     # 自动整理被关（config），只保留手动
    elif active >= mx:
        level = "full"                    # 已达上限（下一次回复后自动整理）
    elif active >= warn:
        level = "warn"                    # 接近上限
    else:
        level = "ok"
    return {"level": level, "active_turns": active, "total_turns": total,
            "window_max": mx, "keep_turns": keep, "warn_turns": warn,
            "auto": mx > 0}


def maybe_schedule(mode: str = DEFAULT_MODE, on_done=None, client=None) -> bool:
    """活跃窗口到上限就起一个后台线程整理。返回是否已排程。**绝不抛异常**。

    client：**由调用方在请求线程里取好传进来**。服务器版 `cfg.get_client()` 依赖
    线程本地的用户上下文（`_user_ctx`），后台线程里那个上下文是空的——
    不传的话会取到错误的客户端/取不到。本地版传不传都一样，但统一走这条更安全。
    on_done(mode, new_head)：整理成功后的回调（在后台线程里执行）。用于刷新
    进程内会话的 memory_head —— 放在调用方（api/chat）是因为 modules 不应反向
    依赖 routes/api 层。
    """
    try:
        mx = window_max()
        if mx <= 0:
            return False
        act = active_turns(mode)
        if act < mx:
            return False
        with _lock:
            if mode in _RUNNING:
                return False
            _RUNNING.add(mode)
        # ★ 2026-09-19：把当前请求的上下文**整体带进后台线程**（contextvars.copy_context）。
        #
        # 为什么必须：`contextvars` 在**新建线程里是空的、不继承**。worker 里
        # `_user_ctx_dir()` 返回 None ⇒ `core/paths.py` 的 `mode_root()` 回落到
        # `_user_ctx_dir() or USER_DIR` ⇒ **全局 USER_DIR**。服务器版后果：
        #   · 用户的「自动整理」读写**错目录** ⇒ 他自己的活跃窗口永不收缩
        #     （一直涨到 100+ 轮 → 每轮 prompt 越来越长 → 越聊越慢、越聊越贵）；
        #   · 全局根下堆出无用目录；同名时还可能被别的用户读到。
        # 本地版**完全看不出来**（本地本来就没有用户上下文，USER_DIR 就是正确的根）
        # —— 又一例"本地全绿、服务器错"（与错误 #10 同族）。
        #
        # 带上整个 context 后 user_dir / api_key / user_overlay 全部可见，
        # docstring 里那个"client 必须在请求线程取好"的变通也就不再必要了
        # （参数保留是为了把这个依赖显式暴露出来）。
        import contextvars
        ctx = contextvars.copy_context()
        threading.Thread(target=ctx.run, args=(_worker, mode, on_done, client),
                         name=f"auto-rest-{mode}", daemon=True).start()
        logger.info("自动整理已排程（mode=%s, 活跃窗口 %d 轮 ≥ 上限 %d）", mode, act, mx)
        return True
    except Exception as e:
        logger.warning("自动整理排程失败（忽略，不影响聊天）: %s", e)
        return False


def _worker(mode: str, on_done=None, client=None) -> None:
    global _AUTO_COUNT, _AUTO_ERRORS
    new_head = ""
    try:
        from modules import app_config as cfg
        from modules.conversation_store import load_all_context, count_user_turns
        from modules.memory_manager import MemoryManager

        if client is None:
            client = cfg.get_client()
        if not client:
            return
        # 与手动 rest 同口径：**盘上全量**（role 形状，不是 load_all 的 who 形状）
        # 历史 + 盘上真实轮数。内存 ctx 被活跃窗口截断，用它会让游标写错。
        full = load_all_context(mode)
        if not full:
            return
        mm = MemoryManager(client, cfg.MODEL, mode=mode)
        res = mm.rest(full, count_user_turns(mode=mode), keep_turns=keep_turns())
        if not res.success:
            logger.info("自动整理跳过（mode=%s）: %s", mode, res.error)
            return
        new_head = mm.load_head()
        mm.update_journal(full[-100:])
        try:
            from modules.llm_base import reload_journal
            reload_journal(mode)
        except Exception as e:
            logger.debug("自动整理：重载手账失败: %s", e)
        with _lock:
            _AUTO_COUNT += 1
        logger.info("自动整理完成（mode=%s，游标推进到第 %d 轮）", mode, res.integrated_turn)
    except Exception as e:
        with _lock:
            _AUTO_ERRORS += 1
        logger.warning("自动整理失败（忽略，不影响聊天）: %s", e)
    finally:
        if new_head and on_done:
            try:
                on_done(mode, new_head)
            except Exception as e:
                logger.debug("自动整理回调失败: %s", e)
        with _lock:
            _RUNNING.discard(mode)
