# -*- coding: utf-8 -*-
"""回复器白盒测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.polisher import Polisher, PolisherInput, PolisherOutput, InputRejected, _parse_response

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
    p = Polisher(None, model="mock")
    p.polish(PolisherInput(user_input="你好", analyzer_summary="正常聊天", recent_history=[]))
    check("正常输入→不抛异常", True)
except Exception:
    check("正常输入→不抛异常", False)

try:
    p = Polisher(None, model="mock")
    p.polish("非法输入")
    check("非 PolisherInput→InputRejected", False)
except (InputRejected, TypeError):
    check("非 PolisherInput→InputRejected", True)
except Exception:
    check("非 PolisherInput→InputRejected", True)

# ══════════════════════════════════════════════════
# 响应解析
# ══════════════════════════════════════════════════
print("\n=== 响应解析 ===")

raw = """[MSG]第一条消息
[MSG]第二条消息"""
msgs = _parse_response(raw)
check("解析→2条消息", len(msgs) == 2)
check("第1条 type=text", msgs[0]["type"] == "text")
check("第1条内容正确", msgs[0]["content"] == "第一条消息")
check("第2条 type=text", msgs[1]["type"] == "text")

raw2 = "[MSG]只有一条"
msgs2 = _parse_response(raw2)
check("单条→1条", len(msgs2) == 1)

raw3 = """[MSG]空白后忽略

[MSG]实际内容"""
msgs3 = _parse_response(raw3)
check("空行忽略→2条", len(msgs3) == 2)

raw4 = ""
msgs4 = _parse_response(raw4)
check("空响应→默认消息", len(msgs4) >= 1)
check("空响应→type=text", msgs4[0]["type"] == "text")

raw5 = " [MSG]  带空格的内容"
msgs5 = _parse_response(raw5)
check("带空格→trim正确", msgs5[0]["content"] == "带空格的内容")

# STICKER 行解析器不处理（已移交组织器）
raw6 = """[MSG]文字后面
[STICKER]"""
msgs6 = _parse_response(raw6)
check("[STICKER] 行忽略", len(msgs6) == 1)

# ══════════════════════════════════════════════════
# 带图失败 → 诚实降级（2026-08-29：不再假装看见/让用户填描述）
# ══════════════════════════════════════════════════
print("\n=== 带图失败降级 ===")

from unittest.mock import patch

p_img = Polisher(None, model="mock")
try:
    out = p_img.polish(PolisherInput(user_input="看图", analyzer_summary="x",
                                   vision_images=["data:image/png;base64,AAAA"]))
    check("带图+调用失败→降级输出", out.degraded and out.messages)
    check("带图+调用失败→看不出图片（不脑补内容）", "图片" in out.messages[0]["content"]
          and "看不清" in out.messages[0]["content"])
except Exception as e:
    import traceback
    traceback.print_exc()
    check("带图+调用失败→降级输出", False)

p_none = Polisher(None, model="mock")
try:
    out2 = p_none.polish(PolisherInput(user_input="你好", analyzer_summary="x"))
    check("无图+调用失败→通用话术（不出现图片字样）", out2.degraded
          and "图片" not in out2.messages[0]["content"])
except Exception as e:
    import traceback
    traceback.print_exc()
    check("无图+调用失败→通用话术", False)

print("\n=== 计数器 ===")
print("\n=== 计数器 ===")
from modules.polisher import get_counters
c = get_counters()
check("含 polish_count", "polish_count" in c)
check("含 llm_errors", "llm_errors" in c)

print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
sys.exit(1 if FAIL else 0)
