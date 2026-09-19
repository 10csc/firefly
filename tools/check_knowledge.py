# -*- coding: utf-8 -*-
"""知识库一致性门禁（2026-09-15）：index.md 必须与实文件严格一致

- story 包知识库五域结构（world/factions/story/character/dialogues）；
- index.md 由 --rebuild 重新生成（内容含全部实文件链接，顺序确定）；
- 校验模式（默认）：index.md 与"按当前文件重新生成"的内容逐字一致 → PASS，否则 FAIL（提示跑 --rebuild）。
发版/提交流程纳入四门禁之外的本门禁（见 docs/构建规范.md）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "app" / "assets" / "character" / "story" / "knowledge"
GROUPS = [("world", "世界观（星神命途/地区/纪时/术语）"), ("factions", "势力档案"),
          ("story", "主线剧情（总览 + 各版本全任务）"), ("character", "流萤个人（档案/关系/主线经历/对话样本）"),
          ("dialogues", "剧情对话压缩版")]


def build_index() -> str:
    out = ["# 知识库索引 — 流萤·剧情模式", "",
           "> 结构：world/世界观 · factions/势力 · story/主线剧情 · character/角色个人 · dialogues/对话压缩。",
           "> 本文件由脚本生成（tools/check_knowledge.py --rebuild），与实文件严格一致；手工加文件后必须重建。", ""]
    for d, label in GROUPS:
        files = sorted(p for p in (KB / d).rglob("*.md")) if (KB / d).is_dir() else []
        out.append(f"## {d}/ — {label}（{len(files)} 个）")
        out.append("")
        for fp in files:
            rel = fp.relative_to(KB).as_posix()
            first = fp.read_text(encoding="utf-8").split("\n")[0].lstrip("# ").strip()
            out.append(f"- [{rel}]({rel}) — {first}")
            out.append("")
        out.append("")
    return "\n".join(out)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not KB.is_dir():
        print("知识库目录不存在:", KB)
        return 1
    want = build_index()
    if "--rebuild" in sys.argv:
        (KB / "index.md").write_text(want, encoding="utf-8")
        print("index.md 已重建")
        return 0
    cur = (KB / "index.md").read_text(encoding="utf-8") if (KB / "index.md").exists() else ""
    if cur == want:
        print("知识库索引与实文件一致 ✓")
        return 0
    print("index.md 与实文件不一致——请运行 python tools/check_knowledge.py --rebuild")
    return 1


if __name__ == "__main__":
    sys.exit(main())
