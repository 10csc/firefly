#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gitee 发行版 · 建/补 v0.9.0 并挂资产（APK + 公告）。

`docs/部署与发布约定.md §2`：**下载优先级 Gitee 第一，服务器第二**。
而 Gitee 侧此前只有到 v0.8.1 的 release —— 于是：
  · 整包兜底（"取 id 最大的 tag"）指向 v0.8.1 ⇒ **0.9.0 没有可下载的地址**；
  · 热更/公告的 Gitee 镜像根是 `.../download/v{base}` = `v0.9.0` ⇒ **404**。
本脚本一次解决两件事：建 v0.9.0 release（若不存在）+ 挂上该挂的资产。

**幂等**：release 已存在就复用；资产按名字去重，已存在就跳过（不重复上传）。
**不推送 git**：只碰 HTTP 资产。

    python tools/publish_gitee_release.py --apk android/firefly.apk
    python tools/publish_gitee_release.py --apk android/firefly.apk --notice hotupdate_dist/notice --serial 1
    python tools/publish_gitee_release.py --verify-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path.home() / ".config" / "firefly" / "gitee_token.txt"
OWNER_REPO = "cpt-asymmetry/firefly"
API = f"https://gitee.com/api/v5/repos/{OWNER_REPO}"
DL = f"https://gitee.com/{OWNER_REPO}/releases/download"
TAG = "v0.9.0"

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def token() -> str:
    """读 Gitee token（`utf-8-sig`：该文件带 BOM，BOM 不被 strip 会拼进 URL）。"""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    except Exception:
        return ""


def api_json(url: str, data: dict | None = None, method: str = "GET"):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": "FireflyPublish/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw.strip() else {}


def releases(tok: str) -> list:
    return api_json(f"{API}/releases?access_token={tok}&per_page=100") or []


def find_release(tok: str, tag: str) -> dict | None:
    for r in releases(tok):
        if str(r.get("tag_name")) == tag:
            return r
    return None


def assets_of(rid: int, tok: str) -> list:
    """★ 附件列表必须走 `/attach_files`（release 详情里的 assets 只有 name/url，没有 id）。"""
    try:
        return api_json(f"{API}/releases/{rid}/attach_files?access_token={tok}") or []
    except Exception:
        return []


def delete_asset(rid: int, asset_id: int, tok: str) -> bool:
    """删掉一个已挂的附件（**替换同名资产**必须先删：Gitee 不允许同名重复挂）。

    ⚠️ 附件 id 只能从 `GET /releases/{rid}/attach_files` 拿 —— release 详情里的 assets
       只有 name/browser_download_url，DELETE 会 404（2026-09-19 踩过）。
    """
    try:
        api_json(f"{API}/releases/{rid}/attach_files/{asset_id}?access_token={tok}",
                 method="DELETE")
        return True
    except Exception as e:
        print(f"    ! 删除附件失败 id={asset_id}: {type(e).__name__}: {e}")
        return False


