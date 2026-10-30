# -*- coding: utf-8 -*-
"""下载兜底版本解析的独立 oracle（P1-4，2026-10-30）。

只测 `_gitee_latest_tag()` 的**版本判定**：用手工构造的 update.json / version.json
断言解析结果，不联网、不碰生产、不读真实清单。

背景：`FALLBACK_TAG` 曾是 `v0.8.1`。update.json 缺失/损坏 + Gitee 拉取失败时，
下载页与 302 会一起指向半年前的包，而 0.8.1 缺 0.9.x 的协议兼容代码 ——
用户装回去等于被迫降级到不可用的客户端。现在：
  1. update.json（权威）
  2. version.json（同目录投影）
  3. Gitee 列表 API
  4. FALLBACK_TAG（停在当前已发布稳定版，不再是 0.8.1）
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
for _p in (ROOT, SERVER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import download_lib  # noqa: E402
import update_manifest  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  V {name}" + (f"   {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  X {name}" + (f"   {detail}" if detail else ""))


def _offline():
    """让 Gitee API 一定失败（测"本地两份都拿不到"的分支）。"""
    def _boom(*a, **k):
        raise OSError("test: network disabled")
    return _boom


class case:
    """临时目录 + 清单文件；进出时恢复 _MANIFEST_FILE 与缓存。"""

    def __init__(self, update=None, version=None):
        self.update = update
        self.version = version

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.orig = download_lib._MANIFEST_FILE
        if self.update is not None:
            (d / "update.json").write_text(self.update, encoding="utf-8")
        if self.version is not None:
            (d / "version.json").write_text(self.version, encoding="utf-8")
        download_lib._MANIFEST_FILE = str(d / "update.json")
        download_lib._GITEE_TAG_CACHE["tag"] = None
        download_lib._GITEE_TAG_CACHE["at"] = 0.0
        update_manifest.invalidate()
        return self

    def __exit__(self, *exc):
        download_lib._MANIFEST_FILE = self.orig
        download_lib._GITEE_TAG_CACHE["tag"] = None
        download_lib._GITEE_TAG_CACHE["at"] = 0.0
        update_manifest.invalidate()
        self.tmp.cleanup()
        return False


def main() -> int:
    print("=== 1. update.json 正常 ===")
    with case(update='{"schema":1,"tag":"0.9.1","assets":{}}'):
        check("取 update.json 的 tag", download_lib._gitee_latest_tag() == "v0.9.1",
              download_lib._gitee_latest_tag())

    print("\n=== 2. update.json 缺失，version.json 在位 ===")
    with case(version='{"tag":"v0.9.1","exe":"","apk":""}'):
        got = download_lib._gitee_latest_tag()
        check("回退到 version.json", got == "v0.9.1", got)

    print("\n=== 3. update.json 损坏（非法 JSON），version.json 在位 ===")
    with case(update="{ 这不是 json", version='{"tag":"v0.9.1"}'):
        got = download_lib._gitee_latest_tag()
        check("坏 update.json 不影响版本判定", got == "v0.9.1", got)

    print("\n=== 4. version.json 里的 tag 不带 v ===")
    with case(version='{"tag":"0.9.1"}'):
        got = download_lib._gitee_latest_tag()
        check("补上 v 前缀", got == "v0.9.1", got)

    print("\n=== 5. 本地两份都没有 + 远端不可达 → FALLBACK_TAG ===")
    _orig = urllib.request.urlopen
    urllib.request.urlopen = _offline()
    try:
        with case():
            got = download_lib._gitee_latest_tag()
            check("落到 FALLBACK_TAG", got == download_lib.FALLBACK_TAG, got)
    finally:
        urllib.request.urlopen = _orig

    print("\n=== 6. 回归守卫：兜底不得是 0.8.1 ===")
    check("FALLBACK_TAG 已离开 v0.8.1", download_lib.FALLBACK_TAG != "v0.8.1",
          download_lib.FALLBACK_TAG)
    check("FALLBACK_TAG 形如 v<major>.<minor>.<patch>",
          bool(__import__("re").fullmatch(r"v\d+\.\d+\.\d+", download_lib.FALLBACK_TAG)),
          download_lib.FALLBACK_TAG)

    print("\n=== 7. tag 注入防护（Gitee 返回畸形 tag 时回退兜底）===")
    class _Resp:
        def read(self):
            return json.dumps([{"id": 1, "tag_name": "../etc/passwd"}]).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    _orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _Resp()
    try:
        with case():
            got = download_lib._gitee_latest_tag()
            check("畸形 tag 被拒", got == download_lib.FALLBACK_TAG, got)
    finally:
        urllib.request.urlopen = _orig

    print(f"\n=== 下载兜底 oracle: PASS={PASS} FAIL={FAIL} ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
