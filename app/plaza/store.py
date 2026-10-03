# -*- coding: utf-8 -*-
"""广场存储层（共创平台 M1）

数据根：环境变量 `FIREFLY_PLAZA_DIR`，缺省 `{USER_DIR.parent}/plaza`
（服务器上 = `/opt/firefly/plaza`；本地版用不到广场，但测试要能在沙箱里跑）。

布局：
```
plaza/
  cards/<id>/card.zip           卡本体（**唯一真相源**）
  cards/<id>/meta.json          广场侧元数据（状态/作者/分类/标签/下载数）
  assets/<id>/thumb.<ext>       列表用小图（V1：列表**只**用它；从卡里抽出，避免反复解压）
  assets/<id>/display.<ext>     详情大图（≤300KB）
  assets/<id>/avatar.<ext>      头像
  assets/<id>/sticker-1..8.<ext> 表情包
  assets/<id>/cover.<ext>       老槽位（M1；V1 起列表不用，仅存量卡兼容）
  audit/<id>.jsonl              审核与治理流水（M2 起；**永不写 API Key**）
```

口径纪律（`docs/错误总结.md` #10 的教训）：**目录是唯一真相源**，不额外维护一份清单文件——
多一份清单就多一个会漂移的口径。列表接口每次扫描 `cards/`（卡量级很小），
meta.json 读不到就跳过并告警，绝不因为一张坏卡让整个广场 500。
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from modules import app_config as cfg
from modules.storage import atomic_write_bytes, atomic_write_json
from plaza import cache as _cache

from plaza import card_format as cf

logger = logging.getLogger(__name__)

STATUSES = ("draft", "pending", "published", "rejected", "archived")
PUBLIC_STATUSES = ("published",)
SORTS = ("new", "hot", "name")
MAX_PAGE_SIZE = 50
REPORT_THRESHOLD = 3          # 几个**不同**举报人即自动转待复核（M3 治理）

# 存储层错误码（与 card_format 的 CARD_* 分开：那些是"卡不合规"，这些是"卡库操作不允许"）
S_EXISTS = "PLAZA_CARD_EXISTS"
S_NOT_FOUND = "PLAZA_CARD_NOT_FOUND"
S_BAD_ID = "PLAZA_BAD_CARD_ID"


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_WRITE_LOCK = threading.RLock()


# ── 路径 ────────────────────────────────────────────
def plaza_root() -> Path:
    """广场数据根。env 优先，缺省与 user_data 同级（服务器 /opt/firefly/plaza）。"""
    import os
    env = (os.environ.get("FIREFLY_PLAZA_DIR") or "").strip()
    if env:
        return Path(env)
    return Path(cfg.USER_DIR).parent / "plaza"


def cards_dir() -> Path:
    return plaza_root() / "cards"


def card_dir(card_id: str) -> Path:
    return cards_dir() / card_id


def card_zip_path(card_id: str) -> Path:
    return card_dir(card_id) / "card.zip"


def meta_path(card_id: str) -> Path:
    return card_dir(card_id) / "meta.json"


def assets_dir(card_id: str) -> Path:
    return plaza_root() / "assets" / card_id


def audit_path(card_id: str) -> Path:
    return plaza_root() / "audit" / f"{card_id}.jsonl"


def valid_card_id(card_id) -> bool:
    from core.preset_parse import _PRESET_ID_RE
    return bool(isinstance(card_id, str) and _PRESET_ID_RE.fullmatch(card_id))


# ── 读 ──────────────────────────────────────────────
def _read_meta(card_id: str) -> dict | None:
    fp = meta_path(card_id)
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("广场卡片 %s 的 meta.json 读取失败，跳过：%s", card_id, e)
        return None
    return data if isinstance(data, dict) else None


def _find_asset(card_id: str, slot: str) -> str | None:
    """返回 assets/<id>/ 下该槽位的实际文件名（含扩展名），没有则 None。"""
    d = assets_dir(card_id)
    if not d.is_dir():
        return None
    for fp in sorted(d.iterdir()):
        if fp.is_file() and fp.stem == slot and fp.suffix.lower() in cf.IMAGE_EXT:
            return fp.name
    return None


def _find_assets(card_id: str) -> dict:
    """**一次目录列举**拿到所有槽位文件名：`{槽位: 文件名或 None}`。

    为什么批量：单个 `_find_asset` 每槽位都要 `iterdir` 一遍，V1 有 13 个槽位
    （thumb/avatar/display/sticker-1..8/cover）⇒ 480 张卡就是 6240 次目录列举，
    冷启动会直接顶穿规模预算（"缓存只包住一半"那版就是这么翻车的，见 cache.py 顶部）。
    """
    out = {slot: None for slot in cf.IMAGE_SLOTS}
    d = assets_dir(card_id)
    if not d.is_dir():
        return out
    for fp in sorted(d.iterdir()):
        if not fp.is_file():
            continue
        stem, ext = fp.stem, fp.suffix.lower()
        if stem in out and ext in cf.IMAGE_EXT and out[stem] is None:
            out[stem] = fp.name
    return out


def get_card(card_id: str, *, statuses=PUBLIC_STATUSES) -> dict | None:
    """取单卡详情（不含卡正文）。`statuses=None` 表示不限状态（管理台用）。"""
    if not valid_card_id(card_id):
        return None
    meta = _read_meta(card_id)
    if not meta:
        return None
    if statuses is not None and meta.get("status") not in statuses:
        return None
    if not card_zip_path(card_id).is_file():
        logger.warning("广场卡片 %s 有 meta 但缺 card.zip，视为不可用", card_id)
        return None
    return _detail_view(meta, card_id)


def _public_view(meta: dict, card_id: str, *, scope: str = "list") -> dict:
    """**列表**投影：绝不包含卡正文，图片只给文件名（不是字节）。

    V1 图片口径（06 §3.3）：
      · **列表只发 `thumb`**，不下发任何大图地址 —— 有 thumb 时 `cover` 明确给 `None`；
      · `avatar` **只有详情才有**（2026-10-01 task-20）：列表带它今天不产生流量，但那是潜伏陷阱 ——
        将来任何客户端顺手按它取图就是一屏 20×≤30KB，正好抵消"从源头砍流量"。
        用**结构性保证**（列表响应里连文件名都不出现）取代"前端别请求"的 UI 纪律；
      · 没有 thumb 的存量卡（V1 之前）在列表里回落到 `cover`，否则老卡在老客户端里会整片白图
        （这条兼容逻辑保持不变）；`scope="detail"` 时 `cover`/`avatar` 照给。
    """
    thumb = _asset_name(card_id, cf.LIST_SLOT)
    cover = _asset_name(card_id, "cover")
    if scope == "list" and thumb:
        cover = None
    out = {
        "id": meta.get("id") or card_id,
        "name": meta.get("name", ""),
        "char_name": meta.get("char_name", ""),
        "user_name": meta.get("user_name", ""),
        "presentation": meta.get("presentation", "sticker"),
        "desc": meta.get("desc", ""),
        "tagline": meta.get("tagline", ""),
        "category": cf.normalize_category(meta.get("category")),
        "tags": list(meta.get("tags") or [])[:cf.MAX_TAGS],
        "author": meta.get("author") or {"uid_hash": "", "display": "", "official": False},
        "status": meta.get("status", "published"),
        "created_at": int(meta.get("created_at") or 0),
        "published_at": int(meta.get("published_at") or 0),
        "downloads": int(meta.get("downloads") or 0),
        "size_bytes": int(meta.get("size_bytes") or 0),
        "digest": meta.get("digest", ""),
        "thumb": thumb,
        "cover": cover,
    }
    if scope != "list":
        out["avatar"] = _asset_name(card_id, "avatar")
    return out


def _detail_view(meta: dict, card_id: str) -> dict:
    """**详情**投影：在列表口径之上补详情才需要的大图与表情包名单（`cover` 也照给）。

    `stickers` 的形状（2026-10-01 统一）：**`[{file, label}]`** —— 契约 06 §3.2 的表情包带 label；
    前端（`plaza_detail.js` / 平台 bundle）两种都吃（`it.file || it.name || it.path` + `it.label`），
    所以老客户端不吃亏、新客户端拿得到标签。标签来自发布时存进 meta 的 `sticker_labels`
    （老卡没有这份 ⇒ label 给空串，形状仍然是对象数组，消费方不用分支）。
    """
    v = _public_view(meta, card_id, scope="detail")
    v["display"] = _asset_name(card_id, "display")
    _labels = meta.get("sticker_labels") or {}
    v["stickers"] = [{"file": n, "label": str(_labels.get(n) or "")[:cf.MAX_STICKER_LABEL]}
                     for n in (_asset_name(card_id, s) for s in cf.STICKER_SLOTS) if n]
    return v


def _sticker_labels_from_files(files: dict) -> dict:
    """从卡内 `stickers.json`（{文件名: 标签}）取标签；缺失/坏格式返回 {}（不抛）。"""
    blob = (files or {}).get("stickers.json")
    if not blob:
        return {}
    try:
        data = json.loads(blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else str(blob))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v)[:cf.MAX_STICKER_LABEL] for k, v in data.items()}


# ── 索引缓存：**已拆到 `plaza/cache.py`**（2026-10-01，P4-11 整改）──────────
# 为什么不留在本文件：缓存有**自己的正确性规矩**（三重校验 + 写后失效 + 有界陈旧），
# 留在这里会把 `store.py` 顶破"无文件 >500 行"铁律。本文件只留薄包装 ⇒ 调用点一处都不用改。
def _invalidate_index() -> None:
    """写操作后调用：内存缓存与持久索引**一起**作废（下架/发布后马上可见）。"""
    _cache.invalidate()


def _asset_name(card_id: str, slot: str) -> str | None:
    """`_find_asset` 的带缓存版本，只给列表/详情的投影用。

    ⚠ 发图那条路（`api.py` 的 asset 端点）仍走未缓存的 `_find_asset`，保证用户拿到的图片永远最新。
    """
    return _cache.asset_name(card_id, slot)


def _scan_cards() -> tuple:
    """真的去扫盘：读每张卡的 meta + 检查卡体在不在 + 记下 mtime + **记下各图片槽位文件名**。

    返回 `(entries, assets)`：
      · `entries = [(目录名, meta, zip_mtime_ns), …]` —— 对外口径不变；
      · `assets  = {目录名: {槽位: 文件名或 None}}`（槽位见 `cf.IMAGE_SLOTS`，一次列举拿全）。

    ⚠ mtime 必须**一起缓存**：它是"同秒发布"时的次级排序键，`list_cards` 原本每条请求
    都要为每张卡 `stat()` 一次 —— 480 张卡实测光这一步就 300~450ms（Windows 上 stat 不便宜），
    导致加了缓存热路径也压不下来（第一版就是这么翻车的：缓存只包住了"读 meta"）。
    ⚠ **资源名也要一起缓存，且必须一次拿全**：投影每张卡要查多个槽位，每个槽位单独
    `iterdir` 一遍 `assets/<id>/` ⇒ V1 的 13 个槽位 × 480 张 = 6240 次目录列举，冷读直接爆预算
    （持久索引最初只存 meta+zip，冷读仍 283ms 正是被它拖的；"只缓存 cover/avatar 两个"
    是同一个病的一半，V1 扩槽位时一并改对）。
    """
    out = []
    assets: dict = {}
    root = cards_dir()
    if not root.is_dir():
        return out, assets
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = _read_meta(d.name)
        if not meta:
            continue
        zp = card_zip_path(d.name)
        if not zp.is_file():          # 卡本体不在 → 列表也不出现（同一事实两个口径）
            logger.warning("广场卡片 %s 有 meta 但缺 card.zip，跳过", d.name)
            continue
        try:
            mt = zp.stat().st_mtime_ns
        except OSError:
            mt = 0
        out.append((d.name, meta, mt))
        assets[d.name] = _find_assets(d.name)
    return out, assets


def _all_cards() -> list:
    """`_scan_cards()` 的两级缓存（内存短 TTL + 落盘 `cards/_index.json`）。

    `list_cards` 与 `stats` 共用它 ⇒ 两者口径天然一致（见 `docs/错误总结.md` #10）。
    实现与正确性规矩见 `app/plaza/cache.py`。
    """
    return _cache.all_cards()


# 注入四件套（`cache.py` 刻意不 import 本模块，避免循环依赖）：
# 全量扫描 / 扫描根 / 单槽位资源查找 / **索引文件路径**。
# ⚠ 索引文件必须在**扫描根之外**：若放进 `cards/`，"写索引"本身就会改 `cards/` 的 mtime，
#   把「目录变了就重扫」这条校验变成**自激** ⇒ 每个请求都全量重扫
#   （实测 480 张热路径 p95 24ms → 490ms，压测当场抓到）。
_cache.configure(scan=_scan_cards, root_getter=cards_dir, asset_getter=_find_asset,
                 index_getter=lambda: plaza_root() / "_index.json")


def list_cards(*, category=None, tag=None, q=None, official=None, sort="new",
               page=1, size=20, statuses=PUBLIC_STATUSES) -> dict:
    """扫描 cards/ 目录并筛选分页。返回 {total, page, size, items, categories}。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 20
    size = max(1, min(MAX_PAGE_SIZE, size))
    if sort not in SORTS:
        sort = "new"
    cat = cf.normalize_category(category) if category else None
    tag_s = str(tag or "").strip()
    q_s = str(q or "").strip().lower()
    official_only = official is True

    items: list = []
    # 扫描走 `_all_cards()`（带短 TTL 缓存）：卡体缺失的卡与 mtime 都已在缓存里，
    # 于是**热路径一次盘都不用碰**（第一版漏了 mtime，每请求仍要 480 次 stat）。
    for _cname, meta, mt in _all_cards():
        if statuses is not None and meta.get("status") not in statuses:
            continue
        view = _public_view(meta, _cname)
        if cat and view["category"] != cat:
            continue
        if tag_s and tag_s not in view["tags"]:
            continue
        if official_only and not view["author"].get("official"):
            continue
        if q_s:
            hay = " ".join([view["name"], view["char_name"], view["desc"],
                            view["tagline"], " ".join(view["tags"])]).lower()
            if q_s not in hay:
                continue
        items.append((view, mt))

    # 同一秒内发布多张卡是常态（批量导入官方卡），单靠整数秒排序不稳定 →
    # 用 card.zip 的纳秒 mtime 做次级键，"最新发布在前"才有确定性。
    if sort == "hot":
        items.sort(key=lambda t: (-t[0]["downloads"], -t[0]["published_at"], -t[1], t[0]["id"]))
    elif sort == "name":
        items.sort(key=lambda t: (t[0]["name"], t[0]["id"]))
    else:
        items.sort(key=lambda t: (-t[0]["published_at"], -t[1], t[0]["id"]))

    views = [t[0] for t in items]
    total = len(views)
    start = (page - 1) * size
    return {"total": total, "page": page, "size": size,
            "items": views[start:start + size], "categories": list(cf.CATEGORIES)}


