# -*- coding: utf-8 -*-
"""长期记忆机制回归（2026-09-18 现状：活跃窗口 30–100 + 原文存档 + 压缩摘要）

一次讲清这套机制的四层（用户 2026-09-18 口径）：

| 层 | 内容 | 谁读 | 更新 |
|----|------|------|------|
| L0 出厂记忆 `{pack}/memory/default.md` | 角色开局记忆 | 分析器 + 回复器 | 角色卡管理手改 |
| L1 用户记忆 `{mode}/data/memory.md` | 压缩摘要 | **回复器**（memory_head） | 整理时自动 / 用户点「AI 压缩」 |
| L2 历史存档 `{mode}/data/archive/YYYY-MM.md` | **完整对话原文** | **只进检索器** | 整理时把滚出窗口的原文追加 |
| L3 活跃窗口（conversation.jsonl 里"自上次整理以来"的部分） | 对话原文 | 分析器 + 回复器 | 每轮；整理后保留最近 30 轮 |

本测试钉死四件容易回退的事：

1. **活跃窗口语义**：整理只搬走"除最近 keep_turns 轮以外"的部分，
   保留的那几十轮**仍在窗口里**（用户：压缩后分析器/回复器照样看近 30 轮完整对话）。
2. **存档是原文、只增不改**，且可读/可写/可列出月份（用户要能自己看与改）。
3. **窗口阈值**：100 轮触发 / 80 轮提醒 / 30 轮保底，且阈值宽到正常对话不触发。
4. **token 预算是兜底不是裁剪手段**：预算必须宽到 100 轮窗口不触发——
   一旦从窗口头部裁剪，窗口就从"增长"变"滑动"，历史块的缓存前缀每轮都变（命中率归零）。

另外回归上一轮的 P0 修复：rest 用**盘上全量**历史与轮数（内存 ctx 被窗口截断，
用它当全量会让游标写错、整理永久跳过）；清历史时记忆/手账/游标/存档一起清、角色卡资产不动。

沙箱纪律：USER_DIR 指到临时目录，绝不触碰真实 user_data/。
"""

import json
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import modules.app_config as cfg          # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_memory_auto_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


from core import config as _cc                                    # noqa: E402
import modules.app_config as _ac                                  # noqa: E402
from modules import auto_rest, memory_archive                     # noqa: E402
from modules.app_config import mode_data_dir, mode_journal_dir    # noqa: E402
from modules.context_manager import (BUDGET_ANALYZER, BUDGET_POLISHER,  # noqa: E402
                                     ContextManager, InputRejected, _MIN_TURNS_KEEP)
from modules.conversation_store import (append_message, count_user_turns,  # noqa: E402
                                        load_all, load_all_context)
from modules.memory_files import _index_file, _journal_file, _memory_file  # noqa: E402

MODE = "story"


