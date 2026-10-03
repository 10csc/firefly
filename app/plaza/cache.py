# -*- coding: utf-8 -*-
"""广场索引的两级缓存（共创平台 M4 规模压测 → P4-11）

**为什么单独成模块**：`store.py` 管的是"卡的生命周期"，而缓存有**自己的正确性规矩**
（三重校验、写后失效、有界陈旧），塞在一起会把 `store.py` 顶破"无文件 >500 行"铁律。
本模块**刻意不 import `store`**：扫描函数、路径函数、资源查找函数由 `store` 在模块末尾
用 `configure()` 注入，从而没有循环依赖。

## 两级缓存

| 级别 | 开关 | 作用 |
|---|---|---|
| 内存短 TTL | `FIREFLY_PLAZA_INDEX_TTL`（默认 2s，`0` = 全关） | 把"**每个**请求都慢"变成"每 TTL 一次慢" |
| 落盘索引 `cards/_index.json` | `FIREFLY_PLAZA_INDEX_FILE_TTL`（默认 **300s**，`0` = 全关） | 把"每 TTL 一次慢"也压掉：冷启动只需 一次 `stat` + 一次 `scandir` 计数 + 一次 JSON 读 |

实测（`tests/test_plaza_scale.py`）：480 张卡热路径 p95 451ms → **24ms**；冷启动 431ms → **预算内**。

### 为什么文件 TTL 是 300s（原 60s；2026-10-01 task-19/20 实测后放宽）

`tests/test_plaza_scale_v1.py` 实测：480 张 V1 卡走落盘索引的冷读 **6ms**，但**第③条判过期那一次读
要全量重扫 460–500ms**（旧格式同口径 392ms，均已超 300ms 预算）⇒ 60s 的 TTL 等于"**每分钟一次
500ms 尖峰**"。放宽到 300s 把它变成"每 5 分钟一次"，代价可忽略：

1. task-17 之后**所有 store 写路径**都 `os.utime(cards_dir())` ⇒ "程序写入的即时性"由**第②条**接管
   （跨进程/手工调用 store 也在 1 次读内生效）；
2. 第③条因此只剩"**手工改盘且不动目录 mtime**"这一个极端兜底 —— 5 分钟的有界陈旧完全可以接受；
3. 热路径与冷读的 O(1) 语义不变（②③都只是"何时重扫"的开关）。

## 正确性：**不信文件**，三重校验

1. `dir_count` 与实时 `scandir` 计数不符 ⇒ 重扫（**自愈**：外面增删过卡）；
2. `cards/` 目录 mtime 变了 ⇒ 重扫（跨进程新增**不必等 TTL**就能被看见；原地重写 meta 由写路径
   的 `os.utime(cards_dir())` 触发，见 `store._touch_cards_dir()`）；
3. `written_at` 超过文件 TTL ⇒ 重扫（**有界陈旧**，防"别的进程/手工直接改盘"无限期脏读）。

写操作必须调 `invalidate()`：**内存与文件一起作废**。否则"下架"这类**只改卡内 `meta.json`、
不动 `cards/` 目录 mtime** 的写会被第 2 条漏掉，出现"下架了还挂在广场上"的治理事故。
"""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from modules.storage import atomic_write_json
from plaza import card_format as cf

logger = logging.getLogger(__name__)

# 持久索引文件版本：**改形状必须升版本**（旧文件直接被拒 → 重扫，不会带着半套数据继续用）。
# v2 → v3（2026-10-01，V1 制卡自由度）：assets 从 {cover,avatar} 扩到全部图片槽位
# （thumb/display/sticker-1..8），V2 的索引里没有这些槽位，若继续认会让列表读到
# "thumb=None" 直到 TTL 过期 —— 典型的"缓存只缓存了一半"。
INDEX_VERSION = 3


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return float(default)


# 排查"持久索引为何没吃到"时开：`FIREFLY_PLAZA_INDEX_DEBUG=1`（默认关，生产不输出）
_DEBUG = os.environ.get("FIREFLY_PLAZA_INDEX_DEBUG", "") not in ("", "0")


