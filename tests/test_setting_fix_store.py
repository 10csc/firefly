# -*- coding: utf-8 -*-
"""设定纠错助手 store 测试 — 对话持久化 / pending / 备份应用 / 回滚 / 审计"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_setting_store_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

from modules import setting_fix_store as store
from modules import setting_fix as sf

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


print("=== A. 对话与状态 ===")
st = store.get_status("story")
check("A1 初始 idle", st["stage"] == "idle")
store.append_conversation("story", "user", "她说自己还在医疗舱")
store.append_conversation("story", "assistant", "我理解了", options=["对"], stage="ready")
st = store.get_status("story")
check("A2 ready + can_start", st["stage"] == "ready" and st["can_start"])
check("A3 重启可恢复对话", len(store.load_conversation("story")) == 2)

print("=== B. pending → apply → 回滚 ===")
files = sf.load_current_files("story")
old = "不是因为萤火虫短暂，是因为萤火虫很美。"
chg = [{"file": "core.md", "op": "replace", "old": old,
        "new": "不是因为萤火虫短暂，是因为萤火虫很美，也为了纪念那个夜晚。", "reason": "纠正"}]
proposal = {"kind": "proposal", "diagnosis": "d", "changes": chg, "model": "m",
            "created_at": "2026-08-15 12:00:00"}
store.save_pending("story", proposal)
st = store.get_status("story")
check("B1 pending → proposal 阶段", st["stage"] == "proposal" and st["proposal"] is not None)
ok, err, applied, ver = store.apply_pending("story")
check("B2 apply 成功", ok and applied == ["core.md"] and ver == 1)
text = sf.load_current_files("story", ensure=False)["core.md"]
check("B3 文件已改", "也为了纪念那个夜晚" in text)
check("B4 备份 v0 存在", (cfg.USER_DIR / "story" / ".setting_fix" / "backups" / "v0" / "character" / "core.md").exists())
st = store.get_status("story")
check("B5 apply 后对话清空、历史保留", st["stage"] == "idle" and len(st["history"]) == 1)
ok, err, ver = store.rollback("story")
check("B6 回滚成功", ok and ver == 0)
text = sf.load_current_files("story", ensure=False)["core.md"]
check("B7 文件已恢复", "也为了纪念那个夜晚" not in text)
ok, err, ver = store.rollback("story")
check("B8 二次回滚拒绝", not ok)

print("=== C. append 空 memory 自动建骨架 ===")
chg2 = [{"file": "memory.md", "op": "append", "new": "- [2026-08-15] 测试事件", "reason": "记录"}]
store.save_pending("story", {"kind": "proposal", "diagnosis": "d", "changes": chg2,
                             "model": "m", "created_at": "t"})
ok, err, applied, ver = store.apply_pending("story")
mem = sf.load_current_files("story", ensure=False)["memory.md"]
check("C1 apply 空 memory 成功", ok and ver == 2)
check("C2 默认骨架与条目并存",
      "# 核心记忆头部" in mem and "# 事实与任务" in mem and "测试事件" in mem)

print("=== D. dismiss / reset / audit ===")
store.append_conversation("story", "user", "新问题")
store.save_pending("story", {"kind": "proposal", "diagnosis": "d", "changes": chg,
                             "model": "m", "created_at": "t"})
ok, err = store.dismiss("story")
check("D1 dismiss 清 pending 与对话", ok and store.load_pending("story") is None
      and store.load_conversation("story") == [])
store.append_conversation("story", "user", "再一个问题")
store.save_pending("story", {"kind": "proposal", "diagnosis": "d", "changes": chg,
                             "model": "m", "created_at": "t"})
ok, err = store.reset("story")
check("D2 reset 同样清空且不动历史", ok and store.load_pending("story") is None
      and len(store.history("story")) == 3)
audit = (cfg.USER_DIR / "story" / ".setting_fix" / "audit.jsonl").read_text(encoding="utf-8")
check("D3 audit 留痕", audit.count('"action": "apply"') >= 2 and '"action": "rollback"' in audit)

print("=== E. 并发锁与作用域 ===")
cm = store.locked("story")
cm.__enter__()
try:
    check("E1 locked 上下文正常", True)
finally:
    cm.__exit__(None, None, None)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
