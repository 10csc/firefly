# -*- coding: utf-8 -*-
"""热更新客户端（v1：只支持 web 层）—— 状态机与编排。

契约见 `docs/热更新/02_实现契约.md`；纪律见 `docs/热更新规范.md`。
本模块是**唯一**改盘的地方（覆盖层 + state.json），`net.py` 只搬字节、`verify.py` 只转发验签。

铁律：
  · 公开函数**永不抛异常**（都兜成状态字段）—— 热更是后台设施，不许拖垮聊天主链。
  · **任何校验不过 ⇒ 整包丢弃**（要么全部可信，要么一个都不留）。
  · 坏补丁不能把用户钉死：`pending` + `boot_fail_count` → 自动回滚到 base。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from core import paths
from hotupdate import net, verify
from modules import app_config as cfg

logger = logging.getLogger(__name__)

# 更新源根地址：客户端实际访问 `{root}/{APP_VERSION}/`。
# 顺序 = 优先级：主源失败自动降级下一个（与整包更新的"双源"同思路）。
# 主源 = 自己的下载网关（8787）：热更包和整包同一个端口，走同一条已加固的公开下载通道。
#   · 用 http 而不是 https **可以接受**：manifest 有 RSA 签名（信任根在 APK 里），
#     中间人改不了内容；能做的只有"拒绝服务"或"回放旧补丁"，而客户端用
#     `serial > applied_serial` + base 精确匹配把回放挡住了（见规范 §四 防回滚）。
# Gitee 兜底镜像待建（同一批文件传一份 release 资产即可）。
# Gitee 兜底镜像（2026-09-19）：把补丁挂到**已有的整包 release 资产**上，而不是新建 release。
#   ★ 为什么不新建 release：APK 更新的 Gitee 兜底是"取 release 列表里 id 最大的 tag"
#     （Gitee 没有 /releases/latest 端点）——新建一个热更 release 会让它取到错 tag，
#     于是 `.../download/<错tag>/firefly.apk` 404，**打断整包兜底下载**。挂到现有 release 上就没这问题。
#   资产 URL = .../releases/download/v0.8.1/<file>，与主源目录形态不同，故用 {base} 占位符 + 'v' 前缀。
DEFAULT_URL_ROOTS = (
    "http://101.200.14.126:8787/hotupdate",
    "https://gitee.com/cpt-asymmetry/firefly/releases/download/v{base}",
)

CHECK_INTERVAL = 24 * 3600      # 规范 §5.1：每天一次
STARTUP_DELAY = 30              # 冷启动后 30 秒
IDLE_CONFIRM = 3.0              # 规范 §5.2.1：连续 3 秒无活动才算空闲
BOOT_FAIL_LIMIT = 2             # 规范 §5.5：连续 2 次带 pending 启动失败 ⇒ 回滚

_LOCK = threading.RLock()
_thread = None

_DEFAULTS = {
    "enabled": True,
    "url_roots": list(DEFAULT_URL_ROOTS),
    "base_version": "",
    "applied_serial": 0,
    "applied_hash": "",
    "pending_serial": 0,
    "boot_fail_count": 0,
    "last_check": 0.0,
    "last_error": "",
    "rolled_back_reason": "",
}

# 运行期状态（不落盘）
_RT = {"available": None, "reload_pending": False, "busy": False, "busy_why": "",
       "last_busy_at": 0.0, "applying": False, "last_note": ""}


# ── 目录与状态 ──────────────────────────────────────
def root() -> Path:
    return paths.hotupdate_root()


def web_dir() -> Path:
    return root() / "web"


def _new_dir() -> Path:
    return root() / "web.new"


def _state_file() -> Path:
    return root() / "state.json"


def load_state() -> dict:
    d = dict(_DEFAULTS)
    if not _DEFAULTS["url_roots"]:
        pass
    try:
        raw = json.loads(_state_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d.update({k: v for k, v in raw.items() if k in _DEFAULTS or k == "enabled"})
            if not d.get("url_roots"):
                d["url_roots"] = list(DEFAULT_URL_ROOTS)
    except Exception:
        pass
    return d


def save_state(d: dict) -> None:
    try:
        root().mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning("写 hotupdate/state.json 失败: %s", e)


def _clear_overlay() -> int:
    """清空覆盖层（回滚/整包升级用）。返回删掉的条目数。"""
    import shutil
    n = 0
    for d in (web_dir(), _new_dir()):
        if d.exists():
            n += sum(1 for _ in d.rglob("*") if _.is_file())
            shutil.rmtree(d, ignore_errors=True)
    for f in root().glob("patch-*.zip"):
        f.unlink(missing_ok=True)
    return n


def _overlay_files() -> int:
    try:
        return sum(1 for p in web_dir().rglob("*") if p.is_file())
    except Exception:
        return 0


def base_urls(d: dict) -> list:
    """把 url_roots 展开成"该 base 的目录 URL"。

    两种写法（2026-09-19 为 Gitee 兜底镜像而加占位符）：
      · 普通根 `<root>`          → `<root>/<APP_VERSION>`（自己的服务器就是这个形态）
      · 含占位符 `<root>{base}`  → 直接替换成 APP_VERSION，**不再追加**
        —— Gitee 的 release 资产 URL 是 `.../releases/download/<tag>/<file>`，
        而 tag 带 `v` 前缀（`v0.8.1`），拼法跟服务器不同，所以需要它。
    """
    roots = d.get("url_roots") or list(DEFAULT_URL_ROOTS)
    out = []
    for r in roots:
        s = str(r).strip()
        if not s:
            continue
        out.append(s.replace("{base}", cfg.APP_VERSION) if "{base}" in s
                   else f"{s.rstrip('/')}/{cfg.APP_VERSION}")
    return out


# ── 空闲判定（规范 §5.2.1）──────────────────────────
def _voice_downloading() -> bool:
    try:
        from voice import plugin as vp
        return bool(vp._DL.get("running"))
    except Exception:
        return False


def idle() -> bool:
    """空闲 = 前端不忙 + 模型没在下载 + 没有补丁在应用。"""
    if _RT["applying"] or _RT["busy"] or _voice_downloading():
        return False
    return (time.time() - float(_RT["last_busy_at"] or 0)) >= IDLE_CONFIRM


def note_activity(busy: bool, why: str = "") -> dict:
    with _LOCK:
        _RT["busy"] = bool(busy)
        _RT["busy_why"] = str(why or "")[:40]
        if busy:
            _RT["last_busy_at"] = time.time()
    return {"ok": True}


# ── 状态输出 ────────────────────────────────────────
def status() -> dict:
    d = load_state()
    with _LOCK:
        avail = _RT["available"]
        out = {
            "ok": True,
            "enabled": bool(d["enabled"]),
            "url_roots": list(d["url_roots"]),
            "base_version": cfg.APP_VERSION,
            "applied_serial": int(d["applied_serial"]),
            "applied_hash": d["applied_hash"],
            "pending_serial": int(d["pending_serial"]),
            "boot_fail_count": int(d["boot_fail_count"]),
            "last_check": d["last_check"],
            "last_error": d["last_error"],
            "rolled_back_reason": d["rolled_back_reason"],
            "overlay_files": _overlay_files(),
            "reload_pending": bool(_RT["reload_pending"]),
            "idle": idle(),
            "busy_why": _RT["busy_why"],
            "verify_ok": verify.available(),
            "verify_backend": verify.backend(),
            "patch_ready": bool(avail),
            "available": ({"serial": avail["serial"], "hash": avail["hash"],
                           "files": len(avail["manifest"].get("files") or []),
                           "note": avail["manifest"].get("note", "")} if avail else None),
            # 客户端上报给排障用的三元组（契约 §二）
            "running": {"base_version": cfg.APP_VERSION,
                        "hot_serial": int(d["applied_serial"]),
                        "patch_hash": d["applied_hash"]},
        }
    return out


# ── 检查（拉索引 → 验签清单）────────────────────────
def check(manual: bool = False) -> dict:
    """拉 latest → 必要时取 manifest 并**验签** → 记 available；顺带执行 kill switch。

    ⚠️ 当 `enabled=False` 时仍然执行本函数（联网），但**只**为让吊销指令生效 —— 规范 §5.1 的安全例外。
    """
    d = load_state()
    urls = base_urls(d)
    if not urls:
        d["last_error"] = "未配置更新源"
        save_state(d)
        return {"ok": False, "error": d["last_error"], "status": status()}

    latest, err, used = None, "", ""
    for u in urls:
        try:
            latest = net.fetch_latest(u)
            used = u
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    if latest is None:
        d["last_error"] = f"检查失败（{err}）"
        d["last_check"] = time.time()
        save_state(d)
        return {"ok": False, "error": d["last_error"], "status": status()}

    if str(latest.get("base") or "") != cfg.APP_VERSION:
        d["last_error"] = f"更新源的 base 不符（{latest.get('base')} != {cfg.APP_VERSION}）"
        d["last_check"] = time.time()
        save_state(d)
        return {"ok": False, "error": d["last_error"], "status": status()}

    serial = int(latest.get("serial") or 0)
    d["last_check"] = time.time()
    applied = int(d["applied_serial"])

    # 没有更新的 serial ⇒ 无需取清单（吊销靠"发一版新的"生效，见契约 §三）
    if serial <= applied:
        with _LOCK:
            _RT["available"] = None
        d["last_error"] = ""
        save_state(d)
        return {"ok": True, "up_to_date": True, "serial": serial, "status": status()}

    try:
        raw, sig = net.fetch_manifest(used, serial)
    except Exception as e:
        d["last_error"] = f"取清单失败：{type(e).__name__}: {e}"
        save_state(d)
        return {"ok": False, "error": d["last_error"], "status": status()}

    if not verify.verify(raw, sig):
        d["last_error"] = "清单验签失败（拒绝该补丁）"
        save_state(d)
        with _LOCK:
            _RT["available"] = None
        return {"ok": False, "error": d["last_error"], "status": status()}

    try:
        m = net.parse_manifest(raw)
    except Exception as e:
        d["last_error"] = f"清单格式非法：{e}"
        save_state(d)
        return {"ok": False, "error": d["last_error"], "status": status()}

    # kill switch：新清单有权宣布"旧 serial 不安全"
    revoked = [int(x) for x in (m.get("revoked_serials") or []) if str(x).isdigit()]
    min_safe = int(m.get("min_safe_serial") or 1)
    bad = applied and (applied in revoked or applied < min_safe)
    d["last_error"] = ""
    save_state(d)
    if bad:
        rollback(f"补丁 {applied} 已被吊销（min_safe={min_safe}, revoked={revoked}）")

    with _LOCK:
        _RT["available"] = {"serial": serial, "hash": net.manifest_hash(raw),
                            "raw": raw, "sig": sig, "manifest": m, "base_url": used}
    return {"ok": True, "serial": serial, "note": m.get("note", ""),
            "files": len(m.get("files") or []), "status": status()}


# ── 应用（下载 → 校验 → 原子替换）────────────────────
def apply_available() -> dict:
    with _LOCK:
        avail = _RT["available"]
    d = load_state()
    if not d["enabled"]:
        return {"ok": False, "error": "热更已关闭（设置里可开启）"}
    if not avail:
        return {"ok": False, "error": "没有已就绪的补丁（先检查更新）"}
    if int(avail["serial"]) <= int(d["applied_serial"]):
        return {"ok": False, "error": f"补丁 {avail['serial']} 已应用过"}

    m = avail["manifest"]
    if str(m.get("base_version")) != cfg.APP_VERSION:
        return {"ok": False, "error": f"补丁底座不符（{m.get('base_version')}）"}

    with _LOCK:
        _RT["applying"] = True
    zip_path = root() / str(m["zip"]["name"])
    try:
        root().mkdir(parents=True, exist_ok=True)
        net.download_zip(avail["base_url"], m["zip"]["name"], zip_path,
                         sha_expect=str(m["zip"].get("sha256") or ""),
                         size_expect=int(m["zip"].get("size") or 0))
        n = net.extract_verified(zip_path, m.get("files") or [], _new_dir())
        # 原子替换：先删旧、再改名。删/换之间那一瞬请求会回落 STATIC_DIR（=base），
        # 所以这个窗口是**无害**的（用户看到的是底座版本，不是白屏）。
        import shutil
        if web_dir().exists():
            shutil.rmtree(web_dir(), ignore_errors=True)
        _new_dir().rename(web_dir())
        zip_path.unlink(missing_ok=True)
        d["applied_serial"] = int(avail["serial"])
        d["applied_hash"] = avail["hash"]
        d["pending_serial"] = int(avail["serial"])
        d["boot_fail_count"] = 0
        d["last_error"] = ""
        d["rolled_back_reason"] = ""
        save_state(d)
        with _LOCK:
            _RT["reload_pending"] = True
            _RT["available"] = None
        logger.info("热更新已应用：serial=%s files=%s", avail["serial"], n)
        return {"ok": True, "serial": avail["serial"], "files": n, "status": status()}
    except Exception as e:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        import shutil
        shutil.rmtree(_new_dir(), ignore_errors=True)
        d["last_error"] = f"应用失败：{type(e).__name__}: {e}"
        save_state(d)
        return {"ok": False, "error": d["last_error"], "status": status()}
    finally:
        with _LOCK:
            _RT["applying"] = False


# ── 回滚 / 开关 / 启动确认 ──────────────────────────
def rollback(reason: str = "") -> dict:
    d = load_state()
    n = _clear_overlay()
    d["applied_serial"] = 0
    d["applied_hash"] = ""
    d["pending_serial"] = 0
    d["boot_fail_count"] = 0
    d["rolled_back_reason"] = reason or "手动回滚"
    save_state(d)
    with _LOCK:
        _RT["reload_pending"] = True
        _RT["available"] = None
    logger.info("热更新已回滚：%s（清掉 %s 个文件）", d["rolled_back_reason"], n)
    return {"ok": True, "removed": n, "reason": d["rolled_back_reason"], "status": status()}


def set_enabled(on: bool) -> dict:
    d = load_state()
    d["enabled"] = bool(on)
    save_state(d)
    return {"ok": True, "enabled": d["enabled"], "status": status()}


def set_url_roots(roots: list) -> dict:
    d = load_state()
    d["url_roots"] = [str(x).strip() for x in (roots or []) if str(x).strip()]
    save_state(d)
    return {"ok": True, "url_roots": d["url_roots"], "status": status()}


def boot_ok() -> dict:
    """前端首帧渲染完成 → 清 pending（规范 §5.5 的"启动成功判据"）。"""
    d = load_state()
    changed = False
    if int(d["pending_serial"]):
        d["pending_serial"] = 0
        changed = True
    if int(d["boot_fail_count"]):
        d["boot_fail_count"] = 0
        changed = True
    if changed:
        save_state(d)
    with _LOCK:
        _RT["reload_pending"] = False
    return {"ok": True, "status": status()}


# ── 启动 ────────────────────────────────────────────
def on_startup() -> dict:
    """进程启动时调一次：整包变更清理 → 安全模式判定 → 起后台检查线程。"""
    d = load_state()
    note = ""
    if d.get("base_version") != cfg.APP_VERSION:
        old = d.get("base_version") or "(无)"
        n = _clear_overlay()
        d = dict(_DEFAULTS)
        d["base_version"] = cfg.APP_VERSION
        d["url_roots"] = list(DEFAULT_URL_ROOTS) if DEFAULT_URL_ROOTS else load_state()["url_roots"]
        d["rolled_back_reason"] = f"整包已升级（{old} → {cfg.APP_VERSION}），旧补丁已作废"
        save_state(d)
        note = f"base 变更，已清空覆盖层（{n} 个文件）"
        logger.info("热更新：%s", note)
    elif int(d["pending_serial"]):
        d["boot_fail_count"] = int(d["boot_fail_count"]) + 1
        save_state(d)
        if d["boot_fail_count"] >= BOOT_FAIL_LIMIT:
            rollback(f"补丁 {d['pending_serial']} 连续 {d['boot_fail_count']} 次未能完成启动，已自动回退")
            note = "安全模式触发，已回滚"
        else:
            note = f"上次补丁未确认启动（{d['boot_fail_count']}/{BOOT_FAIL_LIMIT}）"
    _start_background()
    return {"ok": True, "note": note, "status": status()}


def _start_background() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    # ★ 2026-09-19：带上当前上下文（见 modules/auto_rest.py 同一处的说明）。
    # `hotupdate_root()` = USER_DIR.parent/hotupdate ⇒ 不带上下文时在服务器上会指向全局根。
    # 本模块在服务器版被 403，属预防性统一。
    import contextvars
    ctx = contextvars.copy_context()
    _thread = threading.Thread(target=ctx.run, args=(_worker,), daemon=True, name="hotupdate")
    _thread.start()


def _worker() -> None:
    time.sleep(STARTUP_DELAY)
    while True:
        try:
            r = check()
            if r.get("ok") and r.get("serial"):
                d = load_state()
                if d["enabled"] and idle():
                    apply_available()
            # 应用后等前端确认；确认由 /hotupdate/boot-ok 完成
        except Exception:
            logger.exception("热更新后台轮询异常（已吞掉，不影响主流程）")
        time.sleep(CHECK_INTERVAL)


# ── 测试用 ──────────────────────────────────────────
def reset_for_test() -> None:
    global _thread
    with _LOCK:
        _RT.update({"available": None, "reload_pending": False, "busy": False,
                    "busy_why": "", "last_busy_at": 0.0, "applying": False})
    _thread = None