class FakeH:
    def __init__(self, body=None, path="/x"):
        self.data = None
        self.status = 200
        self.path = path
        raw = json.dumps(body or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = __import__("io").BytesIO(raw)
        self.wfile = __import__("io").BytesIO()

    def _json(self, data, status=200):
        self.data = data
        self.status = status


def _rejects_empty(mm_obj) -> bool:
    from modules.memory_manager import InputRejected as _IR
    try:
        mm_obj.compress_text("   ")
        return False
    except _IR:
        return True


def _wake_interrupted(mode: str) -> bool:
    """走真实端点读「上次休息是否被中断」（清历史后必须是 False）。"""
    h = FakeH(path=f"/wake-status?mode={mode}")
    routes.get_wake_status(h)
    return bool((h.data or {}).get("interrupted"))


def _plant(turns: int, start: int = 1):
    """往盘上种 turns 轮对话（seq 续写）。"""
    for i in range(start, start + turns):
        append_message("user", {"type": "text", "content": f"用户第{i}轮"}, mode=MODE)
        append_message("firefly", {"type": "text", "content": f"流萤第{i}轮"}, mode=MODE)


print("=== A. token 预算是兜底不是裁剪手段（缓存命中率铁律） ===")
cm = ContextManager(token_capacity=100000)
for i in range(120):
    cm.add_turn(f"用户第{i}轮" + "啊" * 40, "流萤回复" + "哦" * 40)

_a = cm.get_recent(3)
check("A1 不传预算时仍是「最后 n 轮」（旧语义不变）",
      [m["role"] for m in _a] == ["user", "assistant"] * 3)

_a = cm.get_recent(100, max_tokens=BUDGET_ANALYZER)
_users = len([m for m in _a if m["role"] == "user"])
check(f"A2 100 轮活跃窗口在预算内**不被裁剪**（{_users} 轮全给；裁了就等于滑动窗口、缓存全废）",
      _users == 100)
check("A3 回复器预算同样容得下 100 轮", BUDGET_POLISHER >= BUDGET_ANALYZER)

_a = cm.get_recent(100, max_tokens=1)
check(f"A4 预算极小时至少保留 {_MIN_TURNS_KEEP} 轮（不给空上下文）",
      len([m for m in _a if m["role"] == "user"]) == _MIN_TURNS_KEEP)
check("A5 裁剪按整轮，首条一定是 user（半轮会让 LLM 看到「没有提问的回答」）",
      _a[0]["role"] == "user")

try:
    cm.get_recent(0)
    _ok = False
except InputRejected:
    _ok = True
check("A6 非法 n_turns 仍被审查阶段拒绝", _ok)


print("=== B. load_all 不受分页限制（rest 的盘上口径前置） ===")
d = mode_data_dir(MODE)
d.mkdir(parents=True, exist_ok=True)
_plant(100)                      # 200 条消息
_all = load_all(MODE)
check("B1 load_all 取到全部 200 条（load_recent 默认只有 150）", len(_all) == 200)
check("B2 按 seq 升序", [m["seq"] for m in _all] == sorted(m["seq"] for m in _all))
check("B3 count_user_turns = 100", count_user_turns(mode=MODE) == 100)

# ★ B4-B6：形状必须与 rest 的读法一致。
# 2026-09-18 实测踩过：把 load_all()（jsonl 原始行，字段是 who）直接喂给 rest，
# 而 rest 的切片读 role → 每行落进「（行为）」分支、turn 永不涨 → 切出来是空的。
_ctxmsgs = load_all_context(MODE)
check("B4 load_all_context 把 who 形状转成 role 形状（rest 的读法）",
      _ctxmsgs and all("role" in m for m in _ctxmsgs))
check("B5 逐条 1:1、不合并连发（否则与 count_user_turns 的游标口径分叉）",
      len([m for m in _ctxmsgs if m["role"] == "user"]) == count_user_turns(mode=MODE))
check("B6 带时间戳（整理日期必须从真实消息时间取）",
      all(m.get("time") for m in _ctxmsgs))


print("=== C. 活跃窗口阈值：100 触发 / 80 提醒 / 30 保底 ===")
_cc.config.pop("memory_window_turns", None)
_cc.config.pop("memory_keep_turns", None)
_cc.config.pop("memory_warn_turns", None)
check("C1 默认上限 100 轮", auto_rest.window_max() == 100)
check("C2 默认保留 30 轮", auto_rest.keep_turns() == 30)
check("C3 默认提醒 80 轮", auto_rest.warn_turns() == 80)
check("C4 阈值足够宽（触发 ≥ 3× 保底：避免频繁烧 token）",
      auto_rest.window_max() >= 3 * auto_rest.keep_turns())

# 此刻 100 轮、游标不存在 → 活跃窗口 100，正好到上限
_st = auto_rest.status(MODE)
check("C5 活跃窗口 = 盘上轮数 − 游标（此处 100 − 0）", _st["active_turns"] == 100)
check("C6 到上限 → level=full", _st["level"] == "full")
check("C7 active_turns 与 status 一致", auto_rest.active_turns(MODE) == 100)

# 游标推到 30 → 活跃窗口 70 → 落在 warn 区间
_index_file(MODE).write_text(json.dumps({"last_integrated_turn": 30}), encoding="utf-8")
_st = auto_rest.status(MODE)
check("C8 活跃窗口 70 轮 → warn（≥80 才提醒，70 还没到）", _st["level"] == "ok")
_index_file(MODE).write_text(json.dumps({"last_integrated_turn": 15}), encoding="utf-8")
check("C9 活跃窗口 85 轮 → warn", auto_rest.status(MODE)["level"] == "warn")
_index_file(MODE).write_text(json.dumps({"last_integrated_turn": 0}), encoding="utf-8")

_cc.config["memory_window_turns"] = 0
check("C10 上限 0 = 关闭自动整理（手动休息仍可用）",
      auto_rest.status(MODE)["level"] == "off" and auto_rest.maybe_schedule(MODE) is False)
_cc.config["memory_window_turns"] = 500
check("C11 未到上限不排程", auto_rest.maybe_schedule(MODE) is False)
_cc.config.pop("memory_window_turns", None)


print("=== D. 历史存档：原文、只增不改、可读可写可列月份 ===")
n1 = memory_archive.append_archive(MODE, text="[第1轮] 开拓者: 明天去看电影\n      流萤: 好呀",
                                   turn_from=1, turn_to=30, today="2026-09-18")
check("D1 返回写入字符数 > 0", n1 > 0)
check("D2 月份列表含 2026-09", memory_archive.archive_months(MODE) == ["2026-09"])
_txt = memory_archive.read_archive(MODE, "2026-09")
check("D3 存的是**对话原文**（不是压缩后的事实条目）",
      "[第1轮] 开拓者: 明天去看电影" in _txt and "流萤: 好呀" in _txt)
check("D4 小标题带轮次范围（人能定位）", "第 1–30 轮" in _txt)
check("D5 首次写入带说明头（幂等）", _txt.count("历史对话存档 · 2026-09") == 1)

memory_archive.append_archive(MODE, text="[第31轮] 开拓者: 到门口了",
                              turn_from=31, turn_to=60, today="2026-09-25")
_txt2 = memory_archive.read_archive(MODE, "2026-09")
check("D6 只增不改：上一段原文仍在", "明天去看电影" in _txt2)
check("D7 新一段已追加", "到门口了" in _txt2)
check("D8 说明头只写一次", _txt2.count("历史对话存档 · 2026-09") == 1)

check("D9 空 text 不写文件",
      memory_archive.append_archive("haruno", text="", today="2026-09-18") == 0
      and memory_archive.archive_months("haruno") == [])

_loaded = memory_archive.load_archive_text(MODE)
check("D10 可拼成检索器读的一段（带说明 + 原文）",
      "历史对话存档" in _loaded and "明天去看电影" in _loaded)

check("D11 用户可编辑存档（改完检索器就看到改后的）",
      memory_archive.write_archive(MODE, "2026-09", "用户自己改过的存档") is True
      and memory_archive.read_archive(MODE, "2026-09") == "用户自己改过的存档")
_memory_archive_cache = __import__("modules.llm_retriever", fromlist=["x"])._KNOWLEDGE_CACHE
check("D12 改存档后检索器缓存已失效（不会「改了却检索不到」）", (MODE + "::") not in "".join(_memory_archive_cache.keys()))
check("D13 月份非法（路径穿越）被拒", memory_archive.write_archive(MODE, "../evil", "x") is False)
check("D14 内容清空 = 删该月文件",
      memory_archive.write_archive(MODE, "2026-09", "   ") is True
      and memory_archive.archive_months(MODE) == [])


print("=== E. rest 的窗口语义：只搬走最近 30 轮以外的部分 ===")
from modules.memory_manager import MemoryManager   # noqa: E402


class MockMessage:
    def __init__(self, content):
        self.content = content


class MockResponse:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": MockMessage(content)})()]


class MockClient:
    def __init__(self, responses):
        self._q = list(responses)
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kw):
        return MockResponse(self._q.pop(0) if self._q else "{}")


