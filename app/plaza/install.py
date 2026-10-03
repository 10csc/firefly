# -*- coding: utf-8 -*-
"""广场卡的安装/卸载（共创平台 M1）

**在哪安装**：永远是"当前这份数据根"——
- 服务器模式：装进该账号的 `user_data/{uid}/{card_id}/`（D1=A 的限额见下）；
- 本地模式：装进本机 `user_data/{card_id}/`，卡体从认证服务器的广场**下载**过来。

**两条取卡路径**：
- 服务器模式：直接读本机广场库 `plaza/cards/{id}/card.zip`（不走网络）；
- 本地模式：`GET {auth_server}/plaza/api/download?id=...`（带 `auth_store` 的 token）。

**安全（本模块的两条硬约束）**：
1. 只写 `character/` 子树：**绝不创建/覆盖 `data/`、`journal/`、`images/`**（用户数据铁律）。
2. **安装时剥离 `preset.json` 的 `knowledge_dirs`**。原因：`_clean_knowledge_dirs`
   只拒绝"仓库外"的路径，内仓库相对目录会被放行 —— 一张别人发布的卡就能让
   **你的**检索器去读仓库内任意目录并注入提示词。卡片按契约只带自己的内容，
   因此这里把它清成 None 再落盘（01 文档已记此约束）。

D1 限额（**仅服务器模式**，用户 2026-10-01 拍板）：每人 ≤5 张、总量 ≤8MB。
本地版不限额（用户自己的磁盘，且本地版本来就能自建任意数量的包）。

**安装不只是"拷文件"**（2026-10-01 共创平台 V1 补）：卡片契约新增的槽位要真的进 App 的
消费链，否则会出现"装上了但用不了、且零报错"：
- `character/knowledge/**` → 知识库缓存失效 + 检索器读用户副本（`modules/llm_retriever`）；
- `character/assets/sticker-N.<ext>` + `character/stickers.json` → 复制进用户表情包目录并登记
  `pack=<卡id>` 的专属表情包（见下"卡上表情包"一节），卸载时按 pack 反向清理；
- `assets/thumb|display` → 由 `/modes` 的槽位回落链兜底（`routes_config`），卡可以只有 thumb。
**残留清理**：卸载必须同时撤掉上面这些"包外产物"，不能只删 `{卡id}/` 目录（残渣会粘在
下一个同 id 的卡上——同一个 id 再装一张就串了）。
"""

import json
import logging
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from modules import app_config as cfg
from modules.storage import atomic_write_bytes

from plaza import card_format as cf
from plaza import store as st

logger = logging.getLogger(__name__)

SOURCE = "plaza"                       # packs.json 里的来源标记（区别于 custom/imported/bundled）
QUOTA_MAX_CARDS = 5                    # 服务器模式：每人最多安装张数
QUOTA_MAX_BYTES = 8 * 1024 * 1024      # 服务器模式：每人安装总量上限
_FETCH_TIMEOUT = 60


def _is_server() -> bool:
    from routes_common import _is_server as f
    return bool(f())


def _user_root() -> Path:
    from core.userctx import _user_ctx_dir
    return Path(_user_ctx_dir() or cfg.USER_DIR)