def _admin_view(meta: dict, card_id: str) -> dict:
    """管理视角：在公开投影之上补状态/举报数/审核结论（**只给管理员**）。"""
    v = _public_view(meta, card_id)
    reports = meta.get("reports") or []
    v.update({
        "report_count": len(reports),
        "reports": [{"reason": str(r.get("reason", ""))[:120],
                     "at": int(r.get("at") or 0)} for r in reports[-10:]],
        "review": {k: (meta.get("review") or {}).get(k) for k in ("verdict", "risk", "model")},
    })
    return v


def add_report(card_id: str, reason: str, by_hash: str = "") -> dict | None:
    """记录一次举报（**同一举报人对同一张卡只算一次**）。

    达到 `REPORT_THRESHOLD` 个不同举报人 → 自动把该卡从 `published` 转为 `pending`
    （公开列表立刻消失，转人工复核）。**不是删除**：内容与审计都留着。

    返回更新后的公开投影；卡片不存在返回 None。
    """
    if not valid_card_id(card_id):
        return None
    with _WRITE_LOCK:
        meta = _read_meta(card_id)
        if not meta:
            return None
        reports = list(meta.get("reports") or [])
        who = str(by_hash or "")[:64]
        if who and any(str(r.get("by", "")) == who for r in reports):
            return _public_view(meta, card_id)          # 同一人重复举报：幂等，不重复计数
        reports.append({"by": who, "reason": str(reason or "")[:120], "at": int(time.time())})
        meta["reports"] = reports[-20:]
        if (len({str(r.get("by", "")) for r in reports if r.get("by")}) >= REPORT_THRESHOLD
                and meta.get("status") == "published"):
            meta["status"] = "pending"
            meta["auto_hidden_at"] = int(time.time())
            logger.warning("广场卡片 %s 举报达阈值 %d，已自动转待复核", card_id, REPORT_THRESHOLD)
        meta["updated_at"] = int(time.time())
        atomic_write_json(meta_path(card_id), meta)
        append_audit(card_id, {"action": "report", "by": who, "reason": str(reason or "")[:120],
                               "count": len(reports), "status": meta.get("status")})
        _touch_cards_dir()          # 原地重写 meta ⇒ 顶目录 mtime（跨进程读者立即生效）
        _invalidate_index()
        return _public_view(meta, card_id)


