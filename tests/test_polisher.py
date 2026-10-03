# -*- coding: utf-8 -*-
"""回复器白盒测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from modules.polisher import (Polisher, PolisherInput, PolisherOutput, InputRejected,
                              _parse_response, DEGRADED_TEXT)

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
# 思考块污染（2026-09-24 用户报障"偶尔出现的消息重复"）
#
# 根因：MiniMax-M3 不遵守 reasoning_content 通道约定，把整段思考内联写进 content，
# 且思考里会预写多版 [MSG] 草稿。旧解析只看行首 [MSG] → 草稿与正文一起进列表。
# 下面是用户诊断包里的**真实 raw**（截取），必须只出正文。
# ══════════════════════════════════════════════════
print("\n=== 思考块污染（消息重复根因）===")

# 真实形态：think 内 3 条草稿 + think 外 3 条正文
raw_think = """<think>
用户在催我，我要回应得俏皮一点。
[MSG] 五点半，记住了
[MSG] 那我故意慢慢开的计划要泡汤了
[MSG] 蛋糕卷和夜宵都带着，亲亲的账见面一起算
嗯，第三条再短一点更像短信。
</think>
[MSG] 五点半，记住了
[MSG] 那我故意慢慢开的计划要泡汤了
[MSG] 蛋糕卷和夜宵都带着，亲亲的账见面一起算"""
m_t = _parse_response(raw_think)
check("think 块被剥离→3条", len(m_t) == 3)
check("think 块不生重复", len({m["content"] for m in m_t}) == len(m_t))
check("取的是正文（最后一条）", m_t[-1]["content"] == "蛋糕卷和夜宵都带着，亲亲的账见面一起算")
check("不泄漏 think 文字", all("用户" not in m["content"] and "更像短信" not in m["content"]
                            for m in m_t))

# 逐字重复（两组内容完全相同）→ 去重后只剩一组
raw_dup = """<think>
[MSG] A1
[MSG] A2
[MSG] A3
</think>
[MSG] A1
[MSG] A2
[MSG] A3"""
m_d = _parse_response(raw_dup)
check("两组逐字重复→去重为3条", len(m_d) == 3)
check("去重后无重复", len({m["content"] for m in m_d}) == 3)

# 只有 think、没有正文（闭合标签后为空）→ 退回 think 内草稿，不整轮降级
raw_only_think = """<think>
[MSG] 只有思考里写了消息
</think>
"""
m_ot = _parse_response(raw_only_think)
check("正文为空→退回 think 兜底（不降级）", len(m_ot) == 1
      and m_ot[0]["content"] == "只有思考里写了消息")

# 只有开标签、无闭标签（流式被截断）→ 全按思考处理，仍能拿到 [MSG]
raw_open_only = "<think>\n[MSG] 被截断的思考里也有消息\n[MSG] 第二条"
m_oo = _parse_response(raw_open_only)
check("缺闭合标签→仍能解析出消息", len(m_oo) == 2)

# 完全不是 [MSG] 的整段文字 → 不得把思考原文当消息发出
raw_junk = "用户说了句你好，我想想怎么回。\n还是直接说在吧。"
m_j = _parse_response(raw_junk)
check("无 [MSG] 行→降级话术（不发思考原文）",
      len(m_j) == 1 and m_j[0]["content"] == DEGRADED_TEXT)

# 上限仍生效：正文超过 6 条 → 截断到 6（且保留靠前的正文）
raw_long = "\n".join(f"[MSG] 第{i}条" for i in range(1, 13))
m_l = _parse_response(raw_long)
check("超上限→截断到 6 条", len(m_l) == 6)
check("截断保留靠前正文", m_l[0]["content"] == "第1条" and m_l[-1]["content"] == "第6条")

# 兼容 <thinking> 变体
raw_th2 = "<thinking>\n[MSG] 草稿\n</thinking>\n[MSG] 正文"
m_th2 = _parse_response(raw_th2)
check("<thinking> 变体同样剥离", len(m_th2) == 1 and m_th2[0]["content"] == "正文")

# ══════════════════════════════════════════════════
# 带图失败 → 诚实降级（2026-08-29：不再假装看见/让用户填描述）
# ══════════════════════════════════════════════════
print("\n=== 带图失败降级 ===")

from unittest.mock import patch  # noqa: E402  （暂无使用，保留原有结构）

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
from modules.polisher import get_counters
c = get_counters()
check("含 polish_count", "polish_count" in c)
check("含 llm_errors", "llm_errors" in c)

print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
sys.exit(1 if FAIL else 0)
