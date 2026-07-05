# -*- coding: utf-8 -*-
"""记忆管理器 — 单会话记忆的持久化与整理

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出
以"流萤休息/起床"为整理时机：休息时 LLM 重写头部概括+追加尾部事实，起床时加载头部。
"""

import json, logging, threading
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger(__name__)
_lock = threading.Lock()


# ── 异常 ──────────────────────────────────────────
class MemoryError(Exception): pass
class InputRejected(MemoryError): pass
class OutputInvalid(MemoryError): pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class RestResult:
    success: bool
    new_head: str
    added_entries: list   # [{"type":"承诺"|"偏好"|"事件", "text":..., "date":...}]
    resolved_entries: list # [{"type":..., "text":...}]
    integrated_turn: int
    error: str = ""


# ── 文件路径 ──────────────────────────────────────
_MEMORY_FILE = Path(__file__).resolve().parent.parent / "data" / "memory.md"
_INDEX_FILE = Path(__file__).resolve().parent.parent / "data" / ".memory_index"

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

_REST_PROMPT = """你正在整理流萤与开拓者的对话记忆。只输出 JSON。

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

## 输出（只输出一行 JSON）
{{"new_head": "...", "resolved": [{{"type":"承诺","text":"..."}}], "added": [{{"type":"承诺","text":"...","date":"2026-07-02"}}]}}"""