_mem = _memory_file(MODE)
_idx = _index_file(MODE)
_mem.parent.mkdir(parents=True, exist_ok=True)
for p in (_mem, _idx):
    if p.exists():
        p.unlink()

mm = MemoryManager(MockClient([json.dumps({"new_head": "新头部", "resolved": [], "added": []})]),
                   memory_file=_mem, index_file=_idx)

_r = mm.rest(load_all_context(MODE), 100, today="2026-09-18", keep_turns=30)
check("E1 rest 成功", _r.success is True)
check("E2 游标推进到 100−30=70（最近 30 轮留在活跃窗口）", _r.integrated_turn == 70)
check("E3 游标确实落盘", json.loads(_idx.read_text(encoding="utf-8"))["last_integrated_turn"] == 70)
check("E4 活跃窗口回落到 30 轮", auto_rest.active_turns(MODE) == 30)

_arch = memory_archive.read_archive(MODE, "2026-09")
check("E5 搬走的那段原文进了存档（第 1–70 轮）", "用户第1轮" in _arch and "第 1–70 轮" in _arch)
check("E6 保留的 30 轮**没进存档**（留在窗口，避免重复）", "用户第100轮" not in _arch)

# 窗口不足 keep 轮时不该整理（也不该动游标）
mm2 = MemoryManager(MockClient([]), memory_file=_mem, index_file=_idx)
_r2 = mm2.rest(load_all_context(MODE), 100, today="2026-09-18", keep_turns=30)
check("E7 活跃窗口已只剩 30 轮 → 无需整理（不烧 LLM 调用）",
      _r2.success is True and _r2.integrated_turn == 70 and "无需整理" in _r2.error)


