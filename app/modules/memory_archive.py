# -*- coding: utf-8 -*-
"""历史对话存档（长期记忆的"原文留底"层）

## 定位（2026-09-18 用户口径）
「整理」要解决的**不是**"把旧聊天压成几句话"，而是**把滚出活跃窗口的对话原文搬走**：

```
                活跃窗口（分析器 + 回复器读它）
   ┌──────────────────────────────────────────────┐
   │  始终常驻 30–100 轮**完整对话原文**            │
   └──────────────────────────────────────────────┘
   整理时把"除最近 30 轮以外"的部分搬到这里 ↓
   ┌──────────────────────────────────────────────┐
   │  历史对话存档（**只进检索器**）                 │
   │  {mode}/data/archive/YYYY-MM.md    只增不改    │
   └──────────────────────────────────────────────┘
```

所以本模块存的是**完整对话原文**，不是压缩后的事实条目（早期版本存过事实条目，已废弃）。
压缩是可选的：`memory.md`（用户记忆）是压缩产物，只进回复器；用户也可以在设定文件页
点「AI 压缩」对某个月的原文手动触发一次压缩。

## 为什么这么设计（缓存命中率）
分析器/回复器把「## 最近对话」放在 **user 消息最前面**，后面才是环境/摘要/本轮输入；
DeepSeek 按**前缀**打折。于是活跃窗口在 30→100 的**增长期**里历史块是"只往后追加"的，
老部分是稳定前缀 → 每轮只有新那一轮没命中。
反例：固定 20 轮滑动窗口的第一行每轮都在变 → 历史块永远命中不了。
**推论（改动前必须想清楚）**：任何"从窗口头部裁剪"的逻辑都会把增长期变成滑动期，
等于把缓存打掉——`context_manager` 的 token 兜底预算因此必须放宽到正常对话不触发。

## 存储
`{mode}/data/archive/YYYY-MM.md` —— 按月分片；每片只有一段头部（幂等）。
用户可在「菜单 → 设定文件 → 历史对话存档」里直接编辑原文（改完检索器看到的就是改后的），
所以读写都要走本模块的口径。
"""

import logging
from pathlib import Path

from modules.app_config import DEFAULT_MODE, mode_data_dir

logger = logging.getLogger(__name__)

# 注入检索器时的上限（防存档无限增长把 system 撑爆）
# 原文比"事实条目"大得多：一轮约 150 字，70 轮一次整理 ≈ 1 万字。
# 80000 字 ≈ 7-8 次整理（约 500 轮）的原文，再早的靠 memory.md 压缩摘要覆盖。
MAX_MONTHS = 12
MAX_CHARS = 80000

_HEADER = """# 历史对话存档 · {month}

> 这里是**已整理部分的完整对话原文**（只增不改）。
> 它**只进检索器**；分析器/回复器读的是活跃窗口（最近 30–100 轮原文）。
> 可直接编辑本文件；也可在设定文件页点「AI 压缩」，把某个月压进「用户记忆」。
"""


def archive_dir(mode: str = DEFAULT_MODE) -> Path:
    return mode_data_dir(mode) / "archive"


def archive_months(mode: str = DEFAULT_MODE) -> list:
    """按月倒序（最新在前）返回有存档的月份，如 ["2026-09", "2026-08"]。"""
    d = archive_dir(mode)
    if not d.is_dir():
        return []
    try:
        return sorted((p.stem for p in d.glob("*.md") if p.is_file()), reverse=True)
    except OSError:
        return []


def archive_files(mode: str = DEFAULT_MODE) -> list:
    """按月倒序返回存档文件路径。"""
    d = archive_dir(mode)
    return [d / f"{m}.md" for m in archive_months(mode)]


