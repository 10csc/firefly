#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交前凭据 / 大文件自检 —— 防止密钥与巨型二进制混进 git

用途：提交**之前**跑一遍，确认将要入库的内容里没有：
  ① 活跃凭据（`sk-` 前缀的 API Key、token/password/api_key 等赋了非空值）
  ② 超过阈值（默认 10MB）的大文件
出问题时**退出码非零**，可直接挂在提交流程里当闸门用。

场景：2026-10-03 做 R-11 提交范围核查时发现，工作区里躺着明文 API Key
      （`user_data/config.json`、`_promo_run/user_data/config.json`、`_harden/qa_evi2/…`），
      它们**侥幸**被 `.gitignore` 挡住且从未进过历史，但防线是"碰巧对"而不是"有人在查"。
      本工具把这道防线固定下来：每次提交前必查，不再依赖运气。

用法：
    python tools/check_no_secrets.py                # 扫**暂存区**（默认，提交前用这个）
    python tools/check_no_secrets.py --worktree     # 扫工作区里所有**未被忽略**的文件
    python tools/check_no_secrets.py --path docs    # 扫指定路径
    python tools/check_no_secrets.py --max-mb 5     # 改大文件阈值
    python tools/check_no_secrets.py --allow-large  # 只查凭据，不查体积（仍会打印大小）

退出码：0 = 干净；1 = 发现凭据；2 = 有大文件；3 = 两者都有。

★ 输出**永不使用原文**：命中只显示路径、行号、类型与掩码片段（如 `sk-****` + 长度），
  避免"为了排查而把密钥又打印一遍"——本工具自己也不该成为泄密源。
"""
import argparse
import os
import re
import subprocess
import sys

# ── 凭据模式 ──
# sk- 前缀：厂商 API Key 最常见的形态（字面量写作 sk- 后跟一串字符；
# 注意别在本文件里写能匹配自身正则的示例，否则自检会命中自己）
RE_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
# 常见凭据字段赋值：key = "非空且不像占位符的值"
RE_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_\-]?key|access[_\-]?key|private[_\-]?key)"
    r"\s*[:=]\s*[\"']([^\"']{6,})[\"']"
)
# 占位符不算命中：xxx / your-key-here / ${ENV} / *** / changeme / 空
PLACEHOLDER = re.compile(
    r"(?i)^(xxx+|\*{3,}|your[_\-]?\w+|<[^>]+>|\$\{[^}]+\}|%s|changeme|todo|none|null|placeholder|example|\.\.\.)$"
)

SKIP_SUFFIX = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",  # 图片
    ".apk", ".exe", ".dll", ".so", ".jar", ".zip", ".gz", ".tar", ".7z",  # 二进制
    ".wav", ".mp3", ".ogg", ".flac", ".mp4",  # 音视频
    ".ttf", ".otf", ".woff", ".woff2",  # 字体
    ".db", ".sqlite", ".pdf",
}
DEFAULT_MAX_MB = 10


def mask(s: str, keep: int = 3) -> str:
    """掩码：只留前缀与长度，绝不回显原文。"""
    head = s[:keep]
    return f"{head}{'*' * min(12, max(4, len(s) - keep))}(len={len(s)})"


def git(*args) -> list:
    r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return []
    return [x for x in r.stdout.splitlines() if x.strip()]


def collect_staged() -> list:
    """暂存区里新增/修改的文件（删除的不必扫）。"""
    return git("diff", "--cached", "--name-only", "--diff-filter=ACM")


def collect_worktree() -> list:
    """工作区里所有未被 .gitignore 忽略的文件（tracked + 未忽略的未跟踪）。"""
    tracked = git("-c", "core.quotepath=false", "ls-files")
    untracked = git("-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard")
    return tracked + untracked


def collect_path(base: str) -> list:
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules"}]
        for f in files:
            out.append(os.path.join(root, f).replace("\\", "/"))
    return out


def scan(files, max_bytes, allow_large):
    hits_secret, hits_large = [], []
    for rel in files:
        if not os.path.isfile(rel):
            continue  # 已删除
        try:
            size = os.path.getsize(rel)
        except OSError:
            continue
        if not allow_large and size > max_bytes:
            hits_large.append((rel, size))

        ext = os.path.splitext(rel)[1].lower()
        if ext in SKIP_SUFFIX or size > 8 * 1024 * 1024:
            continue  # 二进制/超大文件不做内容扫描（只管体积）
        try:
            with open(rel, "r", encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    for m in RE_SK.finditer(line):
                        hits_secret.append((rel, i, "sk-前缀 API Key", mask(m.group(0), 3)))
                    for m in RE_ASSIGN.finditer(line):
                        val = m.group(2)
                        if PLACEHOLDER.match(val.strip()):
                            continue
                        hits_secret.append((rel, i, f"{m.group(1)} 赋值", mask(val, 0)))
        except OSError:
            continue
    return hits_secret, hits_large


def main() -> int:
    ap = argparse.ArgumentParser(description="提交前凭据/大文件自检")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="扫暂存区（默认）")
    g.add_argument("--worktree", action="store_true", help="扫工作区未被忽略的文件")
    g.add_argument("--path", metavar="DIR", help="扫指定路径")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB, help=f"大文件阈值 MB（默认 {DEFAULT_MAX_MB}）")
    ap.add_argument("--allow-large", action="store_true", help="不因大文件失败（仍打印）")
    args = ap.parse_args()

    if args.path:
        files, scope = collect_path(args.path), f"路径 {args.path}"
    elif args.worktree:
        files, scope = collect_worktree(), "工作区（未忽略）"
    else:
        files, scope = collect_staged(), "暂存区"

    max_bytes = int(args.max_mb * 1024 * 1024)
    print(f"=== 凭据 / 大文件自检 ===")
    print(f"范围: {scope} ｜ 文件数: {len(files)} ｜ 大文件阈值: {args.max_mb} MB")
    print()

    secrets, large = scan(files, max_bytes, args.allow_large)

    print("=== ① 凭据命中 ===")
    if secrets:
        for rel, ln, kind, m in secrets:
            print(f"  ✗ {rel}:{ln}  [{kind}]  {m}")
    else:
        print("  无 ✓")

    print()
    print("=== ② 大文件（> {} MB）===".format(args.max_mb))
    if large:
        for rel, size in sorted(large, key=lambda x: -x[1]):
            print(f"  ✗ {rel}  {size:,} B ({size / 1024 / 1024:.1f} MB)")
    else:
        print("  无 ✓")

    print()
    code = 0
    if secrets:
        code |= 1
    if large and not args.allow_large:
        code |= 2
    if code == 0:
        print("结果: PASS（可提交）")
    else:
        reasons = []
        if code & 1:
            reasons.append(f"凭据 {len(secrets)} 处")
        if code & 2:
            reasons.append(f"大文件 {len(large)} 个")
        print(f"结果: FAIL —— {'、'.join(reasons)}；请先处理再提交")
    return code


if __name__ == "__main__":
    sys.exit(main())
