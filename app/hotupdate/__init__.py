# -*- coding: utf-8 -*-
"""热更新客户端（v2：`web/` 前端层 + `py/` Python 层）—— 状态机与编排。

契约见 `docs/热更新/02_实现契约.md`；纪律见 `docs/热更新规范.md`。
本模块是**唯一**改盘的地方（覆盖层 + state.json），`net.py` 只搬字节、`verify.py` 只转发验签。

两层同属一个 serial（规范 §三「单包管两层」），但**生效方式不同**：
  · `web/` → 覆盖层替换 → 空闲时前端 reload（或壳 reload）
  · `py/`  → 覆盖层替换 → 空闲时**重启 App 进程**（Chaquopy 一进程只能 `Python.start()` 一次）
因为方式不同，`status()` 把两个标志分开报：`reload_pending`（可 reload）与
`restart_pending`（必须重启进程，reload 无意义）。**含 py 的补丁必须是"已 import 的模块"**，
否则重启也换不掉旧代码 —— 这一点由 `_overlay_effective()` 在应用时就查清并如实上报（见 §七）。

铁律：
  · 公开函数**永不抛异常**（都兜成状态字段）—— 热更是后台设施，不许拖垮聊天主链。
  · **任何校验不过 ⇒ 整包丢弃**（要么全部可信，要么一个都不留）。
  · **两层要么都换、要么都不换**：先全部落盘校验，再逐个换入；中途失败按逆序还原。
  · 坏补丁不能把用户钉死：`pending` + `boot_fail_count` → 自动回滚到 base。
"""
from __future__ import annotations

import json
import logging
import os
import sys
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
    "applied_layer": "",          # v2：本 serial 含哪些层（"web" / "py" / "web+py"）
    "applied_pid": 0,             # 应用补丁时的进程号（见 boot_ok：同进程的 boot-ok 不算 py 层启动确认）
    "pending_serial": 0,
    "boot_fail_count": 0,
    "last_check": 0.0,
    "last_error": "",
    "rolled_back_reason": "",
}

# 运行期状态（不落盘）
_RT = {"available": None, "reload_pending": False, "restart_pending": False,
       "busy": False, "busy_why": "",
       "last_busy_at": 0.0, "applying": False, "last_note": ""}


# ── 目录与状态 ──────────────────────────────────────
# 覆盖层两层（契约 §四 / §七）：
#   hotupdate/web/       → 前端覆盖层（server.py::_static_file 优先命中）
#   hotupdate/py/        → Python 覆盖层（进程启动时插进 sys.path **首位**，见 start_server.py）
LAYERS = ("web", "py")


def root() -> Path:
    return paths.hotupdate_root()


def web_dir() -> Path:
    return root() / "web"


def py_dir() -> Path:
    return root() / "py"


def layer_dir(layer: str) -> Path:
    return {"web": web_dir, "py": py_dir}.get(layer, web_dir)()