def _month_file(mode: str, month: str) -> Path | None:
    """月份审查：只接受 YYYY-MM（防路径穿越）。"""
    import re
    m = str(month or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        return None
    return archive_dir(mode) / f"{m}.md"


def append_archive(mode: str = DEFAULT_MODE, text: str = "", turn_from: int = 0,
                   turn_to: int = 0, today: str = "") -> int:
    """把一段**对话原文**追加进当月存档。返回写入字符数（0 = 无事可做）。

    text：已格式化好的对话原文（`llm_base.format_history` 口径，带轮次标注）。
    turn_from/turn_to：这段原文覆盖的轮次范围，写进小标题便于人读与定位。
    只增不改、失败不抛——存档是留底，绝不能反过来影响记忆或聊天。
    """
    import time as _time
    body = str(text or "").strip()
    if not body:
        return 0
    day = str(today or "").strip() or _time.strftime("%Y-%m-%d")
    month = day[:7]
    d = archive_dir(mode)
    try:
        d.mkdir(parents=True, exist_ok=True)
        fp = d / f"{month}.md"
        chunks = []
        if not fp.exists():
            chunks.append(_HEADER.format(month=month))
        span = f"（第 {turn_from}–{turn_to} 轮）" if turn_to else ""
        chunks.append(f"\n## {day} 整理{span}\n\n{body}\n")
        with fp.open("a", encoding="utf-8") as f:
            f.write("".join(chunks))
        # 存档进了检索器的 system 稳定层（进程内缓存），必须同步失效——
        # 否则"本次整理的内容检索不到"，与项目踩过的"改了设定但没生效"同类
        _invalidate_retriever_cache(mode)
        return len(body)
    except OSError as e:
        logger.warning("历史存档写入失败（忽略）: %s", e)
        return 0
    except Exception as e:
        logger.warning("历史存档写入异常（忽略）: %s", e)
        return 0


def read_archive(mode: str = DEFAULT_MODE, month: str = "") -> str:
    """读某个月的存档原文（month 省略时取最新月）。读不出返回空串。"""
    months = archive_months(mode)
    m = str(month or "").strip() or (months[0] if months else "")
    fp = _month_file(mode, m)
    if fp is None or not fp.exists():
        return ""
    try:
        return fp.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("历史存档读取失败: %s", e)
        return ""


def write_archive(mode: str = DEFAULT_MODE, month: str = "", content: str = "") -> bool:
    """覆盖写某个月的存档（用户在设定文件页编辑后保存）。

    允许用户改是因为存档是**用户私有数据**：他改了什么，检索器就该看到什么。
    内容为空 → 视为删除该月文件（并清缓存）。
    """
    fp = _month_file(mode, month)
    if fp is None:
        return False
    text = str(content or "")
    try:
        if not text.strip():
            if fp.exists():
                fp.unlink()
        else:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(text, encoding="utf-8")
        _invalidate_retriever_cache(mode)
        return True
    except OSError as e:
        logger.warning("历史存档写入失败: %s", e)
        return False


def load_archive_text(mode: str = DEFAULT_MODE, max_months: int = MAX_MONTHS,
                      max_chars: int = MAX_CHARS) -> str:
    """拼接最近若干个月的存档原文（供检索器注入 system）。超长时保留较新的部分。"""
    fps = archive_files(mode)[:max(1, int(max_months or MAX_MONTHS))]
    if not fps:
        return ""
    parts = []
    used = 0
    for fp in fps:                    # 已按最新在前排序
        try:
            txt = fp.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not txt:
            continue
        if used + len(txt) > max_chars:
            room = max_chars - used
            if room < 500:
                break
            txt = txt[:room] + "\n（…本月存档过长，已截断）"
        parts.append(txt)
        used += len(txt)
    if not parts:
        return ""
    body = "\n\n".join(parts)
    return ("## 历史对话存档（你们过去聊过的原文，按月分片、越靠前越新；"
            "回答前先看这里有没有相关的旧事）\n\n" + body)


def _invalidate_retriever_cache(mode: str) -> None:
    """存档变了 → 检索器的知识缓存必须失效（懒导入避免模块环）。"""
    try:
        from modules.llm_retriever import clear_knowledge_cache
        clear_knowledge_cache(mode)
    except Exception as e:
        logger.debug("存档后清知识库缓存失败（下次重启自然生效）: %s", e)
