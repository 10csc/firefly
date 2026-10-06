#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上兼容性冒烟（R-25）—— 七组判据，只读优先。

⛔ **本脚本在 R-14 部署之后才跑**；本轮（2026-10-04）只写不运行。
⛔ **禁止压测 / 攻击性测试**：每个端点只发一次请求，无并发、无重试风暴、不构造畸形包。

用法：
    python tools/smoke_online.py --base http://101.200.14.126:8787
    python tools/smoke_online.py --base <url> --token <登录令牌>   # 需要登录态的组才有效
    python tools/smoke_online.py --base <url> --allow-sync-write   # 第 6 组才真的写（默认跳过）

七组判据（见 docs/交接/PC端-交接执行计划/12_发布前合并任务单.md §四）：
  1. 登录与身份：`/auth/me` 与 `/auth/state` 都回 `role`（管理员能发官方卡）
  2. 公告：`/notice/*` 可拉、含 `refreshing` 语义
  3. 热更清单：`/hotupdate/<ver>/latest.json` 可达且版本正确（声明 none 时说明预期）
  4. 平台六端点 200：广场 / 详情 / 我的卡 / 已装 / 配额 / `derive`
  5. 图片资产：**自己包 200 · 他人包 404 · 未登录 401 · 全局资产 200**
  6. 数据同步：写入 → 拉回**一致**（会写生产数据 ⇒ 默认跳过，需 --allow-sync-write）
  7. 下载清单：`/version.json` 的 `tag` 与两个下载 URL **指向同一版本**（哈希回填之后）

输出：每组 PASS/FAIL + 原始片段；失败项会区分**服务端问题**（连不上 / 5xx / 超时）
      与**客户端问题**（解析失败 / 字段缺失）。
退出码：0 = 全通过；N = 失败组数（最多 7）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 10

GROUPS: list[str] = []
FAILS: list[str] = []


def http(method: str, url: str, token: str = "", body: bytes | None = None,
         headers: dict | None = None) -> tuple[int, bytes, str]:
    """返回 (status, body, err)。err 非空 ⇒ 连不上/超时/5xx 之外的传输层问题。"""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", "firefly-smoke/1.0")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(), ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b""), ""
    except Exception as e:                      # 超时 / 连不上 / DNS
        return -1, b"", f"{type(e).__name__}: {e}"


