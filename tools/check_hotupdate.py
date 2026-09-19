#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热更新 · 第 5 条门禁（见 `docs/热更新规范.md §九`）。

对发布区里的补丁做**发布前**审查。任何一条不过 ⇒ 非零退出，禁止上传。

    python tools/check_hotupdate.py                      # 查默认发布区最新一个
    python tools/check_hotupdate.py --all                # 查全部 serial
    python tools/check_hotupdate.py --dist <dir> --base 0.8.1

校验项（与规范一一对应）：
  1. patch-<N>.json 结构合法、manifest 字段完整
  2. **签名可用公钥验证**（用私钥导出公钥做对照；公钥在 Kotlin 侧，这里用私钥自洽性验证）
  3. zip 本体 sha256 == 清单声明
  4. 包内文件与 files 清单**完全一致**（不多不少）
  5. 每个文件 sha256 匹配；**路径不越界**、只允许 `web/` 前缀
  6. **无删除语义**：累积式 ⇒ 相对 base 基线，清单必须是"基线之外的变更"，
     且不得出现基线里有、包里没有的情况被当成删除（由 build 阶段拦；这里复查 serial 单调与累积性）
  7. serial 单调递增、latest 指向最大 serial
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
DEFAULT_KEY = Path.home() / ".firefly" / "hotupdate_key.pem"
DEFAULT_DIST = ROOT / "hotupdate_dist"

PASS = FAIL = 0


def check(desc: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def find_openssl() -> str:
    import os
    p = os.environ.get("OPENSSL") or shutil.which("openssl")
    if p and Path(p).is_file():
        return p
    for c in (r"C:\Program Files\Git\usr\bin\openssl.exe",
              r"C:\Program Files\Git\mingw64\bin\openssl.exe",
              "/usr/bin/openssl"):
        if Path(c).is_file():
            return c
    return ""


def verify_with_key(key: Path, data: bytes, sig: bytes) -> bool:
    ossl = find_openssl()
    if not ossl or not key.is_file():
        return False
    pub = subprocess.run([ossl, "pkey", "-in", str(key), "-pubout"], capture_output=True)
    if pub.returncode != 0:
        return False
    tmp = ROOT / ".tmp_hotupdate" / "_chk_pub.pem"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(pub.stdout)
    sigf = tmp.with_suffix(".sig")
    sigf.write_bytes(sig)
    r = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(tmp),
                        "-signature", str(sigf)], input=data, capture_output=True)
    tmp.unlink(missing_ok=True)
    sigf.unlink(missing_ok=True)
    return r.returncode == 0


def check_one(dist: Path, serial: int, key: Path) -> None:
    print(f"\n--- serial {serial} ---")
    jf = dist / f"patch-{serial}.json"
    zf = dist / f"patch-{serial}.zip"
    check("清单文件存在", jf.is_file(), str(jf.name))
    check("包体文件存在", zf.is_file(), str(zf.name))
    if not (jf.is_file() and zf.is_file()):
        return

    try:
        payload = json.loads(jf.read_text(encoding="utf-8"))
    except Exception as e:
        check("清单可解析", False, str(e))
        return
    check("清单结构 {v, manifest_b64, sig}", {"v", "manifest_b64", "sig"} <= set(payload))

    try:
        raw = base64.b64decode(payload["manifest_b64"], validate=True)
        sig = base64.b64decode(payload["sig"], validate=True)
    except Exception as e:
        check("manifest_b64 / sig 可 base64 解码", False, str(e))
        return
    check("manifest 字节非空且 sig 长度为 256（RSA-2048）",
          bool(raw) and len(sig) == 256, f"{len(raw)}B / {len(sig)}B")

    # 2. 签名
    if key.is_file():
        check("★ 签名验证通过", verify_with_key(key, raw, sig))
    else:
        print(f"  ! 没有私钥（{key}）→ 跳过签名验证（发布机上不应跳过）")

    try:
        m = json.loads(raw.decode("utf-8"))
    except Exception as e:
        check("manifest 是合法 JSON", False, str(e))
        return

    # 1. 字段完整
    need = ("format", "base_version", "serial", "issued_at", "cumulative",
            "layer", "zip", "files", "rollout_percent", "min_safe_serial", "revoked_serials")
    missing = [k for k in need if k not in m]
    check("manifest 字段完整", not missing, f"缺 {missing}" if missing else "")
    check("serial 与文件名一致", int(m.get("serial", -1)) == serial)
    check("cumulative 为 true（v1 只接受累积式）", m.get("cumulative") is True)
    check("layer 为 web（v1 范围）", m.get("layer") == "web")

    # 3. zip 本体 sha256
    zsha = hashlib.sha256(zf.read_bytes()).hexdigest()
    check("★ zip 本体 sha256 与清单一致",
          zsha == str(m["zip"].get("sha256")), f"{zsha[:12]}…")
    check("zip 大小与清单一致", zf.stat().st_size == int(m["zip"].get("size") or -1))

    # 4/5. 包内容与逐文件校验
    files = m.get("files") or []
    check("files 非空", bool(files), f"{len(files)} 个")
    bad_path, bad_sha, bad_prefix = [], [], []
    with zipfile.ZipFile(zf) as z:
        names = set(z.namelist())
        for f in files:
            p = str(f.get("path") or "")
            if (not p or p.startswith("/") or "\\" in p or ".." in p.split("/")):
                bad_path.append(p)
            elif not p.startswith("web/"):
                bad_prefix.append(p)
            else:
                data = z.read(p) if p in names else b""
                if hashlib.sha256(data).hexdigest() != str(f.get("sha256")):
                    bad_sha.append(p)
        check("★ 路径合法（无 .. / 绝对路径 / 反斜杠）", not bad_path, str(bad_path[:3]))
        check("★ 只含 web/ 前缀（v1 不许碰 py/ 与其它目录）", not bad_prefix, str(bad_prefix[:3]))
        check("★ 每个文件 sha256 匹配", not bad_sha, str(bad_sha[:3]))
        check("★ 包内文件与清单完全一致（不多不少）",
              names == {f["path"] for f in files},
              f"多 {sorted(names - {f['path'] for f in files})[:3]} "
              f"缺 {sorted({f['path'] for f in files} - names)[:3]}")
        total = sum(zi.file_size for zi in z.infolist())
    print(f"    （解压后 {total / 1024:.1f} KB，压缩包 {zf.stat().st_size / 1024:.1f} KB）")


