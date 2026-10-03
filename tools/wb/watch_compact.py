#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 上下文压缩监控器。

用途
----
WorkBuddy(开发工具)自身的上下文压缩阈值不在可配置项里 ——
它由服务端下发的 harness 策略决定(emergency-auto / MaxToken),
实测水位约 20 万 token。本脚本只做一件事:
实时盯住 WorkBuddy 日志, 一旦发生压缩就把关键行捞出来, 让你能:

  1. 看到"压缩前真实水位是多少"(tokenCount / totalTokens)
  2. 验证你做的任何调整(换模型 / 改 preMessageCompactPct)是否生效
  3. 观察到压缩频率, 判断当前任务是否被压缩打断

用法
----
    # 盯当前项目(默认) + 最近 5 分钟回放
    python tools/wb/watch_compact.py

    # 盯指定项目日志
    python tools/wb/watch_compact.py --project firefly

    # 列出所有项目日志
    python tools/wb/watch_compact.py --list

    # 只回放, 不实时跟随
    python tools/wb/watch_compact.py --no-follow

日志位置
--------
    ~/.workbuddy/logs/<YYYY-MM-DD>/<项目名>__<hash>.log

关键日志行(实测样本)
--------------------
    [Compact] session=... tokenCount=357447/unknown (?%, threshold=90.0%),
              force=true, historyLen=377, compactType=emergency-auto,
              enablePreMessageCompact=true -> routing to strategy: MaxToken
    [Compact:MaxToken] consecutiveMaxTokenCompactCount=1/5
    [Compact:MaxToken] Smart boundary trim done: 1007 items -> 646 kept item(s)
    [Compact:MaxToken] Summary Agent succeeded: 5492 estimated tokens
    [Compact] Added trailing tool result tokens: +8116, totalTokens=200548
    [shouldCompact] Added trailing tool result tokens: +1716, totalTokens=48508

重要提醒: tokenCount 与 totalTokens 是两套口径
    tokenCount  —— 原始累计(含尾部工具结果), 实测会一路涨到 ~40 万
    totalTokens —— 压缩后回落到的目标水位, 及每轮叠加值

实测结论(2026-09-24 firefly 会话)
--------------------------------
    18:06  水位 202680   -> 不压缩
    18:18  水位 300226   -> 不压缩
    18:24  水位 340268   -> 不压缩
    21:41  tokenCount=398864/unknown -> 压缩!

    threshold=90.0% 永远命中不了, 因为分母是 "unknown"(解析失败)。
    真正让压缩发生的是 force=true —— 上下文逼近模型物理上限时的
    保护性强制压缩, 而不是某个可配置的百分比阈值。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

WB_HOME = Path(os.environ.get("WORKBUDDY_HOME", Path.home() / ".workbuddy"))
LOG_ROOT = WB_HOME / "logs"

# 只抓这几类行, 其余一律忽略
PATTERNS = [
    ("触发", re.compile(r"\[Compact\] session=.*?compactType=(\S+?),\s*isSubAgent=(\S+?),\s*enablePreMessageCompact=(\S+?)\s*→\s*routing to strategy:\s*(\S+)")),
    ("计数", re.compile(r"\[Compact:MaxToken\] consecutiveMaxTokenCompactCount=(\S+)")),
    ("裁剪", re.compile(r"\[Compact:MaxToken\] Smart boundary trim done:\s*(\S+)\s*items\s*→\s*(\S+)\s*kept")),
    ("摘要", re.compile(r"\[Compact:MaxToken\] Summary Agent succeeded:\s*(\S+)\s*estimated tokens")),
    ("完成", re.compile(r"\[Compact:MaxToken\] Compression complete")),
    ("终止", re.compile(r"\[Compact:MaxToken\] Session cancelled before replacing history")),
    ("加水", re.compile(r"\[shouldCompact\] Added trailing tool result tokens:\s*\+(\d+),\s*totalTokens=(\d+)")),
    ("加水", re.compile(r"\[Compact\] Added trailing tool result tokens:\s*\+(\d+),\s*totalTokens=(\d+)")),
    ("水位", re.compile(r"\[Compact\] session=\S+\s+tokenCount=(\d+)/(\S+)\s*\(")),
    ("配置", re.compile(r"setSessionConfigOption called: configId=(\S+?),\s*value=(\S+)")),
    ("冷却", re.compile(r"\[shouldCompact\] Skip: cooldown")),
]

TS_RE = re.compile(r"^\[(\d{4}/\d{1,2}/\d{1,2} \d{2}:\d{2}:\d{2})\.(\d{3})\]")

RESET = "\033[0m"
DIM = "\033[2m"
RED = "\033[31m"
YEL = "\033[33m"
GRN = "\033[32m"
CYN = "\033[36m"
MAG = "\033[35m"

COLORS = {
    "触发": RED,
    "计数": YEL,
    "裁剪": CYN,
    "摘要": CYN,
    "完成": GRN,
    "终止": MAG,
    "加水": DIM,
    "水位": DIM,
    "配置": GRN,
    "冷却": DIM,
}


def find_logs(project: str | None) -> list[Path]:
    """找出今天(或最近一天有日志的)项目日志文件。"""
    if not LOG_ROOT.exists():
        return []
    today = date.today().strftime("%Y-%m-%d")
    day_dir = LOG_ROOT / today
    if not day_dir.exists():
        # 回退到最近一天
        dirs = sorted([d for d in LOG_ROOT.iterdir() if d.is_dir()], reverse=True)
        if not dirs:
            return []
        day_dir = dirs[0]

    files = sorted(day_dir.glob("*.log"))
    if project:
        files = [f for f in files if f.name.startswith(project)]
    return files


