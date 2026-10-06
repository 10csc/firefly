#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载页产物一致性门禁 —— 发布前查「用户在下载页拿到的东西 = 我们发布的产物」。

为什么需要这道闸（2026-10-04 立，0.9.1 发布交付）：
  · 历史上下载页出过两类真事故：① **入口指向压缩包**（Gitee 对 .apk 以 zip MIME 下发，
    部分浏览器直接存成 `.zip`，用户拿到一个"装不上的压缩包"——详见 download_server.py 的
    `_send_file` 注释与 download_page.py 的 `poorBlobUa` 分支）；② **入口版本 ≠ 当前版本**
    （页头写着新版本，按钮却还挂在上一版 release 上，属"改了但没生效"的假绿）。
  这两类都不是靠人盯能长期守住的 —— 所以固化成门禁：每次发布自动查下载页，不靠人记。

判据（4 条，任一不通过即退出码非零）：
  ① 页面上的下载入口**不得是 zip/压缩包**（href/src 里不得出现 `.zip`/`.tar.gz` 等
     指向安装包的压缩链接）—— 用户要下的是能直接安装的 `.apk`/`.exe`。
  ② 入口指向**当前版本**：页头显示的版本号 == 期望 tag（默认取本地 `server/version.json`
     的 `tag`，可用 `--expect-tag` 覆盖）。版本号一律去掉前导 `v` 再比。
  ③ 每个入口**可达**（HTTP 200）且返回**正确的产物类型**（`.apk` ⇒
     `application/vnd.android.package-archive`，或**至少不是 zip**；`.exe` ⇒ 可执行 MIME）。
  ④ 下载到的字节 **sha256 与发布物清单逐条一致**（清单格式见
     `_harden/release_manifest_<ver>.json`：`{"version","version_code","artifacts":[{"name",
     "path","size","sha256"}]}`；下载页按 `name` 取 `/download/<name>`）。

设计约束（与仓库既有 tools 门禁同款）：
  · **标准库优先**（urllib/hashlib/json/re），不引第三方依赖；
  · **只读、无副作用**：只 GET，不写远端、不并发、不重试风暴（单请求、串行）；
  · 失败信息可归因：区分「连不上/5xx（服务端问题）」与「解析/字段不符（产物问题）」。

用法：
    python tools/check_download_page.py --base http://101.200.14.126:8787 \
        --manifest _harden/release_manifest_0.9.1.json
    python tools/check_download_page.py --base <url> --manifest <json> --expect-tag v0.9.1

退出码：0 = 全通过；N = 失败条数（≥1 即非零）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:  # Windows GBK 控制台打印 ✓ 会 UnicodeEncodeError（与 check_version 同款兜底）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent

# 会被当成"压缩包下载"的扩展名（大小写不敏感，按去 query/hash 后的路径比较）
ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz", ".tar", ".rar", ".7z", ".gz", ".bz2", ".xz")

# 各产物应有的 MIME（③）。`.apk` 的首选值来自 download_server._send_file 的实测基线。
APK_BEST = "application/vnd.android.package-archive"
# `.exe` 可接受的"可执行" MIME（Inno/直连常见值；octet-stream 作兜底但仍非压缩/文本即算过）
EXE_OK = {
    "application/vnd.microsoft.portable-executable",
    "application/x-msdownload",
    "application/x-msdos-program",
    "application/octet-stream",
}

# 页面里下载入口的锚点：id=dl-exe/dl-apk 或 href 落在 /download/<file>
ENTRY_HREF_RE = re.compile(r'href="(/download/[^"?#]+)[^"]*"')
ENTRY_ID_RE = re.compile(r'id="dl-(exe|apk)"[^>]*href="([^"]*)"')
# 页头版本号：`当前版本 v0.9.1 ｜ 累计下载 N 次`
VERSION_RE = re.compile(r"(?:当前版本|最新)\s*(v?\d+\.\d+\.\d+)")

FAILS: list[str] = []


def _fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  X {msg}")


def _norm_tag(s: str) -> str:
    """版本号归一：去空白 + 去前导 v，便于 `v0.9.1` 与 `0.9.1` 相等比较。"""
    return (s or "").strip().lstrip("vV")


