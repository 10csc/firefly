#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公告 / 更新说明 · 产出器（见 `docs/公告通道规范.md`）。

**内容源**（进 git，人写）：`notice_src/`
    notice_src/source.json   条目与结构化块（p/h/li/tip/img）
    notice_src/images/*      公告图（img 块用 "file" 引用）
    notice_src/serial.txt    serial 计数器（**进 git** —— 序号必须可追溯，不许靠目录状态猜）

★ 为什么内容源叫 `notice_src/` 而不是 `notice/`：
  运行期缓存目录就叫 `notice/`（`paths.notice_root()` = `USER_DIR.parent/notice`，开发机
  = 仓库根/notice，与 `/voice/` 同一个公式、同样 gitignored）。两者同名会把"人写的内容源"
  和"跑出来的缓存"混在一起 —— 2026-09-19 第一版就是这样，建目录时才发现。

**产物**（默认 `hotupdate_dist/notice/`，gitignored，上传用）：
    latest                 {"serial":N}
    notice-<N>.json        {v, manifest_b64, sig}
    img-<名>               与清单里 sha256 对应的图片字节

四步与模块铁律一致：**审查（源格式+图片）→ 处理（打包+签名）→ 验证（回读+客户端校验器）
→ 输出**。第三步会**用客户端自己的 `app/notice.py::validate()` 再验一遍** —— 发布端和
客户端"两边各写一套校验"是最难查的一类不一致，这里从结构上消灭它。

用法：
    python tools/build_notice.py --note "0.9.0 更新说明"      # serial +1，产出并签名
    python tools/build_notice.py --check                      # 只审查 + 报告差异，不产出
    python tools/build_notice.py --note "…" --force           # 内容没变也强制出一版
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "notice_src"
SOURCE_JSON = SRC / "source.json"
IMG_SRC = SRC / "images"
SERIAL_F = SRC / "serial.txt"

DEFAULT_KEY = Path.home() / ".firefly" / "hotupdate_key.pem"
DEFAULT_DIST = ROOT / "hotupdate_dist" / "notice"

MAX_IMG = 1536 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPENSSL_CANDIDATES = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    "/usr/bin/openssl",
)


def die(msg: str, code: int = 1):
    print(f"X {msg}")
    sys.exit(code)


def find_openssl() -> str:
    import os
    p = os.environ.get("OPENSSL") or shutil.which("openssl")
    if p and Path(p).is_file():
        return p
    for c in _OPENSSL_CANDIDATES:
        if Path(c).is_file():
            return c
    die("找不到 openssl（签名要用）。装一个或用环境变量 OPENSSL 指定。")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def canonical_bytes(obj) -> bytes:
    """★ 规范化**只有一处**：转发到 `app/hotupdate/net.py`（与补丁同一口径）。"""
    sys.path.insert(0, str(ROOT / "app"))
    from hotupdate.net import canonical_bytes as _cb
    return _cb(obj)


def sign(key: Path, data: bytes) -> bytes:
    r = subprocess.run([find_openssl(), "dgst", "-sha256", "-sign", str(key)],
                       input=data, capture_output=True)
    if r.returncode != 0:
        die("签名失败: " + r.stderr.decode("utf-8", "replace")[:300])
    return r.stdout


def verify_sig(key: Path, data: bytes, sig: bytes, workdir: Path) -> bool:
    ossl = find_openssl()
    pub = subprocess.run([ossl, "pkey", "-in", str(key), "-pubout"], capture_output=True)
    if pub.returncode != 0:
        return False
    workdir.mkdir(parents=True, exist_ok=True)
    tmp, sigf = workdir / "_pub.pem", workdir / "_pub.sig"
    tmp.write_bytes(pub.stdout)
    sigf.write_bytes(sig)
    try:
        r = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(tmp),
                            "-signature", str(sigf)], input=data, capture_output=True)
        return r.returncode == 0
    finally:
        tmp.unlink(missing_ok=True)
        sigf.unlink(missing_ok=True)


# ── ① 审查：源格式 ────────────────────────────────
def load_source() -> tuple:
    """返回 (entries, images)：entries 里 img 块已换成 {name, sha256, size}。"""
    if not SOURCE_JSON.is_file():
        die(f"内容源不存在：{SOURCE_JSON}")
    try:
        src = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"内容源 JSON 解析失败：{e}")
    if not isinstance(src, dict) or int(src.get("schema") or 0) != 1:
        die("内容源 schema 必须是 1")
    entries = src.get("entries")
    if not isinstance(entries, list) or not entries:
        die("内容源 entries 必须是非空数组")

    images, out, seen_ids = {}, [], set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            die(f"entries[{i}] 不是对象")
        eid = str(e.get("id") or "")
        if not _SAFE_NAME.match(eid):
            die(f"entries[{i}].id 非法（只允许字母数字._-，≤128）：{eid!r}")
        if eid in seen_ids:
            die(f"entries[{i}].id 重复：{eid}")
        seen_ids.add(eid)
        title = str(e.get("title") or "").strip()
        if not title:
            die(f"entries[{i}].title 为空")
        lvl = str(e.get("level") or "info")
        if lvl not in ("info", "warn", "critical"):
            die(f"entries[{i}].level 非法：{lvl!r}")
        blocks, imgs = [], 0
        for j, b in enumerate(e.get("blocks") or []):
            if not isinstance(b, dict):
                die(f"entries[{i}].blocks[{j}] 不是对象")
            t = str(b.get("t") or "")
            if t not in ("p", "h", "li", "tip", "img"):
                die(f"entries[{i}].blocks[{j}].t 非法：{t!r}（只允许 p/h/li/tip/img）")
            if t == "img":
                fn = str(b.get("file") or "")
                if not _SAFE_NAME.match(fn) or "/" in fn:
                    die(f"entries[{i}].blocks[{j}].file 非法：{fn!r}")
                fp = IMG_SRC / fn
                if not fp.is_file():
                    die(f"图片不存在：{fp}")
                if fp.stat().st_size > MAX_IMG:
                    die(f"图片过大（{fp.stat().st_size} > {MAX_IMG}）：{fp}")
                name = f"img-{fn}"
                if len(name) > 116:
                    die(f"图片名过长（加上 img- 前缀后 ≤116）：{fn}")
                images[name] = fp
                blocks.append({"t": "img", "name": name,
                               "sha256": sha256_file(fp), "size": fp.stat().st_size,
                               "alt": str(b.get("alt") or "")[:120]})
                imgs += 1
                continue
            text = str(b.get("text") or "").strip()
            if not text:
                die(f"entries[{i}].blocks[{j}] 文本为空")
            blocks.append({"t": t, "text": text})
        if imgs > 6:
            die(f"entries[{i}] 图片超过 6 张（{imgs}）")
        out.append({
            "id": eid,
            "title": title,
            "date": str(e.get("date") or time.strftime("%Y-%m-%d"))[:10],
            "level": lvl,
            "pinned": bool(e.get("pinned")),
            "min_app_version": str(e.get("min_app_version") or ""),
            "max_app_version": str(e.get("max_app_version") or ""),
            "blocks": blocks,
        })
    return out, images


def read_serial() -> int:
    try:
        return int(SERIAL_F.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="公告/更新说明产出器")
    ap.add_argument("--note", default="", help="本次发布说明（写进 CHANGELOG）")
    ap.add_argument("--key", default=str(DEFAULT_KEY))
    ap.add_argument("--dist", default=str(DEFAULT_DIST))
    ap.add_argument("--check", action="store_true", help="只审查，不产出")
    ap.add_argument("--force", action="store_true", help="内容未变也强制出一版")
    a = ap.parse_args()

    dist = Path(a.dist)
    key = Path(a.key).expanduser()

    print("=== ① 审查（内容源 + 图片）===")
    entries, images = load_source()
    total_img = sum(p.stat().st_size for p in images.values())
    print(f"  V 条目 {len(entries)} 个，图片 {len(images)} 张（{total_img / 1024:.1f} KB）")
    for e in entries:
        nb = len(e["blocks"])
        ni = sum(1 for b in e["blocks"] if b["t"] == "img")
        print(f"    · {e['id']}  «{e['title']}»  {nb} 块 / {ni} 图 / {e['level']}"
              + ("  [置顶]" if e["pinned"] else ""))

    prev = read_serial()
    if a.check:
        body_old = dist / "_last_body.json"
        if body_old.is_file():
            try:
                old = json.loads(body_old.read_text(encoding="utf-8"))
                same = old.get("entries") == entries
                print(f"  {'·' if same else '!'} 与上次产出的内容"
                      f"{'一致（无需发版）' if same else '**有变化**'}")
            except Exception:
                print("  · 无法读取上次产出（忽略）")
        print(f"  · 当前 serial = {prev}，下一个将产出 {prev + 1}")
        return 0

    # 内容未变 → 不白出一版（serial 是"发布事件"的编号，不该被无意义地推进）
    body_f = dist / "_last_body.json"
    if body_f.is_file() and not a.force:
        try:
            old = json.loads(body_f.read_text(encoding="utf-8"))
            if old.get("entries") == entries:
                print("\n=== ② 处理 ===")
                print("  · 内容与上次产出一致 → 跳过（要强制出一版用 --force）")
                return 0
        except Exception:
            pass

    serial = prev + 1
    json_name = f"notice-{serial}.json"
    if (dist / json_name).exists() and not a.force:
        die(f"发布区已存在 {json_name} —— 已发布的公告**永不覆盖**（改 serial 或移走旧产物）")

    print("\n=== ② 处理：打包 + 签名 ===")
    dist.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1,
        "serial": serial,
        "generated_at": int(time.time()),
        "min_safe_serial": 0,
        "revoked_serials": [],
        "entries": entries,
    }
    raw = canonical_bytes(manifest)
    if not key.is_file():
        die(f"私钥不存在：{key}\n  先跑 `python tools/hotupdate_keys.py --gen`")
    sig = sign(key, raw)
    (dist / json_name).write_text(json.dumps(
        {"v": 1, "manifest_b64": base64.b64encode(raw).decode("ascii"),
         "sig": base64.b64encode(sig).decode("ascii")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  V 清单：{json_name}（规范化 {len(raw)} 字节，签名 {len(sig)} 字节）")
    for name, fp in images.items():
        shutil.copyfile(fp, dist / name)
    print(f"  V 图片 {len(images)} 张已复制到发布区")

    print("\n=== ③ 验证 ===")
    if not verify_sig(key, raw, sig, dist / "_tmp"):
        for f in (json_name,):
            (dist / f).unlink(missing_ok=True)
        die("签名自检失败，已删除产物。")
    print("  V 签名自检（openssl -verify）通过")

    # ★ 用**客户端自己的校验器**再验一遍 —— 两边各写一套校验是最难查的不一致
    sys.path.insert(0, str(ROOT / "app"))
    from notice import validate as client_validate
    try:
        got = client_validate(raw)
    except Exception as e:
        (dist / json_name).unlink(missing_ok=True)
        die(f"客户端校验器拒绝了这份清单：{type(e).__name__}: {e}")
    if len(got["entries"]) != len(entries):
        (dist / json_name).unlink(missing_ok=True)
        die(f"客户端校验后条目数变少（{len(got['entries'])} vs {len(entries)}）"
            "—— 说明有内容会被端侧静默丢弃，先修好再发。")
    n_img = sum(1 for e in got["entries"] for b in e["blocks"] if b["t"] == "img")
    print(f"  V 客户端校验器接受：{len(got['entries'])} 条 / {n_img} 图")

    print("\n=== ④ 输出 ===")
    (dist / "latest").write_text(json.dumps({"serial": serial}, ensure_ascii=False),
                                 encoding="utf-8")
    body_f.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    SERIAL_F.write_text(str(serial), encoding="utf-8")
    log = dist / "CHANGELOG.md"
    if not log.exists():
        log.write_text("# 公告通道 CHANGELOG\n\n"
                       "> serial 单调递增、产物不可变。回滚 = 发一版把 "
                       "`revoked_serials` 填上旧号（或直接改内容再发一版）。\n\n",
                       encoding="utf-8")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"## serial {serial} · {time.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"- 说明：{a.note or '(无)'}\n")
        f.write(f"- 条目（{len(entries)}）：" + "、".join(e["id"] for e in entries) + "\n")
        f.write(f"- 图片（{len(images)}）：" + ("、".join(images) or "无") + "\n")
        f.write(f"- 清单：`{json_name}`（sha256 `{sha256_bytes(raw)[:16]}…`）\n\n")
    print(f"  V 发布区：{dist}")
    print(f"    latest → {{\"serial\":{serial}}}")
    print(f"    {json_name}" + (f" + {len(images)} 张图" if images else ""))
    print(f"  V serial 推进到 {serial}（notice/serial.txt 记得一起提交）")
    print(f"\n  下一步：python tools/publish_notice.py --serial {serial}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