# ── 取卡 ────────────────────────────────────────────
def fetch_card_zip(card_id: str) -> bytes:
    """取卡体。服务器模式读本机广场库；本地模式从认证服务器下载。"""
    if not st.valid_card_id(card_id):
        raise cf.CardError(cf.E_BAD_MANIFEST, "卡片 id 非法")
    if _is_server():
        card = st.get_card(card_id)
        if not card:
            raise cf.CardError(cf.E_BAD_MANIFEST, "卡片不存在或未公开")
        return st.card_zip_path(card_id).read_bytes()
    from modules.auth_store import get_token
    from routes_auth import _auth_server_base
    base = _auth_server_base().rstrip("/")
    req = urllib.request.Request(f"{base}/plaza/api/download?id={card_id}",
                                 headers={"Authorization": f"Bearer {get_token() or ''}"},
                                 method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise cf.CardError(cf.E_BAD_MANIFEST, "请先登录后再下载角色卡")
        if e.code == 404:
            raise cf.CardError(cf.E_BAD_MANIFEST, "卡片不存在或未公开")
        logger.warning("广场服务返回异常状态：%s", e.code)
        raise cf.CardError(cf.E_BAD_MANIFEST, "广场服务暂时不可用，请稍后重试")
    except Exception as e:
        logger.warning("无法连接广场服务：%s", e)
        raise cf.CardError(cf.E_BAD_MANIFEST, "无法连接广场服务，请检查网络后重试")


# ── 配额（仅服务器模式）──────────────────────────────
def plaza_usage() -> dict:
    """当前数据根下已安装的广场卡：{count, bytes, ids}（按清单 source 统计，不扫盘猜）。"""
    out = {"count": 0, "bytes": 0, "ids": []}
    try:
        reg = cfg.pack_registry()
        data = getattr(reg, "data", {}) or {}
    except Exception as e:
        logger.warning("读取包清单失败：%s", e)
        return out
    root = _user_root()
    for pid, meta in data.items():
        if (meta or {}).get("source") != SOURCE:
            continue
        out["ids"].append(pid)
        out["count"] += 1
        d = root / pid
        try:
            for fp in d.rglob("*"):
                if fp.is_file():
                    out["bytes"] += fp.stat().st_size
        except OSError:
            pass
    return out


def check_quota(new_bytes: int, *, replacing: bool = False,
                is_admin: bool = False) -> tuple[bool, str]:
    """服务器模式配额检查；本地模式恒放行。返回 (ok, 错误文案)。

    `is_admin=True` ⇒ **张数与容量双豁免**（2026-10-01 用户拍板：管理员账号不该被安装配额卡住
    —— 用运营号看广场时被「卡片 0/5 张」拦住没有意义）。

    ⚠ `is_admin` 必须由**调用方**传入：本模块拿不到请求上下文（服务器侧的当前用户由
    `plaza/api.py::_current_user(h)` 解析，`install_card` 已被加上同名形参一路传下来）。
    """
    if not _is_server():
        return True, ""
    if is_admin:
        return True, ""
    u = plaza_usage()
    if not replacing and u["count"] >= QUOTA_MAX_CARDS:
        return False, f"最多安装 {QUOTA_MAX_CARDS} 张广场角色卡（可先卸载不再使用的）"
    if u["bytes"] + new_bytes > QUOTA_MAX_BYTES:
        return False, f"广场角色卡总量不能超过 {QUOTA_MAX_BYTES // 1024 // 1024}MB（当前已用 {u['bytes'] // 1024}KB）"
    return True, ""


# ── 安装 ────────────────────────────────────────────
def _sanitize_preset(blob: bytes) -> bytes:
    """剥离 `knowledge_dirs`（见模块头约束 2），其余字段原样保留。"""
    try:
        data = json.loads(blob.decode("utf-8"))
    except Exception:
        return blob
    if isinstance(data, dict) and data.get("knowledge_dirs"):
        data.pop("knowledge_dirs", None)
        return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    return blob


def _backup_existing(pid: str) -> str:
    """覆盖安装前把旧包目录整体留档（失败只告警，但覆盖动作本身要继续——卡已校验过）。

    留档目录名带**唯一后缀**：同一秒内连续两次覆盖安装时，`f"{pid}-{时间戳}"` 会重名，
    `copytree` 直接抛 FileExistsError ⇒ 留档静默失败（只打一条 warning），
    而"覆盖前留档"是删数据的最后一道保险（本项目踩过"删除不可恢复"）。"""
    src = _user_root() / pid
    if not src.is_dir():
        return ""
    base = _user_root() / "backups" / "plaza"
    stamp = int(time.time())
    dst = base / f"{pid}-{stamp}"
    n = 1
    while dst.exists() and n < 100:
        n += 1
        dst = base / f"{pid}-{stamp}-{n}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        return str(dst)
    except OSError as e:
        logger.warning("覆盖安装前备份失败 %s: %s", pid, e)
        return ""


# ── 卡上表情包 → 本包专属表情包（V1 契约 06 §3.1）──────────────
# 卡里的表情包是 `character/assets/sticker-1..8.<ext>`，而 App 侧表情包**只有一条可用链路**：
# `{数据根}/stickers/registry.json` + `stickers/<文件名>` 的图片（domain/stickers/picker.py
# 的路径校验只认 `stickers/` 前缀）。卡里的图不在那个目录，所以必须**安装时桥接**：
# 复制进用户表情包目录 + 登记 `pack=<卡id>` 的条目 → 组织器/聊天面板按包过滤即可用
# （全局共享 + 本包专属；见 picker.get_enabled_stickers）。
# 卸载/覆盖时按 `pack` 反向清理，不留条目也不留孤儿图。
#
# 写入口用同模块的 `_save_user_entry` 而不是公开的 `add_sticker`：后者校验
# `pack in cfg.MODES`（**全局内置包**），而服务器模式下用户装的广场卡不在全局 MODES 里
# ⇒ 走它必抛。桥接本身是"系统按卡定义登记"，不是"用户在某个当前包下上传"，语义上更贴近
# 前者。表情包是附属品：任何一步失败只告警，绝不让安装/卸载本身失败。
STICKER_REL_PREFIX = "assets/sticker-"


def _sticker_items(files: dict) -> list:
    """从卡文件里挑出表情包槽位 → [(序号, 扩展名, 字节)]，按序号升序。"""
    out = []
    for rel, blob in (files or {}).items():
        rel_s = str(rel).replace("\\", "/")
        if not rel_s.startswith(STICKER_REL_PREFIX):     # 只认 assets/sticker-N.<ext>
            continue
        p = Path(rel_s)
        idx = p.stem[len("sticker-"):]
        if not idx.isdigit():
            continue
        out.append((int(idx), p.suffix.lower() or ".png", blob))
    return sorted(out)


def _sticker_labels(files: dict) -> dict:
    """卡里的 `character/stickers.json`（契约 06 §3.1）→ {序号: label}，容错解析。

    形如 `{"sticker-1.webp": "开心"}`；键按**文件名**匹配（也容忍只写 `sticker-1`）。
    label ≤12 字（与契约同口径，超长截断）；解析失败一律当没有 label（不阻塞安装）。"""
    blob = (files or {}).get("stickers.json")
    if not blob:
        return {}
    try:
        raw = json.loads(bytes(blob).decode("utf-8"))
    except Exception:
        logger.warning("卡内 stickers.json 解析失败，改用占位标签")
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, label in raw.items():
        stem = Path(str(name).replace("\\", "/").rsplit("/", 1)[-1]).stem
        if not stem.startswith("sticker-") or not stem[len("sticker-"):].isdigit():
            continue
        s = str(label or "").strip()
        if s:
            out[int(stem[len("sticker-"):])] = s[:12]
    return out


def _drop_pack_stickers(card_id: str) -> int:
    """删除该卡专属的表情包条目（含不再被引用的图片）。返回删除条数。

    只删 `pack == card_id` 的条目：全局共享表情包与别的包的专属表情包一律不动
    （与 api/pack_lifecycle._detach_pack_stickers 的"贴纸是用户资产"原则一致——
    广场卡的表情包随卡走，所以这里是**删除**而不是降级为全局共享）。"""
    try:
        from domain.stickers.picker import list_all_stickers, delete_sticker
    except Exception as e:                      # pragma: no cover - 模块不可用不该让卸载失败
        logger.warning("表情包模块不可用，跳过卡表情包清理 %s: %s", card_id, e)
        return 0
    removed = 0
    try:
        entries = [s for s in list_all_stickers() if (s.pack or "") == card_id]
    except Exception as e:
        logger.warning("读取表情包注册表失败，跳过清理 %s: %s", card_id, e)
        return 0
    for s in entries:
        try:
            delete_sticker(s.id)
            removed += 1
        except Exception as e:
            logger.warning("清理卡专属表情包失败 %s/%s: %s", card_id, s.id, e)
    return removed


def _install_pack_stickers(card_id: str, files: dict, char_name: str) -> int:
    """把卡上的 `assets/sticker-N.<ext>` 登记为本包专属表情包。返回登记条数。

    幂等：**先清该卡旧条目**（覆盖安装不会堆叠；新版把表情包删光了也要清干净），再写。
    label 取卡里的 `stickers.json`；**缺 label 也合法**（契约 §3.2），此时用
    `{角色名}的表情{N}` 兜底——但那只是占位，组织器按 label 匹配含义
    （pick_sticker_by_label），占位标签基本选不中，详见 07 文档。"""
    _drop_pack_stickers(card_id)                # 覆盖安装：先清旧（哪怕新版一张都没有）
    items = _sticker_items(files)
    if not items:
        return 0
    try:
        from domain.stickers.picker import _save_user_entry, _user_registry_file
    except Exception as e:                      # pragma: no cover
        logger.warning("表情包写入口不可用，跳过卡表情包登记 %s: %s", card_id, e)
        return 0
    labels = _sticker_labels(files)
    d = Path(_user_registry_file()).parent
    n = 0
    for idx, ext, blob in items:
        fname = f"plaza-{card_id}-{idx}{ext}"
        if not atomic_write_bytes(d / fname, blob):
            logger.warning("卡表情包落盘失败（跳过该张）：%s", fname)
            continue
        label = labels.get(idx) or f"{char_name}的表情{idx}"
        try:
            _save_user_entry(f"plaza_{card_id}_{idx}", f"stickers/{fname}", "可爱",
                             label, card_id)
            n += 1
        except Exception as e:
            logger.warning("卡表情包登记失败 %s: %s", fname, e)
    if n:
        logger.info("卡专属表情包已就绪：%s（%d 张，带 label %d 张）", card_id, n, len(labels))
    return n


def _clear_knowledge_cache(card_id: str) -> None:
    """知识库缓存失效（安装/覆盖/卸载都会改 `{包}/character/knowledge/` 的可见内容）。

    漏掉它 = 覆盖安装后检索器仍用旧知识（R-06 的失效入口纪律，见错误总结 #10 同族）。"""
    try:
        from modules.llm_retriever import clear_knowledge_cache
        clear_knowledge_cache(card_id)
    except Exception as e:                      # pragma: no cover
        logger.warning("清知识库缓存失败 %s: %s", card_id, e)


def _knowledge_file_count(card_id: str) -> int:
    """装完后**本包**知识库里的 .md 数量（安装回执；检索器读的就是这个目录）。

    只数用户副本根（广场卡的知识就落在这里），不走 bundled：回执要回答的是
    "这张卡带来了多少知识"，不是"这个 mode 一共能读多少"。"""
    d = _user_root() / card_id / "character" / "knowledge"
    if not d.is_dir():
        return 0
    try:
        return sum(1 for f in d.rglob("*.md") if f.is_file())
    except OSError:
        return 0


def install_card(card_id: str, *, replace: bool = False,
                 is_admin: bool = False) -> tuple[bool, str, dict]:
    """安装（或覆盖）一张广场卡到当前数据根。返回 (ok, 错误文案, info)。

    `is_admin=True` ⇒ 跳过配额（张数/容量都不限，见 `check_quota`）。**所有安装路径都走这里**
    （`plaza/api.py::plaza_install` 传 `_is_admin_user(h)`），别在别处另开旁路，否则豁免只在一半路径生效。
    """
    try:
        data = fetch_card_zip(card_id)
    except cf.CardError as e:
        return False, e.message, {}
    try:
        parsed = cf.parse_card_zip(data)
    except cf.CardError as e:
        return False, f"卡片校验未通过（{e.code}）：{e.message}", {}

    cid = parsed["manifest"]["id"]
    target = _user_root() / cid
    if target.exists() and not replace:
        return False, f"已安装同名角色卡「{parsed['manifest']['name']}」，如需更新请选择覆盖", {}

    ok, why = check_quota(len(data), replacing=target.exists(), is_admin=is_admin)
    if not ok:
        return False, why, {}

    backup = _backup_existing(cid) if target.exists() else ""
    try:
        # 只写 character/：卡里的键都是 character/ 相对路径（01 契约）
        for rel, blob in parsed["files"].items():
            if rel == "preset.json":
                blob = _sanitize_preset(blob)
            dst = target / "character" / rel
            if not atomic_write_bytes(dst, blob):
                raise OSError(f"写入失败：{rel}")
        # 覆盖安装时清掉卡里已不含的旧文件（否则被删掉的槽位会残留旧内容）
        allowed = {Path(r).as_posix() for r in parsed["files"]} | {"preset.json"}
        for fp in (target / "character").rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(target / "character").as_posix()
                if rel not in allowed:
                    try:
                        fp.unlink()
                    except OSError:
                        pass
        reg = cfg.pack_registry()
        entry = reg.register(cid, source=SOURCE, parsed=parsed["preset"])
        if not entry:
            raise OSError("登记进包清单失败")
        cfg.reload_presets()
    except (OSError, cf.CardError) as e:
        logger.warning("安装广场卡失败 %s: %s", cid, e)
        return False, f"安装失败：{e}", {}

    info = {
        "id": cid, "name": parsed["manifest"]["name"],
        "char_name": parsed["manifest"]["char_name"],
        "size_bytes": len(data), "backup": backup,
        "upgraded": bool(backup),
    }
    # 附属内容接进 App 的消费链（失败只告警，不影响"卡已装好"这个结论）：
    # ① 知识库缓存失效（覆盖安装必须让新知识生效）；② 卡上表情包 → 本包专属表情包。
    _clear_knowledge_cache(cid)
    info["knowledge_files"] = _knowledge_file_count(cid)
    info["stickers"] = _install_pack_stickers(cid, parsed["files"],
                                             parsed["manifest"].get("char_name") or "")
    logger.info("广场卡已安装：%s（%d 字节，覆盖=%s，知识文件=%s，专属表情包=%s）",
                cid, len(data), bool(backup), info["knowledge_files"], info["stickers"])
    return True, "", info


def uninstall_card(card_id: str) -> tuple[bool, str]:
    """卸载：仅允许卸载**广场来源**的包（内置包与自建包不归这里管）。

    删除前留一份全量留档（"删除不可恢复"这个坑本项目踩过）。"""
    if not st.valid_card_id(card_id):
        return False, "卡片 id 非法"
    reg = cfg.pack_registry()
    meta = reg.get(card_id) or {}
    if not meta:
        return False, "没有安装这张角色卡"
    if meta.get("source") != SOURCE:
        return False, "只有从广场安装的角色卡才可在此卸载"
    backup = _backup_existing(card_id)
    target = _user_root() / card_id
    try:
        if target.is_dir():
            shutil.rmtree(target)
        reg.unregister(card_id)
        cfg.reload_presets()
    except OSError as e:
        return False, f"卸载失败：{e}"
    # 附属内容一并撤掉（卸载 = 不留残渣）：
    # ① 卡专属表情包条目与图片（pack=card_id 的条目 + 不再被引用的图）；
    # ② 知识库缓存（否则同一 id 再装一张时检索器会用上一张的旧知识）。
    _drop_pack_stickers(card_id)
    _clear_knowledge_cache(card_id)
    logger.info("广场卡已卸载：%s（留档 %s）", card_id, backup or "无")
    return True, ""
