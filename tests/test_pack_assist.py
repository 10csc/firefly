# -*- coding: utf-8 -*-
"""pack_assist 校验器单测（不调 LLM）：规则表 / 锚点唯一性 / 硬边界 / 白名单"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import tempfile
import core.paths as _paths
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_assist_"))
_paths.USER_DIR = _tmp
_paths.CONFIG_FILE = _tmp / "config.json"

from modules.pack_assist import assistable, rule_for, validate_proposal  # noqa: E402

PASS = FAIL = 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")

print("=== A. 规则表与白名单 ===")
check("A1 人设核心件可辅助", assistable("core.md") and assistable("prompts/polisher.md"))
check("A2 知识库通配", assistable("knowledge/world/regions.md"))
check("A3 出厂记忆通配", assistable("memory/default.md"))
check("A4 包定义不可辅助（preset.json 走表单）", not assistable("preset.json"))
check("A5 杂项不可辅助", not assistable("random.txt") and not assistable("../evil.md"))
check("A6 polisher 强制保留 [MSG] 协议段", rule_for("prompts/polisher.md").get("must_contain") == ["[MSG]"])
check("A7 memory 强制两区结构", rule_for("memory/").get("must_contain") == ["## 核心记忆", "## 既定事实"])

print("=== B. 锚点与操作校验 ===")
doc = "# 标题\n甲内容。\n乙内容。\n"
ok, errs = validate_proposal(doc, [{"op": "replace", "old": "甲内容。", "new": "甲新内容。"}], "core.md")
check("B1 合法 replace 通过", ok)
ok, errs = validate_proposal(doc, [{"op": "replace", "old": "不存在", "new": "x"}], "core.md")
check("B2 锚点不存在被拒", not ok and any("逐字唯一" in e for e in errs))
doc2 = "# 标题\n重复。\n重复。\n"
ok, errs = validate_proposal(doc2, [{"op": "replace", "old": "重复。", "new": "x"}], "core.md")
check("B3 锚点不唯一被拒", not ok)
ok, errs = validate_proposal(doc, [{"op": "delete", "old": "甲", "new": ""}], "core.md")
check("B4 白名单外操作（delete）被拒", not ok)
ok, errs = validate_proposal(doc, [{"op": "append", "new": "新条目", "reason": "r"}], "core.md")
check("B5 append 合法", ok)
ok, errs = validate_proposal(doc, [{"op": "append", "new": "x" * 2001}], "core.md")
check("B6 单处超 2000 字被拒", not ok)

print("=== C. 结构硬边界 ===")
pol = "人设段。\n[MSG] 输出协议。\n"
ok, errs = validate_proposal(pol, [{"op": "replace", "old": "[MSG] 输出协议。", "new": "改掉了"}], "prompts/polisher.md")
check("C1 改掉 [MSG] 协议段被拒", not ok and any("[MSG]" in e for e in errs))
ok, errs = validate_proposal(pol, [{"op": "replace", "old": "人设段。", "new": "温柔一点的人设。"}], "prompts/polisher.md")
check("C2 只改人设段通过", ok)
mem = "## 核心记忆\n甲。\n## 既定事实\n- [事实] 乙。\n"
ok, errs = validate_proposal(mem, [{"op": "replace", "old": "## 既定事实", "new": "## 旧事"}], "memory/default.md")
check("C3 改坏记忆两区结构被拒", not ok)
ok, errs = validate_proposal(mem, [{"op": "append", "new": "- [事实] 丙。", "reason": "r"}], "memory/default.md")
check("C4 记忆区追加通过", ok)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