# ── 核心类 ────────────────────────────────────────
class MemoryManager:
    def __init__(self, client, model: str = "deepseek-v4-flash",
                 memory_file: Path = _MEMORY_FILE, index_file: Path = _INDEX_FILE):
        if client is None: raise InputRejected("client 不能为 None")
        self._client = client
        self._model = model
        self._mem_file = memory_file
        self._idx_file = index_file
        self._mem_file.parent.mkdir(parents=True, exist_ok=True)

    def load_head(self) -> str:
        """加载头部概括（进 reply prompt 会话稳定层）。无文件返回空。"""
        if not self._mem_file.exists(): return ""
        content = self._mem_file.read_text(encoding="utf-8")
        # 头部 = "# 核心记忆头部" 到 "# 事实与任务" 之间
        start = content.find("# 核心记忆头部")
        end = content.find("# 事实与任务")
        if start < 0 or end < 0: return ""
        return content[start:end].strip()

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
        self._idx_file.write_text(
            json.dumps({"last_integrated_turn": turn}, ensure_ascii=False), encoding="utf-8")

    def rest(self, full_history: list, current_turn_count: int) -> RestResult:
        """休息时整理。full_history = context_manager.get_full() 全量历史。
        流程：审查 → 读旧记忆 → LLM 整理 → 验证 → 原子落盘 → 更新 index
        """
        global _REST_COUNT, _REST_ERRORS
        with _lock: _REST_COUNT += 1

        # 1. 审查
        if not isinstance(full_history, list):
            with _lock: _REST_ERRORS += 1
            raise InputRejected("full_history 必须为 list")
        if not isinstance(current_turn_count, int) or current_turn_count < 0:
            with _lock: _REST_ERRORS += 1
            raise InputRejected("current_turn_count 非法")

        old_head = self.load_head()
        old_tail = self.load_tail()
        last_integrated = self._read_index()
        # 新对话 = 第 last_integrated 轮之后的历史
        new_dialogue = self._slice_new_dialogue(full_history, last_integrated)
        if not new_dialogue.strip():
            return RestResult(True, old_head, [], [], last_integrated, "无新对话，跳过")

        # 2. LLM 整理
        try:
            raw = self._call_llm(old_head, old_tail, new_dialogue)
            parsed = self._validate_output(raw)
        except OutputInvalid as e:
            logger.error("记忆整理 LLM 输出异常: %s", e)
            with _lock: _REST_ERRORS += 1
            return RestResult(False, old_head, [], [], last_integrated, str(e))
        except Exception as e:
            logger.error("记忆整理 API 失败: %s", e)
            with _lock: _REST_ERRORS += 1
            return RestResult(False, old_head, [], [], last_integrated, str(e))

        # 3. 原子落盘（先写临时文件再 rename）
        new_content = self._compose_memory_text(parsed.new_head, old_tail, parsed)
        tmp = self._mem_file.with_suffix(".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(self._mem_file)
        self._write_index(current_turn_count)
        return RestResult(True, parsed.new_head, parsed.added, parsed.resolved, current_turn_count)

    def wake(self) -> str:
        """起床时加载头部。若 index 不完整（上次休息被中断）抛 MemoryError。"""
        # 完整性检查：index 存在 + memory.md 存在 = 上次整理完成
        if self._idx_file.exists() and not self._mem_file.exists():
            raise MemoryError("上次休息被中断，记忆不完整")
        return self.load_head()

    def _slice_new_dialogue(self, history: list, last_turn: int) -> str:
        """切片：第 last_turn 轮之后的历史转文本。每轮 = 1 user + 1 assistant（+可能的 system 行为记录）"""
        lines = []
        # 简化：从第 last_turn*2 条消息开始
        start = min(last_turn * 2, len(history))
        for m in history[start:]:
            role = "开拓者" if m.get("role") == "user" else ("流萤" if m.get("role") == "assistant" else "（行为）")
            lines.append(f"{role}: {m.get('content','')}")
        return "\n".join(lines)

    def _call_llm(self, old_head, old_tail, new_dialogue) -> str:
        prompt = _REST_PROMPT.format(
            old_head=old_head or "（无）",
            old_tail=old_tail or "（无）",
            new_dialogue=new_dialogue,
        )
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": prompt}],
            max_tokens=2000, temperature=0.3,
            extra_body={"reasoning_effort": "high"},
        )
        return resp.choices[0].message.content.strip()

    def _validate_output(self, raw: str):
        """大括号深度匹配解析 JSON"""
        start = raw.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == "{": depth += 1
                elif raw[i] == "}":
                    depth -= 1
                    if depth == 0:
                        raw = raw[start:i+1]; break
        try:
            data = json.loads(raw)
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

    def _compose_memory_text(self, new_head, old_tail, parsed) -> str:
        """组装新 memory.md：新头部 + 处理后的尾部（resolved 标记完成 + added 追加）"""
        lines = ["# 核心记忆头部", "", new_head, "", "# 事实与任务（追加区）", ""]
        # 旧尾部去掉头部标题行（避免重复）
        if old_tail:
            old_body = old_tail.replace("# 事实与任务", "").replace("# 事实与任务（追加区）", "").strip()
            if old_body:
                # 对 resolved 条目在行尾加（已完成）标记，保留历史可追溯
                resolved_texts = [r.get("text", "").strip() for r in parsed.resolved if isinstance(r, dict)]
                for raw_line in old_body.split("\n"):
                    line = raw_line.rstrip()
                    if not line:
                        lines.append("")
                        continue
                    # 匹配 "- [日期] 内容" 形式的条目行
                    matched = False
                    if line.startswith("- "):
                        for rt in resolved_texts:
                            if rt and rt in line and "已完成" not in line:
                                lines.append(f"{line}（已完成）")
                                matched = True
                                break
                    if not matched:
                        lines.append(line)
                # 确保尾部有空行分隔
                if lines and lines[-1]:
                    lines.append("")
        # 追加新条目
        for entry in parsed.added:
            if not isinstance(entry, dict):
                continue
            t = entry.get("type", "事件")
            text = entry.get("text", "")
            date = entry.get("date", "")
            section = "## 承诺" if t == "承诺" else "## 偏好" if t == "偏好" else "## 事件"
            lines.append(f"{section}")
            lines.append(f"- [{date}] {text}")
            lines.append("")
        return "\n".join(lines)


# ── 监控 ──────────────────────────────────────────
_REST_COUNT = 0
_REST_ERRORS = 0


def get_counters() -> dict:
    with _lock:
        return {"rest_count": _REST_COUNT, "rest_errors": _REST_ERRORS}


def wake(client=None, model: str = "deepseek-v4-flash") -> str:
    """模块级起床入口：加载头部到会话。

    若 memory.md 不存在或为空，返回空字符串（首次启动、无记忆）。
    若检测到中断（index 存在但 memory.md 缺失），返回空串并记 error 计数——
    不抛异常，让会话以"无记忆"状态启动，避免一次中断锁死整个会话。
    """
    mm = MemoryManager(client) if client is not None else MemoryManager.__new__(MemoryManager)
    if client is not None:
        mm._client = client
        mm._model = model
    mm._mem_file = _MEMORY_FILE
    mm._idx_file = _INDEX_FILE
    try:
        return mm.wake()
    except MemoryError as e:
        logger.warning("wake: 上次休息被中断，以无记忆启动: %s", e)
        return ""
    except Exception as e:
        logger.error("wake: 加载记忆失败，以无记忆启动: %s", e)
        return ""