def admin_list(*, statuses=None, page: int = 1, size: int = 20) -> dict:
    """管理台列表：**不限公开状态**，带举报数与审核结论。"""
    base = list_cards(statuses=statuses if statuses is not None else None,
                      page=page, size=size)
    items = []
    for v in base["items"]:
        meta = _read_meta(v["id"]) or {}
        items.append(_admin_view(meta, v["id"]))
    base["items"] = items
    base["ok"] = True
    return base


def stats() -> dict:
    """广场总量（管理台/自查用）：按状态与分类计数、总字节数。"""
    out = {"cards": 0, "bytes": 0, "by_status": {}, "by_category": {}}
    # 与 list_cards 共用 `_all_cards()` ⇒ 同一个事实只有一个口径（错误总结 #10）
    for _cname, meta, _mt in _all_cards():
        out["cards"] += 1
        out["bytes"] += int(meta.get("size_bytes") or 0)
        st = str(meta.get("status", "published"))
        out["by_status"][st] = out["by_status"].get(st, 0) + 1
        cat = cf.normalize_category(meta.get("category"))
        out["by_category"][cat] = out["by_category"].get(cat, 0) + 1
    return out


# ── 写 ──────────────────────────────────────────────
def _touch_cards_dir() -> None:
    """把 `cards/` 目录 mtime 顶一下 —— **跨进程/手工改盘的"1 次读内生效"开关**（2026-10-01）。

    为什么需要：落盘索引的三重校验是 ① 子目录数、② `cards/` 目录 mtime、③ 有界 TTL(`FILE_TTL`)。
    **原地重写 `cards/<id>/meta.json`** 既不改子目录数、也不动父目录 mtime ⇒ 若是**别的进程**
    （探针/脚本/手工改盘）写的，读者进程会一直吃旧索引，直到 `FILE_TTL`（默认 60s）过期
    —— verifier 的"治理后列表不反映"就是这么来的（task-15 排查结论）。
    写完 meta 顺手 utime 一下，读者的第②条校验当场不过 ⇒ 下一次读就重扫（跨进程也立即生效）。
    **增删卡目录**本来就动 ①/②，不需要这步。

    代价与纪律：只有**写**才动它，热路径/冷读的 O(1) 语义不变；失败**只告警**——
    新鲜度手段不能反过来搞坏主流程（同 `/download/tls` 的纪律）。
    """
    try:
        os.utime(cards_dir(), None)
    except OSError as e:
        logger.warning("顶 cards/ 目录 mtime 失败（跨进程读者可能吃旧索引）: %s", e)