def _dbg(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write("[PLAZA-IDX] %s\n" % msg)


MEM_TTL = _env_float("FIREFLY_PLAZA_INDEX_TTL", 2.0)
# 文件 TTL 默认 300s（原 60s，2026-10-01 task-19/20）：模块顶部「为什么文件 TTL 是 300s」写了推理链 ——
# 第②条接管程序写入的即时性后，第③条只剩极端兜底，放宽 5× 消掉"每分钟一次 460–500ms 全量重扫尖峰"。
FILE_TTL = _env_float("FIREFLY_PLAZA_INDEX_FILE_TTL", 300.0)

_LOCK = threading.Lock()
# `mtime` = 建这份缓存时 `cards/` 目录的 mtime；每次读都校验一次（一个 stat 的事）
_MEM: dict = {"t": 0.0, "entries": None, "mtime": None}
_ASSETS: dict = {}            # (card_id, slot) → (t, 文件名)；写操作时整体清空
_DIRTY: dict = {"v": False}    # 写操作后置真：下一次读必须重扫并重写索引文件

# 由 `store.configure()` 注入（避免循环依赖）
_scan = None
_root = None
_asset = None
_index_path = None


def configure(*, scan, root_getter, asset_getter, index_getter) -> None:
    """注入 `store` 侧的四件套：全量扫描、扫描根、单槽位资源查找、**索引文件路径**。

    ⚠ `index_getter` 指向的路径**必须在扫描根之外**（本项目是 `plaza_root()/_index.json`，
    而扫描根是 `plaza_root()/cards/`）。> 一开始我把它放在 `cards/_index.json`，
    **写索引本身就会改 `cards/` 的 mtime** ⇒ 第 ② 条校验每次都被自己触发 ⇒ 每个请求都全量重扫，
    480 张热路径 p95 从 24ms 反弹到 **490ms**（压测当场抓到）。这类"缓存文件的写入反馈到
    它自己的失效判据"是自激，位置一挪就断。
    """
    global _scan, _root, _asset, _index_path
    _scan, _root, _asset, _index_path = scan, root_getter, asset_getter, index_getter


def invalidate() -> None:
    """写操作后调用：内存缓存与持久索引**一起**作废。"""
    with _LOCK:
        _MEM["entries"] = None
        _MEM["mtime"] = None
        _ASSETS.clear()
    _DIRTY["v"] = True


def _index_file_path() -> Path:
    """索引文件路径（**在扫描根之外**，见 `configure()` 的告警）。"""
    return _index_path()


def _count_dirs(root: Path) -> int:
    """数广场根下的**子目录**数（一次 `scandir`，480 个 ≈ 几 ms）。

    自愈校验的核心：比"逐张读 meta + 逐张 stat zip"便宜两个数量级，
    却足以发现"外面增删过卡"（`_index.json` 自己是文件，不会被算进去）。
    """
    try:
        with os.scandir(root) as it:
            return sum(1 for e in it if e.is_dir())
    except OSError:
        return -1


def _load_file_index(root: Path, st):
    """读持久索引。返回 `(entries, assets)`；任一校验不过返回 `(None, None)`（调用方重扫）。"""
    if FILE_TTL <= 0 or _DIRTY["v"]:
        _dbg("拒：FILE_TTL=%s dirty=%s" % (FILE_TTL, _DIRTY["v"]))
        return None, None
    try:
        doc = json.loads(_index_file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        _dbg("拒：读/解析失败 %r" % (e,))
        return None, None
    if not isinstance(doc, dict) or doc.get("v") != INDEX_VERSION:
        _dbg("拒：版本不符 %r" % (doc.get("v") if isinstance(doc, dict) else type(doc),))
        return None, None
    entries = doc.get("entries")
    assets = doc.get("assets")
    if not isinstance(entries, list) or not isinstance(assets, dict):
        _dbg("拒：entries/assets 形状非法")
        return None, None
    n_dirs = _count_dirs(root)
    if doc.get("dir_count") != n_dirs:          # ① 增删过 ⇒ 自愈重扫
        _dbg("拒①：dir_count 记录=%s 实际=%s" % (doc.get("dir_count"), n_dirs))
        return None, None
    if doc.get("dir_mtime_ns") != st.st_mtime_ns:          # ② 目录被改过
        _dbg("拒②：mtime 记录=%s 实际=%s" % (doc.get("dir_mtime_ns"), st.st_mtime_ns))
        return None, None
    try:
        age = time.time() - float(doc.get("written_at") or 0)
        if age > FILE_TTL:                                 # ③ 有界陈旧
            _dbg("拒③：年龄 %.1fs > FILE_TTL %.1fs" % (age, FILE_TTL))
            return None, None
    except (TypeError, ValueError):
        _dbg("拒③：written_at 非法")
        return None, None
    out = []
    for e in entries:
        if not (isinstance(e, list) and len(e) == 3):
            _dbg("拒：条目形状非法")
            return None, None
        out.append((e[0], e[1], e[2]))
    warm = {k: v for k, v in assets.items() if isinstance(v, dict)}
    _dbg("命中：%d 条 + %d 张资源名（年龄 %.1fs）" % (len(out), len(warm), age))
    return out, warm


def _save_file_index(entries: list, assets: dict, root: Path, st) -> None:
    """把扫描结果（**含资源名**）落盘（best-effort：失败只告警，绝不影响请求）。"""
    if FILE_TTL <= 0:
        return
    doc = {"v": INDEX_VERSION, "written_at": time.time(), "dir_count": _count_dirs(root),
           "dir_mtime_ns": st.st_mtime_ns,
           "entries": [[c, m, t] for c, m, t in entries], "assets": assets}
    if atomic_write_json(_index_file_path(), doc):
        _DIRTY["v"] = False


def _warm_assets(assets: dict, now: float) -> None:
    """把索引里的资源名灌进 `_ASSETS`：冷读之后**投影不必再去 iterdir**。

    这是"冷读真的 O(1)"的另一半 —— 只缓存 meta 而资源名每次重查时，480 张卡
    仍有上千次目录列举（≈260ms+），冷启动就卡在设计预算边缘。
    ⚠ 槽位表取 `card_format.IMAGE_SLOTS`（**单一真相源**）：V1 扩了 thumb/display/sticker，
    这里若还硬编码 ("cover","avatar") 就等于只缓存一半 —— 列表里的 thumb 会永远查不到。
    """
    if not assets:
        return
    with _LOCK:
        for cid, slots in assets.items():
            if not isinstance(slots, dict):
                continue
            for slot in cf.IMAGE_SLOTS:
                _ASSETS[(cid, slot)] = (now, slots.get(slot))


def all_cards() -> list:
    """两级缓存版的全量扫描。筛选/排序/分页留在调用方（`store`）。

    `MEM_TTL=0` = 两级缓存**全关**（排查脏读时用）。
    """
    if MEM_TTL <= 0:
        entries, _assets = _scan()
        return entries
    root = _root()
    try:
        st = root.stat()
    except OSError:
        st = None
    now = time.time()
    with _LOCK:
        ent = _MEM["entries"]
        # 快路径：TTL 内 **且** 目录 mtime 没变（一个 stat 换"跨进程新增立即可见"）
        if (ent is not None and (now - _MEM["t"]) < MEM_TTL
                and (st is None or _MEM["mtime"] == st.st_mtime_ns)):
            return ent
    if st is not None:                      # ★ 冷路径先试持久索引（P4-11 的关键）
        out, warm = _load_file_index(root, st)
        if out is not None:
            _warm_assets(warm, now)
            with _LOCK:
                _MEM["entries"] = out
                _MEM["t"] = now
                _MEM["mtime"] = st.st_mtime_ns
            return out
    out, assets = _scan()
    _warm_assets(assets, now)
    with _LOCK:
        _MEM["entries"] = out
        _MEM["t"] = now
        _MEM["mtime"] = None if st is None else st.st_mtime_ns
    if st is not None:
        _save_file_index(out, assets, root, st)
    return out


def asset_name(card_id: str, slot: str) -> str | None:
    """单槽位资源名的**带缓存**版本（列表/详情投影用）。

    病：投影每张卡要查 `cover` + `avatar`，而每次查找都要 `iterdir` 一遍 `assets/<id>/`
    ⇒ 480 张卡时每请求约 **960 次目录列举**（前两版缓存漏了它，所以热路径一直压不下来）。
    ⚠ 发图端点（`api.py`）**不走**这里，保证用户拿到的图片永远是最新的。
    """
    if MEM_TTL <= 0:
        return _asset(card_id, slot)
    key = (card_id, slot)
    now = time.time()
    with _LOCK:
        ent = _ASSETS.get(key)
    if ent is not None and (now - ent[0]) < MEM_TTL:
        return ent[1]
    val = _asset(card_id, slot)
    with _LOCK:
        _ASSETS[key] = (now, val)
    return val
