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
  4. 平台六端点：广场 · 我的卡 · 已装（GET，断言 200）· 详情 · 草稿（先取真实 id 再 GET，断言 200）
     · `derive`（POST 存在性探针：接受 400/401/403，仅 404 判失败；不代表功能通过）
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
import urllib.parse
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
    print("\n=== 4. 平台六端点（每项判据见行末；全程单请求、不并发、不重试）===")
    # ⚠️ 端点对照 app/plaza/api.py 的 GET_ROUTES / POST_ROUTES，禁杜撰、禁重名：
    #    · `/plaza/api/derive` 是 **POST-only**（两份 GET_ROUTES 均无它、仅 POST_ROUTES 有：
    #      app/api/router.py:146 · app/plaza/api.py:1037）⇒ 发 GET 会落 do_GET() 的 404，
    #      故必须用 POST 探测（⑥ 为「存在性探针」，**不是**功能通过）。
    #    · `/plaza/api/card?id=` 缺 id ⇒ 400（api.py:231-233）；
    #      `/plaza/api/draft?id=` 缺 id ⇒ 404（api.py:788-791）⇒ 二者必须先取**真实 id**。
    # 判据两类：**200 断言**（无参即可 GET）· **存在性断言**（需参数/登录，仅 404 判失败）。

    # ① 广场 —— GET，判据 200；响应顺带供 ② 取真实 id（同一响应复用，不重复请求）
    st, body, err = http("GET", base + "/plaza/api/list", token)
    if err:
        rec("广场 GET /plaza/api/list（判据：200）", False, err, server_side=True)
        cards: list = []
    else:
        rec(f"广场 GET /plaza/api/list → {st}（判据：200）", st == 200,
            "" if st == 200 else body[:100].decode("utf-8", "ignore"),
            server_side=(st >= 500))
        cards = _json_items(body)

    # ② 详情 —— GET ?id=，判据 200；id 取自广场列表首张，列表为空则如实 SKIP（不伪装通过）
    cid = _first_id(cards)
    if cid:
        _probe_get_id("详情", "/plaza/api/card", cid, base, token)
    else:
        print("  - SKIP 详情 /plaza/api/card：广场列表为空（取不到 id）⇒ 无法构造 200 判据，不伪装通过")

    # ③ 我的卡 —— GET，判据 200；响应顺带供 ⑤ 取真实 id（同一响应复用，不重复请求）
    st, body, err = http("GET", base + "/plaza/api/drafts", token)
    if err:
        rec("我的卡 GET /plaza/api/drafts（判据：200）", False, err, server_side=True)
        drafts: list = []
    else:
        rec(f"我的卡 GET /plaza/api/drafts → {st}（判据：200）", st == 200,
            "" if st == 200 else body[:100].decode("utf-8", "ignore"),
            server_side=(st >= 500))
        drafts = _json_items(body)

    # ④ 已装 —— GET，判据 200
    _probe_get_200("已装", "/plaza/api/installed", base, token)

    # ⑤ 草稿 —— GET ?id=，判据 200；id 取自我的草稿列表首条，列表为空则如实 SKIP（不伪装通过）
    did = _first_id(drafts)
    if did:
        _probe_get_id("草稿", "/plaza/api/draft", did, base, token)
    else:
        print("  - SKIP 草稿 /plaza/api/draft：我的草稿列表为空（取不到 id）⇒ 无法构造 200 判据，不伪装通过")

    # ⑥ derive —— POST，判据「存在性」：接受 200/400/401/403，仅 404 判失败
    _probe_derive(base, token)


def _json_items(body: bytes) -> list:
    """从响应体解析 {…, 'items': [...]}；解析失败或形状不符返回 []。"""
    try:
        items = (json.loads(body.decode("utf-8")) or {}).get("items")
    except Exception:
        return []
    return items if isinstance(items, list) else []


def _first_id(items: list) -> str:
    """取 items[0]['id']；缺失或非字符串返回 ""。"""
    if items and isinstance(items[0], dict):
        v = items[0].get("id")
        if isinstance(v, str) and v:
            return v
    return ""


def _probe_get_200(name: str, path: str, base: str, token: str) -> None:
    """G4 项：无参 GET，判据「200 断言」。"""
    st, body, err = http("GET", base + path, token)
    if err:
        rec(f"{name} GET {path}（判据：200）", False, err, server_side=True)
        return
    rec(f"{name} GET {path} → {st}（判据：200）", st == 200,
        "" if st == 200 else body[:100].decode("utf-8", "ignore"),
        server_side=(st >= 500))


def _probe_get_id(name: str, path: str, cid: str, base: str, token: str) -> None:
    """G4 项：带 ?id= 的 GET，判据「200 断言」。"""
    url = f"{base}{path}?id={urllib.parse.quote(cid)}"
    st, body, err = http("GET", url, token)
    if err:
        rec(f"{name} GET {path}?id=…（判据：200）", False, err, server_side=True)
        return
    rec(f"{name} GET {path}?id=… → {st}（判据：200）", st == 200,
        "" if st == 200 else body[:100].decode("utf-8", "ignore"),
        server_side=(st >= 500))


def _probe_derive(base: str, token: str) -> None:
    """G4 项：`/plaza/api/derive` 是 POST-only ⇒ 用 POST 做「存在性探针」。

    判据：接受 200 / 400 / 401 / 403（端点可达，只是需要参数或登录）；**仅 404 判失败**。
    ⚠️ 这是「端点存在性」探针，**不代表** derive 业务功能通过。
    """
    st, body, err = http("POST", base + "/plaza/api/derive", token, b"{}",
                         {"Content-Type": "application/json"})
    if err:
        rec("derive POST /plaza/api/derive（判据：存在性，仅 404 失败）", False, err,
            server_side=True)
        return
    ok = st in (200, 400, 401, 403)
    detail = ("端点存在：可达但需参数/登录（存在性探针 ≠ 功能通过）" if ok
              else f"404 ⇒ 端点缺失：{body[:80].decode('utf-8', 'ignore')}")
    rec(f"derive POST /plaza/api/derive → {st}（判据：存在性，仅 404 失败）", ok, detail,
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
