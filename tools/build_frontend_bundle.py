# -*- coding: utf-8 -*-
"""前端 bundle 生成器：把 app/static/js/*.js（ES Module 源码）拼成 js/bundle.js（classic script）。

为什么需要它：安卓服务器模式以 file:// 加载页面，WebView 按 CORS 拦截 type=module，
导致整站 JS 失效（2026-08-18 事故：手机端卡死在无 JS 的静态聊天页）。
js/ 下的模块仍是开发源码（组织结构用），运行时三端统一加载 bundle.js。

用法：
    python tools/build_frontend_bundle.py          # 生成 app/static/js/bundle.js
    python tools/build_frontend_bundle.py --check  # 只校验 bundle 是否与模块源码一致（漂移退出 1）
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "app" / "static" / "js"
BUNDLE = JS_DIR / "bundle.js"
INDEX = ROOT / "app" / "static" / "index.html"

# 拼接顺序 = 原 app.js 的章节顺序（单作用域，声明提升天然兼容，无循环导入问题）
# 0.9.1 拆分：panels→panels/settings/update/sync，chat→chat_render/chat/chat_media/chat_history；
# 各模块顶层只注册事件（跨模块引用全部发生在事件/函数调用期），家族内顺序无 TDZ 约束
# 2.5 拆分：panels 按面板再拆成 js/panels/{packs,data,debug}.js（外壳仍是 js/panels.js）——
#   子目录模块用相对路径登记（"panels/packs"），紧随外壳之后。
ORDER = ["state", "util", "ui_select", "imgzip", "api",
         "panels", "panels/packs", "panels/data", "panels/debug", "panels/stickers", "panels/pack_assist", "panels/pack_tree",
         "settings", "update", "sync",
         "chat_render", "chat", "chat_media", "chat_history", "voice_plugin",
         "fix", "views", "proactive", "relay", "guide", "main", "hotupdate", "notice",
         "diag"]

# 不参与 bundle 的 js 模块名（写模块名，不带 .js，子目录用相对路径；各自有独立加载方式）：
# - bundle：本脚本的产物，不能自我包含
# - pc_shell：PC 三栏外壳，index.html 用独立 <script> 加载（只在 ≥1100px 激活；
#   放在 bundle 外是为了"PC 逻辑不参与移动端那一个作用域"——见 docs/设计/PC端前端重构.md）
# 退役记录：pc_nav.js（petite-vue 双栏导航）已删除，由 pc_shell.js 取代；连同
# vendor/petite-vue.iife.js 一并移除（PC 端不再下载框架）。
NON_BUNDLE = {"bundle", "pc_shell"}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _module_names() -> set:
    """js/ 下（含一层子目录，如 js/panels/）的全部模块名，用相对路径表示。

    2026-09-13（阶段 2.5）：panels 拆进子目录后，集合断言必须跟着递归，
    否则新模块在门禁里"看不见"（门禁会把 bundle 漏模块放过去）。"""
    return {p.relative_to(JS_DIR).with_suffix("").as_posix()
            for p in JS_DIR.rglob("*.js")} - NON_BUNDLE


def check_module_set() -> bool:
    """js/ 目录里的模块集合必须与 ORDER 逐字相等（错一个就 FAIL）。

    2026-09-13（门禁修复 D-3）：原来 ORDER 只是硬编码 18 项，新增 js 模块忘登记时
    bundle 会**静默漏掉**该模块（运行时才报 undefined，门禁全绿）。现在盘上多出
    未登记的模块 → 门禁 FAIL，逼着登记进 ORDER（或显式放进 NON_BUNDLE 并说明加载方式）。"""
    actual = _module_names()
    expected = set(ORDER)
    if len(ORDER) != len(expected):
        print(f"X ORDER 内有重复项: {sorted(ORDER)}")
        return False
    unlisted = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unlisted:
        print(f"X js/ 下有未登记进 ORDER 的模块（bundle 会漏掉它们）: {[n + '.js' for n in unlisted]}")
    if missing:
        print(f"X ORDER 登记了但盘上不存在（拼写错或文件被删）: {[n + '.js' for n in missing]}")
    if unlisted or missing:
        print("  补救：登记进 ORDER（要打进 bundle），或加进 NON_BUNDLE（独立加载，需说明）")
        return False
    return True


def build() -> str:
    parts = [
        "/* ═══════════════════════════════════════════",
        "   流萤前端运行时 bundle（classic script）—— 本文件由 tools/build_frontend_bundle.py 生成，",
        "   请勿手改！源码在 app/static/js/*.js（ES Module），改完跑该脚本重新生成。",
        "   ═══════════════════════════════════════════ */",
    ]
    for name in ORDER:
        fp = JS_DIR / f"{name}.js"
        text = fp.read_text(encoding="utf-8")
        out_lines = []
        for ln in text.split("\n"):
            s = ln.strip()
            if s.startswith("import ") and s.endswith('";'):   # 模块导入行（含副作用导入）
                continue
            ln = re.sub(r"^export (?=(?:async\s+)?function|const|let|var)", "", ln)
            out_lines.append(ln)
        parts.append(f"\n/* ── 来源：js/{name}.js ── */")
        parts.extend(out_lines)
    return "\n".join(parts).rstrip() + "\n"


def _bundle_ref(content: str) -> str:
    """bundle.js 的内容指纹（index.html 引用带 ?v= 防浏览器/WebView 缓存旧版）。"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:8]


