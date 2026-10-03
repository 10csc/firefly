# -*- coding: utf-8 -*-
"""PC IA 的**静态闸门**（把两条红线变成断言，而不是只写在文档里）。

红线（IA 方案 §一/§六）：
  A. **底部状态栏只有信息、没有命令** —— 后人顺手给它加 onclick/按钮就红。
  B. **卡内命令 ∩ 侧边栏命令 = ∅** —— 侧边栏只放"卡外"功能；同一命令出现两处就红
     （微软 UX 指南：Present each command on only one tab；2026-09-19 第一版正是栽在这里）。
  C. **手机端零影响**：`#pc-shell` 必须在 @media 之外 `display:none`，且 PC 规则只在
     `@media (min-width:1100px)` 里；手机上聊天页头部的 ☰ 必须保留。
  D. 菜单把手在 `#pc-shell` 内（⇒ 窄屏随外壳隐藏），且只调既有 openMenu/closeMenu。

用法：`python tests/test_pc_ia.py`（纯静态扫描，不需要服务器/浏览器）
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "pc.css").read_text(encoding="utf-8")
SHELL = (ROOT / "app" / "static" / "js" / "pc_shell.js").read_text(encoding="utf-8")

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def _sub(text, start, end):
    i = text.find(start)
    j = text.find(end, i + 1)
    return text[i:j] if i >= 0 and j > i else ""


def _calls(fragment):
    """片段里出现的内联命令调用名（onclick="a(); b()" → {a, b}）。"""
    return set(re.findall(r'onclick="\s*([A-Za-z_$][\w$]*)\s*\(', fragment))


print("=== A. 底部状态栏：只有信息，没有命令 ===")
bar = _sub(HTML, 'id="pc-statusbar"', "</footer>")
check("A1 #pc-statusbar 是空元素（无内联 onclick / 无按钮）",
      "onclick" not in bar and "<button" not in bar, bar[:80])
# 只扫 renderStatus 里拼 innerHTML 的那段（别把 renderContacts 里 createElement('button') 算进来）
_rs = _sub(SHELL, "function renderStatus()", "function refresh()")
check("A2 renderStatus 只拼 span/文本，不拼 button 与 onclick",
      "innerHTML" in _rs and "<button" not in _rs and "onclick" not in _rs, _rs.count("innerHTML") and "含命令")
check("A2b 联系人列的按钮是 createElement（不拼 HTML），且全文件没有 onclick 赋值",
      "createElement('button')" in SHELL and "onclick =" not in SHELL and ".onclick" not in SHELL)
# T2-1（增强）：状态栏是纯信息，连"归属标记"都不许有 —— 后人顺手加 data-pc 会让人以为它能点。
check("A3 #pc-statusbar 内不得出现 data-pc（状态栏只有信息，没有命令/归属标记）",
      "data-pc" not in bar, bar[:80])

print("=== B. 卡内命令 ∩ 侧边栏命令 = ∅ ===")
shell_block = _sub(HTML, 'id="pc-shell"', "<!-- ═══ 首页")
nav_block = _sub(shell_block, 'id="pc-nav"', "</nav>")
menu_block = _sub(HTML, 'id="menu-drawer"', 'id="assist-view"')      # 抽屉到下一块视图之间
sidebar_cmds = _calls(nav_block) | _calls(_sub(shell_block, 'id="pc-contacts"', "</aside>"))
in_card = _calls(menu_block) | _calls(_sub(HTML, 'id="app"', 'id="menu-overlay"'))
both = sorted(sidebar_cmds & in_card)
# 重复命令必须在 PC 上被显式隐藏：`data-pc-hide="…"` 的按钮，其命令要在 pc.css 里被"PC 隐藏"覆盖
hidden_cmds = set()
for m in re.finditer(r'<button[^>]*data-pc-hide="([^"]+)"[^>]*onclick="\s*([A-Za-z_$][\w$]*)\s*\(', HTML):
    tag, cmd = m.group(1), m.group(2)
    if re.search(r'\[data-pc-hide="' + re.escape(tag) + r'"\]\s*\{\s*display:\s*none', CSS):
        hidden_cmds.add(cmd)
check(f"B1 侧边栏命令 {sorted(sidebar_cmds)}", len(sidebar_cmds) >= 5)
check(f"B2 卡内命令 {sorted(in_card)}", len(in_card) >= 3)
check(f"B3 交集里的每条命令都在 PC 上被隐藏（交集={both}，已隐藏={sorted(hidden_cmds)}）",
      all(c in hidden_cmds for c in both), f"未覆盖：{[c for c in both if c not in hidden_cmds]}")
check("B4 侧边栏无 'account' 重复项（账号就是首页的一段，不再单列按钮）",
      'data-pc="account"' not in HTML)

# ── T2 增强（只加不减）────────────────────────────────────────────────────
# T2-2：把「卡内命令 ∩ 侧边栏命令 = ∅」做成**显式残余空集**断言（B3 是"交集全在隐藏清单里"，
#       这里是"排除隐藏项后残余为空"，两条互为表里，任一条被后人削弱都会被另一条拦住）。
residual = sorted(set(both) - hidden_cmds)
check(f"B5 卡内命令 ∩ 侧边栏命令（排除 PC 已隐藏项后）为空（残余={residual}）",
      not residual, f"残余：{residual}")
# data-pc 归属标记也不许两侧同名（它不是命令，但同是"重复路径"的信号）。
sidebar_data = set(re.findall(r'data-pc="([^"]+)"', nav_block))
menu_data = set(re.findall(r'data-pc="([^"]+)"', menu_block))
check(f"B6 data-pc 归属标记无重叠（侧边栏={sorted(sidebar_data)} 菜单={sorted(menu_data)}）",
      not (sidebar_data & menu_data), f"重叠：{sorted(sidebar_data & menu_data)}")
# IA §二 的卡外入口必须齐全：首页/卡库/广场/语音/公告/设置/反馈与诊断。
check(f"B7 侧边栏卡外入口齐全 {sorted(sidebar_data)}",
      {"home", "cards", "plaza", "voice", "notice", "settings", "feedback"} <= sidebar_data,
      f"缺：{sorted({'home','cards','plaza','voice','notice','settings','feedback'} - sidebar_data)}")
# T2-3：「+ 新建角色」必须带意图参数（直达新建表单），不能与「卡库」同义。
newbtn = re.search(r'id="pc-contact-new"[^>]*onclick="([^"]*)"', HTML)
check("B8 「+ 新建角色」带意图参数（同时调 openCardsView 与 togglePackCreate(true)）",
      bool(newbtn) and "openCardsView" in newbtn.group(1)
      and "togglePackCreate(true)" in newbtn.group(1),
      (newbtn.group(1) if newbtn else "未找到 #pc-contact-new"))

print("=== C. 手机端零影响（默认 display:none + PC 规则只在 ≥1100px）===")


def _split_media(text):
    """返回 (媒体块之外的裸声明, 全部媒体块内容)。花括号配平，避免"我的块恰好排在媒体块之后"这种假绿。"""
    out, media, depth, buf, in_media = [], [], 0, "", False
    i = 0
    while i < len(text):
        if text.startswith("@media", i):
            in_media = True
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if in_media and depth == 0:
                media.append(buf)
                buf, in_media = "", False
                i += 1
                continue
        (buf if in_media else out).append(ch) if False else None
        if in_media:
            buf += ch
        else:
            out.append(ch)
        i += 1
    return "".join(out), "".join(media)


_bare, _media = _split_media(CSS)
check("C1 #pc-shell 在**媒体块之外**声明 display:none（窄屏连盒子都不产生）",
      re.search(r"#pc-shell\s*\{[^}]*display:\s*none", _bare) is not None)
check("C2 #pc-shell 的 flex 布局只在媒体块里",
      re.search(r"#pc-shell\s*\{[^}]*display:\s*flex", _media) is not None)
check("C3 手机上聊天页头部的 ☰ 保留（只在 PC 档隐藏）",
      re.search(r"#menu-btn\s*\{\s*display:\s*none\s*!important", _media) is not None
      and '<button id="menu-btn"' in HTML)

print("=== D. 菜单把手（右缘、随外壳隐藏、只调既有入口）===")
check("D1 把手在 #pc-shell 内（⇒ 窄屏一起隐藏）", 'id="pc-menu-handle"' in shell_block)
check("D2 把手 onclick 调 pcMenuToggle()", 'onclick="pcMenuToggle()"' in shell_block)
check("D3 pcMenuToggle 只调既有 openMenu/closeMenu（不新增业务逻辑）",
      "window.openMenu" in SHELL and "window.closeMenu" in SHELL
      and "pcMenuToggle" in SHELL)
check("D4 展开时用 pc-menu-open 让聊天区让位（不是遮住输入区）",
      "pc-menu-open" in CSS and "pc-menu-open" in SHELL)

print("=== E. 卡详情抽屉 + 聊天页右缘把手（T4/T5 红线，2026-10-02 新增）===")
# T4：修复"只能收不能展"的真实 bug —— 优先既有 window.openMenu，缺失则回退点击既有 #menu-btn。
check("E1 T4 把手展开走既有入口：优先 window.openMenu，缺失回退点击 #menu-btn",
      "window.openMenu" in SHELL and "menu-btn" in SHELL and "click()" in SHELL)
# T5：卡详情入口在**菜单页**（卡内命令），侧边栏**不得**出现同名入口。
check("E2 T5 「卡详情」入口只在菜单页（菜单含 pcOpenPackDrawer；侧边栏不含）",
      "pcOpenPackDrawer" in menu_block and "pcOpenPackDrawer" not in nav_block)
check("E3 T5 pc_shell 提供 pcOpenPackDrawer，且窄屏不执行（desktop() 守卫）",
      "pcOpenPackDrawer" in SHELL and "function desktop()" in SHELL)
# T5：PC 档 #pack-view 变成受限宽抽屉（宽度变量 + 规则都在 @media 内）。
check("E4 T5 PC 档 #pack-view 用 --pc-drawer-w 做受限宽抽屉（规则在 @media 内）",
      "--pc-drawer-w" in _media
      and re.search(r"#pack-view\s*\{[^}]*width:\s*var\(--pc-drawer-w\)", _media) is not None)
check("E5 T5 抽屉宽度受限（max-width 上限 ⇒ 必然 < 视口宽，左侧留出聊天列）",
      re.search(r"#pack-view\s*\{[^}]*max-width:\s*60vw", _media) is not None)
check("E6 T5 抽屉展开时聊天列让位（pc-drawer-open 同时出现在 pc.css 与 pc_shell.js）",
      "pc-drawer-open" in CSS and "pc-drawer-open" in SHELL)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