def publish_card(manifest: dict, files: dict, *, status: str = "published",
                 uid_hash: str = "", display: str = "", official: bool = False,
                 replace: bool = False, is_admin: bool = False,
                 extra_meta: dict | None = None) -> dict:
    """发布（或替换）一张卡。

    先 `build_card_zip`（自校验通过才会产出），再原子落盘：card.zip → 抽封面/头像 → meta.json。
    已存在且 `replace=False` 时抛 `CardError(E_UNKNOWN_FILE)` 之外的自定义码见下。
    `is_admin=True`：管理员发布 ⇒ **文字总量不限**（2026-10-01 松绑，见 `card_format`）。
    `extra_meta`：额外写进 meta 的**只记录**字段（如 derive 的 `derived_from`）。
    ⚠ 这些字段**不进任何投影**（`_public_view`/`_detail_view`/管理台列表都按白名单取字段）
    ⇒ 只用于将来追溯，不会暴露给客户端。
    """
    if status not in STATUSES:
        raise ValueError(f"非法状态：{status}")
    m = dict(manifest)
    m["author"] = {"uid_hash": str(uid_hash or "")[:64],
                   "display": str(display or "")[:40],
                   "official": bool(official)}
    data = cf.build_card_zip(m, files, is_admin=is_admin)   # 自带回读自校验
    parsed = cf.parse_card_zip(data, is_admin=is_admin)
    cid = parsed["manifest"]["id"]
    now = int(time.time())

    with _WRITE_LOCK:
        existed = meta_path(cid).exists()
        if existed and not replace:
            raise StoreError(S_EXISTS, f"卡片已存在：{cid}（如需覆盖请显式传 replace=True）")
        old = _read_meta(cid) or {}
        card_dir(cid).mkdir(parents=True, exist_ok=True)
        if not atomic_write_bytes(card_zip_path(cid), data):
            raise OSError(f"card.zip 写入失败：{cid}")
        meta = dict(parsed["manifest"])
        meta.update({
            "status": status,
            "created_at": int(old.get("created_at") or parsed["manifest"].get("created_at") or now),
            "updated_at": now,
            "published_at": int(old.get("published_at") or (now if status == "published" else 0)),
            "downloads": int(old.get("downloads") or 0),
            "size_bytes": len(data),
            "digest": parsed["digest"],
            "card_format": parsed["manifest"]["format"],
        })
        _extract_assets(cid, parsed["files"])
        # 表情包**标签**随 meta 落盘（2026-10-01）：详情投影要发 `[{file,label}]` 而不是
        # 光秃秃的文件名数组（契约 06 §3.2 的 label 语义）；标签原本只活在卡内 `stickers.json`
        # 里，投影时再解 zip 太贵 ⇒ 发布时就存一份（`build_card_zip` 已校验过，这里不会再坏）。
        _labels = _sticker_labels_from_files(parsed["files"])
        if _labels:
            meta["sticker_labels"] = _labels
        if extra_meta:
            meta.update({str(k): v for k, v in extra_meta.items() if v not in (None, "")})
        if not atomic_write_json(meta_path(cid), meta):
            raise OSError(f"meta.json 写入失败：{cid}")
        _touch_cards_dir()          # 原地重写 meta ⇒ 顶目录 mtime（跨进程读者立即生效）
    logger.info("广场卡片已发布：%s（%d 字节，状态 %s）", cid, len(data), status)
    _invalidate_index()
    return _public_view(meta, cid)


