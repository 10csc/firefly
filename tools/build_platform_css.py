# -*- coding: utf-8 -*-
"""生成/校验角色卡平台的样式：`app/static/style.css` → `server/frontend/platform.css`

**为什么需要它**：平台页（`server/frontend/platform.html`）只能引用平台自己的资源
（不许引用 App 的 `style.css`/`pc.css` —— 服务端已把它们挡成 404）。而广场/制卡/治理台的
样式全在 App 的 `style.css` 里，必须**按选择器抽**出来。

**为什么抽错会很难看**（2026-10-01 真实教训）：第一版抽漏了 `.pfc-*`（制卡两列布局，
写法是 `.pfc-` 而不是 `.pf-`）⇒ `.pfc-cols{position:absolute;inset:0}` 丢失 ⇒ 制卡表单
被撑到 **4696px** 高、审核弹窗落到表单后面**点不到**。假 DOM 测试看不见，只有真浏览器 e2e 抓到。
所以这里把"哪些选择器要抽"写成**显式常量**，并用 `--check` 让漂移变成门禁红灯。

用法：
    python tools/build_platform_css.py            # 重新生成 server/frontend/platform.css
    python tools/build_platform_css.py --check     # 只校验（漂移退出 1）

抽取口径：
  · 保留 `:root` / `body.theme-light`（主题 token）、三条 `*` reset、`input, textarea`；
  · 保留选择器里含 `KEEP_SELECTOR_TOKENS` 任一项的规则（见下方常量，含两处易漏项）；
  · `@media` 递归（只留命中的内层规则，空块丢弃）；`@keyframes` 只留被保留文本引用到的；
  · 跳过 `@font-face`（服务器没有 StarRailFont 字体文件，平台用系统字体）；
  · 文件末尾追加 `PLATFORM_SHELL`：平台自己的外壳样式（头部/导航/闸门/桌面三列）。
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "static" / "style.css"
DST = ROOT / "server" / "frontend" / "platform.css"

# ★ 抽取白名单。加平台新面板时，把它的选择器前缀加到这里（并跑 --check 看差多少）。
KEEP_SELECTOR_TOKENS = (
    "#plaza-", "#pf-", "#pz-",            # 广场/制卡/治理台的 id 与容器
    ".pz-", ".pf-", ".pfc-", ".pza-", ".pzr-", ".pff-", ".pfi-",   # 各层类名前缀
    # 两处**不在 plaza 前缀下**但平台依赖的：
    "ui-select",     # ui_select.js 造的增强下拉（.ui-select-btn/pop/opt/native）
    "#app-toast",    # util.js 的 showToast（plaza 里 30 处用它做反馈）
)
# `.cv-*` 里只有广场头部用到这三个（`.cv-card/.cv-list` 是 App「角色卡管理页」的）
KEEP_CV = (".cv-head", ".cv-back", ".cv-title")
KEEP_EXACT = {":root", "body.theme-light", "*", "*:focus", "*:focus-visible", "input, textarea"}

HEADER = """/* ═══════════════════════════════════════════
   角色卡平台样式（server/frontend/platform.css）

   ⚠ 本文件由 `tools/build_platform_css.py` **生成**（从 app/static/style.css 按选择器抽），
     别手改：要加规则请改那个脚本的 KEEP_SELECTOR_TOKENS 或 PLATFORM_SHELL，然后重新生成。
   来源口径：只取平台用到的（plaza 前缀 + 两个基础模块自己的样式 + 主题 token + 基础 reset）。
   ⚠ 不引用 App 的 style.css / pc.css / js/bundle.js —— 平台必须自包含。
   ⚠ 不含 @font-face：服务器没有 StarRailFont 字体文件（平台用系统字体）。
   ═══════════════════════════════════════════ */

"""

PLATFORM_SHELL = """

/* ═══════════════════════════════════════════
   平台外壳（角色卡平台专有；不在 App 里）
   服务器只做后台：这个页面只有 广场/制卡/我的卡/治理台，没有任何 App 功能入口。
   ═══════════════════════════════════════════ */
:root { --pf-head-h: 52px; }

html, body { margin: 0; padding: 0; }
body.platform {
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
    color: var(--fg-main);
    background: var(--page-bg);
    min-height: 100vh;
    overflow-x: hidden;
}
/* 头部 + 导航（固定；高于 #plaza-view 的 z-index 6） */
#pf-shell-head {
    position: fixed; top: 0; left: 0; right: 0; height: var(--pf-head-h); z-index: 20;
    display: flex; align-items: center; gap: 10px; padding: 0 clamp(12px, 3vw, 22px);
    background: var(--panel-veil); backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border-mid);
}
#pf-shell-brand { font-weight: 600; color: var(--fg-accent); white-space: nowrap; }
#pf-shell-brand::before { content: "✦ "; }
#pf-shell-nav { display: flex; gap: 6px; margin-left: auto; flex-wrap: wrap; }
.pf-tab {
    padding: 6px 12px; border-radius: 999px; cursor: pointer; font: inherit; font-size: 0.86em;
    border: 1px solid var(--border-mid); background: var(--ctrl-bg-3); color: var(--fg-main);
    transition: color .18s, border-color .18s, background .18s;
}
.pf-tab:hover { color: var(--fg-accent); border-color: var(--gold-line); background: var(--gold-glow); }
.pf-tab.on { color: var(--fg-accent); border-color: var(--gold-line); background: var(--gold-glow); }
#pf-shell-user { color: var(--fg-muted); font-size: 0.78em; white-space: nowrap; }
#pf-account { display: flex; align-items: center; gap: 8px; }
#pf-logout {
    padding: 5px 10px; border-radius: 999px; font: inherit; font-size: 0.8em; cursor: pointer;
    border: 1px solid var(--border-mid); background: var(--ctrl-bg-3); color: var(--fg-main);
}
#pf-logout:hover { color: var(--fg-accent); border-color: var(--gold-line); }