print("=== F. compress_text：用户点「AI 压缩」把存档压进用户记忆 ===")
memory_archive.append_archive(MODE, text="[第1轮] 开拓者: 我想去看海", turn_from=1,
                              turn_to=70, today="2026-10-01")
mm3 = MemoryManager(
    MockClient([json.dumps({"new_head": "压缩后的记忆头部", "resolved": [],
                            "added": [{"type": "事件", "text": "约好去看海", "date": "2026-10-01"}]})]),
    memory_file=_mem, index_file=_idx)
_r3 = mm3.compress_text(memory_archive.read_archive(MODE, "2026-10"), today="2026-10-01")
check("F1 压缩成功", _r3.success is True)
check("F2 新头部写进 memory.md", "压缩后的记忆头部" in _mem.read_text(encoding="utf-8"))
check("F3 不动游标（压缩 ≠ 整理）",
      json.loads(_idx.read_text(encoding="utf-8"))["last_integrated_turn"] == 70)
check("F4 空内容被审查阶段拒绝", _rejects_empty(mm3))

_loaded2 = memory_archive.load_archive_text(MODE)
check("F5 存档本身不受压缩影响（原文留着，可反复压）", "我想去看海" in _loaded2)


print("=== G. 尾部结构化：去重 + 分节标题只写一次 ===")


class _P:
    def __init__(self, added=None, resolved=None):
        self.added = added or []
        self.resolved = resolved or []


mmx = MemoryManager.__new__(MemoryManager)
mmx._mode = MODE
_old_tail = "# 事实与任务（追加区）\n\n## 承诺\n- [2026-09-01] 一起看星星\n\n"
_parsed = _P(added=[
    {"type": "承诺", "text": "一起看星星", "date": "2026-09-01"},   # 与旧条目重复
    {"type": "承诺", "text": "带蛋糕", "date": "2026-09-02"},
    {"type": "承诺", "text": "看日落", "date": "2026-09-03"},
    {"type": "事件", "text": "去了海边", "date": "2026-09-04"},
])
_out = mmx._compose_memory_text("新头部", _old_tail, _parsed)
check("G1 重复事实不再重复追加（旧实现每轮都会再追加一遍）", _out.count("一起看星星") == 1)
check("G2 ## 承诺 分节标题只出现一次（旧实现每条 entry 都写一次）", _out.count("## 承诺") == 1)
check("G3 新条目都在", "带蛋糕" in _out and "看日落" in _out and "去了海边" in _out)
check("G4 分节标题不累积", _out.count("## 事件") == 1)
check("G5 头部与尾部锚点仍在（load_head 靠这两个标记切片）",
      "# 核心记忆头部" in _out and "# 事实与任务" in _out)
_many = "# 事实与任务（追加区）\n\n" + "\n".join(
    f"- [2026-08-{i:02d}] 旧事{i}（已完成）" for i in range(1, 61))
check("G6 已完成条目被裁到上限（原文存档另有留底）",
      mmx._compose_memory_text("新头部", _many, _P()).count("（已完成）") <= 40)


print("=== H. 清除历史：记忆/手账/游标/存档一起清，角色卡资产不动 ===")
_mem.write_text("# 核心记忆头部\n\n聊天产生的旧摘要\n", encoding="utf-8")
_idx.write_text('{"last_integrated_turn": 70}', encoding="utf-8")
_journal_file(MODE).parent.mkdir(parents=True, exist_ok=True)
_journal_file(MODE).write_text("# 手账\n\n聊天产生的旧手账\n", encoding="utf-8")
memory_archive.append_archive(MODE, text="存档原文", turn_from=1, turn_to=70, today="2026-10-02")
_pack_mem = mode_data_dir(MODE) / "character" / "memory" / "default.md"
_pack_mem.parent.mkdir(parents=True, exist_ok=True)
_pack_mem.write_text("# 出厂记忆\n\n角色开局就记得的事\n", encoding="utf-8")

import routes   # noqa: E402

routes.clear_history(FakeH({"mode": MODE}))
check("H1 memory.md 被删（聊天产物）", not _mem.exists())
check("H2 手账.md 被删", not _journal_file(MODE).exists())
check("H3 .memory_index 被删（一起删才不会被判成「上次休息被中断」）", not _idx.exists())
check("H4 历史存档目录被删（留着就等于「清了历史还记得细节」）",
      not memory_archive.archive_dir(MODE).is_dir())
