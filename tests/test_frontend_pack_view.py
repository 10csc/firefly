# -*- coding: utf-8 -*-
"""包详情页代际保护守卫（F-3）—— 阶段 1 · 任务 1.12

缺陷：`panels.js` 的 `loadPackView` 既不捕获模式代际、写回又全部用**实时的** `CURRENT_MODE`。
A→B 快速切换后，仍开着的 A 编辑页点「保存」会把 A 的文案写进 B；A 的响应晚到还会把
A 的数据渲染进 B 的页面（`chat_history.loadHistory` 早就有正确范式，包详情页漏了）。

本文件守三条结构不变量：
1. `loadPackView` 进入时捕获 `mode` 与 `gen`，await 之后校验 gen；
2. 包详情页的所有写回（保存/恢复默认/换资产/加表情包/主动配置）用**捕获的 mode**
   （`mode` 或 `_packViewMode`），不读实时 `CURRENT_MODE`；
3. 表情包子加载同样按捕获的 mode + gen 判定。

真实浏览器端到端验证见 tests/manual_ui_pack_gen.py（手工跑；已实测 A 页保存写回 A）。
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
# 2026-09-13（阶段 2.5）：包详情页从 js/panels.js 拆到 js/panels/packs.js，守卫跟着实现走
PANELS = ROOT / "app" / "static" / "js" / "panels" / "packs.js"
BUNDLE = ROOT / "app" / "static" / "js" / "bundle.js"
PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


src = PANELS.read_text(encoding="utf-8")


def _body(name):
    m = re.search(rf"(?:async\s+)?function {name}\([^)]*\)\s*\{{(.*?)\n\}}", src, re.S)
    return m.group(1) if m else ""


print("=== A. loadPackView 捕获 mode + gen 并在 await 后校验 ===")
lpv = _body("loadPackView")
check("A1 函数体取到", bool(lpv))
check("A2 进入时捕获 CURRENT_MODE", "const mode = CURRENT_MODE;" in lpv)
check("A3 进入时捕获模式代际 _modeGen", "const gen = _modeGen;" in lpv)
check("A4 await 之后校验代际（切走了就丢弃本次结果）", "if (gen !== _modeGen) return;" in lpv)
check("A5 捕获的量存到 _packViewMode（供本页其它写回用）", "_packViewMode = mode;" in lpv)
check("A6 函数体内 CURRENT_MODE 只出现 1 次（就是那行捕获）",
      lpv.count("CURRENT_MODE") == 1)
check("A7 fetch 用的是捕获的 mode", "`/pack-files?mode=${encodeURIComponent(mode)}`" in lpv)

print("=== B. 写回一律用捕获的包（不再是实时 CURRENT_MODE） ===")
check("B1 保存人设文案用 mode",
      "JSON.stringify({mode, filename: f.name, content})" in lpv)
check("B2 恢复默认用 mode",
      "JSON.stringify({mode, filename: f.name})" in lpv)
check("B3 恢复资产默认用 mode",
      "JSON.stringify({mode, slot})" in lpv)
check("B4 表情包区按捕获的 mode 加载", "_loadPackStickers(mode);" in lpv)
sts = _body("_loadPackStickers")
check("B5 表情包加载以 _packViewMode 为默认参数",
      "async function _loadPackStickers(mode = _packViewMode || CURRENT_MODE)" in src)
check("B6 表情包过滤/标记用捕获的 mode（不再读 CURRENT_MODE）",
      "s.pack === mode" in sts and "s.pack === CURRENT_MODE" not in sts
      and "s.pack === CURRENT_MODE" not in src)
check("B7 表情包子加载也有代际校验", "if (gen !== _modeGen) return;" in sts)
check("B8 加表情包归属本页展示的包", 'fd.append("mode", _packViewMode || CURRENT_MODE);   // 归属"本页展示的包"' in src)
check("B9 换封面/头像归属本页展示的包",
      'fd.append("mode", _packViewMode || CURRENT_MODE);   // 本页展示的包' in src)
pro = _body("_proScheduleSave")
check("B10 主动消息配置写回本页展示的包",
      "mode: _packViewMode || CURRENT_MODE," in pro)
check("B11 模板字面量里也不再残留 {mode: CURRENT_MODE}",
      "JSON.stringify({mode: CURRENT_MODE, slot})" not in src
      and "JSON.stringify({mode: CURRENT_MODE, filename" not in src)

print("=== C. 声明与说明在位（后续维护者能看懂为什么） ===")
check("C1 _packViewMode/_packViewGen 已声明",
      "let _packViewMode" in src and "let _packViewGen" in src)
check("C2 注释指明 F-3 与「不用实时 CURRENT_MODE」的理由",
      "不用实时 CURRENT_MODE" in src and "_packViewMode" in src)

print("=== D. bundle 三副本同源（漂移由 sync_frontends --check 兜底） ===")
bundle = BUNDLE.read_text(encoding="utf-8")
check("D1 bundle 含 _packViewMode", "_packViewMode" in bundle)
check("D2 bundle 含代际校验", "gen !== _modeGen" in bundle)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