def _get(url: str, timeout: int) -> tuple[int, dict, bytes, str]:
    """单次 GET。返回 (status, headers, body, err)。err 非空 ⇒ 传输层问题（连不上/超时）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "firefly-downloadpage-gate/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), dict(r.headers.items()), r.read(), ""
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), (e.read() or b""), ""
    except Exception as e:
        return -1, {}, b"", f"{type(e).__name__}: {e}"


def _download_and_hash(url: str, timeout: int) -> tuple[int, dict, int, str, str]:
    """流式下载并边下边算 sha256（不把整包读进内存）。

    返回 (status, headers, size, sha256, err)。err 非空 ⇒ 连不上/超时。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "firefly-downloadpage-gate/1.0"})
    h = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st = getattr(r, "status", 200)
            hdr = dict(r.headers.items())
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
            return st, hdr, size, h.hexdigest(), ""
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), 0, "", ""
    except Exception as e:
        return -1, {}, 0, "", f"{type(e).__name__}: {e}"


def _content_type(headers: dict) -> str:
    """取 Content-Type，去掉 charset 参数并小写（`type/subtype`）。"""
    raw = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            raw = v
            break
    return raw.split(";", 1)[0].strip().lower()


def _is_zip_like(mime: str) -> bool:
    return "zip" in mime or mime in {"application/x-compressed", "multipart/x-zip"}


def _is_text_like(mime: str) -> bool:
    return (not mime) or mime.startswith("text/") or "html" in mime or "json" in mime


def check_no_archive_entries(html: str) -> None:
    """① 页面上不得有指向压缩包的下载入口。"""
    print("\n=== ① 下载入口不是 zip/压缩包 ===")
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', html)
    offenders = []
    for href in hrefs:
        path = href.split("?", 1)[0].split("#", 1)[0].lower()
        if path.endswith(ARCHIVE_EXTS):
            offenders.append(href)
    # 安装包入口必须是 .apk/.exe，不得被换成压缩包后缀
    entries = ENTRY_HREF_RE.findall(html)
    bad_entries = [e for e in entries
                   if e.split("?", 1)[0].split("#", 1)[0].lower().endswith(ARCHIVE_EXTS)]
    if offenders or bad_entries:
        _fail(f"页面存在指向压缩包的链接（用户会下到装不上的包）: {sorted(set(offenders + bad_entries))}")
    else:
        print(f"  V 未发现压缩包下载入口（安装包入口 {sorted(set(entries))} 均为 .apk/.exe）")


def check_version(html: str, expect_tag: str) -> None:
    """② 页面显示版本 == 期望 tag。"""
    print(f"\n=== ② 入口指向当前版本（期望 {expect_tag!r}）===")
    found = VERSION_RE.findall(html)
    if not found:
        _fail("页面里找不到版本号（`当前版本 x.y.z` / `最新 x.y.z`）")
        return
    want = _norm_tag(expect_tag)
    norms = {_norm_tag(v) for v in found}
    print(f"  页面版本: {sorted(norms)}   期望: {want!r}")
    if norms != {want}:
        _fail(f"页面版本 {sorted(norms)} 与期望 {want!r} 不一致（入口指向了别的版本）")
    else:
        print("  V 页面版本与期望一致")


def check_manifest_artifacts(base: str, manifest: dict, timeout: int) -> None:
    """③④ 逐个清单产物：可达 + MIME 正确 + 字节 sha256/size 一致。"""
    print("\n=== ③④ 入口可达 / 产物类型 / 字节哈希 ===")
    arts = manifest.get("artifacts") or []
    if not arts:
        _fail("发布物清单里 artifacts 为空（无法核对字节）")
        return
    for a in arts:
        name = str(a.get("name") or "").strip()
        if not name:
            _fail(f"清单条目缺 name 字段: {a!r}")
            continue
        url = base + "/download/" + name
        print(f"\n  · {name}  ← {url}")
        st, hdr, size, sha, err = _download_and_hash(url, timeout)
        if err:
            _fail(f"{name} 不可达（服务端问题）: {err}")
            continue
        if st != 200:
            _fail(f"{name} 返回 {st}（期望 200；可达性不通过，服务端问题）")
            continue
        # ③ 产物类型
        mime = _content_type(hdr)
        low = name.lower()
        if low.endswith(".apk"):
            if _is_zip_like(mime):
                _fail(f"{name} 的 Content-Type 是压缩包 {mime!r}"
                      f"（安卓浏览器会存成 .zip ⇒ 装不上；期望 {APK_BEST}）")
            elif _is_text_like(mime):
                _fail(f"{name} 的 Content-Type 是文本 {mime!r}（不是安装包）")
            elif mime == APK_BEST:
                print(f"    V 类型 {mime}")
            else:
                print(f"    ~ 类型 {mime}（非首选 {APK_BEST}，但非 zip，按「至少不是 zip」放行）")
        elif low.endswith(".exe"):
            if _is_zip_like(mime) or _is_text_like(mime):
                _fail(f"{name} 的 Content-Type {mime!r} 不是可执行类型")
            elif mime in EXE_OK or "executable" in mime or "msdownload" in mime:
                print(f"    V 类型 {mime}")
            else:
                _fail(f"{name} 的 Content-Type {mime!r} 不是可执行 MIME")
        else:
            _fail(f"{name} 的后缀既不是 .apk 也不是 .exe（下载页不该出现此类入口）")
        # ④ 字节一致
        want_sha = str(a.get("sha256") or "").strip().lower()
        want_size = a.get("size")
        if want_sha:
            if sha.lower() != want_sha:
                _fail(f"{name} sha256 不一致：线上 {sha} ≠ 清单 {want_sha}")
            else:
                print(f"    V sha256 一致 {sha}")
        else:
            _fail(f"{name} 清单缺 sha256，无法核对字节")
        if isinstance(want_size, int) and want_size >= 0:
            if size != want_size:
                _fail(f"{name} 大小不一致：线上 {size} B ≠ 清单 {want_size} B")
            else:
                print(f"    V 大小一致 {size} B")
        else:
            _fail(f"{name} 清单缺 size（或非整数），无法核对大小")