def _extract_assets(card_id: str, files: dict) -> None:
    """把卡里的封面/头像抽到 assets/<id>/，供列表页直接取图（长缓存、不重复解压）。

    同时清理同槽位的旧扩展名文件（换了 png→webp 不至于两张图并存）。"""
    d = assets_dir(card_id)
    for slot in cf.IMAGE_SLOTS:
        keep = None
        for rel, blob in files.items():
            p = Path(rel)
            if p.parent.as_posix() == "assets" and p.stem == slot and p.suffix.lower() in cf.IMAGE_EXT:
                keep = p.name
                atomic_write_bytes(d / p.name, blob)
        if keep:
            for fp in (d.iterdir() if d.is_dir() else []):
                if fp.is_file() and fp.stem == slot and fp.name != keep:
                    try:
                        fp.unlink()
                    except OSError:
                        pass


def set_status(card_id: str, status: str) -> bool:
    """治理：改状态（发布/驳回/归档）。"""
    if status not in STATUSES:
        raise ValueError(f"非法状态：{status}")
    if not valid_card_id(card_id):
        return False
    with _WRITE_LOCK:
        meta = _read_meta(card_id)
        if not meta:
            return False
        meta["status"] = status
        meta["updated_at"] = int(time.time())
        if status == "published" and not meta.get("published_at"):
            meta["published_at"] = int(time.time())
        ok = atomic_write_json(meta_path(card_id), meta)
        if ok:
            _touch_cards_dir()      # 原地重写 meta ⇒ 顶目录 mtime（跨进程读者立即生效）
            _invalidate_index()
        return ok


