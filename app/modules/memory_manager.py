# -*- coding: utf-8 -*-
"""记忆管理器 — 单会话记忆的持久化与整理

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
以"流萤休息/起床"为整理时机：休息时 LLM 重写头部概括+追加尾部事实，起床时加载头部。
"""

import json, logging, threading
from pathlib import Path
from dataclasses import dataclass

from modules.app_config import DEFAULT_MODE, char_name, user_name
from modules.storage import atomic_write_text   # B2/B5（审计 2026-09-15）：记忆/手账/游标统一原子写

logger = logging.getLogger(__name__)
_lock = threading.Lock()
# 整理串行锁（与计数器用的 _lock 分开，避免长事务持锁期间计数器取不到）：
# 手动「休息」与后台自动整理不能同时写 memory.md / .memory_index。
_REST_SERIAL_LOCK = threading.Lock()

# 尾部"（已完成）"条目保留条数上限（2026-09-18）。见 _compose_memory_text docstring：
# 尾部只是喂给 rest 的输入，不是唯一真相源（历史归档另有留底），所以可以裁剪。
_TAIL_DONE_KEEP = 40


# ── 异常 ──────────────────────────────────────────
class MemoryManagerError(Exception): pass
class InputRejected(MemoryManagerError): pass
class OutputInvalid(MemoryManagerError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class RestResult:
    success: bool
    new_head: str
    added_entries: list   # [{"type":"承诺"|"偏好"|"事件", "text":..., "date":...}]
    resolved_entries: list # [{"type":..., "text":...}]
    integrated_turn: int
    error: str = ""


# ── 文件路径/迁移/游标自愈 ─────────────────────────
# 阶段 2.8：实现抽至 modules/memory_files.py；此处 re-export 保持消费方
# （api/chat、api/debug、server.py、tests 的 `from modules.memory_manager import _index_file` 等）零改动。
from modules.memory_files import (   # noqa: F401
    _memory_file, _index_file, _journal_file,
    _verify_today, _migrate_legacy, migrate_legacy_memory,
    _verify_index_file, verify_index,
)

# re-export 面（pyflakes 的"已使用"标记；server.py 启动链与 tests 经此取 migrate_legacy_memory）
__all__ = [
    "MemoryManager", "RestResult", "MemoryManagerError", "InputRejected", "OutputInvalid",
    "wake", "get_counters",
    "_memory_file", "_index_file", "_journal_file",
    "_verify_today", "_migrate_legacy", "migrate_legacy_memory",
    "_verify_index_file", "verify_index",
]

# memory.md 结构：
# # 核心记忆头部（休息时整体重写）
# <概括文本>
# # 事实与任务（追加区）
# ## 承诺
# - [日期] 内容（未完成/已完成）
# ## 偏好
# - [日期] 内容
# ## 事件
# - [日期] 内容

_REST_PROMPT = """你正在整理{char_name}与{user_name}的对话记忆。只输出 JSON。
当前日期：{today}

## 上次的核心记忆头部
{old_head}

## 上次的尾部事实条目
{old_tail}

## 自上次整理以来的新对话
{new_dialogue}

## 任务
1. 重写"核心记忆头部"：整合旧头部 + 本轮新发生的事，约 500-1000 字，覆盖关系现状/重要约定/近期情绪走向。不是覆盖，是整合。
2. 标记尾部哪些条目本轮"已解决"（如约定已兑现）。
3. 提取本轮新增的事实条目（承诺/偏好/事件）。

## 铁律（违反即失败）
- **只记录对话中真实发生的内容**：{user_name}明确说过的话、双方实际达成的约定、真实发生的互动。
- **不记录推断与脑补**：不把"对方可能喜欢""以后大概会"这类推测当事实；不把{char_name}自己的比喻、客气话、随口假设写成事实条目。
- **对话里不存在的人、事、物、约定——一律不新增**。
- **日期必须真实**：每条 added 的 date 取该对话内容发生时的时间（新对话每行开头 `[YYYY-MM-DD HH:MM]` 时间标记，或取"当前日期"）；格式 YYYY-MM-DD。**严禁照抄输出示例中的占位日期**（示例里的 <发生日期> 只是格式演示，不是真实值）。
- **相对时间锚定**：约定/计划里的相对时间词（"明天""后天""周末""下周"）按**说话当天的日期**换算成绝对日期再记录——如昨天说的"后天"记为明天的日期，并在条目里用绝对日期表达（可括注当时的原词）。
- 拿不准某条是不是真实发生的 → 不记。

## 输出（只输出一行 JSON）
{{"new_head": "...", "resolved": [{{"type":"承诺","text":"..."}}], "added": [{{"type":"承诺","text":"...","date":"<发生日期>"}}]}}"""

_JOURNAL_PROMPT = r"""你是{char_name}。你在整理自己的手账。以第一人称口吻，更新以下两栏。

## 当前手账
{old}

## 新一轮对话
{new}

## 手账格式（严格遵循，不超过1000字符）
# {char_name}的手账

## 我和{user_name}聊了什么
（以{char_name}的口吻，摘要记录本轮新对话中的重要内容。若已有旧内容，保留重要的旧条目并追加新的。每条一行，不超过整体40%）
## 我想要去做的事/约定
（记录想做的事和重要约定。已完成的事标记（已做完），新增事项追加到末尾。每条一行）

只输出手账正文，不要任何其他文字。"""


# ── 核心类 ────────────────────────────────────────
class MemoryManager:
    def __init__(self, client, model: str = "deepseek-flash",
                 mode: str = DEFAULT_MODE,
                 memory_file: Path | None = None, index_file: Path | None = None):
        if client is None: raise InputRejected("client 不能为 None")
        self._client = client
        self._model = model
        self._mode = mode
        self._mem_file = memory_file or _memory_file(mode)
        self._idx_file = index_file or _index_file(mode)
        self._mem_file.parent.mkdir(parents=True, exist_ok=True)

    def load_head(self) -> str:
        """加载头部概括（进 reply prompt 会话稳定层）。

        回落链（2026-09-15，记忆库入包）：运行时 memory.md → 包内 memory/default.md
        （出厂记忆）。运行时文件不存在/无头部时，用包出厂记忆的「核心记忆」段起步——
        这样新建角色也有"角色开局就记得的事"，而不是空白开局。"""
        content = ""
        if self._mem_file.exists():
            content = self._mem_file.read_text(encoding="utf-8")
        # 头部 = "# 核心记忆头部" 到 "# 事实与任务" 之间
        start = content.find("# 核心记忆头部")
        end = content.find("# 事实与任务")
        if start >= 0 and end >= 0:
            return content[start:end].strip()
        # 回落：包内出厂记忆（用户副本 → bundled，与知识库同哲学）
        try:
            from modules.app_config import mode_character_dir, bundled_character_dir
            for base in (mode_character_dir(self._mode) / "memory" / "default.md",
                         bundled_character_dir(self._mode) / "memory" / "default.md"):
                if base.is_file():
                    c = base.read_text(encoding="utf-8")
                    s = c.find("## 核心记忆")
                    e = c.find("## 既定事实")
                    if s >= 0:
                        return c[s:e if e >= 0 else None].strip()
        except Exception:
            pass
        return ""

    def load_tail(self) -> str:
        if not self._mem_file.exists(): return ""
        content = self._mem_file.read_text(encoding="utf-8")
        start = content.find("# 事实与任务")
        return content[start:].strip() if start >= 0 else ""

    def _read_index(self) -> int:
        """返回上次整理到的 turn_count，无 index 返回 0"""
        if not self._idx_file.exists(): return 0
        try:
            data = json.loads(self._idx_file.read_text(encoding="utf-8"))
            return int(data.get("last_integrated_turn", 0))
        except Exception: return 0

    def _write_index(self, turn: int):
        # B2（审计 2026-09-15）：游标文件同样原子写——截断的 .memory_index 虽然
        # 有 _verify_index_file 自愈兜底，但没必要留这个窗口
        atomic_write_text(self._idx_file,
                          json.dumps({"last_integrated_turn": turn}, ensure_ascii=False))

    def rest(self, full_history: list, current_turn_count: int, today: str | None = None,
             keep_turns: int | None = None) -> RestResult:
        """整理：把活跃窗口里**除最近 keep_turns 轮以外**的对话搬出窗口。

        full_history = **盘上全量**历史（`conversation_store.load_all`）。
        keep_turns：整理后仍留在活跃窗口的轮数（用户口径：压缩后分析器/回复器照样看近 30 轮
          完整对话）。`None` = 用 `auto_rest.keep_turns()`；显式 `0` = 全部搬走（全量整理）。
        today：整理日（YYYY-MM-DD，服务器时间优先/本地兜底由 routes 解析后传入）。

        整理做两件事（**原文留底 + 压缩摘要**，用户 2026-09-18 口径）：
        1. 搬走的那段**原文**追加进历史对话存档 `{mode}/data/archive/YYYY-MM.md`
           （只进检索器）；
        2. 同一段原文交给 LLM 压缩进 `memory.md` 头部（只进回复器）。
        游标 `last_integrated_turn` 推进到 `current_turn_count - keep_turns`。

        2026-09-18：本方法串行化（见 `_rest_locked`）。手动「让流萤休息」与后台
        *自动整理*（modules/auto_rest.py）会同时写 memory.md / .memory_index，
        并发时后写者覆盖前者的成果、游标互相错位。拿不到锁就**跳过本轮**——
        整理不是实时性任务，下一次触发会补上。
        """
        if not _REST_SERIAL_LOCK.acquire(blocking=False):
            logger.info("已有整理在进行，跳过本轮（mode=%s）", self._mode)
            return RestResult(True, self.load_head(), [], [], 0, "已有整理在跑，跳过")
        try:
            return self._rest_locked(full_history, current_turn_count, today, keep_turns)
        finally:
            _REST_SERIAL_LOCK.release()

    def _rest_locked(self, full_history: list, current_turn_count: int,
                     today: str | None = None, keep_turns: int | None = None) -> RestResult:
        """rest 的实际实现（持有 _REST_SERIAL_LOCK）。拆分原因见 rest 的 docstring。"""
        global _REST_COUNT, _REST_ERRORS
        with _lock: _REST_COUNT += 1
        today = _verify_today(today)

        # 1. 审查
        if not isinstance(full_history, list):
            with _lock: _REST_ERRORS += 1
            raise InputRejected("full_history 必须为 list")
        if not isinstance(current_turn_count, int) or current_turn_count < 0:
            with _lock: _REST_ERRORS += 1
            raise InputRejected("current_turn_count 非法")

        old_head = self.load_head()
        old_tail = self.load_tail()
        # 阶段 3.6：整理前先自愈游标 —— 游标偏大会让"新对话"判定为空、整理被永久跳过
        last_integrated = _verify_index_file(self._idx_file, self._mode)
        # B9（审计 2026-09-15）：None = 对话历史读不出来（无法验证游标）——跳过本轮
        # 整理而非把 None/-1 当切片下界（后者会把全量历史喂给 LLM）
        if last_integrated is None:
            with _lock: _REST_ERRORS += 1
            logger.error("游标自愈不可用（对话历史读取失败），跳过本轮整理，下轮重试")
            return RestResult(False, old_head, [], [], 0, "游标自愈不可用，跳过本轮整理")

        # 要归档的区间 =（last_integrated, archive_end]，其中 archive_end = 总轮数 − 保留轮数。
        # 活跃窗口不足 keep 轮时 archive_end <= last_integrated → 没有可搬走的，直接跳过。
        if keep_turns is None:
            try:
                from modules.auto_rest import keep_turns as _keep
                keep_turns = _keep()
            except Exception:
                keep_turns = 30
        archive_end = max(0, int(current_turn_count) - max(0, int(keep_turns)))
        if archive_end <= last_integrated:
            return RestResult(True, old_head, [], [], last_integrated,
                              f"活跃窗口不足 {keep_turns} 轮，无需整理")
        new_dialogue = self._slice_dialogue(full_history, last_integrated, archive_end)
        if not new_dialogue.strip():
            return RestResult(True, old_head, [], [], last_integrated, "无新对话，跳过")

        # 2. LLM 整理
        res = self._integrate(new_dialogue, old_head, old_tail, today)
        if not res.success:
            with _lock: _REST_ERRORS += 1
            return res

        # 3. 游标推进 + 原文进历史存档（只进检索器）
        self._write_index(archive_end)
        try:
            from modules.memory_archive import append_archive
            append_archive(self._mode, new_dialogue,
                           turn_from=last_integrated + 1, turn_to=archive_end, today=today)
        except Exception as e:
            # 存档失败不回滚记忆：记忆已经写盘了，存档下次整理会连同新内容一起补
            logger.warning("rest：历史存档写入失败（记忆本身已保存）: %s", e)
        return RestResult(True, res.new_head, res.added_entries, res.resolved_entries, archive_end)

    def _integrate(self, new_dialogue: str, old_head: str, old_tail: str,
                   today: str) -> RestResult:
        """把一段对话原文整合进 memory.md（rest 与 compress_text 共用）。

        只做 LLM 调用 + 校验 + 原子落盘，**不动游标、不写存档**——调用方决定这两件事。
        """
        try:
            raw = self._call_llm(old_head, old_tail, new_dialogue, today)
            parsed = self._validate_output(raw)
            parsed = self._sanitize_added_dates(parsed, today, new_dialogue)
        except OutputInvalid as e:
            logger.error("记忆整理 LLM 输出异常: %s", e)
            return RestResult(False, old_head, [], [], 0, str(e))
        except Exception as e:
            logger.error("记忆整理 API 失败: %s", e)
            return RestResult(False, old_head, [], [], 0, str(e))
        # 原子落盘（A2 收口：统一 storage.atomic_write_text）
        new_content = self._compose_memory_text(parsed.new_head, old_tail, parsed)
        # B2（审计 2026-09-15）：原子写返回值必须检查——写盘失败若仍推进游标并报成功，
        # 本轮 LLM 整理产物永久丢失（下次从 current_turn_count 之后切片，永不补回），
        # 前端还显示"记忆已更新"。
        if not atomic_write_text(self._mem_file, new_content):
            logger.error("memory.md 原子写失败（本轮整理丢弃，游标未推进，下轮重试）: %s",
                         self._mem_file)
            return RestResult(False, old_head, [], [], 0, "memory.md 写盘失败")
        return RestResult(True, parsed.new_head, parsed.added, parsed.resolved, 0)

    def compress_text(self, text: str, today: str | None = None) -> RestResult:
        """把一段**对话原文**压缩进 memory.md 头部（设定文件页「AI 压缩」按钮用）。

        与 rest 的区别：不切历史、不推进游标、不写存档——输入就是调用方给的那段原文
        （例如历史对话存档里某个月）。用途："用户想把某段旧记录主动压进记忆"。
        同样持串行锁：不能与 rest 并发写 memory.md。
        """
        body = str(text or "").strip()
        if not body:
            raise InputRejected("compress_text：内容为空")
        if not _REST_SERIAL_LOCK.acquire(blocking=False):
            return RestResult(False, self.load_head(), [], [], 0, "已有整理在跑，请稍后重试")
        try:
            today_v = _verify_today(today)
            old_head = self.load_head()
            old_tail = self.load_tail()
            res = self._integrate(body, old_head, old_tail, today_v)
            if not res.success:
                with _lock:
                    global _REST_ERRORS
                    _REST_ERRORS += 1
            return res
        finally:
            _REST_SERIAL_LOCK.release()

    def update_journal(self, new_dialogue) -> bool:
        """用新对话更新手账。以流萤口吻更新两个栏目。

        Args:
            new_dialogue: 对话文本 str，或 context 历史消息 list（内部格式化）
        """
        if isinstance(new_dialogue, list):
            lines = []
            for m in new_dialogue:
                role = user_name(self._mode) if m.get("role") == "user" else (char_name(self._mode) if m.get("role") == "assistant" else "（行为）")
                lines.append(f"{role}: {m.get('content', '')}")
            new_dialogue = "\n".join(lines)
        old_journal = ""
        # 2026-09-15：legacy 手账回退链移除（见 llm_base 同款注记）
        jf = _journal_file(self._mode)
        src = jf
        if src is not None and src.exists():
            old_journal = src.read_text(encoding="utf-8").strip()
        try:
            prompt = _JOURNAL_PROMPT.format(old=old_journal or "（空）", new=new_dialogue,
                                            char_name=char_name(self._mode),
                                            user_name=user_name(self._mode))
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": prompt}],
                max_tokens=10000,
                extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
            )
            new_content = resp.choices[0].message.content.strip()
            rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
            if not new_content and rc:
                new_content = rc
            if not new_content or f"我和{user_name(self._mode)}聊了什么" not in new_content:
                return False
            jf.parent.mkdir(parents=True, exist_ok=True)
            # B5（审计 2026-09-15）：手账唯一副本改原子写——裸写崩溃即手账全损，
            # 用户手改过的内容无法恢复（同模块 memory.md 早已原子写，此处漏了）
            if not atomic_write_text(jf, new_content):
                logger.error("手账原子写失败: %s", jf)
                return False
            return True
        except Exception as e:
            logger.error("手账更新失败: %s", e)
            return False

    def wake(self) -> str:
        """起床时加载头部。若 index 不完整（上次休息被中断）抛 MemoryManagerError。"""
        # 完整性检查：index 存在 + memory.md 存在 = 上次整理完成
        if self._idx_file.exists() and not self._mem_file.exists():
            raise MemoryManagerError("上次休息被中断，记忆不完整")
        return self.load_head()

    def _slice_new_dialogue(self, history: list, last_turn: int) -> str:
        """切片：第 last_turn 轮之后的历史转文本（保留为公共口径，等价于 `_slice_dialogue(h, last_turn, 0)`）。"""
        return self._slice_dialogue(history, last_turn, 0)

    def _slice_dialogue(self, history: list, from_turn: int, to_turn: int = 0) -> str:
        """切片：(from_turn, to_turn] 轮区间转文本；to_turn=0 表示不设上界。

        按 user 消息数计轮次——历史中夹杂的 system 行为消息不影响切片位置。
        每行带消息时间标记 `[YYYY-MM-DD HH:MM]`（记忆整理日期必须从真实消息时间取，
        否则 LLM 只能猜测/照抄模板示例日期——2026-08-22 线上 bug 根因）。
        上界的用途：整理只搬走"活跃窗口里除最近 keep_turns 轮以外"的部分，
        保留的那几十轮**留在活跃窗口**，不进存档也不进压缩（避免重复与信息错位）。
        """
        lines = []
        turn = 0
        for m in history:
            if m.get("role") == "user":
                turn += 1
            if turn <= from_turn or (to_turn and turn > to_turn):
                continue
            role = user_name(self._mode) if m.get("role") == "user" else (char_name(self._mode) if m.get("role") == "assistant" else "（行为）")
            ts = str(m.get("time") or "").strip()
            prefix = f"[{ts}] " if ts else ""
            lines.append(f"{prefix}{role}: {m.get('content','')}")
        return "\n".join(lines)

    def _call_llm(self, old_head, old_tail, new_dialogue, today: str) -> str:
        prompt = _REST_PROMPT.format(
            today=today,
            old_head=old_head or "（无）",
            old_tail=old_tail or "（无）",
            new_dialogue=new_dialogue,
            char_name=char_name(self._mode),
            user_name=user_name(self._mode),
        )
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": prompt}],
            max_tokens=10000,
            extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        )
        raw = resp.choices[0].message.content.strip()
        rc = (getattr(resp.choices[0].message, "reasoning_content", "") or "").strip()
        if not raw and rc:
            raw = rc
        return raw

    def _validate_output(self, raw: str):
        """解析 LLM 输出的 JSON（复用 llm_base.extract_json——处理字符串内引号/括号转义，
        比手写括号深度扫描更健壮；消除重复实现）"""
        from modules.llm_base import extract_json
        extracted = extract_json(raw)
        try:
            data = json.loads(extracted)
        except json.JSONDecodeError:
            raise OutputInvalid(f"JSON 解析失败: {raw[:200]}")
        for k in ("new_head", "resolved", "added"):
            if k not in data: raise OutputInvalid(f"缺少字段: {k}")
        if not isinstance(data["new_head"], str) or not data["new_head"].strip():
            raise OutputInvalid("new_head 为空")

        @dataclass
        class Parsed:
            new_head: str
            resolved: list
            added: list
        return Parsed(data["new_head"], data.get("resolved", []), data.get("added", []))

    def _sanitize_added_dates(self, parsed, today: str, new_dialogue: str):
        """日期校验兜底（2026-08-22 bug 修复的第二道防线）：
        LLM 可能照抄模板示例日期（曾为 2026-07-02）或编造日期——凡不合规的 date
        一律校正为今天（记录 warning）。合规：YYYY-MM-DD，且在
        [对话最早时间 - 7 天, 今天] 区间内。"""
        import re as _re
        from datetime import datetime as _dt, timedelta as _td

        def _ok(d: str) -> bool:
            if not isinstance(d, str):
                return False
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.strip()):
                return False
            try:
                dt = _dt.strptime(d.strip(), "%Y-%m-%d")
            except ValueError:
                return False
            t_today = _dt.strptime(today, "%Y-%m-%d")
            if dt > t_today + _td(days=1):           # 未来日期（含时差 1 天余量）→ 非法
                return False
            # 对话时间下界：取新对话中的最早 `[YYYY-MM-DD` 标记；无标记则放行
            earliest = None
            for mm in _re.finditer(r"\[(\d{4}-\d{2}-\d{2})\s", new_dialogue):
                try:
                    e = _dt.strptime(mm.group(1), "%Y-%m-%d")
                    earliest = e if earliest is None or e < earliest else earliest
                except ValueError:
                    continue
            if earliest is not None and dt < earliest - _td(days=7):
                return False
            return True

        fixed = 0
        for entry in parsed.added:
            if not isinstance(entry, dict):
                continue
            d = entry.get("date")
            if not _ok(d):
                entry["date"] = today
                fixed += 1
        if fixed:
            logger.warning("记忆整理：%d 条条目日期非法/照抄示例，已校正为 %s", fixed, today)
        return parsed

    def _compose_memory_text(self, new_head, old_tail, parsed) -> str:
        """组装新 memory.md：新头部 + 处理后的尾部。

        尾部处理（2026-09-18 P1 增强）：
        1. `resolved` 条目行尾标（已完成），**保留不删**（历史可追溯）；
        2. **去重**：同一条事实（同日期同文本）不重复追加——原先每轮整理都会把
           LLM 复述的旧事实再追加一遍，尾部只增不减；
        3. **分节标题只写一次**：原先每条 entry 前都写一个 `## 承诺/偏好/事件`，
           整理十次就有十个标题，文件越读越乱；
        4. **已完成条目上限**：只留最近 `_TAIL_DONE_KEEP` 条。这条**不丢信息**——
           P2 的历史归档（modules/memory_archive.py）已把每条事实按日期留底，
           tail 只是喂给 rest 的输入缓存，不是唯一真相源。
        """
        lines = ["# 核心记忆头部", "", new_head, "", "# 事实与任务（追加区）", ""]

        # 旧尾部按原分节收进三个桶 —— **分节标题不再原样累积**（原先每条新 entry 前
        # 都写一个 `## 承诺/偏好/事件`，整理十次就有十个标题，文件越读越乱）。
        # 顺带把 resolved 条目行尾标（已完成），保留历史可追溯。
        buckets = {"承诺": [], "偏好": [], "事件": []}
        loose = []          # 非条目、非分节的行（历史残片）：原样保留，绝不静默丢内容
        cur = "事件"
        resolved_texts = [r.get("text", "").strip()
                          for r in parsed.resolved if isinstance(r, dict)]
        if old_tail:
            # B3（审计 2026-09-15）：先削长标题再削短标题——顺序反了时第一次 replace
            # 把「# 事实与任务（追加区）」削成「（追加区）」，第二次再也匹配不到，
            # 残片被写回 memory.md 并逐次累积污染
            old_body = (old_tail.replace("# 事实与任务（追加区）", "")
                        .replace("# 事实与任务", "").strip())
            for raw_line in old_body.split("\n"):
                line = raw_line.rstrip()
                if not line:
                    continue
                if line.startswith("## "):
                    name = line[3:].strip()
                    cur = name if name in buckets else "事件"
                    continue
                if not line.startswith("- "):
                    loose.append(line)
                    continue
                for rt in resolved_texts:
                    if rt and rt in line and "已完成" not in line:
                        line = f"{line}（已完成）"
                        break
                buckets[cur].append(line)

        # 已完成条目裁剪（只留最近 N 条；见 docstring 第 4 点）
        for sec in ("承诺", "偏好", "事件"):
            rows = buckets[sec]
            done_idx = [i for i, ln in enumerate(rows) if "（已完成）" in ln]
            if len(done_idx) > _TAIL_DONE_KEEP:
                drop = set(done_idx[:len(done_idx) - _TAIL_DONE_KEEP])
                buckets[sec] = [ln for i, ln in enumerate(rows) if i not in drop]

        # 追加新条目：按类型分组、去掉与旧条目重复的
        existing = {ln.strip() for rows in buckets.values() for ln in rows}
        for entry in parsed.added:
            if not isinstance(entry, dict):
                continue
            t = str(entry.get("type") or "事件")
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            line = f"- [{str(entry.get('date') or '').strip()}] {text}"
            if line in existing:
                continue          # 去重：同一条事实不重复追加
            existing.add(line)
            buckets["承诺" if t == "承诺" else "偏好" if t == "偏好" else "事件"].append(line)

        for sec in ("承诺", "偏好", "事件"):
            if not buckets[sec]:
                continue
            lines.append(f"## {sec}")
            lines.extend(buckets[sec])
            lines.append("")
        if loose:
            lines.extend(loose)
            lines.append("")
        return "\n".join(lines)