def check_entries_in_manifest(html: str, manifest: dict) -> None:
    """每个页面入口都必须在清单里（否则该入口的字节无从核对）。"""
    print("\n=== 入口 ↔ 清单 覆盖 ===")
    entries = sorted({e.rsplit("/", 1)[-1] for e in ENTRY_HREF_RE.findall(html)})
    names = {str(a.get("name") or "") for a in (manifest.get("artifacts") or [])}
    missing = [e for e in entries if e not in names]
    if not entries:
        _fail("下载页里没有 /download/ 入口锚点（页面结构变了？）")
    if missing:
        _fail(f"页面入口不在发布物清单里（无法核对字节）: {missing}")
    if entries and not missing:
        print(f"  V 页面入口 {entries} 均在清单中")


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="下载页产物一致性门禁（发布前查：用户下的 = 我们发的）")
    ap.add_argument("--base", required=True, help="下载站基址，如 http://101.200.14.126:8787")
    ap.add_argument("--manifest", required=True, help="发布物清单 json（_harden/release_manifest_<ver>.json）")
    ap.add_argument("--expect-tag", default="", help="期望版本 tag（默认取 server/version.json 的 tag）")
    ap.add_argument("--timeout", type=int, default=60, help="单请求超时秒数（默认 60）")
    args = ap.parse_args(argv[1:])

    base = args.base.rstrip("/")
    if base.endswith("/download"):
        base = base[: -len("/download")]

    # 期望版本：--expect-tag 优先，否则读本地 server/version.json
    expect_tag = args.expect_tag.strip()
    if not expect_tag:
        try:
            vj = json.loads((ROOT / "server" / "version.json").read_text(encoding="utf-8"))
            expect_tag = str(vj.get("tag") or "")
        except Exception as e:
            print(f"  X 读不到 server/version.json 的 tag：{e}（可用 --expect-tag 指定）")
            return 1

    # 发布物清单
    mpath = Path(args.manifest)
    if not mpath.is_absolute():
        mpath = ROOT / mpath
    try:
        raw = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"X 读不到发布物清单 {mpath}: {e}")
        return 1
    if isinstance(raw, list):       # 容忍裸列表形态
        raw = {"artifacts": raw}

    print("=== 下载页产物一致性门禁 ===")
    print(f"  base={base}  manifest={mpath}  expect-tag={expect_tag!r}  timeout={args.timeout}s")

    page_url = base + "/download/"
    print(f"\n--- 拉取下载页 {page_url} ---")
    st, _hdr, body, err = _get(page_url, args.timeout)
    if err:
        print(f"X 下载页不可达（服务端问题）: {err}")
        return 1
    if st != 200:
        print(f"X 下载页返回 {st}（期望 200）")
        return 1
    html = body.decode("utf-8", "replace")
    print(f"  页面 {len(body)} 字节")

    check_no_archive_entries(html)
    check_version(html, expect_tag)
    check_entries_in_manifest(html, raw)
    check_manifest_artifacts(base, raw, args.timeout)

    print("\n=== 结果 ===")
    if FAILS:
        print(f"FAIL（{len(FAILS)} 项）：")
        for f in FAILS:
            print(f"  - {f}")
        return len(FAILS)
    print("PASS 下载页与发布物一致 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
