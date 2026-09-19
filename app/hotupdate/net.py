# -*- coding: utf-8 -*-
"""热更新 · 网络与包处理（契约 §二/§三）。

只用标准库（urllib + zipfile + base64）—— 项目「禁止新增第三方依赖」。
本模块**不碰状态、不碰目录布局**，只做四件纯事：
  拉 latest → 拉 manifest 规范化字节 → 下载 zip（带 sha256 对账）→ 解压并逐文件校验。

所有函数都可能抛异常，由上层（`__init__.py`）统一兜成状态字段 —— 网络类模块不许自己吞状态。
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.request
from pathlib import Path

_UA = "FireflyHotUpdate/1.0"
_TIMEOUT = 30
_CHUNK = 1 << 16
_MAX_ZIP = 8 << 20          # 前端补丁不该超过 8MB；超了说明发错东西了（护着服务器与用户流量）


def _get(url: str, timeout: int = _TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Cache-Control": "no-store"})
    return urllib.request.urlopen(req, timeout=timeout)


def canonical_bytes(manifest: dict) -> bytes:
    """manifest 的**规范化字节** —— 签名对象（契约 §三）。

    ★ 全项目**唯一**的规范化处。发布端（tools/build_hotupdate.py）签它，
    客户端**只解码不重新序列化**（`manifest_b64` 就是这个字节串的 base64）——
    所以不存在"Python 与 Kotlin 各序列化一次、参数不一致"的跨语言地雷。
    """
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def fetch_latest(base_url: str) -> dict:
    """索引 `latest`（**未签名、不可信**，只当提示；信任根是 manifest）。

    返回 {"base": "0.8.1", "serial": 3}。任何异常向上抛。
    """
    with _get(base_url.rstrip("/") + "/latest") as r:
        d = json.loads(r.read().decode("utf-8"))
    if not isinstance(d, dict) or "serial" not in d:
        raise ValueError("latest 格式非法")
    return d


def fetch_manifest(base_url: str, serial: int, prefix: str = "patch") -> tuple[bytes, str]:
    """取清单 → (规范化字节, base64 签名)。**返回的是原始字节，不做任何再序列化。**

    `prefix`：热更用 `patch`（`patch-N.json`），公告通道复用本函数时传 `notice`
    （`notice-N.json`）。文件形态、`manifest_b64`/`sig` 信封、验签口径**三条完全一致** ——
    复用而不是复制，就是为了不出现"两套看起来一样、细节不一致"的实现。
    """
    url = f"{base_url.rstrip('/')}/{prefix}-{int(serial)}.json"
    with _get(url) as r:
        d = json.loads(r.read().decode("utf-8"))
    b64 = d.get("manifest_b64") or ""
    sig = d.get("sig") or ""
    if not b64 or not sig:
        raise ValueError("manifest_b64/sig 缺失")
    raw = base64.b64decode(b64, validate=True)
    if not raw:
        raise ValueError("manifest_b64 解码为空")
    return raw, sig


def parse_manifest(raw: bytes) -> dict:
    m = json.loads(raw.decode("utf-8"))
    if not isinstance(m, dict):
        raise ValueError("manifest 不是对象")
    for k in ("base_version", "serial", "zip", "files", "cumulative"):
        if k not in m:
            raise ValueError(f"manifest 缺字段 {k}")
    if not m.get("cumulative"):
        raise ValueError("v1 只接受累积式补丁（cumulative 必须为 true）")
    return m


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def manifest_hash(raw: bytes) -> str:
    """补丁身份（契约 §二 的 patch_hash）= manifest 规范化字节的 sha256 前 16 位。"""
    return hashlib.sha256(raw).hexdigest()[:16]


def download_zip(base_url: str, zip_name: str, dst: Path, sha_expect: str,
                 size_expect: int = 0) -> None:
    """下 zip 到 dst.part，边下边算 sha256，**落盘前**比对；不符就丢弃并抛异常。"""
    url = f"{base_url.rstrip('/')}/{zip_name}"
    part = dst.with_suffix(dst.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    total = 0
    with _get(url, timeout=120) as r, open(part, "wb") as f:
        while True:
            buf = r.read(_CHUNK)
            if not buf:
                break
            total += len(buf)
            if total > _MAX_ZIP:
                f.close()
                part.unlink(missing_ok=True)
                raise ValueError(f"包体超过上限 {_MAX_ZIP // 1048576}MB，拒绝")
            h.update(buf)
            f.write(buf)
    if sha_expect and h.hexdigest() != sha_expect:
        got = h.hexdigest()
        part.unlink(missing_ok=True)
        raise ValueError(f"zip 校验失败（本地 {got[:12]}… vs 清单 {sha_expect[:12]}…）")
    if size_expect and total != size_expect:
        part.unlink(missing_ok=True)
        raise ValueError(f"zip 大小不符（{total} vs {size_expect}）")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    part.replace(dst)


def extract_verified(zip_path: Path, files_meta: list, dst_dir: Path,
                     strip_prefix: str = "web/") -> int:
    """把 zip 解到 dst_dir，**每个文件先校验 sha256 再落盘**；任何不符 → 整目录清掉后抛异常。

    防 TUF 的 mix-and-match：要么全部可信，要么一个都不留。
    路径越界（`..`、绝对路径）一律拒绝。

    ★ `strip_prefix`：清单里的路径带层级前缀（`web/js/x.js`），而 `dst_dir` **就是覆盖层的根**
      （即 `hotupdate/web/`）。所以落盘时要剥掉前缀，否则会变成 `hotupdate/web/web/js/x.js`
      ——第一版就是这么写的，被测试 A6 抓出来（文件数对、路径全错）。
    """
    import shutil
    import zipfile

    want = {}
    for f in files_meta:
        p = str(f.get("path") or "")
        if (not p or p.startswith("/") or "\\" in p or ".." in p.split("/")):
            raise ValueError(f"清单里路径非法：{p!r}")
        if not p.startswith(strip_prefix):
            raise ValueError(f"清单里路径不在允许的层级 {strip_prefix!r} 内：{p!r}")
        want[p] = f.get("sha256") or ""
    if not want:
        raise ValueError("清单 files 为空")

    if dst_dir.exists():
        shutil.rmtree(dst_dir, ignore_errors=True)
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            extra = names - set(want)
            missing = set(want) - names
            if extra or missing:
                raise ValueError(f"包内文件与清单不一致（多 {sorted(extra)} 缺 {sorted(missing)}）")
            for arc, sha in want.items():
                data = z.read(arc)
                if sha and hashlib.sha256(data).hexdigest() != sha:
                    raise ValueError(f"{arc} 内容校验不符")
                out = dst_dir / arc[len(strip_prefix):]
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
    except Exception:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise
    return len(want)
