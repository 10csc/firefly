# -*- coding: utf-8 -*-
"""前端 bundle 生成器：把 app/static/js/*.js（ES Module 源码）拼成 js/bundle.js（classic script）。

为什么需要它：安卓服务器模式以 file:// 加载页面，WebView 按 CORS 拦截 type=module，
导致整站 JS 失效（2026-08-18 事故：手机端卡死在无 JS 的静态聊天页）。
js/ 下的模块仍是开发源码（组织结构用），运行时三端统一加载 bundle.js。

用法：
    python tools/build_frontend_bundle.py          # 生成 app/static/js/bundle.js
    python tools/build_frontend_bundle.py --check  # 只校验 bundle 是否与模块源码一致（漂移退出 1）
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "app" / "static" / "js"
BUNDLE = JS_DIR / "bundle.js"

# 拼接顺序 = 原 app.js 的章节顺序（单作用域，声明提升天然兼容，无循环导入问题）
ORDER = ["state", "util", "imgzip", "api", "panels", "chat", "fix", "views", "proactive", "relay", "guide", "main"]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


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


def main() -> int:
    content = build()
    if "--check" in sys.argv:
        if not BUNDLE.exists():
            print("X bundle.js 不存在（运行 python tools/build_frontend_bundle.py 生成）")
            return 1
        if BUNDLE.read_text(encoding="utf-8") != content:
            print("X bundle.js 与 js/ 模块源码漂移（运行 python tools/build_frontend_bundle.py 重新生成）")
            return 1
        print("bundle.js 与模块源码一致 ✓")
        return 0
    BUNDLE.write_text(content, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(BUNDLE)], capture_output=True, text=True)
    if r.returncode != 0:
        print("!! node --check 失败：\n" + r.stderr[:1000])
        return 1
    print(f"bundle.js 生成完成（{len(content.splitlines())} 行），node --check 通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
