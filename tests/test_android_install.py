# -*- coding: utf-8 -*-
"""APP 内更新（检查更新 → 下载 → 交系统安装器）—— 跨端契约测试。

## 为什么要这个文件

这条链横跨**三端**，任何一端单独看都"没问题"，拼起来却不工作：

    Python 后端 routes_update.py   下载到固定名 firefly-update.apk，回执 {"name": …}
      → 前端 js/update.js          把 name 交给壳，壳自己去 cacheDir/update/ 找文件
        → Kotlin MainActivity      只认那个固定名 + FileProvider 白名单 → 系统安装器

2026-09-25 之前它**根本不工作**：安卓端下载完 85MB 之后只提示"去下载页再下一次"
（一个包下两遍、一次都不装）。这条链没法用单测跑通（需要真机装包），
所以这里用**源码级不变量**把关键约定钉住 —— 两侧改名字/改目录会立刻红，
而不是等到真机上"下载成功但提示找不到安装包"。

⚠️ 本文件**不替代真机验证**：系统安装器弹出的实际行为、未知来源授权引导、
覆盖安装后 user_data 是否保留，这三件只有真机能验（清单见 docs/版本更新规范.md §六）。
"""
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


ROUTES = (ROOT / "app" / "routes_update.py").read_text(encoding="utf-8")
UPDATE_JS = (ROOT / "app" / "static" / "js" / "update.js").read_text(encoding="utf-8")
MAIN = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "firefly"
        / "android" / "MainActivity.kt").read_text(encoding="utf-8")
MANIFEST = (ROOT / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
            ).read_text(encoding="utf-8")
FILE_PATHS = (ROOT / "android" / "app" / "src" / "main" / "res" / "xml" / "file_paths.xml"
              ).read_text(encoding="utf-8")

print("=== A. 固定文件名三端一致（改名字就必须三处一起改）===")
import routes_update as RU                                   # noqa: E402
check("A1 后端有 _DOWNLOAD_FILENAMES 契约常量",
      isinstance(getattr(RU, "_DOWNLOAD_FILENAMES", None), dict))
apk_name = RU._DOWNLOAD_FILENAMES.get("apk", "")
check("A2 apk 固定名是 firefly-update.apk", apk_name == "firefly-update.apk", apk_name)
check("A3 exe 固定名是 firefly-update.exe",
      RU._DOWNLOAD_FILENAMES.get("exe") == "firefly-update.exe")
check("A4 ★ Kotlin 侧同名字面量存在（两边漂移会红）", f'"{apk_name}"' in MAIN)
check("A5 固定名只有 basename（不含路径分隔符/上跳）",
      "/" not in apk_name and "\\" not in apk_name and ".." not in apk_name)

print("=== B. 后端：下载落点稳定 + 回执带 name ===")
check("B1 下载用固定目标而不是随机临时名",
      "tempfile.gettempdir()" in ROUTES and "_DOWNLOAD_FILENAMES[kind]" in ROUTES)
check("B2 不再用 NamedTemporaryFile(delete=False) 随机名",
      "NamedTemporaryFile" not in ROUTES)
check("B3 ★ apk 回执里带 name", 'h._json({"ok": True, "path": local, "name":' in ROUTES)
check("B4 exe/installing 分支也带 name",
      ROUTES.count('"name": _DOWNLOAD_FILENAMES[kind]') >= 2,
      f"出现 {ROUTES.count('\"name\": _DOWNLOAD_FILENAMES[kind]')} 次")
check("B5 下载失败时清理残留文件（不放半个包冒充成品）",
      "unlink(missing_ok=True)" in ROUTES)

print("=== C. Kotlin：只认固定名 + 只从 cacheDir/update/ 取 ===")
check("C1 桥暴露 installApk（@JavascriptInterface）",
      "@JavascriptInterface" in MAIN and "fun installApk(" in MAIN)