/* 广场面板：从"铺满整个视口"改为"铺满头部以下"（DOM 契约与 plaza 源码零改动，只覆盖定位） */
body.platform #plaza-view {
    top: var(--pf-head-h); bottom: 0; height: auto;
    background: var(--panel-veil);
}
/* 平台里没有"上一页"：隐藏广场自带的返回键（标题与管理台/制卡按钮保留） */
body.platform #plaza-list > .cv-head .cv-back { display: none; }

/* 广场之外的面板（制卡/治理台）也必须在头部以下 */
body.platform #plaza-forge, body.platform #plaza-admin { top: var(--pf-head-h); }

/* 未登录提示层（platform.js 在跳转前先显示，避免白屏闪烁） */
#pf-gate {
    position: fixed; inset: 0; z-index: 30; display: none;
    align-items: center; justify-content: center; text-align: center;
    background: var(--panel-solid); color: var(--fg-main); padding: 24px;
}
#pf-gate.show { display: flex; }
#pf-gate .pf-gate-card { max-width: 420px; }
#pf-gate h1 { font-size: 1.1em; margin-bottom: 10px; color: var(--fg-accent); }
#pf-gate p { color: var(--fg-muted); font-size: 0.88em; line-height: 1.7; }
#pf-gate a { color: var(--fg-accent); }

@media (max-width: 520px) {
    #pf-shell-brand { display: none; }
    #pf-shell-user { display: none; }
}

/* 桌面档：对齐 App 的 pc.css 口径（平台不引用 pc.css，所以把平台真正用到的两条重述一遍）。
   注：App 的 `.pz-grid` 三列在 pc.css 的 ≥1100px 媒体查询里；平台直接在下面写等价规则。 */
@media (min-width: 1100px) {
    body.platform .pz-grid { grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 16px 0 6px; }
    body.platform .pz-search-row { max-width: 620px; }
}
"""


def _strip_comments(t: str) -> str:
    return re.sub(r"/\*.*?\*/", "", t, flags=re.S)


def _blocks(t: str):
    out, depth, start = [], 0, 0
    for i, c in enumerate(t):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                out.append(t[start:i + 1].strip())
                start = i + 1
    tail = t[start:].strip()
    if tail:
        out.append(tail)
    return [b for b in out if b]


def _keep_sel(sel: str) -> bool:
    s = " ".join(sel.split())
    if s in KEEP_EXACT:
        return True
    if any(t in s for t in KEEP_SELECTOR_TOKENS):
        return True
    return any(cv in s for cv in KEEP_CV)


def _filter_media(inner: str) -> str:
    kept = []
    for b in _blocks(inner):
        if "{" not in b:
            continue
        sel = b.partition("{")[0]
        if _keep_sel(sel):
            kept.append(b)
    return "\n".join(kept)


def build() -> str:
    raw = _strip_comments(SRC.read_text(encoding="utf-8"))
    kept, keyframes = [], {}
    for b in _blocks(raw):
        head = b.partition("{")[0].strip()
        if head.startswith("@media"):
            inner = _filter_media(b.partition("{")[2].rstrip("}"))
            if inner:
                kept.append(f"{head}{{\n{inner}\n}}")
        elif head.startswith("@keyframes"):
            keyframes[head.split()[1]] = b
        elif head.startswith("@font-face"):
            continue
        elif _keep_sel(head):
            kept.append(b)
    text = "\n\n".join(kept)
    for name in [k for k in keyframes if re.search(r"\b" + re.escape(k) + r"\b", text)]:
        text += "\n\n" + keyframes[name]
    return HEADER + text.rstrip() + PLATFORM_SHELL


def main() -> int:
    content = build()
    if not SRC.is_file():
        print(f"X 找不到源样式: {SRC}")
        return 1
    if "--check" in sys.argv:
        if not DST.is_file():
            print(f"X {DST} 不存在（运行 python tools/build_platform_css.py 生成）")
            return 1
        if DST.read_text(encoding="utf-8") != content:
            print("X platform.css 与 app/static/style.css 的抽取结果漂移"
                  "（运行 python tools/build_platform_css.py 重新生成）")
            return 1
        print(f"platform.css 与抽取口径一致 ✓（{len(content.splitlines())} 行 / "
              f"{len(content.encode('utf-8'))} 字节）")
        return 0
    DST.write_text(content, encoding="utf-8")
    print(f"platform.css 已生成（{len(content.splitlines())} 行 / "
          f"{len(content.encode('utf-8'))} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
