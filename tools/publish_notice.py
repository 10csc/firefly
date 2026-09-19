#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公告 · 发布器：把公告推到**主源（自己的服务器）+ Gitee 兜底镜像**，并逐个 URL 验证。

见 `docs/公告通道规范.md`。三步固定：审查 → 推送 → 验证。

    python tools/publish_notice.py --serial 1                 # 服务器 + Gitee
    python tools/publish_notice.py --serial 1 --only server
    python tools/publish_notice.py --serial 1 --verify-only

与热更发布器的两条共同纪律：
  · **Gitee 不新建 release**：公告文件挂到**已有的整包 release 资产**上（新建 release 会让
    "取 id 最大的 tag"的整包兜底取到错 tag → APK 下载 404）。
  · **不推送 git**：只传 HTTP 资产，不碰任何远程分支。
公告比补丁多一件事：图片也要一个个传（端侧会按清单里的 sha256 逐张校验）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "hotupdate_dist" / "notice"
TOKEN_FILE = Path.home() / ".config" / "firefly" / "gitee_token.txt"

SSH_KEY = Path.home() / ".ssh" / "id_rsa"
SSH_HOST = "root@101.200.14.126"
SERVER_DIR = "/opt/firefly-downloads/notice"
GITEE_OWNER_REPO = "cpt-asymmetry/firefly"
GITEE_API = f"https://gitee.com/api/v5/repos/{GITEE_OWNER_REPO}"
GITEE_DL = f"https://gitee.com/{GITEE_OWNER_REPO}/releases/download"
SERVER_ROOT = "http://101.200.14.126:8787/notice"

# 公告挂在哪个 tag 的 release 资产下（**与热更的兜底 tag 必须一致**：
# `app/notice.py::_to_notice_root` 把热更的 `{base}` 源原样当成公告源）。
GITEE_TAG = "v0.9.0"

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
    """读 Gitee token（`utf-8-sig`：该文件带 BOM，BOM 不被 strip 去掉会拼进 URL）。"""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    except Exception:
        return ""


def gitee_release_id(tag: str, tok: str) -> int:
    url = f"{GITEE_API}/releases?access_token={tok}&per_page=100"
    d = json.loads(urllib.request.urlopen(url, timeout=30).read().decode())
    for r in d or []:
        if str(r.get("tag_name")) == tag:
            return int(r.get("id") or 0)
    return 0


def push_server(local: Path, files: list) -> None:
    print(f"\n--- 主源：{SSH_HOST}:{SERVER_DIR}/ ---")
    subprocess.run(["ssh", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=no",
                    SSH_HOST, f"mkdir -p {SERVER_DIR}"], capture_output=True, timeout=60)
    for f in files:
        r = subprocess.run(["scp", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=no",
                            str(local / f), f"{SSH_HOST}:{SERVER_DIR}/{f}"],
                           capture_output=True, timeout=300)
        print(f"  {'V' if r.returncode == 0 else 'X'} 上传 {f}")


def push_gitee(local: Path, files: list, tok: str) -> None:
    print(f"\n--- Gitee 镜像：release {GITEE_TAG} 附件 ---")
    rid = gitee_release_id(GITEE_TAG, tok)
    if not rid:
        print(f"  X 找不到 Gitee release {GITEE_TAG}（不新建 release：会打断整包兜底）")
        return
    print(f"  release id = {rid}")
    for f in files:
        r = subprocess.run(["curl.exe", "-s", "-X", "POST",
                            f"{GITEE_API}/releases/{rid}/attach_files?access_token={tok}",
                            "-F", f"file=@{local / f}"], capture_output=True, timeout=300)
        out = (r.stdout or b"").decode("utf-8", "replace")
        ok = '"name"' in out or '"id"' in out
        print(f"  {'V' if ok else 'X'} 挂载 {f}" + ("" if ok else f"  → {out[:160]}"))


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "FireflyPublish/1.0",
                                               "Cache-Control": "no-store"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def verify(local: Path, files: list) -> None:
    print("\n--- 验证：两个源都拉回来跟本地比 sha256 ---")
    for label, root in (("主源", SERVER_ROOT), ("Gitee", f"{GITEE_DL}/{GITEE_TAG}")):
        for f in files:
            try:
                got = fetch(f"{root}/{f}")
                want = (local / f).read_bytes()
                ok = sha256(got) == sha256(want)
                check(f"{label} /{f}", ok,
                      f"{len(got)}B sha {sha256(got)[:10]}…" + ("" if ok else " ← 与本地不符"))
            except Exception as e:
                check(f"{label} /{f}", False, f"{type(e).__name__}: {str(e)[:80]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="公告发布器（服务器主 + Gitee 兜底）")
    ap.add_argument("--serial", type=int, required=True)
    ap.add_argument("--dist", default=str(DEFAULT_DIST))
    ap.add_argument("--only", choices=["server", "gitee"], default=None)
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()

    local = Path(a.dist)
    name = f"notice-{a.serial}.json"
    print(f"=== 审查：{local} ===")
    if not (local / name).is_file():
        check(f"{name} 存在", False)
        return 2
    check(f"{name} 存在", True)
    # 从清单里读出图片名（图片必须一起传，否则端侧图块会被丢掉）
    imgs = []
    try:
        raw = json.loads((local / name).read_text(encoding="utf-8"))
        man = json.loads(__import__("base64").b64decode(raw["manifest_b64"]).decode("utf-8"))
        for e in man.get("entries") or []:
            for b in e.get("blocks") or []:
                if b.get("t") == "img" and b["name"] not in imgs:
                    imgs.append(b["name"])
    except Exception as e:
        check("清单可解析", False, f"{type(e).__name__}: {e}")
        return 2
    check("清单可解析", True, f"serial={man.get('serial')} 条目={len(man.get('entries') or [])}")
    miss = [i for i in imgs if not (local / i).is_file()]
    check("图片齐备", not miss, str(miss))
    if miss:
        return 2

    files = ["latest", name] + imgs
    tok = token()
    check("Gitee token 可读", bool(tok), TOKEN_FILE.name)
    check("latest 与 serial 一致",
          json.loads((local / "latest").read_text(encoding="utf-8")).get("serial") == a.serial)

    if not a.verify_only:
        if a.only != "gitee":
            push_server(local, files)
        if a.only != "server":
            if tok:
                push_gitee(local, files, tok)
            else:
                print("  ! 无 token，跳过 Gitee")
        time.sleep(6)   # Gitee 附件挂载是异步的

    verify(local, files)
    print(f"\n统计: PASS={PASS} FAIL={FAIL}")
    print("结果: " + ("PASS 两端可用" if FAIL == 0 else "FAIL 见上面 X"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