def attach(rid: int, fp: Path, tok: str) -> bool:
    """用 `curl -F` 传（`docs/版本更新规范.md`：urllib 手拼 multipart 会 404）。"""
    r = subprocess.run(["curl.exe", "-s", "-X", "POST",
                        f"{API}/releases/{rid}/attach_files?access_token={tok}",
                        "-F", f"file=@{fp}"], capture_output=True, timeout=900)
    out = (r.stdout or b"").decode("utf-8", "replace")
    ok = '"name"' in out or '"id"' in out
    if not ok:
        print(f"    上传响应：{out[:200]}")
    return ok


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "FireflyPublish/1.0",
                                              "Cache-Control": "no-store"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser(description="Gitee 发行版建/补 v0.9.0")
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--apk", default="", help="要挂的 APK 路径")
    ap.add_argument("--exe", default="", help="要挂的安装器路径（可选）")
    ap.add_argument("--notice", default="", help="公告发布区目录（可选）")
    ap.add_argument("--serial", type=int, default=0, help="公告 serial")
    ap.add_argument("--body", default="", help="release 说明（仅新建时用）")
    ap.add_argument("--replace", action="store_true",
                    help="同名资产已存在时先删后传（0.9.0 重发用：同版本号换构建产物）")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()

    tok = token()
    if not tok:
        print(f"X 读不到 Gitee token：{TOKEN_FILE}")
        return 2

    print(f"=== ① 审查：目标 release {a.tag} ===")
    rel = find_release(tok, a.tag)
    if rel:
        print(f"  · 已存在（id={rel['id']}）—— 复用，不新建")
    else:
        print("  · 不存在 —— 将新建")
    print(f"  已有 release 数：{len(releases(tok))}")

    # 要挂的文件
    files = []
    if a.apk:
        p = Path(a.apk)
        check("APK 存在", p.is_file(), f"{p.name} {p.stat().st_size/1048576:.1f}MB" if p.is_file() else str(p))
        if p.is_file():
            files.append(p)
    if a.exe:
        p = Path(a.exe)
        check("安装器存在", p.is_file(), p.name)
        if p.is_file():
            files.append(p)
    if a.notice and a.serial:
        d = Path(a.notice)
        for n in ("latest", f"notice-{a.serial}.json"):
            p = d / n
            check(f"公告文件存在：{n}", p.is_file())
            if p.is_file():
                files.append(p)
        try:
            man = json.loads((d / f"notice-{a.serial}.json").read_text(encoding="utf-8"))
            import base64
            inner = json.loads(base64.b64decode(man["manifest_b64"]).decode("utf-8"))
            for e in inner.get("entries") or []:
                for b in e.get("blocks") or []:
                    if b.get("t") == "img" and (d / b["name"]).is_file():
                        files.append(d / b["name"])
        except Exception as e:
            check("公告清单可解析（取图片名）", False, f"{type(e).__name__}: {e}")

    if a.verify_only:
        files = []

    print(f"\n=== ② 处理：{'新建 release' if not rel else '复用 release'} + 挂 {len(files)} 个资产 ===")
    if not rel and not a.verify_only:
        body = a.body or (f"Firefly {a.tag} 发布。\n\n"
                          "· 应用更名为 Firefly（萤火虫）\n"
                          "· 新增自动修复（热更新）\n· 新增公告通道\n"
                          "详见应用内「公告 · 使用指南」。")
        rel = api_json(f"{API}/releases", {
            "access_token": tok, "tag_name": a.tag, "name": f"Firefly {a.tag}",
            "body": body, "target_commitish": "master", "prerelease": "false",
        }, method="POST")
        check("release 创建成功", bool(rel.get("id")), f"id={rel.get('id')}")
        if not rel.get("id"):
            print("  响应：", str(rel)[:300])
            print(f"\n统计: PASS={PASS} FAIL={FAIL}")
            return 1
    if not rel:
        print("X 目标 release 不存在，且 --verify-only 无法新建")
        return 2

    rid = int(rel["id"])
    have = {str(x.get("name")): x for x in assets_of(rid, tok)}
    print(f"  现有资产：{sorted(have) or '（空）'}")
    if files:
        for p in files:
            if p.name in have:
                if not a.replace:
                    print(f"  · 跳过（已存在）：{p.name}（要覆盖用 --replace）")
                    continue
                aid = have[p.name].get("id")
                print(f"  · 覆盖 {p.name}：先删旧附件 id={aid}")
                if aid and not delete_asset(rid, int(aid), tok):
                    print(f"  X 旧附件删不掉，跳过 {p.name}（避免留下两个同名资产）")
                    continue
                time.sleep(2)
            ok = attach(rid, p, tok)
            print(f"  {'V' if ok else 'X'} 上传 {p.name}（{p.stat().st_size/1048576:.2f}MB）")
        time.sleep(8)          # Gitee 附件挂载是异步的
        have = {str(x.get("name")): x for x in assets_of(rid, tok)}

    print("\n=== ③ 验证：逐个 URL 拉回来比 sha256 ===")
    want = [p for p in files] or []
    if not want:
        print("  （--verify-only 没带文件列表，只校验 release 存在与资产名）")
    for p in want:
        url = f"{DL}/{a.tag}/{p.name}"
        try:
            got = fetch(url)
            ok = sha256(got) == sha256(p.read_bytes())
            check(f"Gitee /{p.name}", ok, f"{len(got)}B sha {sha256(got)[:10]}…"
                  + ("" if ok else " ← 与本地不符"))
        except urllib.error.HTTPError as e:
            check(f"Gitee /{p.name}", False, f"HTTP {e.code}")
        except Exception as e:
            check(f"Gitee /{p.name}", False, f"{type(e).__name__}: {str(e)[:70]}")

    # 兜底链路关键点：整包更新取"id 最大的 tag"，必须就是本 tag
    newest = max(releases(tok), key=lambda r: int(r.get("id") or 0))
    check("★ id 最大的 release 就是本 tag（整包兜底不会取错）",
          str(newest.get("tag_name")) == a.tag, str(newest.get("tag_name")))

    print(f"\n统计: PASS={PASS} FAIL={FAIL}")
    print("结果: " + ("PASS" if FAIL == 0 else "FAIL 见上面 X"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
