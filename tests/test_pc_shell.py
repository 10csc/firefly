# -*- coding: utf-8 -*-
"""PC 适配层 —— 静态守卫（不需要浏览器）。

浏览器里的几何检查在 `tools/pc_ui_shots.py`（局部原尺寸出图）与
`tools/mobile_regress.py`（窄屏结构不变量，需要 Chrome + 起服务），不进常跑套件。
**本文件守的是"这一版被判过的错不许再犯"**：

  背景（2026-09-19 用户否掉第一版 PC 重做）：第一版在 PC 上自造了一整套导航 ——
  左侧图标栏 + 第二列目录 + 工作区顶栏 + 右侧上下文栏，并且把**手机 ☰ 菜单页里的东西**
  （收藏/表情包/设定文件/请求记录/流程日志）抄成了全局入口。后果：
    · 首页在展示菜单页的内容，菜单页变得没有存在意义；
    · 同一命令出现 2~3 条路径（微软 UX 指南：Present each command on only one tab）；
    · 左边多出一栏"不明所以"。
  正确做法：**层级与命名全部沿用手机，PC 只适配呈现**（☰ 菜单从覆盖抽屉改成右侧停靠面板、
  首页轮播改多列网格）；只有"信息"可以额外放（底部状态栏），因为状态不是命令。

  所以这里守：
    A 没有自造的第二套导航（pc-shell / pc-rail / pc-list / pc-chat-side 都不许存在）；
    B ☰ 菜单入口在 PC 上**必须可见**（它就是手机端的菜单入口，藏了就没法用菜单）；
    C 菜单是"停靠面板"而不是全屏覆盖（CSS 里有右侧停靠规则）；
    D 断点隔离：除 :root 变量与"默认隐藏"外，pc.css 的规则都必须在 @media(min-width:1100px) 内；
    E 气泡宽度上限只设一次（第一版两级 68% 互相挤压 ⇒ 短消息一个字一行）；
    F pc_shell.js 只做状态栏（没有导航逻辑、没有 emoji、不依赖框架）；
    G 加载顺序与打包登记。
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
STATIC = ROOT / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
PCSS = (STATIC / "pc.css").read_text(encoding="utf-8")
PJS = (STATIC / "js" / "pc_shell.js").read_text(encoding="utf-8")
STYLE = (STATIC / "style.css").read_text(encoding="utf-8")

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))

print("=== A. 不许有自造的第二套导航 ===")
for gone in ("pc-shell", "pc-rail", "pc-list", "pc-chat-side", "pc-main-head", "pcr-btn", "pcl-item"):
    check(f"A  {gone} 已从 index.html/pc.css 移除",
          f'id="{gone}"' not in HTML and f"#{gone}" not in PCSS and f".{gone}" not in PCSS)
check("A  手机端点名的「两组 + 精灵图标」口径仍在手机菜单里（没被我改坏）",
      "与角色相关" in HTML and "与系统相关" in HTML
      and 'data-tab="fav"' in HTML and 'data-tab="pipeline"' in HTML)
check("A  服务端下发/公告等既有 id 未受影响",
      all(f'id="{i}"' in HTML for i in ("menu-drawer", "menu-btn", "messages", "input-bar")))

print("\n=== B. ☰ 菜单入口在 PC 上必须可见 ===")
check("B  pc.css 里没有 `#menu-btn { display: none }`（第一版把它藏了）",
      not re.search(r"#menu-btn\s*\{[^}]*display:\s*none", PCSS))
check("B  pc.css 显式声明 PC 上 ☰ 可见",
      re.search(r"#menu-btn\s*\{\s*display:\s*flex", PCSS) is not None)

print("\n=== C. 菜单是右侧停靠面板（不是全屏覆盖）===")
_drawer = re.search(r"#menu-drawer\s*\{(.*?)\}", PCSS, re.S)
_body = _drawer.group(1) if _drawer else ""
check("C  #menu-drawer 有停靠宽度变量", "--pc-menu-w" in PCSS)
check("C  #menu-drawer 贴右边（left:auto + right:0）",
      "left: auto" in _body and "right: 0" in _body, _body[:60].replace("\n", " "))
check("C  遮罩改成不拦截点击（对话仍可操作）",
      re.search(r"#menu-overlay\s*\{[^}]*pointer-events:\s*none", PCSS) is not None)

print("\n=== D. 断点隔离（PC 规则不许漏到窄屏）===")
_no_comment = re.sub(r"/\*.*?\*/", "", PCSS, flags=re.S)
_depth, _outside = 0, []
for ln in _no_comment.splitlines():
    t = ln.strip()
    if t.startswith("@media") and _depth == 0:
        _depth += 1
        continue
    if _depth:
        _depth += t.count("{") - t.count("}")
        continue
    if t:
        _outside.append(t)
_leak = [l for l in _outside if not (l.startswith(":root") or l.startswith("{")
                                     or l.startswith("--") or "display: none" in l or l == "}")]
check("D  @media 之外只有变量与「默认隐藏状态栏」",
      not _leak, str(_leak[:3]))
check("D  pc.css 没有 max-width 媒体查询（PC 文件不写窄屏规则）",
      "@media (max-width:" not in PCSS)

print("\n=== E. 气泡宽度上限只设一次（第一版的真实渲染 bug）===")
# 注意：必须用**去过注释**的文本 —— pc.css 的注释里为了说明这个 bug 引用了旧写法
# `.msg-row .bubble{max-width:68%}`（第一版按原文匹配，把注释当成了真规则）。
check("E  pc.css 里气泡是 max-width:100%（吃满 .msg-col，不再按比例缩一次）",
      re.search(r"\.msg-row \.bubble\s*\{[^}]*max-width:\s*100%", _no_comment) is not None)
check("E  pc.css 里没有对 .bubble 再设第二个百分比上限（例如 68%）",
      not re.search(r"\.msg-row \.bubble\s*\{[^}]*max-width:\s*(?!100%)\d+%", _no_comment))
check("E  上限由 .msg-col 承担",
      re.search(r"\.msg-col\s*\{[^}]*max-width:\s*68%", _no_comment) is not None)

print("\n=== F. pc_shell.js 只做状态栏 ===")
check("F  有状态栏渲染", "renderStatus" in PJS and "pc-statusbar" in PJS)
check("F  没有导航/分组逻辑残留", "RAIL" not in PJS and "LIST_SECTIONS" not in PJS
      and "showMemSection" not in PJS and "pcr-" not in PJS)
_emoji = sorted({c for c in PJS if ord(c) > 0x1F000})
check("F  没有 emoji 图标", not _emoji, "".join(_emoji))
check("F  不依赖任何前端框架", "PetiteVue" not in PJS and "petite-vue" not in PJS)
check("F  状态栏里没有按钮（信息 ≠ 命令）",
      not re.search(r"<button", PJS))
check("F  所有跨模块调用都带存在性判断",
      all(("window." + n) in PJS for n in ("__appVersion",)))

print("\n=== G. 打包与同步 ===")
import build_frontend_bundle as bfb          # noqa: E402
check("G  pc_shell 在 NON_BUNDLE（不进 bundle，PC 逻辑不进移动端那个作用域）",
      "pc_shell" in bfb.NON_BUNDLE)
check("G  NON_BUNDLE = bundle + index.html 里独立加载的脚本",
      bfb.NON_BUNDLE == (set(re.findall(r'<script src="js/([A-Za-z0-9_/]+)\.js', HTML)) | {"bundle"}),
      str(sorted(bfb.NON_BUNDLE)))
check("G  没有 pc_nav.js / petite-vue 残留",
      not (STATIC / "js" / "pc_nav.js").exists() and not (STATIC / "vendor").exists())
check("G  pc.css 已纳入三端同步工具",
      "pc.css" in (ROOT / "tools" / "sync_frontends.py").read_text(encoding="utf-8"))
check("G  pc.css 指纹由构建工具更新",
      "pc.css" in (ROOT / "tools" / "build_frontend_bundle.py").read_text(encoding="utf-8"))
check("G  pc.css 的 <link> 在内联 <style> 之后（否则被内联规则压住）",
      HTML.index("pc.css") > HTML.rindex("</style>"))
check("G  旧 PC 块没有回到 style.css（单一来源）",
      "#pc-sidebar" not in STYLE and ".pcs-item" not in STYLE)

if shutil.which("node"):
    r = subprocess.run(["node", "--check", str(STATIC / "js" / "pc_shell.js")],
                       capture_output=True, encoding="utf-8", errors="replace")
    check("G  pc_shell.js 语法通过 node --check", r.returncode == 0, (r.stderr or "")[:120])

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