check("H5 出厂记忆（角色卡资产）不受影响",
      _pack_mem.exists() and "出厂记忆" in _pack_mem.read_text(encoding="utf-8"))
check("H6 对话文件被清空 → 活跃窗口归零", count_user_turns(mode=MODE) == 0
      and auto_rest.active_turns(MODE) == 0)
check("H7 清完历史 wake 不误报「上次休息被中断」（memory.md 与 index 同时不在）",
      _wake_interrupted(MODE) is False)


print("=== J. 端点层：状态 / 存档读 / 存档写 / 动作派发 ===")
from api import debug as _dbg   # noqa: E402

_plant(40, start=1)
_h = FakeH(path=f"/memory-status?mode={MODE}")
_dbg.get_memory_status(_h)
check("J1 /memory-status 返回活跃窗口状态", (_h.data or {}).get("active_turns") == 40)
check("J2 状态条数据源含上限/保底/提醒三个数",
      all(k in (_h.data or {}) for k in ("window_max", "keep_turns", "warn_turns")))

memory_archive.append_archive(MODE, text="[第1轮] 开拓者: 存档探针", turn_from=1,
                              turn_to=40, today="2026-11-05")
_h = FakeH(path=f"/archive?mode={MODE}")
_dbg.get_archive(_h)
check("J3 /archive 返回月份列表（最新在前）",
      ((_h.data or {}).get("months") or [])[:1] == ["2026-11"])
check("J4 /archive 默认给最新月原文", "存档探针" in ((_h.data or {}).get("content") or ""))
_h = FakeH(path=f"/archive?mode={MODE}&month=2020-01")
_dbg.get_archive(_h)
check("J5 不存在的月份 → 404（不是静默给空）", _h.status == 404)

_h = FakeH({"action": "save_archive", "mode": MODE, "month": "2026-11",
            "content": "端点改过的存档"}, path="/memory-action")
_dbg.memory_action(_h)
check("J6 /memory-action save_archive 生效",
      (_h.data or {}).get("ok") is True
      and memory_archive.read_archive(MODE, "2026-11") == "端点改过的存档")

_h = FakeH({"action": "rm -rf /", "mode": MODE}, path="/memory-action")
_dbg.memory_action(_h)
check("J7 未知 action 被白名单拒绝（400 + 列出允许项）",
      _h.status == 400 and "allowed" in (_h.data or {}))

_orig_gc = _ac.__dict__.get("get_client")
# ⚠️ 必须写进 `__dict__` 才能盖住 app_config 里那个 `from core.config import get_client`
# 的**名字拷贝**（改 core.config.get_client 是无效的——两者只在 import 那一刻相同）。
# 不这么做，compress 分支会拿着真实 config 里的 Key 去打真 API（本项目踩过的
# 「测试真烧用户额度」老坑）。下面 J8/J9 全程不允许出现任何网络调用。
_ac.__dict__["get_client"] = lambda *a, **k: None
try:
    _h = FakeH({"action": "compress", "mode": MODE, "month": "2026-11"}, path="/memory-action")
    _dbg.memory_action(_h)
    check("J8 未设 Key 时 compress 明确拒绝（不静默失败）",
          (_h.data or {}).get("ok") is False and "Key" in ((_h.data or {}).get("error") or ""))

    _h = FakeH({"action": "compress", "mode": MODE, "month": "2020-01"}, path="/memory-action")
    _dbg.memory_action(_h)
    check("J9 空月份 compress 被拒（不会拿空文本去调模型）",
          (_h.data or {}).get("ok") is False)
finally:
    if _orig_gc is None:
        _ac.__dict__.pop("get_client", None)
    else:
        _ac.__dict__["get_client"] = _orig_gc


print("=== K. rest 串行锁：手动休息与自动整理不会并发写同一份记忆 ===")
_won = []
_orig_locked = MemoryManager._rest_locked


def _fake_locked(self, full_history, current_turn_count, today=None, keep_turns=None):
    _won.append(1)
    from modules.memory_manager import RestResult
    return RestResult(True, "head", [], [], current_turn_count, "")


MemoryManager._rest_locked = _fake_locked
try:
    mm4 = MemoryManager.__new__(MemoryManager)
    mm4._mode = MODE
    mm4._mem_file = _mem
    mm4._idx_file = _idx
    r1 = mm4.rest([], 0)
    r2 = mm4.rest([], 0)
    check("K1 顺序两次 rest 都真正执行了（锁不误伤正常路径）",
          r1.success and r2.success and len(_won) == 2)
finally:
    MemoryManager._rest_locked = _orig_locked


print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