def bump_download(card_id: str) -> int:
    """下载计数 +1，返回新值（计数写失败不影响下载本身）。"""
    if not valid_card_id(card_id):
        return 0
    with _WRITE_LOCK:
        meta = _read_meta(card_id)
        if not meta:
            return 0
        meta["downloads"] = int(meta.get("downloads") or 0) + 1
        atomic_write_json(meta_path(card_id), meta)
        _touch_cards_dir()          # 原地重写 meta ⇒ 顶目录 mtime（跨进程读者立即生效）
        _invalidate_index()
        return meta["downloads"]


def delete_card(card_id: str) -> bool:
    """彻底删除（管理台/自查用）。审计流水保留。"""
    import shutil
    if not valid_card_id(card_id):
        return False
    with _WRITE_LOCK:
        if not card_dir(card_id).is_dir():
            return False
        shutil.rmtree(card_dir(card_id), ignore_errors=True)
        shutil.rmtree(assets_dir(card_id), ignore_errors=True)
    logger.info("广场卡片已删除：%s", card_id)
    _invalidate_index()
    return True


def append_audit(card_id: str, event: dict) -> None:
    """追加一条治理/审核流水。**调用方必须保证不含任何凭据（sk）**。"""
    rec = dict(event)
    rec.setdefault("ts", int(time.time()))
    fp = audit_path(card_id)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("广场审计流水写入失败 %s: %s", card_id, e)
