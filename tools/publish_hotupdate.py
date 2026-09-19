#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热更新 · 发布器：把补丁推到**主源（自己的服务器）+ Gitee 兜底镜像**，并逐个 URL 验证。

见 `docs/热更新规范.md §六`。发布端固定三步：审查 → 推送 → 验证。

    python tools/publish_hotupdate.py --serial 1              # 推最新一个 serial（服务器 + Gitee）
    python tools/publish_hotupdate.py --serial 1 --only server
    python tools/publish_hotupdate.py --verify-only --serial 1

设计要点：
  · **Gitee 不新建 release**：把补丁挂到**已有的整包 release 资产**上。
    因为 APK 更新的 Gitee 兜底是"取 release 列表里 id 最大的 tag"（Gitee 没有 /releases/latest），
    新建一个热更 release 会让它取到错 tag → 整包兜底下载 404。挂资产则完全无副作用。
  · Gitee 的 multipart 上传用 `curl -F`（`docs/版本更新规范.md` 记着：urllib 手拼 multipart 会 404）。
  · **不推送 git**：只传 HTTP 资产，不碰任何远程分支。
  · 验证阶段会把两个源的字节都拉回来跟本地比 sha256 —— "传上去了"和"传对了"是两件事。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "hotupdate_dist"
TOKEN_FILE = Path.home() / ".config" / "firefly" / "gitee_token.txt"

SSH_KEY = Path.home() / ".ssh" / "id_rsa"
SSH_HOST = "root@101.200.14.126"
SERVER_DIR = "/opt/firefly-downloads/hotupdate"
GITEE_OWNER_REPO = "cpt-asymmetry/firefly"
GITEE_API = f"https://gitee.com/api/v5/repos/{GITEE_OWNER_REPO}"
GITEE_DL = f"https://gitee.com/{GITEE_OWNER_REPO}/releases/download"
SERVER_ROOT = "http://101.200.14.126:8787/hotupdate"

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def token() -> str:
    """读 Gitee token。

    ⚠️ 必须用 `utf-8-sig`：该文件带 BOM（实测 2026-09-19），而 `\\ufeff` **不被 strip() 去掉**，
       它会跟着拼进 URL → `UnicodeEncodeError: 'ascii' codec can't encode character '\\ufeff'`。
    """
    try:
        return TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    except Exception:
        return ""


def app_version() -> str:
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)',
                  (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def gitee_release_id(tag: str, tok: str) -> int:
    """找 tag 对应的 release id（Gitee 无 latest 端点，用列表）。"""
    url = f"{GITEE_API}/releases?access_token={tok}&per_page=100"
    d = json.loads(urllib.request.urlopen(url, timeout=30).read().decode())
    for r in d or []:
        if str(r.get("tag_name")) == tag:
            return int(r.get("id") or 0)
    return 0


def push_server(local_dir: Path, base: str, files: list) -> None:
    print(f"\n--- 主源：{SSH_HOST}:{SERVER_DIR}/{base}/ ---")
    subprocess.run(["ssh", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=no",
                    SSH_HOST, f"mkdir -p {SERVER_DIR}/{base}"],
                   capture_output=True, timeout=60)
    for f in files:
        r = subprocess.run(["scp", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=no",
                            str(local_dir / f), f"{SSH_HOST}:{SERVER_DIR}/{base}/{f}"],
                           capture_output=True, timeout=300)
        print(f"  {'V' if r.returncode == 0 else 'X'} 上传 {f}")


def push_gitee(local_dir: Path, base: str, files: list, tok: str) -> None:
    tag = f"v{base}"
    print(f"\n--- Gitee 镜像：release {tag} 附件 ---")
    rid = gitee_release_id(tag, tok)
    if not rid:
        print(f"  X 找不到 Gitee release {tag}（不新建 release：会打断整包兜底）")
        return
    print(f"  release id = {rid}")
    for f in files:
        r = subprocess.run(["curl.exe", "-s", "-X", "POST",
                            f"{GITEE_API}/releases/{rid}/attach_files?access_token={tok}",
                            "-F", f"file=@{local_dir / f}"],
                           capture_output=True, timeout=300)
        out = (r.stdout or b"").decode("utf-8", "replace")
        ok = '"name"' in out or '"id"' in out
        print(f"  {'V' if ok else 'X'} 挂载 {f}" + ("" if ok else f"  → {out[:160]}"))


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "FireflyPublish/1.0",
                                               "Cache-Control": "no-store"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def verify(local_dir: Path, base: str, files: list) -> None:
    print("\n--- 验证：两个源都拉回来跟本地比 sha256 ---")
    sources = [("主源", f"{SERVER_ROOT}/{base}"), ("Gitee", f"{GITEE_DL}/v{base}")]
    for label, root in sources:
        for f in files:
            try:
                got = fetch(f"{root}/{f}")
                want = (local_dir / f).read_bytes()
                ok = sha256(got) == sha256(want)
                check(f"{label} /{f}", ok,
                      f"{len(got)}B sha {sha256(got)[:10]}…" + ("" if ok else " ← 与本地不符"))
            except Exception as e:
                check(f"{label} /{f}", False, f"{type(e).__name__}: {str(e)[:80]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="热更新补丁发布器（服务器主 + Gitee 兜底）")
    ap.add_argument("--serial", type=int, required=True)
    ap.add_argument("--base", default=None)
    ap.add_argument("--dist", default=str(DEFAULT_DIST))
    ap.add_argument("--only", choices=["server", "gitee"], default=None)
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()

    base = a.base or app_version()
    local = Path(a.dist) / base
    files = ["latest", f"patch-{a.serial}.json", f"patch-{a.serial}.zip"]
    print(f"=== 审查：{local} ===")
    missing = [f for f in files if not (local / f).is_file()]
    check("三个文件齐备（latest/json/zip）", not missing, str(missing))
    if missing:
        return 2
    check("base 与 app 版本一致", base == app_version(), base)
    tok = token()
    check("Gitee token 可读", bool(tok), TOKEN_FILE.name)

    if not a.verify_only:
        if a.only != "gitee":
            push_server(local, base, files)
        if a.only != "server":
            if tok:
                push_gitee(local, base, files, tok)
            else:
                print("  ! 无 token，跳过 Gitee")
        # 附件挂载是异步的，稍等一下再验证（Gitee 侧落盘需要几秒）
        import time
        time.sleep(6)

    verify(local, base, files)
    print(f"\n统计: PASS={PASS} FAIL={FAIL}")
    print("结果: " + ("PASS 两端可用" if FAIL == 0 else "FAIL 见上面 X"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
