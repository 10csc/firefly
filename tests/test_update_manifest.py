# -*- coding: utf-8 -*-
"""版本清单（update.json）+ 检查更新逻辑测试。

覆盖 2026-09-24 的检查更新改造：
  A. update_manifest 的审查/校验（格式非法一律降级，绝不抛）
  B. version.json 兼容投影形状（老客户端读它，字段不能变）
  C. ★ "先成功者胜 → 按版本取最大"（GitHub 停在旧版时不再吞掉 Gitee 的新版）
  D. 线上真实 update.json 的自洽性
"""

import json
import sys
import os
from pathlib import Path

_base = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_base / "app"))
sys.path.insert(0, str(_base / "server"))

from update_manifest import (normalize_tag, parse_version, compare, sanitize,
                             load, project_version_json, invalidate)  # noqa: E402

PASS, FAIL = 0, 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  V", desc)
    else:
        FAIL += 1
        print("  X", desc)


# ══════════════════════════════════════════════════
print("=== A. 版本号解析与比较 ===")
check("A1 normalize_tag 去 v 前缀", normalize_tag("v0.9.1") == "0.9.1")
check("A2 normalize_tag 无前缀原样", normalize_tag("0.9.1") == "0.9.1")
check("A3 normalize_tag 拒绝非数字段", normalize_tag("0.9.1-beta") == "")
check("A4 normalize_tag 拒绝空/None", normalize_tag("") == "" and normalize_tag(None) == "")
check("A5 normalize_tag 拒绝路径穿越", normalize_tag("v../../x") == "")
check("A6 parse_version", parse_version("1.2.3") == (1, 2, 3))
check("A7 parse_version 非法→0", parse_version("abc") == (0, 0, 0))
check("A8 compare 大于", compare("0.9.1", "0.8.1") > 0)
check("A9 compare 相等", compare("v0.9.0", "0.9.0") == 0)
check("A10 compare 小于", compare("0.9.0", "0.10.0") < 0)   # 数字比较，不是字符串
check("A11 compare 数字序（0.9 < 0.10）", compare("0.9.0", "0.10.0") < 0)

# ══════════════════════════════════════════════════
print("\n=== B. sanitize 审查（脏数据不崩、降级正确）===")
s = sanitize(None)
check("B1 None → 合法空清单", isinstance(s, dict) and s["tag"] == "" and s["assets"] == {})
s = sanitize("not a dict")
check("B2 非 dict → 合法空清单", isinstance(s, dict) and s["tag"] == "")

s = sanitize({
    "tag": "v0.9.1",
    "assets": {
        "apk": {"url": "https://gitee.com/x/firefly.apk",
                "sha256": "A" * 64, "size": 123},
        "exe": {"url": "http://insecure/x.exe"},          # http → 丢弃 url
    },
})
check("B3 正常 tag 被采纳", s["tag"] == "0.9.1")
check("B4 https 资产收下", s["assets"]["apk"]["url"].endswith(".apk"))
check("B5 sha256 归一为小写", s["assets"]["apk"]["sha256"] == "a" * 64)
check("B6 size 收下", s["assets"]["apk"]["size"] == 123)
check("B7 ★ http 资产 URL 被拒（防降级劫持）", "exe" not in s["assets"])

s = sanitize({"tag": "v0.9.1", "assets": {"apk": {"url": "https://x/y.apk",
                                                 "sha256": "zz", "size": -5}}})
check("B8 非法 sha256 丢弃（不崩）", "sha256" not in s["assets"]["apk"])
check("B9 负数 size 丢弃", "size" not in s["assets"]["apk"])

s = sanitize({"tag": "v0.9.1", "notes_url": "javascript:alert(1)"})
check("B10 notes_url 非 http(s) 丢弃", s["notes_url"] == "")
s = sanitize({"tag": "0.9.1", "min_supported": "garbage"})
check("B11 min_supported 非法→0.0.0", s["min_supported"] == "0.0.0")

# ══════════════════════════════════════════════════
print("\n=== C. version.json 兼容投影（老客户端读它）===")
proj = project_version_json(sanitize({
    "tag": "v0.9.1",
    "assets": {"apk": {"url": "https://gitee.com/a/b.apk"},
               "exe": {"url": "https://gitee.com/a/b.exe"}},
}))
check("C1 投影只有 tag/exe/apk 三个字段（旧形状）", set(proj.keys()) == {"tag", "exe", "apk"})
check("C2 tag 带 v 前缀", proj["tag"] == "v0.9.1")
check("C3 apk 直链透传", proj["apk"].endswith(".apk"))
check("C4 exe 直链透传", proj["exe"].endswith(".exe"))
proj2 = project_version_json(sanitize({"tag": "v0.9.1", "assets": {}}))
check("C5 exe 未随发 → 空串（不是死链）", proj2["exe"] == "")

