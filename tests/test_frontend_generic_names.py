# -*- coding: utf-8 -*-
"""角色卡化铁律：**前端框架里不许出现角色名字面量**。

项目已角色预设化/角色卡化：角色名、用户称呼、头像、签名全部来自当前角色卡
（`currentPreset()` / 预设包），框架代码只提供**通用**渲染。写死「流萤」「开拓者」
会导致自建卡显示错误称呼（历史上真发生过：F-6.x 调试面板曾硬编码「开拓者」）。

本测试把"已知存量欠债"钉成**显式白名单**：
- 新增任何角色名字面量 → 立刻 FAIL（这是本测试的主要价值）；
- 修掉一条就把白名单那一行删掉（只减不增）。

为什么不是全自动修复：这些散落在收藏页/纠错页/通知标题等处，属于另一批改动，
需要各自验证；先防止继续恶化。
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js"

# 生成物 / 独立脚本不算框架源码
SKIP = {"bundle.js", "pc_nav.js"}

# 角色名/称呼字面量（角色卡化后不应出现在框架里）
NAMES = ("流萤", "开拓者")

# ── 已知存量欠债（文件名 → 出现次数）。修掉一条就删一行，禁止新增。 ──
KNOWN_DEBT = {
                             #   不是角色名（退出的是 App，不是角色），保留
    "panels.js": 2,          # 内置用户头像**资产文件名**（开拓者_穹.png / 开拓者_星.png）——
                             #   随包发布的默认形象资产，改名会断资产引用，保留
}
# 2026-09-18 清掉的（这批是本测试的由来）：
#   chat.js / fix.js / main.js / panels/data.js / panels/debug.js / proactive.js /
#   views.js 的离线回落 / chat_render.js 的引用卡片 who
# → 现在全部走 views.charName()/userName()（同一次提交里新增的统一出口）。

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def strip_comments(src: str) -> str:
    """粗略剥掉 // 与 /* */ 注释（够用：只看有没有字面量，不求精确 parse）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.split("\n"):
        # 不处理字符串里的 //（本仓库这几处都是行尾注释），保守起见只砍明显是注释的
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def name_literals(src: str) -> int:
    """统计角色名在**代码里**的出现次数（已剥注释）。

    不能只匹配 `"流萤"` 这种精确字面量——真正常见的形态是嵌在长句里，
    例如 `showToast("只能给流萤的消息转语音")`。所以剥掉注释后按裸出现次数计。
    """
    src = strip_comments(src)
    return sum(src.count(nm) for nm in NAMES)


found = {}
for fp in sorted(JS.rglob("*.js")):
    if fp.name in SKIP:
        continue
    rel = fp.relative_to(JS).as_posix()
    c = name_literals(fp.read_text(encoding="utf-8"))
    if c:
        found[rel] = c

print("=== A. 前端框架里的角色名字面量 ===")
for rel, c in sorted(found.items()):
    known = KNOWN_DEBT.get(rel)
    tag = f"（已知欠债 {known}）" if known else "（★ 新增，禁止）"
    print(f"    {rel}: {c} 处 {tag}")

new_files = sorted(set(found) - set(KNOWN_DEBT))
check("A1 没有**新增**带角色名字面量的文件", not new_files, str(new_files))

grown = sorted(r for r in found if KNOWN_DEBT.get(r) is not None and found[r] > KNOWN_DEBT[r])
check("A2 已知欠债文件里的字面量没有变多", not grown, str(grown))

fixed = sorted(r for r in KNOWN_DEBT if found.get(r, 0) < KNOWN_DEBT[r])
check("A3 白名单只减不增：修掉的条目要从 KNOWN_DEBT 删掉",
      not fixed, f"已修好但仍挂在白名单: {fixed}")

print("=== B. 统一出口本身干净（角色名只从当前角色卡取）===")
cr = (JS / "chat_render.js").read_text(encoding="utf-8")
vw = (JS / "views.js").read_text(encoding="utf-8")
check("B1 chat_render.js 无角色名字面量", name_literals(cr) == 0, f"{name_literals(cr)} 处")
check("B2 views.js 无角色名字面量（离线兜底已去掉 char_name）",
      name_literals(vw) == 0, f"{name_literals(vw)} 处")
check("B3 views 暴露 charName()/userName() 作为唯一出口",
      "export function charName()" in vw and "export function userName()" in vw)
check("B4 charName()/userName() 取不到时返回空串（不回落角色名字面量）",
      vw.count('return ((p && (p.char_name || p.name)) || "").toString().trim();') == 1
      and vw.count('return ((p && p.user_name) || "").toString().trim();') == 1)
check("B5 chat_render 的名字行与引用卡片都走该出口",
      "_whoName(who)" in cr and "charName()" in cr and "userName()" in cr)
check("B6 引用卡片的 who 不再是写死的角色名（原来是「流萤」）",
      'q.who === "user") ? "我" : charName()' in cr)

print("=== C. HTML 里的**动态**文案走 data-tpl（静态文档不算）===")
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
for desc, tpl in (("菜单「休息」按钮", 'data-tpl="让{c}休息"'),
                  ("手账标签", 'data-tpl="{c}的手账"'),
                  ("手账说明", 'data-tpl="{c}视角的重要对话'),
                  ("用户记忆说明", 'data-tpl="由「让{c}休息」'),
                  ("用户形象弹窗标题", 'data-tpl="选择{u}形象"')):
    check(f"C  {desc} 用模板占位", tpl in html, tpl)
check("C6 存档占位符也走模板（data-tpl-ph）", 'data-tpl-ph="还没有存档' in html)
check("C7 休息/起床遮罩默认文案已去角色名",
      ">正在整理记忆…<" in html and ">正在起床，请稍候…<" in html)
check("C8 views 有 applyTextTemplates 且在换卡时调用",
      "export function applyTextTemplates()" in vw and "applyTextTemplates();" in vw)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
