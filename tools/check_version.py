# -*- coding: utf-8 -*-
"""版本一致性检查：APP_VERSION / CURRENT_VERSION / versionName / AppVersion 四者必须一致。

用法: python tools/check_version.py
任何一处不一致 → 退出码 1（禁止发版）。
"""
import re
import sys
from pathlib import Path

# GBK 控制台（Windows 默认）打印 ✓ 会 UnicodeEncodeError → 强制 UTF-8（与 shared_http 同款兜底）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent

def extract(path: str, pattern: str, group: int = 1) -> str | None:
    fp = ROOT / path
    if not fp.exists():
        print(f"  X 文件缺失: {path}")
        return None
    m = re.search(pattern, fp.read_text(encoding="utf-8"))
    if not m:
        print(f"  X 未找到版本号: {path}（pattern={pattern}）")
        return None
    return m.group(group)

sources = {
    "app_config.APP_VERSION":  ("app/modules/app_config.py", r'APP_VERSION\s*=\s*"([^"]+)"'),
    "app.js CURRENT_VERSION":  ("app/static/app.js",         r'CURRENT_VERSION\s*=\s*"([^"]+)"'),
    "android versionName":     ("android/app/build.gradle.kts", r'versionName\s*=\s*"([^"]+)"'),
    "android versionCode":     ("android/app/build.gradle.kts", r'versionCode\s*=\s*(\d+)'),
    "iss AppVersion":          ("package/firefly.iss",        r'AppVersion=(\d+\.\d+\.\d+)'),
    # 服务器前端为 app/static 的同步副本（tools/sync_frontends.py）；version.json 为服务器版本源
    "server app.js CURRENT_VERSION":     ("server/frontend/app.js",               r'CURRENT_VERSION\s*=\s*"([^"]+)"'),
    "server version.json tag":           ("server/version.json",                  r'"tag"\s*:\s*"v?([^"]+)"'),
}

print("=== 版本一致性检查 ===")
versions = {}
ok = True
for name, (path, pat) in sources.items():
    v = extract(path, pat)
    if v is None:
        ok = False
    else:
        versions[name] = v
        print(f"  {name}: {v}")

# 一致性比较：版本号字符串（versionCode 是映射整数，单独走规则校验；
# version.json tag 提取时已去 v 前缀，直接参与比较）
version_values = {v for k, v in versions.items() if k != "android versionCode"}
if len(version_values) > 1:
    print("\n  X 版本不一致！")
    ok = False
elif version_values:
    print(f"\n  一致: {next(iter(version_values))} ✓")

# 版本格式检查：x.y.z 纯数字，禁止后缀（versionCode 是纯数字整数，不参与格式检查）
for name, v in versions.items():
    if name == "android versionCode":
        continue
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        print(f"  X {name}: '{v}' 不符合 x.y.z 纯数字格式（禁止 -beta/-rc 后缀）")
        ok = False

# versionCode 一致性：安卓覆盖升级要求单调递增。
# 历史最大值 800（0.8.0 整合版曾发布装机）；2026-08-14 起版本号回退显示 0.7.2，
# versionCode 不再按 versionName 映射（0.7.2 的 702 < 800 会导致无法覆盖安装），
# 改为校验：versionCode > PREV_VERSION_CODE（发版后手动更新本常量）。
PREV_VERSION_CODE = 800
if versions.get("android versionCode"):
    try:
        vc = int(versions["android versionCode"])
        if vc <= PREV_VERSION_CODE:
            print(f"  X android versionCode={vc} 未递增（须 > {PREV_VERSION_CODE}，否则无法覆盖安装）")
            ok = False
    except ValueError:
        print("  X android versionCode 解析失败")
        ok = False

print("\n结果:", "PASS 可发布" if ok else "FAIL 禁止发布")
sys.exit(0 if ok else 1)
