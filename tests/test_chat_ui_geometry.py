# -*- coding: utf-8 -*-
"""聊天 UI 几何对齐官方短信 UI 的守卫（2026-09-18）。

背景：用户拿官方发短信页面的截图来对比，指出 **气泡和字体大小和位置** 不对，以及
「第二行头像又下去了」「为什么没有像官方一样把名字加到消息上」。量出来的事实
（官方 591 宽截图，一律按**占视口宽比例**折算到 360px 屏）：

| 项 | 官方 | 改前 | 改后 |
|----|------|------|------|
| 头像直径 | 11.8%（43px） | 8.5%（30.6px） | 10.5%（37.8px） |
| 头像左边距 | 8.8%（32px） | 4.0%（14.4px） | 7.0%（25.2px） |
| 头像↔气泡 | 3.6%（13px） | 2.0%（7.2px） | 3.0%（10.8px） |
| 气泡左缘 | 24.2%（87px） | 14.5%（52px） | 20.5%（74px） |
| 单行气泡高 | 26.8–28.6px | 42.6px | 28.1px |
| 字形高 | 11.0–11.6px | 13.1px | 12.1px |
| 左右内边距 | 12.8px | 12.96px | 12.8px |
| 名字行 | **有**（字形高≈气泡文字 0.79 倍，左缘与气泡左缘差 0px） | 无 | 有 |
| 头像垂直 | **顶对齐名字行** | 底对齐（两行气泡时被拽下去） | 顶对齐 |

本测试只钉**同源不变量 + 官方验收线**，不锁死具体字面量：
1. 语音条缩进的 clamp 必须与 `.msg-avatar` 完全一致（2026-09-18 就因为头像改大而错位过）；
2. `.msg-row .bubble` 与 `.msg-col` 的 max-width 必须一致（不然引用卡片比气泡宽）；
3. 气泡/名字的几何在 360px 视口下的**计算结果**落在与官方对齐的区间内；
4. `.msg-row` 必须是 `align-items: flex-start`（底对齐会让多行气泡的头像下坠）。
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
# 先剥掉 CSS 注释再解析：`.msg-col` 的注释里写了「气泡被 max-width:100% 覆盖」，
# 不剥注释的话 `max-width:\s*(\d+)%` 会先命中注释里的 100% —— 第一版就踩了这个。
CSS = re.sub(r"/\*.*?\*/", "", (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8"),
             flags=re.S)
VW = 360.0
PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def rule(sel):
    """取某个规则块（选择器必须**行首**，避免 .msg-col 匹配到 .msg-col .bubble）。"""
    m = re.search(r"(?:^|\n)[ \t]*" + re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


def clamps(text):
    return [tuple(float(x) for x in m)
            for m in re.findall(r"clamp\(\s*([\d.]+)px\s*,\s*([\d.]+)vw\s*,\s*([\d.]+)px\s*\)", text)]


def px(cl):
    lo, vw, hi = cl
    return min(max(vw / 100 * VW, lo), hi)


def padding_clamps(sel):
    """取某个规则块 padding 简写里的 clamp（[上下, 左右]）。"""
    m = re.search(r"padding:\s*([^;]+);", rule(sel))
    return clamps(m.group(1)) if m else []


print("=== A. 同源不变量：语音条缩进 == 头像宽 + 行间距 ===")
av_cl = clamps(rule(".msg-avatar"))
vo_cl = clamps(rule(".msg-row.firefly.has-voice .voice-row"))
gap_cl = clamps(rule(".msg-row"))
check("A1 头像尺寸 clamp 可解析（width/height 两份，值必须相同）",
      len(av_cl) >= 1 and len(set(av_cl)) == 1, str(av_cl))
check("A2 语音条缩进的 clamp 与头像**完全一致**（防两处漂移）",
      av_cl and vo_cl and av_cl[0] == vo_cl[0], f"头像 {av_cl[:1]} vs 缩进 {vo_cl[:1]}")
check("A3 语音条缩进的间距部分与 .msg-row 的 gap 一致",
      len(vo_cl) >= 2 and gap_cl and vo_cl[1] == gap_cl[0],
      f"缩进 {vo_cl[1:2]} vs gap {gap_cl[:1]}")

print("=== B. 同源不变量：气泡与引用容器的最大宽一致 ===")
bw = re.search(r"max-width:\s*(\d+)%", rule(".msg-row .bubble"))
cw = re.search(r"max-width:\s*(\d+)%", rule(".msg-col"))
check("B1 .msg-row .bubble 与 .msg-col 的 max-width 相同",
      bw and cw and bw.group(1) == cw.group(1),
      f"bubble {bw.group(1) if bw else '?'}% vs msg-col {cw.group(1) if cw else '?'}%")

print("=== C. 头像对齐：必须顶对齐（底对齐会让多行气泡的头像下坠）===")
row_blk = rule(".msg-row")
check("C1 .msg-row 用 align-items: flex-start", "align-items: flex-start" in row_blk,
      re.search(r"align-items:\s*[\w-]+", row_blk).group(0) if "align-items" in row_blk else "缺失")
check("C2 不再残留 flex-end（那正是「第二行头像又下去了」的成因）",
      "align-items: flex-end" not in row_blk)

print("=== D. 与官方对齐的验收线（按 360px 视口算出来比）===")
blk = rule(".msg-row .bubble")
_pad = padding_clamps(".msg-row .bubble")
cl = clamps(blk)          # 块内 clamp 顺序：[上下内边距, 左右内边距, 字号]
lh = float(re.search(r"line-height:\s*([\d.]+)", blk).group(1))
pad_v, pad_h, font = px(_pad[0]), px(_pad[1]), px(cl[-1])
glyph = font * 0.93       # CJK 字形墨高 ≈ 0.93em（与量官方同一口径）
one_line = pad_v * 2 + font * lh
_msg_pad = padding_clamps("#messages")
bub_left = px(_msg_pad[1]) + px(av_cl[0]) + px(gap_cl[0])
print(f"    字号={font:.2f} 行高={lh} 上下内边距={pad_v:.2f} 左右内边距={pad_h:.2f}")
print(f"    单行气泡高={one_line:.1f}  字形高={glyph:.1f}  气泡左缘={bub_left:.1f}")
check("D1 单行气泡高落在官方区间 26–32px（改前 42.6）", 26 <= one_line <= 32, f"{one_line:.1f}px")
check("D2 字形高落在官方区间 11–13.2px（改前 13.1）", 11 <= glyph <= 13.2, f"{glyph:.1f}px")
check("D3 左右内边距落在 11–15px（本来就与官方一致，别改坏）",
      11 <= pad_h <= 15, f"{pad_h:.1f}px")
check("D4 上下内边距明显小于左右（官方就是扁的：6 vs 21 @591）",
      pad_v < pad_h * 0.6, f"{pad_v:.1f} vs {pad_h:.1f}")
check("D5 气泡左缘落在 65–90px（官方 87；改前 52）", 65 <= bub_left <= 90, f"{bub_left:.1f}px")
check("D6 头像直径落在 9.5–13% 视口宽（官方 11.8%）",
      9.5 <= px(av_cl[0]) / VW * 100 <= 13, f"{px(av_cl[0]) / VW * 100:.1f}%")

print("=== E. 名字行（官方每条消息都有）===")
who_blk = rule(".msg-who")
check("E1 .msg-who 规则存在", bool(who_blk))
wcl = clamps(who_blk)
if wcl:
    wfont = px(wcl[0])
    print(f"    名字字号={wfont:.2f}px（气泡字号 {font:.2f}px，比值 {wfont/font:.2f}）")
    check("E2 名字字号 ≈ 气泡字号的 0.7–0.85 倍（官方 0.79）",
          0.70 <= wfont / font <= 0.85, f"{wfont/font:.2f}")
check("E3 名字与气泡左右对齐靠 .msg-col 的 align-items，两侧各有规则",
      "align-items: flex-end" in rule(".msg-row.user .msg-col")
      and "align-items: flex-start" in rule(".msg-row.firefly .msg-col"))
check("E4 名字底到气泡顶留了小间隔（官方 3px@591 ≈ 1.8px@360）",
      re.search(r"margin:\s*0\s+2px\s+(\d+)px", who_blk) is not None,
      re.search(r"margin:\s*([^;]+)", who_blk).group(1).strip() if "margin" in who_blk else "?")

print("=== F. 气泡圆角与尾巴（按官方实测）===")
ff = re.search(r"border-radius:\s*([^;]+);", rule(".msg-row.firefly .bubble"))
uu = re.search(r"border-radius:\s*([^;]+);", rule(".msg-row.user .bubble"))
check("F1 firefly 气泡左上近直角、其余圆角（官方：左上 2px@591，其它 11–15px）",
      ff is not None and ff.group(1).split() [0].startswith("3px"),
      ff.group(1).strip() if ff else "?")
check("F2 user 气泡右上近直角（镜像）",
      uu is not None and len(uu.group(1).split()) >= 2 and uu.group(1).split()[1].startswith("3px"),
      uu.group(1).strip() if uu else "?")
check("F3 没有气泡尾巴（2026-09-18 按官方删掉：官方左缘从顶到底是一条直线，无尖角）",
      ".bubble::before" not in CSS)
check("F4 尾巴主题变量不再被引用", "var(--bub-tail" not in CSS)

print("=== H. 语音条要「贴」在自己那条消息上（2026-09-18 用户真机反馈）===")
# 可见间距 = .msg-row 的 row-gap + .voice-bar 的 margin-top。必须明显小于消息之间的间距，
# 否则屏幕上看不出语音条属于哪条消息（原来 17px vs 10.1px，正好反了）。
_vb_mt = re.search(r"margin-top:\s*(\d+)px", rule(".voice-bar"))
_rowgap = re.search(r"row-gap:\s*(\d+)(?:px)?", rule(".msg-row.has-voice"))
_mgap_m = re.search(r"gap:\s*clamp\(\s*([\d.]+)px\s*,\s*([\d.]+)vw\s*,\s*([\d.]+)px\s*\)",
                    rule("#messages"))
_vb_gap = (int(_vb_mt.group(1)) if _vb_mt else -1) + (int(_rowgap.group(1)) if _rowgap else -1)
_msg_gap = px(tuple(float(x) for x in _mgap_m.groups())) if _mgap_m else -1
print(f"    气泡↔语音条 = {_vb_gap}px   消息之间 = {_msg_gap:.1f}px")
check("H1 .msg-row.has-voice 的 row-gap 归零（否则会叠加在条的 margin-top 上）",
      _rowgap is not None and int(_rowgap.group(1)) == 0,
      (_rowgap.group(1) if _rowgap else "缺"))
check("H2 ★ 气泡↔语音条**小于**消息间距（语音条要看起来属于上面那条消息）",
      0 <= _vb_gap < _msg_gap, f"{_vb_gap} vs {_msg_gap:.1f}")
check("H3 气泡↔语音条在 2–8px（贴紧但仍有缝）", 2 <= _vb_gap <= 8, f"{_vb_gap}px")
check("H4 消息间距在 14–22px（原来 10.1 太挤）", 14 <= _msg_gap <= 22, f"{_msg_gap:.1f}px")

print("=== G. 渲染层：名字行接进了三条消息类型 ===")
JS = (ROOT / "app" / "static" / "js" / "chat_render.js").read_text(encoding="utf-8")
check("G1 有 _mkCol 外壳（名字行 + 可选引用 + 内容）", "function _mkCol(" in JS)
check("G2 三条消息类型都走 _mkCol（定义 1 + 调用 3）", JS.count("_mkCol(") >= 4)
check("G3 不再用 row.replaceChild 替换内容（容器化后会抛 NotFoundError）",
      "row.replaceChild(" not in JS)
check("G4 有受控替换辅助 _replaceNode", "function _replaceNode(" in JS)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