def _pem_body(text: str) -> str:
    """从一段文本里抽出 PEM 正文（先去出 BEGIN/END 之间的块，再去空白）—— 跨语言比对公钥用。

    ⚠️ 必须**先抽块**：传进来的是整个源文件（Kotlin 有 package/注释、Python 有 docstring），
       直接删头尾标记会把整份文件正文当成"公钥"，比对必然失败（第一版就是这么写的）。
    """
    import re
    m = re.search(r"-----BEGIN PUBLIC KEY-----(.*?)-----END PUBLIC KEY-----", text, re.S)
    return "".join(m.group(1).split()) if m else ""


def check_keys() -> None:
    """★ 公钥两份副本必须逐字节一致（2026-09-19 新增）。

    为什么必须有这条：`verify.py` 现在有两条实现路径 —— 安卓走 Kotlin（公钥在
    `HotUpdateKeys.kt`），PC 走纯 Python 自实现（公钥在 `app/hotupdate/pubkey.py`）。
    两者不一致的症状是 **PC 端"所有补丁/公告验签失败"，但没有任何报错** ——
    用户只会觉得"怎么永远没有更新"，是最难查的一类问题。所以用门禁钉死。
    """
    kt = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "firefly" / "android" / "HotUpdateKeys.kt"
    py = ROOT / "app" / "hotupdate" / "pubkey.py"
    print("\n--- 验签公钥一致性 ---")
    check("HotUpdateKeys.kt 存在", kt.is_file(), str(kt))
    check("pubkey.py 存在", py.is_file(), str(py))
    if not (kt.is_file() and py.is_file()):
        return
    a, b = _pem_body(kt.read_text(encoding="utf-8")), _pem_body(py.read_text(encoding="utf-8"))
    check("★ Kotlin 与 Python 两份公钥逐字节一致", a == b and bool(a),
          "" if a == b else f"Kotlin {a[:24]}… vs Python {b[:24]}…")
    # 顺带确认 Python 侧能真的解析出 RSA 参数（不是"字符串一样但结构坏了"）
    sys.path.insert(0, str(ROOT / "app"))
    try:
        from hotupdate import verify as _v
        n, e = _v._parse_spki(__import__("base64").b64decode(a))
        check("★ Python 侧能解析出 RSA 参数", n.bit_length() == 2048 and e == 65537,
              f"{n.bit_length()} 位 / e={e}")
    except Exception as ex:
        check("★ Python 侧能解析出 RSA 参数", False, f"{type(ex).__name__}: {ex}")