# ══════════════════════════════════════════════════
print("\n=== D. ★ 双源取版本最高（旧实现'先成功者胜'吞掉新版）===")


def _pick_best(candidates):
    """复刻 app/routes_update.py::_pick_best（同逻辑，独立验证）。"""
    def _valid(t):
        import re
        return bool(re.fullmatch(r"\d+\.\d+\.\d+", str(t or "")))
    best = None
    for tag, html in candidates:
        if not _valid(tag):
            continue
        v = tuple(int(x) for x in tag.split("."))
        if best is None or v > best[0]:
            best = (v, tag, html)
    return (best[1], best[2]) if best else None


old_online = "0.8.1"
gitee_new = "0.9.0"
# 旧行为：第一个成功（GitHub，0.8.1）即 return → 得到 0.8.1，客户端认为已最新
# 新行为：两个都看，取最大 → 0.9.0
check("D1 ★ GitHub(0.8.1) 在前不再压住 Gitee(0.9.0)",
      _pick_best([(old_online, "gh"), (gitee_new, "gitee")])[0] == "0.9.0")
check("D2 顺序无关（反着来也取最大）",
      _pick_best([(gitee_new, "gitee"), (old_online, "gh")])[0] == "0.9.0")
check("D3 只有一个源时用它", _pick_best([(old_online, "gh")])[0] == "0.8.1")
check("D4 全部失败 → None", _pick_best([]) is None)
check("D5 非法 tag 被跳过", _pick_best([("garbage", "x"), ("0.9.0", "g")])[0] == "0.9.0")
check("D6 大版本优先（0.10.0 > 0.9.9）",
      _pick_best([("0.9.9", "a"), ("0.10.0", "b")])[0] == "0.10.0")

# ══════════════════════════════════════════════════
print("\n=== E. 线上真实 update.json 自洽性 ===")
mf = _base / "server" / "update.json"
check("E1 update.json 存在", mf.is_file())
if mf.is_file():
    invalidate()
    m = load(mf)
    check("E2 tag 合法且非空", m["tag"] != "")
    check("E3 schema=1", m["schema"] == 1)
    check("E4 至少有一个平台的资产", bool(m["assets"]))
    apk = m["assets"].get("apk") or {}
    check("E5 apk.url 是 Gitee https 下载通道",
          apk.get("url", "").startswith("https://gitee.com/"))
    check("E6 apk.sha256 为 64 位十六进制",
          len(apk.get("sha256", "")) == 64)
    check("E7 apk.size > 10MB（不是错误页）",
          apk.get("size", 0) > 10 * 1024 * 1024)
    # 投影自洽
    proj = project_version_json(m)
    check("E8 投影 tag 与清单一致", proj["tag"] == "v" + m["tag"])

# ── 与 download_lib 的一致性（PINNED_TAG 已删除，两处必须同源）──
print("\n=== F. 与下载服务同源（PINNED_TAG 漂移回归）===")
dl = _base / "server" / "download_lib.py"
src = dl.read_text(encoding="utf-8")
check("F1 ★ download_lib.PINNED_TAG 不再是无条件 early-return",
      "if PINNED_TAG:\n        return PINNED_TAG" not in src)
check("F2 PINNED_TAG 恒为 None（保留符号兼容旧引用）",
      'PINNED_TAG = None' in src)
check("F3 _gitee_latest_tag 优先读 update.json",
      "manifest_tag()" in src)

# ── 旧的硬编码常量不再与清单冲突 ──
app_src = (_base / "server" / "app.py").read_text(encoding="utf-8")
check("F4 app.py 的 _OLD_CLIENT_NOTICE_LATEST 不再是 0.8.1",
      '_OLD_CLIENT_NOTICE_LATEST = "0.8.1"' not in app_src)
check("F5 app.py _current_tag 优先读 update.json",
      'update.json' in app_src)

print("\n" + "=" * 50)
print(f"  通过: {PASS}  失败: {FAIL}")
print("=" * 50)
if FAIL:
    sys.exit(1)