def rec(name: str, ok: bool, detail: str = "", server_side: bool = False) -> None:
    GROUPS.append(name)
    tag = "V" if ok else "X"
    kind = " [服务端问题]" if (not ok and server_side) else (" [客户端问题]" if not ok else "")
    print(f"  {tag} {name}{kind}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def g1_identity(base: str, token: str) -> None:
    print("=== 1. 登录与身份（/auth/me 与 /auth/state 都回 role）===")
    for path in ("/auth/me", "/auth/state"):
        st, body, err = http("GET", base + path, token)
        if err:
            rec(f"{path} 可达", False, err, server_side=True)
            continue
        rec(f"{path} 返回 {st}", st == 200, "" if st == 200 else body[:120].decode("utf-8", "ignore"),
            server_side=(st >= 500))
        if st == 200:
            try:
                d = json.loads(body.decode("utf-8"))
                rec(f"{path} 含 role 字段", "role" in d, f"role={d.get('role')!r}")
            except Exception as e:
                rec(f"{path} 可解析", False, str(e))


def g2_notice(base: str, token: str) -> None:
    print("\n=== 2. 公告（可拉 + refreshing 语义）===")
    st, body, err = http("GET", base + "/notice/latest", token)
    if err:
        rec("/notice/latest 可达", False, err, server_side=True)
        return
    rec(f"/notice/latest 返回 {st}", st in (200, 204), str(st), server_side=(st >= 500))
    if st == 200:
        try:
            d = json.loads(body.decode("utf-8"))
            has = any(k in json.dumps(d, ensure_ascii=False) for k in ("refreshing", "serial"))
            rec("公告响应含 serial/refreshing 语义", has,
                f"keys={list(d)[:6]}")
        except Exception as e:
            rec("公告可解析", False, str(e))


def g3_hotupdate(base: str, token: str) -> None:
    print("\n=== 3. 热更清单（/hotupdate/<ver>/latest.json）===")
    ver = _app_version()
    url = f"{base}/hotupdate/{ver}/latest.json"
    st, body, err = http("GET", url, token)
    dec = _declare()
    if err:
        rec(f"{url} 可达", False, err, server_side=True)
        print(f"    （声明 hotupdate={dec!r}；若为 none，404 属**预期**，不算失败）")
        return
    if st == 404 and str(dec).lower() == "none":
        print(f"    声明为 none ⇒ 404 属预期，判定为**通过（预期缺失）**")
        rec("热更清单状态与声明一致", True, "none ⇒ 无清单")
        return
    rec(f"热更清单返回 {st}", st == 200, str(st), server_side=(st >= 500))
    if st == 200:
        try:
            d = json.loads(body.decode("utf-8"))
            rec("清单 base 与当前版本一致", str(d.get("base")) == ver,
                f"{d.get('base')!r} vs {ver!r}")
        except Exception as e:
            rec("清单可解析", False, str(e))


def g4_platform(base: str, token: str) -> None:
    print("\n=== 4. 平台六端点 ===")
    # ⚠️ 端点必须是仓库里**真实存在**的（对照 app/plaza/api.py 的路由键）：
    #    曾误写 `/plaza/api/quota` —— 仓库无此端点（grep "/plaza/api/[a-z_]+" 无 quota）。
    #    安装配额（used_cards/used_bytes/max_cards/max_bytes）本就由 `/plaza/api/installed`
    #    **一并返回**（见 plaza_installed 的 docstring），故不再单列"配额"，改用另一真实端点
    #    `/plaza/api/draft`（草稿读取）—— 六项互不重复、无一杜撰。
    eps = [("广场", "/plaza/api/list"), ("详情", "/plaza/api/card"),
           ("我的卡", "/plaza/api/drafts"), ("已装", "/plaza/api/installed"),
           ("草稿", "/plaza/api/draft"), ("derive", "/plaza/api/derive")]
    for name, path in eps:
        st, body, err = http("GET", base + path, token)
        if err:
            rec(f"{name} {path}", False, err, server_side=True)
        else:
            rec(f"{name} {path} → {st}", st == 200,
                "" if st == 200 else body[:100].decode("utf-8", "ignore"),
                server_side=(st >= 500))


def g5_assets(base: str, token: str) -> None:
    print("\n=== 5. 图片资产（自己包 200 · 他人包 404 · 未登录 401 · 全局资产 200）===")
    print("    ⚠️ 需要真实包 id 才能构造 URL；未提供 --own-pack / --other-pack 时**只做全局资产**")
    # 全局资产（不依赖登录态）
    st, _, err = http("GET", base + "/assets/index", token)
    if err:
        rec("全局资产 /assets/index", False, err, server_side=True)
    else:
        rec(f"全局资产 /assets/index → {st}", st == 200, str(st), server_side=(st >= 500))
    # 自己包 / 他人包 / 未登录（需要参数）
    for label, path in (("自己包（应为 200）", _a.own), ("他人包（应为 404）", _a.other)):
        if not path:
            print(f"    - SKIP {label}：未提供对应包 id")
            continue
        st, _, err = http("GET", base + path, token)
        want = 200 if "自己包" in label else 404
        if err:
            rec(label, False, err, server_side=True)
        else:
            rec(f"{label} → {st}", st == want, f"期望 {want}", server_side=(st >= 500))
    if _a.own:
        st, _, err = http("GET", base + _a.own, "")     # 不带 token ⇒ 期望 401
        if err:
            rec("未登录访问自己包（应 401）", False, err, server_side=True)
        else:
            rec(f"未登录访问自己包 → {st}", st == 401, "期望 401", server_side=(st >= 500))


def g6_sync(base: str, token: str) -> None:
    print("\n=== 6. 数据同步：写入 → 拉回一致（往返一次）===")
    if not _a.allow_sync_write:
        print("    - SKIP：会写生产数据。加 --allow-sync-write 才执行（跑完需人工确认已清理）")
        return
    if not token:
        rec("同步往返", False, "需要 --token（同步接口要登录态）")
        return
    marker = "smoke_" + str(int(__import__("time").time()))
    body = json.dumps({"marker": marker}).encode("utf-8")
    st, _, err = http("POST", base + "/sync/upload", token, body,
                      {"Content-Type": "application/json"})
    if err:
        rec("写入 /sync/upload", False, err, server_side=True)
        return
    rec(f"写入 /sync/upload → {st}", st == 200, str(st), server_side=(st >= 500))
    st2, b2, err2 = http("GET", base + "/sync/download", token)
    if err2:
        rec("拉回 /sync/download", False, err2, server_side=True)
        return
    rec(f"拉回 /sync/download → {st2}", st2 == 200, str(st2), server_side=(st2 >= 500))
    if st2 == 200:
        rec("往返一致（marker 能拉回）", marker.encode() in b2,
            f"marker={marker}")


def g7_version(base: str, token: str) -> None:
    print("\n=== 7. 下载清单（/version.json 的 tag 与两个 URL 同版本）===")
    st, body, err = http("GET", base + "/version.json", token)
    if err:
        rec("/version.json 可达", False, err, server_side=True)
        return
    rec(f"/version.json → {st}", st == 200, str(st), server_side=(st >= 500))
    if st != 200:
        return
    try:
        d = json.loads(body.decode("utf-8"))
    except Exception as e:
        rec("/version.json 可解析", False, str(e))
        return
    tag = str(d.get("tag") or "")
    urls = [str(d.get(k) or "") for k in ("exe", "apk")]
    m = re.search(r"/(v?\d+\.\d+\.\d+)/", " ".join(urls))
    uver = m.group(1) if m else ""
    norm = lambda s: s.lstrip("v")
    rec("tag 与两个下载 URL 指向同一版本", bool(tag) and bool(uver) and norm(tag) == norm(uver),
        f"tag={tag!r} url版本={uver!r}")


def _app_version() -> str:
    try:
        return re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)',
                         (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")).group(1)
    except Exception:
        return "0.0.0"


def _declare() -> str:
    try:
        return str(json.loads((ROOT / "hotupdate_declare.json").read_text(encoding="utf-8"))
                   .get("hotupdate") or "")
    except Exception:
        return ""


def main() -> int:
    global _a
    ap = argparse.ArgumentParser(description="线上兼容性冒烟（R-25，部署后跑）")
    ap.add_argument("--base", required=True, help="线上基址，如 http://101.200.14.126:8787")
    ap.add_argument("--token", default="", help="登录令牌（身份/同步组需要）")
    ap.add_argument("--own-pack", default="", help="自己包的一个资产 URL 路径（用于 200/401 判据）")
    ap.add_argument("--other-pack", default="", help="他人包的资产 URL 路径（用于 404 判据）")
    ap.add_argument("--allow-sync-write", action="store_true",
                    help="第 6 组真的写生产数据（默认跳过）")
    _a = ap.parse_args()

    base = _a.base.rstrip("/")
    print(f"=== 线上兼容性冒烟（R-25）===  base={base}")
    print(f"    本地版本={_app_version()}  热更声明={_declare()!r}")
    print()

    g1_identity(base, _a.token)
    g2_notice(base, _a.token)
    g3_hotupdate(base, _a.token)
    g4_platform(base, _a.token)
    g5_assets(base, _a.token)
    g6_sync(base, _a.token)
    g7_version(base, _a.token)

    print()
    print(f"=== 统计：{len(GROUPS) - len(FAILS)}/{len(GROUPS)} 通过 ===")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print(f"  - {f}")
    print("结果: " + ("PASS" if not FAILS else f"FAIL（{len(FAILS)} 项）"))
    return len(FAILS)


_a: argparse.Namespace

if __name__ == "__main__":
    sys.exit(main())