check("C2 ★ installApk 拒绝非契约文件名（白名单比对，不是直接拼路径）",
      "want != ANDROID_UPDATE_APK" in MAIN and "拒绝：未知的安装包名" in MAIN)
check("C3 ★ 路径由壳自己拼：cacheDir/update/ + 固定名",
      'java.io.File(java.io.File(cacheDir, UPDATE_DIR), ANDROID_UPDATE_APK)' in MAIN)
check("C4 文件不存在/过小时如实报错（不假装成功）",
      "没找到下好的安装包" in MAIN)
check("C5 小文件下限存在（防错误页冒充 APK）", "64 * 1024" in MAIN)

print("=== D. Kotlin：安装意图正确（否则系统安装器起不来）===")
check("D1 用 ACTION_VIEW + package-archive MIME",
      'Intent.ACTION_VIEW' in MAIN and "application/vnd.android.package-archive" in MAIN)
check("D2 ★ 用 FileProvider 的 content://（Android 7+ 的 file:// 会直接抛）",
      "FileProvider.getUriForFile" in MAIN
      and '"com.firefly.android.fileprovider"' in MAIN)
check("D3 带 FLAG_GRANT_READ_URI_PERMISSION", "FLAG_GRANT_READ_URI_PERMISSION" in MAIN)
check("D4 ★ 同时给 clipData（部分安装器只从 ClipData 取读权限）",
      "clipData = android.content.ClipData.newUri" in MAIN)
check("D5 Android 8+ 未授权时引导到「安装未知应用」页",
      "canRequestPackageInstalls()" in MAIN
      and "Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES" in MAIN)
check("D6 用 startActivityForResult 以便回执「用户取消」",
      "startActivityForResult(it, APK_INSTALL_REQUEST)" in MAIN)
check("D7 onActivityResult 处理安装请求码并告知取消",
      "requestCode == APK_INSTALL_REQUEST" in MAIN and "已取消安装" in MAIN)
check("D8 请求码不与既有请求码撞号",
      "APK_INSTALL_REQUEST = 1004" in MAIN and "MEDIA_PERMISSION_REQUEST = 1003" in MAIN)

print("=== E. Kotlin：壳内下载只对本地后端放行（不做出网下载器）===")
check("E1 有更新包判定函数", "fun isUpdatePackage(" in MAIN)
check("E2 ★ 只放行 127.0.0.1 / localhost",
      'host == "127.0.0.1" || host == "localhost"' in MAIN)
check("E3 用 .part 再改名（避免半个包被安装器读到）",
      '"$ANDROID_UPDATE_APK.part"' in MAIN and "renameTo(f)" in MAIN)
check("E4 下载有大小上限与下限",
      "100L * 1024 * 1024" in MAIN and "64L * 1024" in MAIN)
check("E5 DownloadListener 对更新包分叉处理",
      "if (isUpdatePackage(mimeType, contentDisposition, url))" in MAIN)

print("=== F. 权限与 FileProvider 白名单 ===")
check("F1 ★ manifest 声明 REQUEST_INSTALL_PACKAGES",
      "android.permission.REQUEST_INSTALL_PACKAGES" in MANIFEST)
check("F2 ★ file_paths 暴露 cacheDir/update/（少了它安装器读不到文件）",
      'path="update/"' in FILE_PATHS)
check("F3 白名单仍然是最小的（只有 diagnostics + update）",
      FILE_PATHS.count("<cache-path") == 2
      and "external-path" not in FILE_PATHS and "root-path" not in FILE_PATHS)
check("F4 没有为省事放开整个 cacheDir",
      'path="."' not in FILE_PATHS)

print("=== G. 前端：不再「下两遍」，且失败要说人话 ===")
check("G1 ★ update.js 调壳安装", "installApk(" in UPDATE_JS)
check("G2 有壳缺失时的降级说明（不是静默什么都不做）",
      "无法自动安装" in UPDATE_JS)
