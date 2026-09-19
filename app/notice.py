# -*- coding: utf-8 -*-
"""公告 / 更新说明通道（服务端下发，端侧缓存）—— 见 docs/公告通道规范.md。

**为什么需要它**：热更新只能修 web 层代码，"这次修了什么 / 为什么修 / 用户要注意什么"
得有个能立刻送达每个客户端的地方。APK 里的公告是**静态 HTML**，改服务端用户看不见；
所以要有"服务端下发 + 端侧缓存 + 签名验真"的通道。

与热更新的三条关系（设计上刻意对齐）：
  · **同一个签名密钥与验签桥**（`hotupdate.verify` → Kotlin `HotUpdateBridge.verifyRsaSha256`）
    —— 复用即少一处密码学实现，也少一处出错面。
  · **同一套更新源**（`hotupdate` 的 `url_roots` 翻译而来）：一个配置、两条通道。
  · **同构的防回滚语义**（serial 单调 + revoked_serials）。

与热更新的关键差异（**公告不是代码，风险面完全不同**）：
  · 热更 = 下载 zip → 覆盖代码（可执行）⇒ 必须"整包要么全信要么全丢"。
  · 公告 = 结构化 JSON + 图片 ⇒ **不执行任何东西**。
    所以这里的安全线不是"能不能跑"，而是**"能不能注入"**：
    ① 只接受**结构化块**（p/h/li/tip/img），**不接受任何 HTML**；
    ② 前端一律 `textContent` 渲染，从结构上不存在 XSS 面；
    ③ 长度/条数/张数/字节数全部设上限（审查约束），防"签名过的巨型公告"打爆端侧。

**降级原则**：本模块任何公开函数都不抛异常。拉不到公告时前端继续显示 APK 内置的静态
公告与引导（"静态兜底 + 服务端叠加"），用户永远不会因为公告服务挂了而少东西。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from pathlib import Path

from core import paths
from hotupdate import net as hu_net
from hotupdate import verify
from modules import app_config as cfg

logger = logging.getLogger(__name__)

# ── 审查约束（上限表）──────────────────────────────
# 数值不是拍脑袋：一条公告按"人读得完"计，40 条已经是常年累积的上限；
# 单块 4000 字 ≈ 一篇长文，再多用户也不会读。上限的作用是**兜底**，不是产品约束。
MAX_RAW = 256 * 1024          # 签名清单原始字节上限
MAX_ENTRIES = 40              # 条目数上限
MAX_BLOCKS = 120              # 单条目块数上限
MAX_TITLE = 80
MAX_TEXT = 4000
MAX_ALT = 120
MAX_IMGS_PER_ENTRY = 6
MAX_IMG_BYTES = 1536 * 1024   # 单图上限 1.5MB（公告图，不是相册）
MAX_TOTAL_IMG_BYTES = 8 * 1024 * 1024

CHECK_INTERVAL = 3600         # 缓存新鲜度：1 小时内不重复联网（公告比补丁更在意时效）
_TIMEOUT = 20

_BLOCK_TYPES = ("p", "h", "li", "tip", "img")
_LEVELS = ("info", "warn", "critical")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VER_RE = re.compile(r"\d+")

_LOCK = threading.RLock()
_RT = {"refreshing": False, "last_note": ""}


# ── 目录 ────────────────────────────────────────────
def root() -> Path:
    """公告缓存根（与 hotupdate/ 平级，同样**不进 user_data** —— 备份/快照/清理结构上碰不到）。"""
    return paths.notice_root()


def _state_file() -> Path:
    return root() / "state.json"


def img_dir() -> Path:
    return root() / "img"


# ── 更新源：由热更源翻译而来（一个配置，两条通道）────
def _to_notice_root(u: str) -> str:
    """把热更源根翻译成公告源根。

    两种形态（与 `hotupdate.base_urls()` 同一套约定）：
      · 含 `{base}` 占位符 = Gitee release 资产目录（**扁平**，文件按名直放）
        → 原样使用：公告文件与热更补丁放在同一个 release 目录里；
      · 普通目录根（自己的下载网关）
        → 换掉最后一段路径：`/hotupdate` → `/notice`。
    """
    s = str(u).strip().rstrip("/")
    if not s:
        return ""
    if "{base}" in s:
        return s
    # ★ 必须按 URL 结构解析，不能对字符串 rpartition("/")：
    #   `http://1.2.3.4:8787` 这种"只有主机没有路径"的写法，rpartition 会把
    #   `//` 里的那个斜杠当分隔符 → 翻译成 `http://notice`（2026-09-19 测试 A5 抓出）。
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(s)
    path = p.path.rstrip("/")
    new_path = (path.rpartition("/")[0] + "/notice") if path else "/notice"
    return urlunsplit((p.scheme, p.netloc, new_path, "", ""))


def base_urls() -> list:
    """当前生效的公告源（按优先级）。取热更的 url_roots —— 用户在设置里改一处即可。"""
    from hotupdate import load_state as hu_state
    roots = hu_state().get("url_roots") or []
    out = []
    for r in roots:
        n = _to_notice_root(r)
        if n and n not in out:
            out.append(n.replace("{base}", cfg.APP_VERSION) if "{base}" in n else n)
    return out


# ── 状态读写 ────────────────────────────────────────
def _load() -> dict:
    d = {"serial": 0, "hash": "", "manifest": None, "read_ids": [],
         "last_check": 0.0, "last_error": ""}
    try:
        raw = json.loads(_state_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in d:
                if k in raw:
                    d[k] = raw[k]
    except Exception:
        pass
    if not isinstance(d.get("read_ids"), list):
        d["read_ids"] = []
    return d


def _save(d: dict) -> None:
    try:
        from modules.storage import atomic_write_json
        root().mkdir(parents=True, exist_ok=True)
        atomic_write_json(_state_file(), d)
    except Exception as e:
        logger.warning("写 notice/state.json 失败: %s", e)


# ── 校验（模块铁律的"审查约束"阶段）────────────────
def _ver_tuple(s) -> tuple:
    """版本串 → (major, minor, patch)。非数字段忽略（"0.9.0-beta" → (0,9,0)）。"""
    nums = [int(x) for x in _VER_RE.findall(str(s or ""))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _in_range(ent: dict, app_ver: str) -> bool:
    """条目版本定向：min ≤ 本端 < max（空 = 不限）。"""
    v = _ver_tuple(app_ver)
    lo = str(ent.get("min_app_version") or "").strip()
    hi = str(ent.get("max_app_version") or "").strip()
    if lo and v < _ver_tuple(lo):
        return False
    if hi and v >= _ver_tuple(hi):
        return False
    return True


def _clean_blocks(raw_blocks, allow_imgs: bool) -> list:
    """块级校验：只留白名单类型 + 定长字段。**丢弃**而不是报错（一条坏块不该毁掉整条公告）。"""
    out = []
    for b in (raw_blocks or [])[:MAX_BLOCKS]:
        if not isinstance(b, dict):
            continue
        t = str(b.get("t") or "")
        if t not in _BLOCK_TYPES:
            continue
        if t == "img":
            if not allow_imgs:
                continue
            name = str(b.get("name") or "")
            sha = str(b.get("sha256") or "").lower()
            if not _NAME_RE.match(name) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                continue
            try:
                size = int(b.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0 or size > MAX_IMG_BYTES:
                continue
            out.append({"t": "img", "name": name, "sha256": sha, "size": size,
                        "alt": str(b.get("alt") or "")[:MAX_ALT]})
            continue
        text = str(b.get("text") or "").strip()
        if not text:
            continue
        out.append({"t": t, "text": text[:MAX_TEXT]})
    return out


def validate(raw: bytes) -> dict:
    """验证结果（模块铁律的"验证结果"阶段）：非法一律抛 ValueError，由上层兜成错误字段。"""
    if not raw or len(raw) > MAX_RAW:
        raise ValueError(f"清单大小非法（{len(raw)} 字节）")
    m = json.loads(raw.decode("utf-8"))
    if not isinstance(m, dict):
        raise ValueError("清单不是对象")
    if int(m.get("schema") or 0) != 1:
        raise ValueError(f"清单 schema 不支持（{m.get('schema')!r}）")
    if not isinstance(m.get("entries"), list):
        raise ValueError("entries 不是数组")

    app_ver = cfg.APP_VERSION
    entries, dropped = [], 0
    for e in m["entries"][:MAX_ENTRIES]:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        title = str(e.get("title") or "").strip()
        if not _NAME_RE.match(eid) or not title:
            dropped += 1
            continue
        if not _in_range(e, app_ver):
            continue
        blocks = _clean_blocks(e.get("blocks"), allow_imgs=True)
        if not blocks:
            continue
        imgs = [b for b in blocks if b["t"] == "img"]
        if len(imgs) > MAX_IMGS_PER_ENTRY:
            blocks = [b for b in blocks if b["t"] != "img"]
            blocks += imgs[:MAX_IMGS_PER_ENTRY]
        lvl = str(e.get("level") or "info")
        entries.append({
            "id": eid,
            "title": title[:MAX_TITLE],
            "date": str(e.get("date") or "")[:10],
            "level": lvl if lvl in _LEVELS else "info",
            "pinned": bool(e.get("pinned")),
            "min_app_version": str(e.get("min_app_version") or "")[:16],
            "max_app_version": str(e.get("max_app_version") or "")[:16],
            "blocks": blocks,
        })
    if dropped:
        logger.warning("公告清单里有 %d 条非法条目被丢弃", dropped)
    out = {
        "schema": 1,
        "serial": int(m.get("serial") or 0),
        "generated_at": int(m.get("generated_at") or 0),
        "min_safe_serial": int(m.get("min_safe_serial") or 0),
        "revoked_serials": [int(x) for x in (m.get("revoked_serials") or [])
                            if str(x).isdigit()],
        "entries": entries,
    }
    return out


# ── 图片（独立 URL + sha256 逐个对账）───────────────
def _img_names(manifest: dict) -> list:
    names = []
    for e in manifest.get("entries") or []:
        for b in e.get("blocks") or []:
            if b.get("t") == "img" and b["name"] not in names:
                names.append(b["name"])
    return names


def _fetch_images(url: str, manifest: dict) -> tuple:
    """下载并校验清单引用的图片 → (成功集合, 说明)。单张失败只丢该张。"""
    from modules.storage import atomic_write_bytes
    ok, total, notes = set(), 0, []
    img_dir().mkdir(parents=True, exist_ok=True)
    meta = {}
    for e in manifest.get("entries") or []:
        for b in e.get("blocks") or []:
            if b.get("t") == "img":
                meta[b["name"]] = (b["sha256"], int(b["size"]))
    for name in _img_names(manifest):
        dst = img_dir() / name
        sha_expect, size_expect = meta.get(name, ("", 0))
        try:
            if dst.is_file() and dst.stat().st_size == size_expect and \
                    hu_net.sha256_file(dst) == sha_expect:
                ok.add(name)
                total += size_expect
                continue
            if total + size_expect > MAX_TOTAL_IMG_BYTES:
                notes.append(f"{name}: 超出总字节上限，跳过")
                continue
            data = _get_bytes(f"{url.rstrip('/')}/{name}", MAX_IMG_BYTES + 1)
            import hashlib
            if hashlib.sha256(data).hexdigest() != sha_expect:
                notes.append(f"{name}: sha256 不符，丢弃")
                continue
            if len(data) != size_expect:
                notes.append(f"{name}: 大小不符，丢弃")
                continue
            atomic_write_bytes(dst, data)
            ok.add(name)
            total += size_expect
        except Exception as ex:
            notes.append(f"{name}: {type(ex).__name__}")
    return ok, "; ".join(notes)


def _get_bytes(url: str, cap: int) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "FireflyNotice/1.0",
                                              "Cache-Control": "no-store"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        data = r.read(cap)
    if len(data) >= cap:
        raise ValueError(f"响应超过上限 {cap}")
    return data


def image_path(name: str):
    """取已校验的缓存图片路径（**严格白名单**：只认 img_dir 下的正则名，不存在返回 None）。"""
    if not _NAME_RE.match(str(name or "")):
        return None
    p = img_dir() / name
    try:
        return p if p.is_file() else None
    except OSError:
        return None


# ── 检查（拉 latest → 验签 → 校验 → 下载图 → 落缓存）──
def check(force: bool = False) -> dict:
    """联网刷新。返回 {ok, changed, serial, error}；任何异常都兜成字段。"""
    d = _load()
    fresh = (time.time() - float(d.get("last_check") or 0)) < CHECK_INTERVAL
    if fresh and not force:
        return {"ok": True, "changed": False, "serial": int(d.get("serial") or 0),
                "cached": True}

    urls = base_urls()
    if not urls:
        d["last_error"] = "未配置公告源"
        d["last_check"] = time.time()
        _save(d)
        return {"ok": False, "error": d["last_error"]}

    latest, err, used = None, "", ""
    for u in urls:
        try:
            latest = hu_net.fetch_latest(u)     # 复用热更的 latest 格式（{serial: N}）
            used = u
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    if latest is None:
        d["last_error"] = f"公告检查失败（{err}）"
        d["last_check"] = time.time()
        _save(d)
        return {"ok": False, "error": d["last_error"]}

    serial = int(latest.get("serial") or 0)
    d["last_check"] = time.time()

    # 拉清单：serial 相同也**不重拉**（公告是只增的，同 serial = 同内容）
    if serial <= int(d.get("serial") or 0) and d.get("manifest"):
        d["last_error"] = ""
        _save(d)
        return {"ok": True, "changed": False, "serial": serial, "cached": True}

    try:
        raw, sig = hu_net.fetch_manifest(used, serial, prefix="notice")
    except Exception as e:
        d["last_error"] = f"取公告清单失败：{type(e).__name__}: {e}"
        _save(d)
        return {"ok": False, "error": d["last_error"]}

    if not verify.verify(raw, sig):
        d["last_error"] = "公告清单验签失败（拒绝该公告）"
        _save(d)
        return {"ok": False, "error": d["last_error"]}

    try:
        m = validate(raw)
    except Exception as e:
        d["last_error"] = f"公告清单非法：{type(e).__name__}: {e}"
        _save(d)
        return {"ok": False, "error": d["last_error"]}

    # 清单里的 serial 必须与索引一致（否则"索引说 3、清单说 2"可被用来回放）
    if int(m.get("serial") or 0) != serial:
        d["last_error"] = f"公告 serial 不一致（索引 {serial} vs 清单 {m.get('serial')}）"
        _save(d)
        return {"ok": False, "error": d["last_error"]}

    # kill switch：新清单可宣布"旧 serial 作废"（公告无代码，风险低，只清缓存）
    revoked = m.get("revoked_serials") or []
    if int(d.get("serial") or 0) and int(d["serial"]) in revoked:
        d["manifest"] = None

    ok_imgs, note = _fetch_images(used, m)
    # 图片没下来的块 → 直接去掉（宁可不显示，也不显示裂图）
    kept = []
    for e in m["entries"]:
        blocks = [b for b in e["blocks"]
                  if b["t"] != "img" or b["name"] in ok_imgs]
        if blocks:
            e = dict(e, blocks=blocks)
            kept.append(e)
    m["entries"] = kept

    d["serial"] = serial
    d["manifest"] = m
    d["last_error"] = ""
    # 已被撤回的条目从已读表里清掉（否则那条 id 永远占着已读位）
    live = {e["id"] for e in kept}
    d["read_ids"] = [i for i in d["read_ids"] if i in live]
    _save(d)
    return {"ok": True, "changed": True, "serial": serial,
            "entries": len(kept), "img_note": note}


# ── 对外输出 ────────────────────────────────────────
def payload(auto_refresh: bool = True) -> dict:
    """给前端的渲染数据。**永远返回 ok=True**（没有公告也是一种正常状态，不是错误）。

    `auto_refresh`：缓存过期时**后台**刷新，本次先回缓存 —— 公告是"顺带看一眼"的东西，
    绝不能让面板打开卡在网络上（这是它和 /hotupdate/status 的关键差别）。
    """
    d = _load()
    m = d.get("manifest") or {}
    read_ids = set(d.get("read_ids") or [])
    entries = []
    for e in (m.get("entries") or []):
        e = dict(e)
        e["unread"] = e["id"] not in read_ids
        entries.append(e)
    unread = sum(1 for e in entries if e["unread"])
    pending = False
    if auto_refresh and (time.time() - float(d.get("last_check") or 0)) >= CHECK_INTERVAL:
        with _LOCK:
            if not _RT["refreshing"]:
                _RT["refreshing"] = True
                pending = True
                threading.Thread(target=_refresh_bg, daemon=True, name="notice").start()
    return {
        "ok": True,
        "serial": int(d.get("serial") or 0),
        "generated_at": int(m.get("generated_at") or 0),
        "app_version": cfg.APP_VERSION,
        "entries": entries,
        "unread": unread,
        "refreshing": pending,
        "last_check": float(d.get("last_check") or 0),
        # 排障用：last_error 只在"从未成功过"时才外露，避免旧错误长期吓人
        "last_error": "" if entries else str(d.get("last_error") or ""),
    }


def _refresh_bg() -> None:
    try:
        check()
    except Exception:
        logger.exception("公告后台刷新异常（已吞掉）")
    finally:
        with _LOCK:
            _RT["refreshing"] = False


def mark_read(ids=None, all_read: bool = False) -> dict:
    """标记已读。`ids` 为条目 id 列表，或 `all_read=True` 全标。返回未读数。"""
    d = _load()
    m = d.get("manifest") or {}
    live = [e.get("id") for e in (m.get("entries") or [])]
    cur = set(d.get("read_ids") or [])
    if all_read:
        cur |= set(live)
    else:
        want = ids if isinstance(ids, list) else []
        cur |= {str(i) for i in want if str(i) in live}
    d["read_ids"] = sorted(cur)
    _save(d)
    return {"ok": True, "read": len(d["read_ids"]),
            "unread": max(0, len(live) - len(cur))}


def status() -> dict:
    """轻量状态（运维/排障用，不含正文）。"""
    d = _load()
    m = d.get("manifest") or {}
    return {
        "ok": True,
        "serial": int(d.get("serial") or 0),
        "entries": len(m.get("entries") or []),
        "images": len(_img_names(m)),
        "read_ids": len(d.get("read_ids") or []),
        "last_check": float(d.get("last_check") or 0),
        "last_error": str(d.get("last_error") or ""),
        "urls": base_urls(),
        "verify_ok": verify.available(),
        "verify_backend": verify.backend(),
        "dir": str(root()),
    }


# ── 测试用 ──────────────────────────────────────────
def reset_for_test() -> None:
    with _LOCK:
        _RT.update({"refreshing": False, "last_note": ""})
