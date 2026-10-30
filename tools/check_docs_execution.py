# -*- coding: utf-8 -*-
"""D1：三页文档的轻量机械检查。

只检查可确定判断的项目：相对链接、被引用脚本、关键 CLI 参数和已登记常量。
不尝试用关键词替代事实审查；语义仍由 reviewer 对照源码确认。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PAGES = (
    Path("docs/参考/版本与发布.md"),
    Path("docs/操作/热更新发布.md"),
    Path("docs/操作/生产服务器.md"),
)
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target><[^>]+>|[^)\s]+)")
SCRIPT_RE = re.compile(r"(?<![\w.-])((?:tools|tests)[\\/][^\s`'\"，。；：、（）()]+?\.(?:py|mjs))")


def _read_page(root: Path, rel: Path) -> tuple[str, str]:
    try:
        return (root / rel).read_text(encoding="utf-8"), ""
    except OSError as exc:
        return "", f"{rel.as_posix()}: 无法读取: {exc}"


def _relative_links(root: Path, rel: Path, text: str) -> tuple[int, list[str]]:
    checked = 0
    errors = []
    base = (root / rel).parent
    for match in LINK_RE.finditer(text):
        raw = match.group("target").strip("<>")
        parsed = urlsplit(raw)
        if parsed.scheme or raw.startswith("#") or raw.startswith("//"):
            continue
        local = unquote(parsed.path)
        if not local:
            continue
        checked += 1
        target = (base / local).resolve(strict=False)
        if not target.exists():
            errors.append(f"{rel.as_posix()}: 相对链接目标不存在: {raw}")
    return checked, errors


def _script_refs(root: Path, rel: Path, text: str) -> tuple[int, list[str]]:
    refs = sorted({m.group(1).replace("\\", "/") for m in SCRIPT_RE.finditer(text)})
    errors = [
        f"{rel.as_posix()}: 引用脚本不存在: {ref}"
        for ref in refs
        if not (root / ref).is_file()
    ]
    return len(refs), errors


def _help_surface(root: Path, script: str, required: tuple[str, ...]) -> list[str]:
    proc = subprocess.run(
        [sys.executable, str(root / script), "--help"],
        cwd=str(root),
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=20,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode != 0:
        return [f"{script} --help 失败（exit={proc.returncode}）"]
    surface = proc.stdout + "\n" + proc.stderr
    return [f"{script} help 缺少参数: {flag}" for flag in required if flag not in surface]


def _page_contracts(texts: dict[Path, str]) -> tuple[int, list[str]]:
    checks = 0
    errors = []

    def require(page: Path, needle: str) -> None:
        nonlocal checks
        checks += 1
        if needle not in texts.get(page, ""):
            errors.append(f"{page.as_posix()}: 缺少登记内容: {needle}")

    def forbid(page: Path, needle: str) -> None:
        nonlocal checks
        checks += 1
        if needle in texts.get(page, ""):
            errors.append(f"{page.as_posix()}: 含已知失效内容: {needle}")

    version = PAGES[0]
    hotupdate = PAGES[1]
    production = PAGES[2]

    for heading in ("## 版本格式", "## 单一事实源", "## 三档门禁", "## 包内验收"):
        require(version, heading)
    require(version, "本地预检")
    require(version, "不能证明线上")

    for heading in ("## 发布前检查", "## 发布", "## 真机验收", "## 失败与回滚"):
        require(hotupdate, heading)
    # 2026-10-30：生产已部署 P0-4 对齐（69 个 app/*.py），"仍为 patch4" 不再成立。
    # 这里钉的是**当前事实**，不是"检查要能过"——事实再变就跟着改这句。
    for needle in ("0.9.1/5", "生产业务代码已与 0.9.2 源码对齐", "真机未验证"):
        require(hotupdate, needle)

    for heading in ("## 2. 拓扑与目录", "## 4. 标准部署/更新流程",
                    "## 11. 回滚"):
        require(production, heading)
    for needle in ("22 + 443 + 8787", "03:00",
                   "/opt/backups/user_data_YYYYMMDD.tar.gz",
                   "FALLBACK_TAG", "download_lib.py", "SHA-256"):
        require(production, needle)
    for stale in ("§4.5", "md5sum", "公网只开 22 + 8787",
                  "网关 `PINNED_TAG` 指向旧 tag"):
        forbid(production, stale)
    return checks, errors


def check(root: Path = ROOT) -> dict:
    root = Path(root).resolve()
    texts = {}
    errors = []
    for rel in PAGES:
        text, err = _read_page(root, rel)
        texts[rel] = text
        if err:
            errors.append(err)
    link_count = 0
    script_count = 0
    for rel, text in texts.items():
        if not text:
            continue
        n, found = _relative_links(root, rel, text)
        link_count += n
        errors.extend(found)
        n, found = _script_refs(root, rel, text)
        script_count += n
        errors.extend(found)

    cli_count = 0
    cli_contracts = (
        ("tools/check_version.py", ("--profile", "release", "deploy")),
        ("tools/build_hotupdate.py", ("--note",)),
        ("tools/check_hotupdate.py", ("--all", "--declare")),
        ("tools/publish_hotupdate.py", ("--serial", "--verify-only")),
    )
    for script, required in cli_contracts:
        cli_count += 1
        errors.extend(_help_surface(root, script, required))

    contract_count, found = _page_contracts(texts)
    errors.extend(found)
    return {
        "ok": not errors,
        "errors": errors,
        "counts": {
            "pages": len(PAGES),
            "relative_links": link_count,
            "script_refs": script_count,
            "cli_contracts": cli_count,
            "page_contracts": contract_count,
        },
    }


def self_test() -> dict:
    version = PAGES[0]
    hotupdate = PAGES[1]
    production = PAGES[2]
    texts = {
        version: "## 版本格式\n## 单一事实源\n## 三档门禁\n## 包内验收\n"
                 "本地预检；不能证明线上。",
        hotupdate: "## 发布前检查\n## 发布\n## 真机验收\n## 失败与回滚\n"
                   "0.9.1/5；生产业务代码已与 0.9.2 源码对齐；真机未验证。",
        production: "## 2. 拓扑与目录\n## 4. 标准部署/更新流程\n## 11. 回滚\n"
                    "22 + 8787\n§4.5\nmd5sum\n",
    }
    _, contract_errors = _page_contracts(texts)
    fake_root = Path("__d1_self_test__")
    link_count, link_errors = _relative_links(
        fake_root, Path("docs/page.md"), "[bad](missing.md)")
    checks = {
        "stale_contract_detected": len(contract_errors) >= 5,
        "broken_link_detected": link_count == 1 and len(link_errors) == 1,
        "valid_link_counted": _relative_links(
            ROOT, Path("docs/参考/测试基线.md"),
            "[protocol](同步协议.md)")[0] == 1,
    }
    return {"ok": all(checks.values()), "checks": checks,
            "contract_errors": contract_errors}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检查 D1 三页文档的链接、脚本、CLI 与登记常量")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--self-test", action="store_true",
                    help="用固定反例验证检查器会失败，而不是只做空扫")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.self_test:
        result = self_test()
        for name, ok in result["checks"].items():
            print(("  V " if ok else "  X ") + name)
        if result["contract_errors"]:
            print(f"  V 反例命中 {len(result['contract_errors'])} 项")
        print(f"=== doc checker self-test: {'PASS' if result['ok'] else 'FAIL'} ===")
        return 0 if result["ok"] else 1

    result = check(Path(args.root))
    counts = result["counts"]
    print(
        "范围: {pages} 页 | 相对链接 {relative_links} | 脚本引用 {script_refs} | "
        "CLI 合约 {cli_contracts} | 页面常量 {page_contracts}".format(**counts)
    )
    for err in result["errors"]:
        print("  X " + err)
    print("结果: " + ("PASS" if result["ok"] else f"FAIL（{len(result['errors'])} 项）"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
