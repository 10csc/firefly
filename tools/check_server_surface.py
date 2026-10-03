#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器对外面（surface）路由矩阵自检 —— 架构约束：**服务器只做后台**（2026-10-01 task-23）

用户定位：「不能直接在服务器使用 app 功能，服务器仅作为后台……只有角色卡平台（需要登录）」。
服务器只提供：**登录页 + 角色卡平台 + 客户端 API**；**不提供 App 前端**
（无 index.html / App bundle / App 样式）。

用法（只读、可重跑）：

```bash
python tools/check_server_surface.py                     # 起本地沙箱服务（真 server_app.py）跑矩阵
python tools/check_server_surface.py --base http://127.0.0.1:8765      # 打真实实例（SSH 隧道/本机）
python tools/check_server_surface.py --base http://<公网IP>:8787 --gateway   # 经网关（/ 是下载页）
python tools/check_server_surface.py --strict-platform   # 平台页未就位算 FAIL（部署后门禁）
```

判据（全绿 = 既没对外提供 App 前端，平台与 API 也一条不少）：
  · **App 壳** `/`、`/index.html`、`/index`、`/app.html` ⇒ **302 → /login.html**（8765 后台视角）
  · **App 资源** `/style.css`、`/pc.css`、`/js/bundle.js`、`/js/*`（平台白名单外）⇒ **404**
  · **平台** `/platform.html`、`/js/platform*`、`/js/panels/plaza*` ⇒ 200（未就位 ⇒ PENDING）
  · **登录/管理** `/login.html`、`/login` ⇒ 200；`/admin.html` 保持原状（token 闸在 8766）
  · **API 行为不变** `/health`、`/version.json`、`/update-manifest`、`/plaza/api/list`(未登录 401)
  · 信息行（不判定）：`/assets/`（App 静态资产仍公开，属已知残留，非本卡范围）