# ── 监控 ──────────────────────────────────────────
_REST_COUNT = 0
_REST_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {"rest_count": _REST_COUNT, "rest_errors": _REST_ERRORS}


def wake(client=None, model: str = "deepseek-flash", mode: str = DEFAULT_MODE) -> str:
    """模块级起床入口：加载 {mode} 头部到会话。

    若 memory.md 不存在或为空，返回空字符串（首次启动、无记忆）。
    若检测到中断（index 存在但 memory.md 缺失），返回空串并记 error 计数——
    不抛异常，让会话以"无记忆"状态启动，避免一次中断锁死整个会话。

    阶段 3.6：入口先做一次游标自愈（游标偏大 → 整理永久跳过，见 verify_index）。"""
    try:
        verify_index(mode)
    except Exception as e:
        logger.warning("wake: 游标自愈失败（继续）: %s", e)
    mm = MemoryManager(client, model=model, mode=mode) if client is not None else MemoryManager.__new__(MemoryManager)
    if client is not None:
        mm._client = client
        mm._model = model
    mm._mem_file = _memory_file(mode)
    mm._idx_file = _index_file(mode)
    try:
        return mm.wake()
    except MemoryManagerError as e:
        logger.warning("wake: 上次休息被中断，以无记忆启动: %s", e)
        return ""
    except Exception as e:
        logger.error("wake: 加载记忆失败，以无记忆启动: %s", e)
        return ""
