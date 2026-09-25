# -*- coding: utf-8 -*-
"""公告面板重构（2026-09-24，0.9.1）的结构与语义守卫

用户报障原话：
  "公告方面没写好，没做好公告上下滚动和公告内容区分（0.9.0 版本的公告就还是塞在一个使用手册）"
  "按照标准的公告设计（上网搜）来写代码才对"

0.9.0 的事实：`#notice-server`（服务端公告）与 `#notice-static`（内置使用指南）
在同一个 `.notice-body` 里**上下堆叠**，中间只隔一条 `.notice-divider` 文案。
后果：用户分不清哪段是"新公告"、哪段是"常驻手册"；面板内容一长也滚不动。

本文件守卫这次重构的八个点，每一条都对应一个**曾经真实存在**的缺陷或设计约束：

A. `.notice-body` 必须有 `min-height: 0`
   —— flex 子项默认 min-height:auto，内容一长元素被撑高，`overflow-y:auto` **永不触发**，
      表现就是"公告上下滚不动"。这是原因，不是样式偏好，删了必然复发。

B. 两个 Tab 各自成容器（`#notice-pane-entries` / `#notice-pane-guide`），
   且**不再有** `.notice-divider` 把指南当公告的续页。
   —— 这是用户抱怨"公告塞在一个使用手册里"的直接解法。

C. 分类筛选按 **level**（info/warn/critical），不引入新字段
   —— 服务器模式下用户手机跑的是旧版 `notice.py`，其 `validate()` **挑字段组装**，
      未知字段会被丢弃 ⇒ 用新字段筛会在旧客户端上筛成空列表。用 level 才跨版本可用。

D. 徽标只计**未读公告**，不含使用指南
   —— 指南是常驻参考，算进去会让小圆点永远不灭（点掉还是亮的，等于失效）。

E. 三态齐备：有内容 / 空 / 拉取失败
   —— "拉取失败"必须说出来，否则空面板会被当成"这功能压根没做"。

F. 渲染仍**只用 DOM API + textContent**，不得出现 innerHTML/insertAdjacentHTML
   —— 公告通道因此"结构上不存在注入面"（见 docs/公告通道规范.md §四）。
      这是安全约束，不是代码风格。

G. 滚动位置按 Tab 分别记住（`_scroll` 对象 + `_bodyScroll/_setBodyScroll`）
   —— 两 Tab 共用同一个 `.notice-body`，不记位置就会互相踩。

H. 与 views.js 的接口不变：`window.noticeOnOpen` 仍是唯一入口
   —— views.js 先于 notice 出现在 bundle 里，靠 window 调用期解析避开 TDZ
      （见 docs/错误总结.md #15，2026-09-19 真机踩过：TDZ 导致公告整体静默失效）。

真实浏览器端到端渲染见 tests/manual_ui_panels.py 的手工步骤。
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
HTML = STATIC / "index.html"
CSS = STATIC / "style.css"
NOTICE = STATIC / "js" / "notice.js"
VIEWS = STATIC / "js" / "views.js"

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


html = HTML.read_text(encoding="utf-8")
css = CSS.read_text(encoding="utf-8")
js = NOTICE.read_text(encoding="utf-8")
views = VIEWS.read_text(encoding="utf-8")

print("=== A. .notice-body 是真正的滚动容器（min-height: 0 不可删）===")
# 定位 .notice-body 的规则块（取第一条字符串形式定义，忽略注释）
_m = re.search(r"\n\.notice-body\s*\{([^}]*)\}", css)
_body_css = _m.group(1) if _m else ""
check("A1 找到 .notice-body 规则块", bool(_m))
check("A2 声明了 overflow-y: auto", "overflow-y: auto" in _body_css)
check("A3 声明了 min-height: 0（缺它则内容撑高、永不滚动）",
      re.search(r"min-height:\s*0", _body_css) is not None)
check("A4 仍是 flex: 1（撑满面板剩余高度）", "flex: 1" in _body_css)
check("A5 附带 touch 惯性滚动（移动端观感）",
      "-webkit-overflow-scrolling: touch" in _body_css)

print("=== B. 公告 / 使用指南 拆成两个 Tab ===")
check("B1 两个 pane 容器都在 HTML 里",
      'id="notice-pane-entries"' in html and 'id="notice-pane-guide"' in html)
check("B2 两个 tab 按钮都在，且带 role=tab / aria-selected",
      html.count('role="tab"') == 2 and html.count('aria-selected=') >= 2)
check("B3 tablist / tabpanel 的 aria 关联齐全",
      'role="tablist"' in html and 'role="tabpanel"' in html
      and 'aria-controls="notice-pane-entries"' in html
      and 'aria-labelledby="notice-tab-guide"' in html)
check("B4 指南 pane 默认 hidden（打开面板先看公告）",
      re.search(r'id="notice-pane-guide"[^>]*\bhidden\b', html) is not None)
check("B5 服务端公告容器 #notice-server 保留（旧契约不变）", 'id="notice-server"' in html)
check("B6 内置指南 #notice-static 保留（离线可用是硬要求）", 'id="notice-static"' in html)
# 旧实现的分隔线：把指南当作公告的"续页"。新结构里必须消失。
check("B7 不再用 .notice-divider 衔接公告与指南（改为两个 Tab）",
      "notice-divider" not in js and "notice-divider" not in html)
check("B8 面板标题不再叫「公告 · 使用指南」（语义混淆的根源）",
      "公告 · 使用指南" not in html.split("notice-panel")[1][:1200])

print("=== C. 分类筛选按 level（跨版本可用），不引新字段 ===")
check("C1 chips 容器存在", 'id="notice-chips"' in html)
_chip_lv = re.findall(r'class="notice-chip"[^>]*data-lv="([^"]*)"', html)
check(f"C2 chips 只覆盖 level 三档 + 全部（实际 {_chip_lv}）",
      sorted(_chip_lv) == ["", "critical", "info", "warn"])
check("C3 JS 按 e.level 过滤，未引用不存在的 category/kind 字段",
      "e.level" in js and "category" not in js and ".kind" not in js)
check("C4 提供 noticeFilter 并挂到 window（内联 onclick 需要）",
      "export function noticeFilter" in js and "window.noticeFilter" in js)
check("C5 提供 noticeTab 并挂到 window",
      "export function noticeTab" in js and "window.noticeTab" in js)
check("C6 空档位收起（不留一排点了没反应的按钮）",
      re.search(r"c\.hidden\s*=\s*\(lv\s*!==\s*\"\"", js) is not None)

print("=== D. 徽标只计未读公告，不含使用指南 ===")
check("D1 面板头徽标元素存在且默认 hidden", re.search(r'id="notice-badge"[^>]*hidden', html) is not None)
check("D2 徽标计数取自 entries 的 unread，不掺指南条目",
      re.search(r"entries\.filter\(\s*e\s*=>\s*e\.unread\s*\)\.length", js) is not None)
check("D3 首页小圆点 #notice-dot 仍被驱动（且只按 unread）",
      'id="notice-dot"' in html and "notice-dot" in js)
check("D4 已读后徽标归零（_markReadSoon 里 unread 置 0 并重画）",
      re.search(r"unread\s*=\s*0", js) is not None and "_paint(" in js)

print("=== E. 三态空状态齐备 ===")
check("E1 有 .notice-empty 空态容器样式", ".notice-empty" in css)
check("E2 「暂无新公告」（真没公告）",
      "暂无新公告" in js)
check("E3 「暂时连不上公告服务」（拉取失败必须说出来）",
      "暂时连不上公告服务" in js)
check("E4 「这个分类下暂无公告」（筛选为空，与「没公告」区分）",
      "这个分类下暂无公告" in js)
check("E5 失败态会更新 _fetchState 并重画",
      js.count('_fetchState = "fail"') >= 2 and "_paintEntries()" in js)

print("=== F. 安全：渲染只用 DOM API + textContent，不拼 HTML ===")
check("F1 不出现 innerHTML", "innerHTML" not in js)
check("F2 不出现 insertAdjacentHTML / outerHTML / document.write",
      not any(k in js for k in ("insertAdjacentHTML", "outerHTML", "document.write")))
check("F3 文本一律走 createTextNode/textContent",
      "createTextNode" in js and "textContent" in js)
check("F4 继续用 _el 工厂（结构上不存在标记注入面）",
      re.search(r"function\s+_el\(", js) is not None)

print("=== G. 滚动位置按 Tab 分别记住 ===")
check("G1 有 _scroll 双槽记录", re.search(r"_scroll\s*=\s*\{\s*entries:[\s\S]*?guide:", js) is not None)
check("G2 有读取/设置滚动位置的封装",
      "_bodyScroll()" in js and "_setBodyScroll(" in js)
check("G3 切 Tab 时先存旧 Tab 位置再恢复新 Tab 位置",
      re.search(r"_scroll\[_tab\]\s*=\s*_bodyScroll\(\)", js) is not None
      and re.search(r"_setBodyScroll\(_scroll\[name\]", js) is not None)
check("G4 恢复滚动位置延到下一帧（否则内容未布局完会被夹回 0）",
      "requestAnimationFrame" in js)
# G5 是 2026-09-24 浏览器实测抓到的真问题：切 Tab 是"存旧位置→恢复新位置"两步，
# 若两次调用都在下一帧排队，先入队的会后跑 ⇒ 恢复成错的 Tab 的位置。
# 必须 cancelAnimationFrame 掉上一次待执行的回调。
check("G5 ★ 恢复前先 cancelAnimationFrame（连续切 Tab 不会互相盖掉）",
      re.search(r"cancelAnimationFrame\s*\(\s*_scrollRaf\s*\)", js) is not None)
check("G6 换筛选时把记录位置一并归零（否则切 Tab 会跳回旧位置）",
      re.search(r"_scroll\[_tab\]\s*=\s*0", js) is not None)
check("G5 换筛选时回到顶部", re.search(r"_setBodyScroll\(0\)", js) is not None)

print("=== H. 与 views.js 的接口不变（TDZ 教训）===")
check("H1 notice.js 仍导出 initNotice / noticeOnOpen / refreshNotice",
      all(re.search(r"export\s+(?:async\s+)?function\s+" + n + r"\b", js)
          for n in ("initNotice", "noticeOnOpen", "refreshNotice")))
check("H2 noticeOnOpen 仍挂 window 供 views.js 调用期解析",
      "window.noticeOnOpen = noticeOnOpen" in js)
check("H3 views.js 仍以 window.noticeOnOpen 调用（不改成 import）",
      "window.noticeOnOpen" in views)
check("H4 自启动仍挂 DOMContentLoaded，未从 main.js 顶层调",
      "DOMContentLoaded" in js and "initNotice()" not in views)
check("H5 缓存键未改名（改名＝老用户未读态全部丢失）",
      'CACHE_KEY = "firefly_notice_cache"' in js and 'SEEN_OPEN_KEY = "firefly_notice_opened"' in js)

print("=== I. 日期分组 + sticky 组头 ===")
check("I1 有 _groupLabel（今天/昨天/月日/更早）", "function _groupLabel" in js)
# ★ 2026-09-25 用户要求：**取消置顶**（"我没说更新方面的内容需要置顶"）。
#   公告是时间流，谁都不该抢第一位；置顶还会让每条公告都想占那个位置。
#   这两条从"置顶存在"反转为"置顶必须不存在"。
check("I2 ★ 不再有置顶分组（列表纯按日期倒序）", 'e.pinned ? "置顶"' not in js)
check("I3 无日期归到「更早」而不是丢掉", "_NO_DATE" in js)
check("I4 组头 sticky", re.search(r"\.notice-group\s*\{[^}]*position:\s*sticky", css, re.S) is not None)
check("I5 ★ 置顶视觉标记已移除（样式与类名都不留）",
      ".notice-pinned" not in css and "notice-pinned" not in js)

print("=== I-b. ★ 列表默认折叠、点标题展开（用户 2026-09-25 报障）===")
# 用户原话："公告页没做好列表化（没有收起具体内容，直接展开占用大量篇幅）"。
# 成熟做法就是 changelog 式折叠列表：先列标题、点开看详情。
check("I6 ★ 条目有折叠开关（整行标题可点）",
      'notice-ent-toggle' in js and '_el("button", "notice-ent-toggle")' in js
      and 'toggle.addEventListener("click"' in js)
check("I7 ★ 默认折叠：正文容器靠 .notice-open 才显示",
      re.search(r"\.notice-entry:not\(\.notice-open\)\s*\.notice-ent-body\s*\{\s*display:\s*none",
                css) is not None)
check("I8 ★ 展开状态记在模块级 Set（整块重绘后不折回去）",
      "_open = new Set()" in js and "_open.add(e.id)" in js and "_open.has(e.id)" in js)
check("I9 折叠开关带 aria-expanded（无障碍）",
      'setAttribute("aria-expanded"' in js)
check("I10 收起时提示内容段数（省得逐条试点）", "notice-ent-hint" in js and " 段" in js)
check("I11 未读在收起态也能看出来（圆点 + 新）",
      "notice-ent-dot" in js and "notice-ent-new" in js)

print("=== J. 面板无障碍与头规范 ===")
check("J1 面板 role=dialog + aria-modal", 'role="dialog"' in html and 'aria-modal="true"' in html)
check("J2 关闭按钮带 aria-label", re.search(r'class="notice-close"[^>]*aria-label="关闭"', html) is not None)
check("J3 关闭按钮触摸目标 ≥ 34px（移动端可点）",
      re.search(r"\.notice-header\s+\.notice-close\s*\{[^}]*min-height:\s*34px", css, re.S) is not None)
check("J4 三个内联 onclick 目标都挂到了 window",
      all(f"window.{n} = {n}" in js for n in ("noticeTab", "noticeFilter")))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
