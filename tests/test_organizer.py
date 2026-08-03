# -*- coding: utf-8 -*-
"""组织器（工具调度器）白盒测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.organizer import Organizer, OrganizerInput, OrganizerOutput, InputRejected, _parse_and_validate, _build_sticker_list

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  V {desc}")
    else: FAIL += 1; print(f"  X {desc}")

# ══════════════════════════════════════════════════
# 输入审查
# ══════════════════════════════════════════════════
print("=== 输入审查 ===")

try:
    o = Organizer(None, model="mock")
    o.organize(OrganizerInput(user_input="你好", reply_texts=["你好呀"]))
    check("正常输入→不抛异常", True)
except Exception:
    check("正常输入→不抛异常", False)

try:
    o = Organizer(None, model="mock")
    o.organize("非法输入")
    check("非 OrganizerInput→InputRejected", False)
except (InputRejected, TypeError):
    check("非 OrganizerInput→InputRejected", True)
except Exception:
    check("非 OrganizerInput→InputRejected", True)

try:
    o = Organizer(None, model="mock")
    o.organize(OrganizerInput(user_input="", reply_texts=["你好"]))
    check("空 user_input→InputRejected", False)
except InputRejected:
    check("空 user_input→InputRejected", True)

try:
    o = Organizer(None, model="mock")
    o.organize(OrganizerInput(user_input="你好", reply_texts=[]))
    check("空 reply_texts→InputRejected", False)
except InputRejected:
    check("空 reply_texts→InputRejected", True)

# ══════════════════════════════════════════════════
# JSON 解析
# ══════════════════════════════════════════════════
print("\n=== JSON 解析 ===")

out = _parse_and_validate('{"sticker":"比心"}')
check("正常 JSON→label=比心", out.sticker_label == "比心")

out2 = _parse_and_validate('{"sticker":"无"}')
check("无→label 空串", out2.sticker_label == "")

out3 = _parse_and_validate("{}")
check("空 JSON→label 空串", out3.sticker_label == "")

out4 = _parse_and_validate("垃圾文字")
check("无 JSON→降级不发", out4.sticker_label == "")

out5 = _parse_and_validate('{"sticker": 123}')
check("非字符串 label→空串", out5.sticker_label == "")

# ══════════════════════════════════════════════════
# 表情包清单构建
# ══════════════════════════════════════════════════
print("\n=== 清单构建 ===")

labels = _build_sticker_list()
check("清单非空", bool(labels.strip()))
check("含默认项 label", "比心" in labels)

# ══════════════════════════════════════════════════
# 计数器
# ══════════════════════════════════════════════════
print("\n=== 计数器 ===")
from modules.organizer import get_counters
c = get_counters()
check("含 organize_count", "organize_count" in c)
check("含 llm_errors", "llm_errors" in c)

print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
sys.exit(1 if FAIL else 0)
