# -*- coding: utf-8 -*-
"""发版前闸：校验 **APK 里的前端与源文件一致**（防"增量构建给了旧前端"）。

为什么需要它（2026-09-20 事故，见 docs/错误总结.md #15）：
  改了 `app/static/js/*` 后跑**增量** `assembleRelease`，源文件与
  `android/app/src/main/assets/*` 都是新的，但 **APK 里的两份副本仍是旧的** ——
  Gradle 的 `Sync` 任务产物没被判失效，打包读到旧中间产物。
  所有信号都正常（`BUILD SUCCESSFUL`、五门禁全绿、`adb install` Success），
  **唯独用户拿到的是旧前端**。这次直接导致一个陈旧 APK 被发到 Gitee + 服务器并撤回。

判据：同一份前端文件在**三个位置**的 sha256 必须一致
  ① 源：`app/static/<f>`
  ② APK 直接资源：`assets/<f>`（服务器模式 / 旧路径会读它）
  ③ APK 内 `assets/chaquopy/app.imy` → `backend/app/static/<f>`（**安卓本地模式真正读的就是它**）

退出码 0 = 三处一致；1 = 有不一致/缺失（不许发版）。

用法：
    python tools/check_apk_frontend.py                      # 默认 android/firefly.apk
    python tools/check_apk_frontend.py --apk path/to.apk
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
# 前端产物里"会进包"的那几件（源码是唯一真相，其余都是副本）
FILES = ("index.html", "style.css", "pc.css", "js/bundle.js", "js/diag.js",
         "js/pc_shell.js", "js/notice.js")
# app.imy 内部可能的前缀（Chaquopy 把 src/main/python 当根，app/ 被同步成 backend/）
IMY_PREFIXES = ("backend/app/static/", "app/static/")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default=str(ROOT / "android" / "firefly.apk"))
    a = ap.parse_args()
    apk = Path(a.apk)
    if not apk.is_file():
        print(f"X 找不到 APK: {apk}")
        return 1

    z = zipfile.ZipFile(apk)
    names = set(z.namelist())
    imy_name = next((n for n in names if n.endswith("chaquopy/app.imy")), None)
    imy = zipfile.ZipFile(io.BytesIO(z.read(imy_name))) if imy_name else None
    imy_names = set(imy.namelist()) if imy else set()

    print(f"APK: {apk}  ({apk.stat().st_size/1048576:.2f} MB)")
    print(f"  app.imy: {imy_name or '(缺失)'}")
    print(f"\n{'文件':22s} {'源':10s} {'assets':10s} {'app.imy':10s} 判定")
    print("-" * 74)

    bad, checked = [], 0
    for rel in FILES:
        src = STATIC / rel
        if not src.is_file():
            print(f"{rel:22s} {'(源不存在，跳过)':<34s}")
            continue
        checked += 1
        s_src = sha(src.read_bytes())
        # ② APK 直接资源
        m2 = "assets/" + rel
        s_apk = sha(z.read(m2)) if m2 in names else None
        # ③ app.imy 内
        s_imy = None
        for pre in IMY_PREFIXES:
            cand = pre + rel
            if imy and cand in imy_names:
                s_imy = sha(imy.read(cand))
                break
        def mark(x):
            return "缺失" if x is None else x[:8]
        same = (s_apk == s_src) and (s_imy == s_src)
        # pc_shell.js 只在 assets 里（NON_BUNDLE，不进 app.imy 的 bundle 场景也应在）
        verdict = "一致 ✓" if same else "★不一致★"
        print(f"{rel:22s} {mark(s_src):10s} {mark(s_apk):10s} {mark(s_imy):10s} {verdict}")
        if not same:
            bad.append(rel)

    print("-" * 74)
    if bad:
        print(f"\nX 有 {len(bad)} 个文件在 APK 里与源**不一致**：{', '.join(bad)}")
        print("  → 说明这次打包用了陈旧中间产物。**不要发布**，先：")
        print("     cd android && gradlew.bat --no-daemon clean assembleRelease")
        return 1
    print(f"\nV {checked} 个前端文件在「源 / assets / app.imy」三处完全一致，可以发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())