def _new_dir(layer: str = "web") -> Path:
    """下载暂存目录：校验通过后再原子改名成正式覆盖层（契约 §四）。"""
    return root() / f"{layer}.new"


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
    """原子写：先写 `.tmp` 再 replace —— 状态文件写坏 = 回滚判定失去依据（审计 P0-4 同类）。"""
    try:
        root().mkdir(parents=True, exist_ok=True)
        fp = _state_file()
        tmp = fp.with_name(fp.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(fp)
    except Exception as e:
        logger.warning("写 hotupdate/state.json 失败: %s", e)


def _clear_overlay() -> int:
    """清空**两层**覆盖层（回滚/整包升级用）。返回删掉的条目数。"""
    import shutil
    n = 0
    for d in [layer_dir(x) for x in LAYERS] + [_new_dir(x) for x in LAYERS]:
        if d.exists():
            n += sum(1 for _ in d.rglob("*") if _.is_file())
            shutil.rmtree(d, ignore_errors=True)
    # 换入中途留下的备份目录（rename 前的旧层）；正常路径上不存在
    for l in LAYERS:
        bak = root() / f"{l}.old"
        if bak.exists():
            shutil.rmtree(bak, ignore_errors=True)
    for f in root().glob("patch-*.zip"):
        f.unlink(missing_ok=True)
    return n


def _overlay_files() -> int:
    """两层覆盖层的文件总数（状态面板显示用）。"""
    try:
        return sum(1 for l in LAYERS for p in layer_dir(l).rglob("*") if p.is_file())
    except Exception:
        return 0


def _layer_files(layer: str) -> int:
    try:
        return sum(1 for p in layer_dir(layer).rglob("*") if p.is_file())
    except Exception:
        return 0


def applied_layer() -> str:
    """按盘上实际情况给出当前生效的层（不信任 state，防它写坏）。"""
    have = [l for l in LAYERS if _layer_files(l)]
    return "+".join(have)


def layer_from_files(files: list) -> str:
    """清单 files 里出现的前缀 → 层标识（供校验与显示）。"""
    have = [l for l in LAYERS if any(str((f or {}).get("path") or "").startswith(l + "/")
                                     for f in (files or []))]
    return "+".join(have)


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


# ── py 层生效性检查（★ v2 的关键，别删）──────────────
def _overlay_effective(files: list) -> tuple[bool, str]:
    """清单里的 py 文件**重启后是否真的会生效**。

    为什么必须有这条：`py/` 层靠"进程启动早期把覆盖层插进 `sys.path` 首位"生效，
    而**已经 import 过的模块换不掉**（Python import 机制）。如果补丁恰好改了一个
    启动链路上早就 import 的模块，重启也只会加载底座那份 —— 此时若还上报
    `restart_pending=true`，壳就会为一无所获而反复重启用户。

    判据 = **这个模块是否已经被 import 进本进程**（`sys.modules` 才是权威）：

    | 情况 | 判定 |
    |------|------|
    | 在 `sys.modules`，`__file__` 落在覆盖层 | 已经是补丁那份 ⇒ 有效 |
    | 在 `sys.modules`，`__file__` 在底座 | 本进程内换不掉 ⇒ **上报"重启无效"** |
    | 不在 `sys.modules` | 新进程启动时会走覆盖层 ⇒ 有效（**默认放行**） |

    ★ 默认必须是"放行"：漏报 restart 的症状是**改的模块没换、且零提示**（用户以为修好了），
      而误报的代价只是重启一次进程（几秒、不丢数据）。两侧代价不对称，所以宁可多报。

    ★ 早期版本用 `find_spec` 的 origin 判落点，被 E2E 抓到两个假信号：
      ① `find_spec("modules.newthing")` 在子模块**不存在**时会拿父包 `__path__` 兜底回答
         一个 origin ⇒ "补丁新增的模块"被误判成"已从底座加载" ⇒ `restart_pending` 永远为假；
      ② "底座里有、但本进程根本没 import"的模块同样被误判。
      `sys.modules` 两个都能判对，且不依赖任何路径推测。

    ⚠️ 必须在**覆盖层落盘之前**调用（调用点在 `apply_available` 里、`_swap_layers` 之前）。
    """
    pys = [str(f.get("path") or "") for f in (files or [])
           if str(f.get("path") or "").startswith("py/")]
    if not pys:
        return True, ""
    try:
        od = str(py_dir().resolve())
        bad = []
        for p in pys:
            rel = p[len("py/"):]
            if not rel.endswith(".py"):
                continue
            name = rel[:-3].replace("/", ".").removesuffix(".__init__")
            mod = sys.modules.get(name)
            if mod is None:
                continue                          # 尚未 import ⇒ 重启会走覆盖层
            src = str(getattr(mod, "__file__", "") or "")
            if src.startswith(od):
                continue                          # 已经加载的就是覆盖层那份
            bad.append(name)
        if bad:
            return False, f"{len(bad)} 个模块本进程已加载（重启不换代码）：{', '.join(bad[:3])}"
        return True, ""
    except NameError as e:
        # 本函数自己的 bug（例如漏 import sys）—— 一定 log 成 error：
        # 它会被兜成"按可重启处理"，症状是"该报 restart 的不报"或反之，极难察觉。
        logger.error("py 层生效性检查自身有错（按可重启处理）: %s", e)
        return True, ""
    except Exception as e:                     # 检查本身坏了不能挡住补丁应用
        logger.warning("py 层生效性检查异常（按可重启处理）: %s", e)
        return True, ""


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
            "applied_layer": d.get("applied_layer") or applied_layer(),
            "pending_serial": int(d["pending_serial"]),
            "boot_fail_count": int(d["boot_fail_count"]),
            "last_check": d["last_check"],
            "last_error": d["last_error"],
            "rolled_back_reason": d["rolled_back_reason"],
            "overlay_files": _overlay_files(),
            "web_files": _layer_files("web"),
            "py_files": _layer_files("py"),
            "reload_pending": bool(_RT["reload_pending"]),
            # ★ v2：含 py 的补丁**只能**靠重启进程生效；reload 页面换不掉已 import 的模块
            "restart_pending": bool(_RT["restart_pending"]),
            "restart_note": _RT["last_note"],
            "idle": idle(),
            "busy_why": _RT["busy_why"],
            "verify_ok": verify.available(),
            "verify_backend": verify.backend(),
            "patch_ready": bool(avail),
            "available": ({"serial": avail["serial"], "hash": avail["hash"],
                           "layer": avail.get("layer") or layer_from_files(avail["manifest"].get("files")),
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
                            "raw": raw, "sig": sig, "manifest": m, "base_url": used,
                            "layer": layer_from_files(m.get("files"))}
    return {"ok": True, "serial": serial, "note": m.get("note", ""),
            "files": len(m.get("files") or []), "status": status()}


# ── 应用（下载 → 校验 → 原子替换）────────────────────
def _stage_layers(zip_path: Path, files: list) -> dict:
    """把包内每个层的文件解到各自的 `{layer}.new/`（**落盘前逐文件校验 sha256**）。

    返回 {layer: 文件数}。任何一层失败 → 抛异常，由调用方清干净暂存目录。
    """
    out = {}
    for layer in LAYERS:
        sub = [f for f in files if str((f or {}).get("path") or "").startswith(layer + "/")]
        if not sub:
            continue
        # expect_names = **整包**的文件名集合：一层一调时若只看本层子集，
        # 另一层的文件会被"包内多出来的文件"这条校验判为多余 ⇒ 双层包永远装不上。
        out[layer] = net.extract_verified(zip_path, sub, _new_dir(layer),
                                          strip_prefix=layer + "/",
                                          expect_names={str(f.get("path")) for f in files})
    if not out:
        raise ValueError("清单里没有任何 web/ 或 py/ 文件")
    return out


def _swap_layers(staged: dict) -> None:
    """把暂存目录换入正式覆盖层（**两层要么都换、要么都不换**）。

    逐个 `rmtree(旧) → rename(新)`：中途失败按逆序还原（`{layer}.old` 里是本轮刚换下的旧层）。
    最坏情况（还原本身也失败）留下的也是**可用的底座版本**，不是半截文件 ——
    因为正式目录永远是"完整的一层"，不存在写到一半的中间态。
    """
    import shutil
    done = []
    try:
        for layer in LAYERS:
            src = _new_dir(layer)
            if layer not in staged or not src.is_dir():
                continue
            dst = layer_dir(layer)
            bak = root() / f"{layer}.old"
            if bak.exists():
                shutil.rmtree(bak, ignore_errors=True)
            if dst.exists():
                dst.rename(bak)
            src.rename(dst)
            done.append(layer)
        for layer in done:
            shutil.rmtree(root() / f"{layer}.old", ignore_errors=True)
    except Exception:
        for layer in reversed(done):                     # 逆序还原
            bak = root() / f"{layer}.old"
            dst = layer_dir(layer)
            try:
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                if bak.exists():
                    bak.rename(dst)
            except OSError:
                pass
        raise


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
        files = m.get("files") or []
        staged = _stage_layers(zip_path, files)          # 先全部落盘校验（一层不过就整包丢弃）
        layer = "+".join(l for l in LAYERS if l in staged)
        # py 层生效性：必须在换入**之前**问（换入本身就是它要看的目录状态）
        py_ok, py_note = _overlay_effective(files)
        _swap_layers(staged)
        zip_path.unlink(missing_ok=True)
        d["applied_serial"] = int(avail["serial"])
        d["applied_hash"] = avail["hash"]
        d["applied_layer"] = layer
        d["applied_pid"] = os.getpid()      # 供 boot_ok 判断"是不是新进程在确认"
        d["pending_serial"] = int(avail["serial"])
        d["boot_fail_count"] = 0
        d["last_error"] = ""
        d["rolled_back_reason"] = ""
        save_state(d)
        with _LOCK:
            _RT["reload_pending"] = "web" in staged
            # 含 py ⇒ 必须重启进程；py_ok=False（模块早已 import）时不报 restart，
            # 否则壳会为一无所获反复重启用户（见 _overlay_effective 的说明）
            _RT["restart_pending"] = ("py" in staged) and py_ok
            _RT["last_note"] = "" if py_ok else f"py 层重启无效：{py_note}（装新整包后才生效）"
            _RT["available"] = None
        logger.info("热更新已应用：serial=%s layer=%s files=%s",
                    avail["serial"], layer, sum(staged.values()))
        return {"ok": True, "serial": avail["serial"], "layer": layer,
                "files": sum(staged.values()), "restart": bool(_RT["restart_pending"]),
                "note": _RT["last_note"], "status": status()}
    except Exception as e:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        import shutil
        for l in LAYERS:
            shutil.rmtree(_new_dir(l), ignore_errors=True)
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
    d["applied_layer"] = ""
    d["applied_pid"] = 0
    d["pending_serial"] = 0
    d["boot_fail_count"] = 0
    d["rolled_back_reason"] = reason or "手动回滚"
    save_state(d)
    with _LOCK:
        _RT["reload_pending"] = True
        _RT["restart_pending"] = False
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
    """前端首帧渲染完成 → 清 pending（规范 §5.5 的"启动成功判据"）。

    ★ 也是重启生效的完成信号：新进程起来 → 前端报到 → 清掉 `restart_pending`。

    ★ 2026-09-25 真机实测暴露的坑（已修）：**页面 reload 也会重跑前端的 boot()**，
    而它无条件上报 boot-ok ⇒ 含 py 层的补丁在**根本没重启**的情况下被标成"启动成功"，
    于是安全模式的"回滚坏补丁"保护形同虚设（真机日志：进程 PID 没变，却打印
    "新进程已完成启动确认"）。
    判据：应用补丁时记下 `os.getpid()`，boot-ok 若来自**同一个进程**且本次补丁含 py 层，
    就**不清 pending**（页面 reload 换不掉 Python 代码，这类补丁只能靠新进程确认）。
    web 层补丁不受影响：它的生效方式本来就是 reload，同进程确认是正确语义。
    """
    d = load_state()
    layer = str(d.get("applied_layer") or applied_layer())
    same_process = bool(d.get("applied_pid")) and int(d["applied_pid"]) == os.getpid()
    if same_process and "py" in layer:
        logger.info("热更新：同进程的 boot-ok（页面 reload）不构成 py 层启动确认，pending 保留")
        with _LOCK:
            _RT["reload_pending"] = False
        return {"ok": True, "same_process": True, "status": status()}
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
        if _RT["restart_pending"]:
            logger.info("热更新：新进程已完成启动确认，重启标志清零")
        _RT["restart_pending"] = False
    return {"ok": True, "status": status()}


def restart_ready() -> bool:
    """壳问：现在该重启进程吗？（补丁待生效 + 空闲 + 前端无草稿由壳另查）

    `restart_pending` 只在 `apply_available()` 判定"重启确实会换掉代码"时才为真，
    所以这里不再看层 —— 语义是"重启有效且尚未完成"。
    """
    return bool(_RT["restart_pending"]) and idle()


# ── 运行版本标识（用户排障的唯一凭据）────────────────
def running_id() -> dict:
    """当前**真正在跑**的代码版本 = 底座 + 热更序号。

    ★ 为什么必须有它（用户 2026-09-25 明确提的问题："热更新是需要变更版本号的，
    不然无法确定用户处于哪个版本"）。答案不是"热更改版本号"（那会破坏 base 契约），
    而是**让用户能一句话说清自己在跑哪份代码**：

        base_version   装的哪个整包（= APP_VERSION，热更永不改它）
        hot_serial     这个 base 下打到第几个补丁（0 = 没打过）
        patch_hash     那份清单的指纹（前 16 位）—— 两个用户都报 hot1 时靠它区分

    **展示口径（用户 2026-09-25 拍板）**：`0.9.0_hot1` ——
    下划线后缀 `_hot<序号>`，`hot` 是热更新的英文缩写、数字是第几个热更版本。
    没打补丁时**只显示底座版本**（`0.9.0`），不显示 `_hot0`：后缀的语义是"打过补丁"，
    显示 `_hot0` 会让人以为装过补丁却又是第 0 个。

    `id` 是给人看/给人粘的那一行（含指纹）；设置页与诊断包都用它。
    """
    d = load_state()
    serial = int(d.get("applied_serial") or 0)
    out = {"base_version": cfg.APP_VERSION,
           "hot_serial": serial,
           "patch_hash": str(d.get("applied_hash") or ""),
           "layer": str(d.get("applied_layer") or applied_layer())}
    out["display_version"] = (f"{out['base_version']}_hot{serial}" if serial
                              else out["base_version"])
    # 可直接粘贴的一行：展示口径 + 指纹（两个用户都报 hot1 时靠它区分）
    out["id"] = (f"{out['display_version']} · {out['patch_hash']}" if serial
                 else out["display_version"])
    return out


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
        with _LOCK:
            _RT["reload_pending"] = False
            _RT["restart_pending"] = False
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
        _RT.update({"available": None, "reload_pending": False, "restart_pending": False,
                    "busy": False, "busy_why": "", "last_busy_at": 0.0,
                    "applying": False, "last_note": ""})
    _thread = None
