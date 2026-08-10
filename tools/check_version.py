# -*- coding: utf-8 -*-
"""版本一致性检查：APP_VERSION / CURRENT_VERSION / versionName / AppVersion 四者必须一致。

用法: python tools/check_version.py
任何一处不一致 → 退出码 1（禁止发版）。
"""
import re
import sys
from pathlib import Path

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
    "iss AppVersion":          ("package/firefly.iss",        r'AppVersion=([\d.]+)'),
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

if len(set(versions.values())) > 1:
    print("\n  X 版本不一致！")
    ok = False
elif versions:
    print(f"\n  一致: {next(iter(versions.values()))} ✓")

# 版本格式检查：x.y.z 纯数字，禁止后缀
for name, v in versions.items():
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        print(f"  X {name}: '{v}' 不符合 x.y.z 纯数字格式（禁止 -beta/-rc 后缀）")
        ok = False

print("\n结果:", "PASS 可发布" if ok else "FAIL 禁止发布")
sys.exit(0 if ok else 1)
