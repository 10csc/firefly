# -*- coding: utf-8 -*-
"""当前包持久化（F-6.3）回归守卫 —— 阶段 1 · 任务 1.9

原状：localStorage 只存 ui-scale/theme/particles，当前包从不落盘，`/modes` 的 `default`
字段前端也没读 → 切到 haruno 后一刷新就弹回 story（用户以为角色卡丢了）。

本文件守的是**结构不变量**（本地版没有浏览器可用，自动化套件跑不了真实页面）：
1. 持久化键与恢复顺序：last_mode → /modes.default → "story"，逐候选校验合法性；
2. **切包只有一个入口**：所有切换点都经 _applyMode/_switchMode，不许出现裸赋值
   （裸赋值会让持久化时灵时不灵——这正是最容易回归的地方）；
3. views.js / bundle.js 三副本一致由 tools/sync_frontends.py --check 覆盖，这里只守源码。

真实浏览器的端到端验证见 tests/manual_ui_mode_persist.py（手工跑，不入自动化套件）。
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "app" / "static" / "js" / "views.js"
PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


src = VIEWS.read_text(encoding="utf-8")

print("=== A. 持久化键与读写 ===")
check("A1 有 firefly_last_mode 键", "firefly_last_mode" in src)
check("A2 写入用 localStorage.setItem", re.search(r'localStorage\.setItem\(\s*_MODE_KEY', src) is not None)
check("A3 读取用 localStorage.getItem", re.search(r'localStorage\.getItem\(\s*_MODE_KEY', src) is not None)
check("A4 localStorage 访问有 try 兜底（隐私模式/禁用时不炸站）",
      src.count("localStorage.setItem(_MODE_KEY") <= src.count("try {"))

print("=== B. 恢复顺序：last_mode → /modes.default → story ===")
m = re.search(r'\[(_savedMode\(\)[^\]]*)\]\.find', src)
check("B1 候选链存在且顺序为 已存 → 服务端默认 → story",
      bool(m) and m.group(1).replace(" ", "") == '_savedMode(),serverDefault,"story"')
check("B2 /modes 的 default 字段被读取", "data.default" in src)
check("B3 候选逐个校验合法性（无效 id 会被跳过）",
      re.search(r'\.find\(id => id && PRESET_MODES\.some\(m => m\.id === id\)\)', src) is not None)
check("B4 全不合法时回退注册表首包（再兜底 story）",
      "PRESET_MODES[0] && PRESET_MODES[0].id) || \"story\"" in src)

print("=== C. 切包唯一入口（防裸赋值回归） ===")
assigns = [(i + 1, ln.strip()) for i, ln in enumerate(src.split("\n"))
           if re.match(r"^\s*(?:export\s+)?(?:let|const|var)\s+CURRENT_MODE\s*=[^=]", ln)
           or re.match(r"^\s*CURRENT_MODE\s*=[^=]", ln)]
check("C1 CURRENT_MODE 赋值点恰好 3 处（声明 + _applyMode + _switchMode 兜底）",
      len(assigns) == 3)
check("C2 声明处保留初始 story", assigns and assigns[0][1].startswith("export let CURRENT_MODE"))
bodies = [ln for _, ln in assigns[1:]]
check("C3 另两处分别是 _applyMode 的赋值与 _switchMode 的兜底",
      bodies == ['CURRENT_MODE = id;', 'CURRENT_MODE = "story";'])
for fn, need in (("enterMode", "_switchMode"), ("enterCarouselAction", "_switchMode"),
                 ("managePack", "_applyMode"), ("openPackView", "_applyMode"),
                 ("setCurrentMode", "_applyMode")):
    m = re.search(rf"function {fn}\([^)]*\)\s*\{{(.*?)\n\}}", src, re.S)
    body = m.group(1) if m else ""
    check(f"C4 {fn} 经 {need} 切包（不裸赋值）", need in body and "CURRENT_MODE =" not in body)

print("=== D. 首页轮播初始位置跟随当前包（否则点封面会把记忆改掉） ===")
check("D1 renderModeCards 按 CURRENT_MODE 找初始位",
      re.search(r'findIndex\(m => m\.id === CURRENT_MODE\)', src) is not None)
check("D2 找不到时回退第 0 位（不出现负数索引）", "goCarousel(i > 0 ? i : 0)" in src)

print("=== E. bundle 与源码同源（三副本漂移由门禁兜底，这里只确认存在） ===")
bundle = (ROOT / "app" / "static" / "js" / "bundle.js").read_text(encoding="utf-8")
check("E1 bundle.js 含同一持久化逻辑", "firefly_last_mode" in bundle)
check("E2 bundle.js 含恢复顺序链", '_savedMode(), serverDefault, "story"' in bundle
      or '_savedMode(),serverDefault,"story"' in bundle.replace(" ", ""))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