退出码：0 全绿（可含 PENDING）/ 1 有 FAIL / 2 沙箱服务没起来。
"""
import argparse
import json
import os
import shutil
import socket as _sk
import subprocess
import sys
import tempfile
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


# ── 路由矩阵：(路径, 期望状态集合 或 None=只观测, 说明) ──────────────
def matrix(gateway: bool):
    # 公网 `/`：网关对 `/` 发**规范重定向**到 `/download/`（既有行为，部署前就这样）⇒ 接受
    # {200, 301, 302}，真正的"下载页可达"由 main 里那条**跟随重定向后**的校验兜底（+ 体积量级）。
    # 8765 后台：`/` 必须是 302 → /login.html（服务器不再有 App 壳）。
    home = {"200", "301", "302"} if gateway else {"302"}
    return [
        ("/", home, "服务器不再有 App 壳" + ("（网关下载页）" if gateway else "（302→登录页）")),
        ("/login.html", {"200"}, "登录页必须可达（**绝不能被 App 黑名单误伤**）"),
        ("/login", {"200"}, "登录页别名"),
        ("/platform.html", {"200", "302"}, "角色卡平台页：200，或 302→login（未就位 ⇒ PENDING）"),
        ("/config.js", {"200"}, "登录页/平台页依赖"),
        ("/bg-sunset.css", {"200"}, "登录页/平台页依赖"),
        # 平台的 JS **全部打进 platform.bundle.js**（platform.html 只引 4 个资源），
        # 因此这里断言的是平台**实际**引用的文件 —— 不要写 /js/panels/*（那是 App 路径，
        # 2026-10-01 架构改造后服务器上不存在；写成 200 会让本门禁假红、挡住正确部署）。
        ("/platform.css", {"200"}, "平台样式（平台页实际引用）"),
        ("/platform.bundle.js", {"200"}, "平台 JS bundle（平台页实际引用；含 plaza 面板逻辑）"),
        ("/platform.js", {"200", "404"}, "平台入口源码：打进 bundle 后可不单独提供"),
        ("/index.html", {"302"}, "★ App 壳 → 登录页"),
        ("/index", {"302"}, "★ App 壳别名 → 登录页"),
        ("/app.html", {"302"}, "★ App 壳别名 → 登录页"),
        ("/style.css", {"404"}, "★ App 样式"),
        ("/pc.css", {"404"}, "★ App PC 样式"),
        ("/js/bundle.js", {"404"}, "★ App bundle"),
        ("/js/pc_shell.js", {"404"}, "★ App PC 壳脚本"),
        ("/js/panels/packs.js", {"404"}, "★ App 面板"),
        ("/js/panels/debug.js", {"404"}, "★ App 面板（另一个）"),
        ("/js/views.js", {"404"}, "★ App 模块"),
        ("/js/session_crypto.js", {"404"}, "★ App 加密头库（平台要用需加白名单/自带）"),
        ("/admin.html", {"401"}, "管理端：保持原状（401 = 未登录；token 闸在 8766）"),
        ("/static/js/bundle.js", {"404"}, "★ 本地版 App 静态目录也不对外（后门已堵）"),
        ("/assets/", None, "**保留**（客户端要用）：`app/static/js/util.js:107` stickerSrc 在服务器模式拼 "
                           "`apiBase + /assets/<file>`；packs/stickers/chat_render 都在调；"
                           "`test_plaza_use_card` 的 /assets/character/… 也走这里"),
    ]


# 基线里少数几项**与 8765 直连不可比 / 部署后才对齐**的（附理由；别把"尚未部署"当回归）
_BASELINE_GATEWAY_ONLY = {
    "/download/stats",          # 网关（8787）端点；直连 8765 必然 401（不是 App 面）
}
_BASELINE_POST_DEPLOY = {
    "/update-manifest": {"200", "503"},
}
# 这条的说明（别再写成"未部署的旧代码"——2026-10-01 部署后基线已刷新为 503）：
#   · **503 = 版本冻结下的设计内降级**：`update_manifest.load()` 对缺失 `update.json` 返回空清单，
#     app.py 据此 503（日志：`update.json 不存在 —— 检查更新将回退 version.json`）；
#   · `update.json` 是**发布清单**，用户明确要求线上冻结 v0.9.0 未发布 ⇒ **绝不部署它**
#     （传了客户端会看到"新版本"✗✗）；客户端此时回退 `version.json`（206B / v0.9.0）✓；
#   · ⇒ **发版前置动作**：只有正式发布那次才随 `update.json` 一起部署，届时应为 **200**。
_DELTA_NOTES = {
    "/update-manifest": "版本冻结期 503=设计内降级（不部署 update.json）；正式发布随清单部署后应为 200",
}


def _api_rows(baseline: dict, gateway: bool):
    """把 verifier 的基线 API 快照转成矩阵行（期望=基线值）——**API 行为未变**的硬证据。

    基线文件：`_harden/server_surface_baseline.json`（verifier task-10 产出，
    字段 `api_status` = {路径: 状态码}）。逐条**精确匹配**，任何一条变了都算 FAIL；
    少数"不可比/部署后才对齐"的按上面两张表显式豁免并注明理由。
    """
    out = []
    for path, status in (baseline.get("api_status") or {}).items():
        if path in _BASELINE_GATEWAY_ONLY and not gateway:
            out.append((path, None, f"信息：网关端点（8787），8765 直连不可比（基线 {status}）"))
            continue
        if path in _BASELINE_POST_DEPLOY:
            out.append((path, _BASELINE_POST_DEPLOY[path],
                        _DELTA_NOTES.get(path, f"部署后应对齐（基线 {status}）")))
            continue
        out.append((path, {str(status)}, f"API 行为未变（基线 {status}）"))
    return out


# ── 版本冻结核查（**只在 `--gateway` 打真机时**）：这些公开文档的**字节数**也必须不变 ──
# 背景（Lead 的部署纪律）：线上版本冻结在 v0.9.0 —— 部署 `server/app.py` 时**绝不许**带上
# `server/update.json`（否则 `/version.json` 会投影出新版本，客户端看到"有新版本"✗）。
# 线上 `/version.json` 是静态那份（基线实测 **206B**）；沙箱里没有线上那份（走仓库 update.json
# 投影 = 197B），故**沙箱模式跳过字节比对**，只比状态码。
_FROZEN_PATHS = ("/version.json", "/health")


def _frozen_expect(baseline: dict):
    """基线里冻结端点的 (路径 → 字节) —— 只取基线状态为 200 的那些。"""
    out = {}
    for r in baseline.get("rows") or []:
        if r.get("path") in _FROZEN_PATHS and r.get("status") == 200:
            out[r["path"]] = r.get("bytes")
    return out


# ── 全量枚举（verifier 提醒：别只抽 4 个样本）──────────────────────────
# 服务器侧的 App 前端文件：`server/frontend/` 下**除白名单**外的全部文件；
# 另有本地版 `app/static/` 的 App 文件（曾有 STATIC_DIR 回退后门）。
_APP_ALLOW = {"/login.html", "/login", "/platform.html", "/config.js", "/bg-sunset.css"}
_APP_ALLOW_PREFIXES = ("/platform", "/js/platform", "/js/panels/plaza")
_APP_SHELL = {"/index.html", "/index", "/app.html", "/"}


def _app_urls():
    """枚举两处 App 前端目录 → 期望"不可达"的 URL 列表（全量，不是抽样）。

    返回 `(urls, detail)`：`urls` 去重后排序；`detail` 是每棵树各贡献多少（用于打印）。
    """
    urls, detail = set(), []
    for base_dir, tag in ((ROOT / "server" / "frontend", "server/frontend"),
                          (ROOT / "app" / "static", "app/static")):
        if not base_dir.is_dir():
            continue
        n = 0
        for fp in sorted(base_dir.rglob("*")):
            if not fp.is_file() or "__pycache__" in fp.parts:
                continue
            url = "/" + fp.relative_to(base_dir).as_posix()
            if url in _APP_ALLOW or url.startswith(_APP_ALLOW_PREFIXES):
                continue
            urls.add(url)
            n += 1
        detail.append(f"{tag}={n}")
    return sorted(urls), detail


def _get(url: str, timeout: float = 15.0, follow: bool = False):
    """只读 GET：返回 (status, nbytes, headers)。

    · 默认**不跟随** 302（要看到 302 本身 —— App 壳判定靠它）；
    · `follow=True` 时才跟：网关对 `/` 发**规范重定向**到 `/download/`（这是网关既有行为，
      跟随后才是"下载页可达"的真实口径）。

    ⚠ 路径必须先 **percent-encode**：`app/static` 里有中文文件名（`开拓者_穹.png`），
    直接把非 ASCII 塞进 URL 会在 http.client 里抛 `UnicodeEncodeError: 'ascii' codec`，
    请求根本没发出去 —— 那会**假通过**（"没连上"被当成"不可达"）。
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = (urllib.request.build_opener() if follow
              else urllib.request.build_opener(_NoRedirect))
    safe = urllib.parse.quote(url, safe="/?=&%:#")     # 已编码的 %2F 等不再二次编码
    req = urllib.request.Request(safe, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
            return r.status, len(body), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, len(body), dict(e.headers or {})


def _free_port() -> int:
    s = _sk.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_sandbox():
    """起一个真 server_app.py（临时数据根 + 动态端口）；返回 (base, proc, log, sandbox)。"""
    sandbox = Path(tempfile.mkdtemp(prefix="ff_surface_"))
    env = dict(os.environ)
    env.update({"FIREFLY_ANDROID": "1", "FIREFLY_DATA_DIR": str(sandbox),
                "FIREFLY_PLAZA_DIR": str(sandbox / "plaza"),
                "FIREFLY_PORT": str(_free_port()), "PYTHONUNBUFFERED": "1",
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    log = open(sandbox / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{env['FIREFLY_PORT']}"
    for _ in range(80):
        try:
            s, _n, _h = _get(base + "/health", timeout=2)
            if s == 200:
                return base, proc, log, sandbox
        except Exception:
            pass
        time.sleep(0.5)
    print("沙箱服务未起来：", (sandbox / "server.log").read_text("utf-8", "replace")[-1200:])
    try:
        proc.kill()
    except Exception:
        pass
    log.close()
    return None, proc, log, sandbox


def main() -> int:
    ap = argparse.ArgumentParser(description="服务器对外面路由矩阵自检（服务器只做后台）")
    ap.add_argument("--base", default="", help="已有实例的基址（默认起本地沙箱服务）")
    ap.add_argument("--gateway", action="store_true", help="--base 经 8787 网关（/ 是下载页 → 200）")
    ap.add_argument("--strict-platform", action="store_true",
                    help="平台页/bundle 未就位时算 FAIL（部署后门禁用）")
    ap.add_argument("--baseline", default="", help="API 基线 JSON（默认 _harden/server_surface_baseline.json）")
    args = ap.parse_args()

    proc = log = sandbox = None
    base = args.base.rstrip("/")
    try:
        if not base:
            base, proc, log, sandbox = start_sandbox()
            if not base:
                return 2
        print(f"目标：{base}" + ("（经网关）" if args.gateway else "") + "\n")

        baseline_path = Path(args.baseline) if args.baseline else (ROOT / "_harden" / "server_surface_baseline.json")
        baseline = {}
        if baseline_path.is_file():
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  （基线读取失败，忽略：{e}）")
        api_rows = _api_rows(baseline, args.gateway)
        enum_urls, enum_detail = _app_urls()

        stats = {"ok": 0, "FAIL": 0, "PENDING": 0, "INFO": 0}

        def fetch(path):
            try:
                return _get(base + path)
            except Exception as e:
                return 0, 0, {"error": f"{type(e).__name__}: {e}"}

        def run_row(path, expect, note, mode="exact"):
            status, nbytes, headers = fetch(path)
            loc = headers.get("Location", "")
            expect_s = "≠200" if mode == "not200" else ("观测" if expect is None else "/".join(sorted(expect)))
            if mode == "not200":
                # ⚠ status==0 表示请求根本没发出去/连不上 —— **不能当成"不可达"放过**。
                ok = status not in (0, 200)
                verdict = "ok" if ok else "FAIL"
            elif expect is None:
                verdict = "INFO"
            elif str(status) in expect:
                verdict = "ok"
            elif path == "/platform.html" and not args.strict_platform and str(status) in ("401", "404"):
                verdict = "PENDING"      # 平台页还没就位（frontend task-22）；部署后加 --strict-platform
            else:
                verdict = "FAIL"
            stats[verdict] = stats.get(verdict, 0) + 1
            extra = f"  (Location={loc})" if loc else ""
            if status == 0:
                extra += f"  {headers.get('error', '')}"
            flag = "★" if "★" in note else " "
            print(f"  {flag}{path:<40}{status:>5}{nbytes:>9}  {expect_s:<12}{verdict:<8}{note}{extra}")

        print(f"  {'路径':<40}{'状态':>5}{'字节':>9}  {'期望':<12}判定    说明")
        print("  " + "-" * 118)
        print("  ── A. 关键路由（架构约束主线）──")
        for _p, _e, _n in matrix(args.gateway):
            run_row(_p, _e, _n)
        if args.gateway:
            # A2：`/` 的**跟随重定向**校验 —— 网关对 `/` 发规范重定向到 `/download/`，
            # 只有跟随后才是"下载页真的可达"（A 组接受 301/302，真正的断言在这里）。
            st_, n_, h_ = _get(base + "/", follow=True)
            ok = st_ == 200 and n_ >= 10 * 1024
            stats["ok" if ok else "FAIL"] = stats.get("ok" if ok else "FAIL", 0) + 1
            print(f"    {'/':<39}{st_:>5}{n_:>9}  {'200 且≥10KB':<12}"
                  f"{'ok' if ok else 'FAIL':<8}跟随重定向后=下载页（必须可达；实测 ~16KB）")
        if api_rows:
            print(f"\n  ── B. API 行为未变（基线逐条精确匹配：{baseline_path.name}"
                  f"，{baseline.get('generated_at', '')}）──")
            for _p, _e, _n in api_rows:
                run_row(_p, _e, _n)
        else:
            print("\n  ── B. API 基线缺失（跳过；verifier 的 _harden/server_surface_baseline.json 未找到）──")
        # ── B2. 版本冻结核查（只打真机时做；沙箱里没有线上那份 version.json）──
        if args.gateway:
            frozen = _frozen_expect(baseline)
            print("\n  ── B2. **版本冻结核查**（线上冻结 v0.9.0：字节数也必须不变）──")
            if not frozen:
                print("  （基线里没有 200 的 /version.json · /health，跳过）")
            for p, exp_bytes in sorted(frozen.items()):
                status, nbytes, _h = fetch(p)
                ok = status == 200 and (exp_bytes is None or nbytes == exp_bytes)
                verdict = "ok" if ok else "FAIL"
                stats[verdict] = stats.get(verdict, 0) + 1
                print(f"    {p:<29}{status:>5}{nbytes:>9}  ={exp_bytes}        {verdict}    "
                      f"版本冻结未被破坏（不许传 update.json）")
                if p == "/version.json" and status == 200:
                    try:
                        _s2, body, _h2 = _get(base + p)
                        tag = json.loads(body.decode("utf-8")).get("tag")
                        print(f"       ↳ tag = {tag}（应仍为线上 v0.9.0）")
                    except Exception as e:
                        print(f"       ↳ tag 解析失败：{e}")
        print(f"\n  ── C. **全量枚举**两处 App 前端目录 → 逐个断言不可达（≠200）："
              f"去重后 {len(enum_urls)} 个 URL（{'、'.join(enum_detail)}）──")
        for url in enum_urls:
            run_row(url, None, "App 前端文件（枚举）", mode="not200")

        passed, failed, pending = stats["ok"], stats["FAIL"], stats["PENDING"]
        print("  " + "-" * 118)
        print(f"\n  合计：ok={passed} FAIL={failed} PENDING={pending} INFO={stats['INFO']}"
              f"（枚举 {len(enum_urls)} 个 App 文件 + 基线 API {len(api_rows)} 条）"
              + ("\n        PENDING = 平台页未就位（frontend task-22）；部署后请加 --strict-platform 复跑" if pending else ""))
        print("  架构约束：**服务器只做后台** —— 只提供登录页 + 角色卡平台 + 客户端 API；")
        print("            不提供 App 前端（无 index.html / App bundle / App 样式）。")
        print("  量具说明：判定一律用 **GET**（本服务不支持 HEAD，HEAD 全 501 会被误读成'不可达'）。")
        if failed:
            print("\n  ✗ 有 FAIL：App 前端可能仍可访问，或平台/API 被误伤 —— 详见上表。")
            return 1
        print("\n  ✓ 全部符合预期。" + ("（含 PENDING，部署后再跑 --strict-platform）" if pending else ""))
        return 0
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if log is not None:
            log.close()
        if sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