_last_watermark_step = -1


def parse_and_print(line: str, show_ts: bool = True, all_water: bool = False) -> bool:
    """解析一行, 命中则打印并返回 True。"""
    global _last_watermark_step

    m = TS_RE.match(line)
    ts = ""
    if m:
        ts = f"{DIM}{m.group(1)}.{m.group(2)[:2]}{RESET} "

    for tag, pat in PATTERNS:
        mm = pat.search(line)
        if not mm:
            continue

        color = COLORS.get(tag, "")

        # 「加水」行每轮工具调用都刷一条, 默认按 2 万 token 台阶抽样,
        # 避免淹没真正重要的压缩事件。
        if tag == "加水" and not all_water:
            total = int(mm.group(2))
            step = total // 20000
            if step <= _last_watermark_step:
                return False
            _last_watermark_step = step

        if tag == "触发":
            ctype, sub, pre, strategy = mm.groups()
            msg = f"[{color}{tag}{RESET}] 压缩启动 类型={ctype} 策略={strategy} 子代理={sub} 前置压缩={pre}"
        elif tag == "计数":
            msg = f"[{color}{tag}{RESET}] 本会话累计第 {mm.group(1)} 次 (上限 5)"
        elif tag == "裁剪":
            a, b = mm.groups()
            msg = f"[{color}{tag}{RESET}] 历史裁剪 {a} → {b} 条"
        elif tag == "摘要":
            msg = f"[{color}{tag}{RESET}] 摘要生成完毕, 约 {mm.group(1)} tokens"
        elif tag == "加水":
            delta, total = mm.groups()
            msg = f"[{color}{tag}{RESET}] 尾部工具结果 +{delta}, 当前水位 {total}"
        elif tag == "水位":
            count, denom = mm.groups()
            msg = f"[{color}{tag}{RESET}] 原始累计 {count} (分母={denom})"
        elif tag == "配置":
            cid, val = mm.groups()
            if cid == "shouldCompact":
                return False
            msg = f"[{color}{tag}{RESET}] {cid} = {val}"
        elif tag == "冷却":
            return False
        else:
            msg = f"[{color}{tag}{RESET}] {line.strip()[:110]}"

        if show_ts:
            print(f"{ts}{msg}")
        else:
            print(msg)
        return True

    return False


def replay(files: list[Path], show_config: bool, all_water: bool = False) -> None:
    print(f"{DIM}── 回放 {len(files)} 个日志文件 ──{RESET}")
    for f in files:
        print(f"{DIM}   {f.name}{RESET}")
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not show_config and "setSessionConfigOption" in line:
                        continue
                    parse_and_print(line, all_water=all_water)
        except OSError as e:
            print(f"{RED}   读取失败: {e}{RESET}")


def follow(files: list[Path]) -> None:
    """实时跟随所有日志文件的新增内容。"""
    handles: dict[Path, int] = {}
    for f in files:
        try:
            handles[f] = f.stat().st_size
        except OSError:
            handles[f] = 0

    print(f"{DIM}── 实时监控中(每 2 秒轮询), Ctrl+C 退出 ──{RESET}")
    try:
        while True:
            time.sleep(2)
            for f in list(handles.keys()):
                # 日期翻天后自动切换
                for nf in find_logs(f.name.split("__")[0]):
                    if nf not in handles:
                        handles[nf] = 0
                        print(f"{DIM}   + 新日志 {nf.name}{RESET}")
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                if size < handles.get(f, 0):
                    handles[f] = 0  # 日志被轮转/截断
                if size == handles.get(f, 0):
                    continue
                try:
                    with f.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(handles.get(f, 0))
                        for line in fh:
                            parse_and_print(line)
                        handles[f] = fh.tell()
                except OSError:
                    continue
    except KeyboardInterrupt:
        print(f"\n{DIM}── 已停止 ──{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 上下文压缩监控器")
    ap.add_argument("--project", "-p", default=None,
                    help="项目名前缀, 如 firefly; 默认监控所有项目")
    ap.add_argument("--list", "-l", action="store_true", help="列出可用日志后退出")
    ap.add_argument("--no-follow", action="store_true", help="只回放, 不实时跟随")
    ap.add_argument("--show-config", action="store_true",
                    help="回放时也显示 context_window 等配置写入")
    ap.add_argument("--all-water", action="store_true",
                    help="显示每一条水位变化(默认每 2 万 token 抽样一条)")
    args = ap.parse_args()

    files = find_logs(args.project)

    if args.list:
        if not files:
            print(f"{YEL}未找到日志。日志根目录: {LOG_ROOT}{RESET}")
            return 1
        print(f"日志根目录: {LOG_ROOT}")
        for f in files:
            size = f.stat().st_size / 1024
            print(f"  {f.parent.name}/{f.name}  ({size:.0f} KB)")
        return 0

    if not files:
        print(f"{YEL}未找到日志。日志根目录: {LOG_ROOT}{RESET}")
        print(f"{DIM}提示: 确认 WorkBuddy 已产生过日志, 或 --project 名称是否正确{RESET}")
        return 1

    print(f"{DIM}WorkBuddy 压缩监控 | 阈值由 harness 决定(水位 ≈20万), "
          f"上下文窗口设置不影响它{RESET}")

    replay(files, args.show_config, args.all_water)

    if not args.no_follow:
        follow(files)

    return 0


if __name__ == "__main__":
    sys.exit(main())
