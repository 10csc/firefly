# -*- coding: utf-8 -*-
"""语音插件 — 模型下载器

只用标准库（`urllib.request`）—— 项目「禁止新增第三方依赖」，而这里只需要 HTTP GET + Range。

★ 已实测（2026-09-18）：
  · ModelScope 公开模型**匿名可下**（零 token，HTTP 200）
  · **支持 Range**（HTTP 206，LFS 大文件同样支持）→ 断点续传可用
  · 实测速度 17.37 MB/s（本机家宽），872 MB 约 50 秒

URL 形式：配置里存**模板**，`{file}` 占位替换：
  `https://www.modelscope.cn/api/v1/models/<ns>/<name>/repo?Revision=master&FilePath={file}`
无 `{file}` 时按 `{base}/{file}` 拼。

## sha256 对账（2026-09-18 加）

ModelScope 的仓库文件清单接口**免费返回每个文件的 Sha256**，所以下载完可以逐个比对，
用来抓「半截文件 / 被中间设备篡改 / 服务端返回了错误页却当成模型存下来」这类问题——
原来只探了 Content-Length，抓不到内容错误。

实测（用官方 Qwen 仓库的 config.json 对照）：API 的 Sha256 与按上面模板下回来的字节
**完全一致**。清单取不到时**降级跳过校验**（只记日志），绝不因为校验而阻断下载。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from voice import paths, plugin

logger = logging.getLogger(__name__)

_TIMEOUT = 60
_CHUNK = 1 << 20          # 1 MB
_UA = "FireflyVoice/1.0"
_stop = threading.Event()
_thread: threading.Thread | None = None


def _url_for(template: str, filename: str) -> str:
    if "{file}" in template:
        return template.replace("{file}", filename)
    return template.rstrip("/") + "/" + filename


# ── 仓库清单（拿 sha256 用）─────────────────────────
API_BASE = "https://www.modelscope.cn"   # 仅当模板是"无 scheme 的相对路径"时的兜底
_MANIFEST_RE = re.compile(r"/models/([^/]+)/([^/]+)/repo\b")


def _manifest_url(template: str) -> str | None:
    """从下载模板推出仓库清单 URL；推不出返回 None（→ 跳过校验）。

    模板形如 `.../api/v1/models/<ns>/<name>/repo?Revision=master&FilePath={file}`，
    清单端点是同一路径下的 `/repo/files`。Revision 沿用模板里的（默认 master）。

    ★ origin **必须从模板自身取**，不能用写死的 modelscope.cn ——
      否则用户配了自建镜像时，清单会跑去魔搭取，sha 与镜像内容对不上 →
      下载被误判成"校验失败"（第一版就是这么写的，被测试 B3 抓出来）。
      自建镜像若没有同一路径的清单端点 → 404 → 返回 {} → 降级跳过校验（安全）。
    """
    m = _MANIFEST_RE.search(template or "")
    if not m:
        return None
    sp = urlsplit(template or "")
    origin = f"{sp.scheme}://{sp.netloc}" if (sp.scheme and sp.netloc) else API_BASE
    rev = "master"
    mr = re.search(r"[?&]Revision=([^&]+)", template or "")
    if mr:
        rev = mr.group(1)
    return (f"{origin}/api/v1/models/{m.group(1)}/{m.group(2)}"
            f"/repo/files?Revision={rev}")


def _manifest(template: str) -> dict:
    """{文件名: {"sha256":…, "size": int}}。任何异常都返回 {}（校验降级，绝不阻断下载）。

    ★ size 也一并取回来 —— 2026-09-18 真机实测发现 ModelScope 对文件 URL 的
      **HEAD 请求返回 200 但没有任何 Content-Length**，所以原来"先 HEAD 探总大小"
      的做法全部拿到 None → `total` 恒为 0 → 百分比退化成 `已下字节×100/1`，
      真机日志里出现过 `31435628400%` 这种数字（UI 只对进度条做了 clamp，文字会照显）。
      清单里本来就有每个文件的精确 Size，直接用它，顺带省掉 8 次 HEAD 往返。
      清单拿不到时 total=0 → 百分比退回"按文件数"，且**绝不按字节算**（见 _download_one）。
    """
    url = _manifest_url(template)
    if not url:
        logger.info("仓库地址不是 ModelScope 形式，跳过 sha256 校验")
        return {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.warning("取仓库清单失败，跳过 sha256 校验: %s: %s", type(e).__name__, e)
        return {}
    out = {}
    for f in ((data or {}).get("Data") or {}).get("Files") or []:
        name = f.get("Name") or f.get("Path")
        sha = (f.get("Sha256") or "").strip().lower()
        if name and sha:
            out[name] = {"sha256": sha, "size": int(f.get("Size") or 0)}
    logger.info("仓库清单: %d 个文件带 sha256，总大小 %d 字节",
                len(out), sum(v["size"] for v in out.values()))
    return out


def _file_sha256(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def _download_one(url: str, dst: Path, base_done: int, total_hint: int,
                  sha_expect: str = "") -> None:
    """单文件下载，支持断点续传 + sha256 对账。进度写进 plugin._DL。

    `total_hint > 0` 才按字节算百分比；否则**只更新 done**，百分比交给调用方按文件数算
    （这样永远不会出现"按字节/1"的荒唐数字）。
    """
    part = dst.with_suffix(dst.suffix + ".part")
    done = part.stat().st_size if part.exists() else 0

    headers = {"User-Agent": _UA}
    if done:
        headers["Range"] = f"bytes={done}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        code = getattr(r, "status", 200)
        if code == 200 and done:
            done = 0                       # 服务器不支持 Range → 从头来
            part.unlink(missing_ok=True)
        # 边下边算哈希：续传时先把已有前缀补算进去（省一次 872MB 的重读）
        h = hashlib.sha256()
        if done and sha_expect:
            with open(part, "rb") as pf:
                for blk in iter(lambda: pf.read(_CHUNK), b""):
                    h.update(blk)
        mode = "ab" if done else "wb"
        with open(part, mode) as f:
            while True:
                if _stop.is_set():
                    raise RuntimeError("已取消")
                buf = r.read(_CHUNK)
                if not buf:
                    break
                f.write(buf)
                if sha_expect:
                    h.update(buf)
                done += len(buf)
                if total_hint > 0:
                    plugin._DL.update({
                        "done": base_done + done,
                        "percent": min(100, int((base_done + done) * 100 / total_hint)),
                    })
                else:
                    plugin._DL.update({"done": base_done + done})
    # 校验：**落盘前**比对，坏了就不覆盖正式文件（下次重下）
    if sha_expect:
        got = h.hexdigest()
        if got != sha_expect:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{dst.name} 校验失败（sha256 不符："
                               f"本地 {got[:12]}… vs 仓库 {sha_expect[:12]}…），已丢弃，请重试")
        plugin._DL.update({"verified": int(plugin._DL.get("verified") or 0) + 1})
    os.replace(part, dst)


def _run(template: str, files: list[str]) -> None:
    md = paths.plugin_root() / "models"
    md.mkdir(parents=True, exist_ok=True)

    # 清单一次取回：sha256（校验）+ size（精确总大小）
    man = _manifest(template)
    plugin._DL.update({"verify": "on" if man else "off", "verified": 0})

    # 总大小直接用清单里的（不再 HEAD 探；ModelScope 的 HEAD 不给 Content-Length）
    total = sum(int((man.get(f) or {}).get("size") or 0) for f in files)
    plugin._DL.update({"total": total, "done": 0, "percent": 0})

    base = 0
    try:
        for i, f in enumerate(files, 1):
            if _stop.is_set():
                raise RuntimeError("已取消")
            dst = md / f
            plugin._DL.update({"file": f"{i}/{len(files)} {f}"})
            # 已有正式文件且哈希正确 → 跳过（支持"断在最后一个文件"后重跑）
            exp = (man.get(f) or {}).get("sha256", "")
            if exp and dst.exists() and dst.stat().st_size > 0:
                if _file_sha256(dst) == exp:
                    logger.info("跳过已就绪且校验通过的文件: %s", f)
                    base += dst.stat().st_size
                    plugin._DL.update({"verified": int(plugin._DL.get("verified") or 0) + 1,
                                       "done": base,
                                       "percent": (min(100, int(base * 100 / total)) if total else 0)})
                    continue
                dst.unlink(missing_ok=True)     # 内容不对 → 删掉重下
            _download_one(_url_for(template, f), dst, base, total, sha_expect=exp)
            base += dst.stat().st_size if dst.exists() else 0
            if not total:
                plugin._DL.update({"percent": int(i * 100 / len(files)),
                                   "done": base, "total": 0})
        plugin._DL.update({"running": False, "percent": 100, "file": "",
                           "error": "", "finished_at": time.time()})
    except Exception as e:
        plugin._DL.update({"running": False, "error": f"{type(e).__name__}: {e}",
                           "finished_at": time.time()})


def start(url: str | None = None, files: list[str] | None = None) -> dict:
    """启动下载（异步）。返回启动后的状态。"""
    global _thread
    template = (url or plugin.repo_url() or "").strip()
    if not template:
        return {"ok": False, "error": "未配置模型仓库地址（见插件页「模型仓库地址」）"}
    if plugin._DL.get("running"):
        return {"ok": False, "error": "已有下载在进行"}
    if _thread is not None and _thread.is_alive():
        return {"ok": False, "error": "已有下载在进行"}

    flist = list(files or plugin.DEFAULT_REPO_FILES)
    _stop.clear()
    plugin.download_reset()
    plugin._DL.update({"running": True, "started_at": time.time(), "file": "准备中…"})
    # ★ 2026-09-19：带上请求线程的用户上下文（见 modules/auto_rest.py 里同一处的详细说明）。
    # contextvars 在新线程里为空、不继承；不带就会落到全局 USER_DIR ⇒ 服务器版写错目录。
    # 语音插件在服务器版被 403（端侧功能），但统一成同一模式，免得以后复用这段时踩坑。
    import contextvars
    ctx = contextvars.copy_context()
    _thread = threading.Thread(target=ctx.run, args=(_run, template, flist),
                               daemon=True, name="voice-download")
    _thread.start()
    return {"ok": True, "download": plugin.download_status()}


def cancel() -> dict:
    _stop.set()
    plugin._DL.update({"running": False, "error": "已取消", "finished_at": time.time()})
    return {"ok": True, "download": plugin.download_status()}