def _style_ref() -> str:
    """style.css 的内容指纹（同理由）。"""
    return hashlib.md5((ROOT / "app" / "static" / "style.css").read_bytes()).hexdigest()[:8]


def _pc_css_ref() -> str:
    """pc.css 的内容指纹（同 style.css 的理由：内容变 → URL 变 → 客户端必然拉新版）。

    2026-09-19 PC 重构新增：pc.css 是 PC 三栏外壳的唯一样式源（见 docs/设计/PC端前端重构.md），
    它同样要被 sync_frontends 同步到三端，所以指纹也必须在 index.html 里跟着更新。
    """
    fp = ROOT / "app" / "static" / "pc.css"
    if not fp.is_file():
        return ""
    return hashlib.md5(fp.read_bytes()).hexdigest()[:8]


def main() -> int:
    if not check_module_set():
        return 1
    content = build()
    if "--check" in sys.argv:
        if not BUNDLE.exists():
            print("X bundle.js 不存在（运行 python tools/build_frontend_bundle.py 生成）")
            return 1
        if BUNDLE.read_text(encoding="utf-8") != content:
            print("X bundle.js 与 js/ 模块源码漂移（运行 python tools/build_frontend_bundle.py 重新生成）")
            return 1
        idx = INDEX.read_text(encoding="utf-8")
        if f"js/bundle.js?v={_bundle_ref(content)}" not in idx:
            print("X index.html 的 bundle 引用版本指纹过期（运行 python tools/build_frontend_bundle.py 更新）")
            return 1
        pcv = _pc_css_ref()
        if pcv and f"pc.css?v={pcv}" not in idx:
            print("X index.html 的 pc.css 引用版本指纹过期（运行 python tools/build_frontend_bundle.py 更新）")
            return 1
        print("bundle.js 与模块源码一致 ✓")
        return 0
    BUNDLE.write_text(content, encoding="utf-8")
    # 更新 index.html 引用版本指纹（内容变 → URL 变 → 客户端必然拉新版）
    v = _bundle_ref(content)
    sv = _style_ref()
    idx_text = INDEX.read_text(encoding="utf-8")
    # ★ 只替换 **href/src 属性里的** 引用：早期写法用裸文件名正则，把注释里的
    #   `pc.css` 也替换成了 `pc.css?v=xxxx`（2026-09-19 实际踩到，注释被改花）。
    idx_new = re.sub(r'(src="js/bundle\.js)(\?v=[0-9a-f]{8})?"', rf'\1?v={v}"', idx_text)
    idx_new = re.sub(r'(href="style\.css)(\?v=[0-9a-f]{8})?"', rf'\1?v={sv}"', idx_new)
    pcv = _pc_css_ref()
    if pcv:
        idx_new = re.sub(r'(href="pc\.css)(\?v=[0-9a-f]{8})?"', rf'\1?v={pcv}"', idx_new)
    if idx_new != idx_text:
        INDEX.write_text(idx_new, encoding="utf-8")
        print(f"index.html 引用指纹已更新（bundle ?v={v}, style ?v={sv}）")
    r = subprocess.run(["node", "--check", str(BUNDLE)], capture_output=True, text=True)
    if r.returncode != 0:
        print("!! node --check 失败：\n" + r.stderr[:1000])
        return 1
    print(f"bundle.js 生成完成（{len(content.splitlines())} 行），node --check 通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