def check_notice(dist: Path, key: Path) -> None:
    """公告通道门禁：清单可验签 + **客户端校验器接受** + 图片字节对账。"""
    print(f"\n--- 公告通道：{dist} ---")
    if not dist.is_dir():
        check("公告发布区存在", False, f"{dist} 不存在（先跑 tools/build_notice.py）")
        return
    latest_f = dist / "latest"
    if not latest_f.is_file():
        check("latest 存在", False)
        return
    try:
        serial = int(json.loads(latest_f.read_text(encoding="utf-8")).get("serial") or 0)
    except Exception as e:
        check("latest 可解析", False, str(e))
        return
    check("latest 可解析且 serial > 0", serial > 0, f"serial={serial}")

    jf = dist / f"notice-{serial}.json"
    check("最新清单文件存在", jf.is_file(), jf.name)
    if not jf.is_file():
        return
    payload = json.loads(jf.read_text(encoding="utf-8"))
    check("清单信封 {v, manifest_b64, sig}", {"v", "manifest_b64", "sig"} <= set(payload))
    raw = base64.b64decode(payload["manifest_b64"])
    sig = base64.b64decode(payload["sig"])
    check("签名长度 256（RSA-2048）", len(sig) == 256, f"{len(sig)} 字节")
    check("★ 签名验证通过", verify_with_key(key, raw, sig))

    # ★ 客户端自己的校验器必须接受（发布端/客户端各写一套校验 = 最难查的不一致）
    sys.path.insert(0, str(ROOT / "app"))
    try:
        from notice import validate as _cv
        got = _cv(raw)
        check("★ 客户端校验器接受该清单", bool(got.get("entries")),
              f"{len(got.get('entries') or [])} 条")
    except Exception as ex:
        check("★ 客户端校验器接受该清单", False, f"{type(ex).__name__}: {ex}")
        return

    imgs = [b["name"] for e in got["entries"] for b in e["blocks"] if b["t"] == "img"]
    bad = []
    for name in imgs:
        fp = dist / name
        want = next(b["sha256"] for e in got["entries"] for b in e["blocks"]
                    if b["t"] == "img" and b["name"] == name)
        if not fp.is_file() or hashlib.sha256(fp.read_bytes()).hexdigest() != want:
            bad.append(name)
    check("★ 引用的图片齐备且 sha256 匹配", not bad, str(bad) if bad else f"{len(imgs)} 张")


def main() -> int:
    ap = argparse.ArgumentParser(description="热更新补丁门禁")
    ap.add_argument("--dist", default=str(DEFAULT_DIST), help="发布区根目录")
    ap.add_argument("--base", default=None, help="base 版本（默认取 app 的 APP_VERSION）")
    ap.add_argument("--all", action="store_true", help="校验全部 serial（默认只校最新）")
    ap.add_argument("--key", default=str(DEFAULT_KEY), help="私钥（用于自洽验签）")
    a = ap.parse_args()

    if a.base is None:
        sys.path.insert(0, str(ROOT / "app"))
        try:
            import re
            a.base = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)',
                               (ROOT / "app" / "core" / "config.py")
                               .read_text(encoding="utf-8")).group(1)
        except Exception:
            print("X 无法确定 base，请用 --base 指定")
            return 2

    dist = Path(a.dist) / a.base
    print(f"=== 热更新门禁：{dist} ===")
    # 信任根与公告：**无论本次有没有补丁都要查**（它们与补丁共用同一个信任根）
    check_keys()
    check_notice(Path(a.dist) / "notice", Path(a.key).expanduser())
    if not dist.is_dir():
        # 语义要分清（否则会挡住正常发版）：
        #   · 没显式指定 --dist（用默认发布区）→ 本次就是没有补丁要发 → **通过**，只是打一行说明
        #   · 显式指定了却不存在 → 拼错路径/上传漏了 → **失败**
        if a.dist == str(DEFAULT_DIST):
            print("\n本次无热更补丁（默认发布区不存在）—— 跳过补丁校验")
            print(f"\n统计: PASS={PASS} FAIL={FAIL}")
            print("结果: " + ("PASS 可发布" if FAIL == 0 else "FAIL 禁止发布"))
            return 1 if FAIL else 0
        print(f"X 指定的发布区不存在：{dist}")
        return 2

    latest_f = dist / "latest"
    serials = sorted(int(p.stem.split("-")[1]) for p in dist.glob("patch-*.json")
                     if p.stem.split("-")[1].isdigit())
    if not serials:
        print("X 发布区里没有任何补丁")
        return 2
    print(f"  发现 serial：{serials}")

    # 7. serial 单调 + latest 指向最大
    check("serial 从 1 起连续无缺口", serials == list(range(1, len(serials) + 1)), str(serials))
    if latest_f.is_file():
        try:
            lt = json.loads(latest_f.read_text(encoding="utf-8"))
            check("latest.base 与目录一致", lt.get("base") == a.base, str(lt))
            check("latest.serial == 最大 serial",
                  int(lt.get("serial") or 0) == max(serials), str(lt))
        except Exception as e:
            check("latest 可解析", False, str(e))
    else:
        check("latest 存在", False, "缺 latest（客户端第一个请求就是它）")

    for s in (serials if a.all else [max(serials)]):
        check_one(dist, s, Path(a.key).expanduser())

    print(f"\n统计: PASS={PASS} FAIL={FAIL}")
    print("结果: " + ("PASS 可发布" if FAIL == 0 else "FAIL 禁止发布"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