check("G3 壳返回错误时如实显示 + 给下载页兜底",
      "自动安装未启动" in UPDATE_JS and "也可前往下载页手动安装" in UPDATE_JS)
check("G4 ★ 旧文案已删（引导用户去下载页再下一次 = 下两遍）",
      "请从" not in UPDATE_JS and "下载 APK 安装" not in UPDATE_JS)
check("G5 成功文案明确「系统弹窗里点安装」",
      "请在系统弹窗里点「安装」" in UPDATE_JS)
check("G6 PC 静默安装那条路没被改坏",
      'data.installing' in UPDATE_JS and "/VERYSILENT" in ROUTES)

print("=== H. 产物级：前端 bundle 与三端同步副本 ===")
BUNDLE = (ROOT / "app" / "static" / "js" / "bundle.js").read_text(encoding="utf-8")
check("H1 bundle 里已含新逻辑（否则手机跑的是旧前端）", "installApk(" in BUNDLE)
for rel in ("server/frontend/js/update.js", "server/frontend/js/bundle.js",
            "android/app/src/main/assets/js/update.js",
            "android/app/src/main/assets/js/bundle.js"):
    fp = ROOT / rel
    ok = fp.is_file() and "installApk(" in fp.read_text(encoding="utf-8")
    check(f"H2 {rel} 已同步新逻辑", ok)

print("=== I. 契约常量与注释里的口径一致（防「注释说 A、代码做 B」）===")
m = re.search(r'ANDROID_UPDATE_APK = "([^"]+)"', MAIN)
check("I1 Kotlin 常量与后端字典逐字相同", m is not None and m.group(1) == apk_name,
      f"Kotlin={m.group(1) if m else None} Python={apk_name}")
m2 = re.search(r'UPDATE_DIR = "([^"]+)"', MAIN)
check("I2 缓存子目录名与 file_paths 白名单一致",
      m2 is not None and f'path="{m2.group(1)}/"' in FILE_PATHS,
      f"dir={m2.group(1) if m2 else None}")

print("=== J. 运行版本三元组：用户能一句话说清自己在跑哪份代码 ===")
INDEX = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
SETTINGS = (ROOT / "app" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
DIAG = (ROOT / "app" / "api" / "diag.py").read_text(encoding="utf-8")
check("J1 设置页有运行版本行与复制按钮",
      'id="running-id-text"' in INDEX and 'id="copy-running-id-btn"' in INDEX)
check("J2 ★ 复制按钮已接线（不是摆样子的）",
      'getElementById("copy-running-id-btn")' in SETTINGS
      and 'addEventListener("click"' in SETTINGS)
check("J3 老 WebView 无 clipboard API 时有降级提示",
      "复制失败，请手动记下" in SETTINGS and "navigator.clipboard" in SETTINGS)
check("J4 loadConfig 顺手刷新三元组（打开设置即可见）",
      "_renderRunningInfo(data.running)" in SETTINGS)
check("J5 三层信息都渲染（base/hot序列/指纹），不是只显示版本号",
      "hot_serial" in SETTINGS and "patch_hash" in SETTINGS and "layer" in SETTINGS)
check("J6 ★ 三元组进诊断包（顶层 running 字段）",
      'd["running"] = _hu_running()' in DIAG and "running_id" in DIAG)
check("J7 README 里说明了 running 的用途（用户敢发、我收得明白）",
      "运行版本三元组" in DIAG)
check("J8 bundle 里已含复制逻辑（否则手机端是旧前端）",
      "copyRunningId" in BUNDLE)
for rel in ("server/frontend/index.html", "android/app/src/main/assets/index.html",
            "server/frontend/js/settings.js", "android/app/src/main/assets/js/settings.js"):
    fp = ROOT / rel
    ok = fp.is_file() and ("running-id" in fp.read_text(encoding="utf-8")
                           or "copy-running-id" in fp.read_text(encoding="utf-8"))
    check(f"J9 {rel} 已同步", ok)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
