# -*- coding: utf-8 -*-
"""包注册表（packs.json）的读写与自愈（阶段 2.8 自 core/presets.py 拆出，纯移动）

`packs.json` 是包存在性的唯一权威（阶段 3.2）；槽位三态维护见 `refresh_slots`（3.3）。
本模块只依赖 preset_parse/paths；对 `MODES` 的引用全部走**函数内 lazy import**
（`core.presets` 会在 import 期反过来 import 本模块，顶层互引会成环）。
"""

import json
import logging
from pathlib import Path

from core import paths as _paths
from core.userctx import _user_ctx_dir
from core.preset_parse import (_PACKS_FILE, PACK_SLOT_FILES, PACK_STATES, PRESET_SCHEMA,
                               SLOT_STATES, _now, _parse_preset, _scan_custom_dirs,
                               slot_state_from_disk)

logger = logging.getLogger(__name__)


def _modes() -> tuple:
    """当前 MODES（lazy import 防循环：presets.py 顶层 import 本模块）。"""
    from core import presets as _p
    return _p.MODES


class PackRegistry:
    """`user_data/packs.json` 的读写与自愈（包存在性的唯一权威）。

    记录字段：id / source(bundled|custom|imported) / schema / name / char_name /
    capabilities / state(active|archived) / slots / created_at / updated_at。
    `slots` 供 3.3 维护卡定义三态（baseline/inherited/customized），本卡只建壳。

    **内置包不入清单**：它们由发行版提供，扫描即可（清单只描述用户区，避免"升级换了内置包
    却要迁移清单"的伪问题）。source=bundled 保留给将来"导入的内置副本"。
    """

    def __init__(self, fp: Path | None = None):
        # C8（审计 2026-09-15）：默认 fp 跟随用户上下文——包数据走 paths.pack_root
        # （带 user_ctx，服务器版按账号隔离），清单原先却固定全局 USER_DIR：
        # 服务器版多账号共用一份 packs.json 却各自拥有 {uid}/{pid}/ 数据，
        # register/unregister 会跨账号串写清单。本地版 _user_ctx_dir()=None，
        # 行为与原先完全一致。
        self.fp = Path(fp) if fp else ((_user_ctx_dir() or _paths.USER_DIR) / _PACKS_FILE)
        self.data: dict[str, dict] = {}
        # 内置包的槽位三态（3.3）：内置包按 3.2 的定论**不进 `packs`**（清单只描述用户区），
        # 但它们的槽位同样需要"是否被用户改过"的记录（story 的 core.md 正是用户最常改的文件），
        # 故单列一个顶层键存放，避免与"清单=用户包"的语义打架。
        self.bundled_slots: dict[str, dict] = {}

    # ── 读（含自愈）──
    def load(self, heal: bool = True, persist: bool = False) -> dict:
        """读清单。

        persist=False（默认）：自愈只在**内存**里做，不落盘 —— 因为本函数会被
        `_discover_presets()` 在 **import 期**调用，而在 import 期写盘会破坏
        "import 零文件系统副作用"（阶段 2.6 的验收）。落盘交给启动链
        （`run_startup_init()` 里显式 `persist()`）与每次真实变更（register/unregister）。"""
        raw = None
        try:
            if self.fp.exists():
                raw = json.loads(self.fp.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("packs.json 读取失败，将从目录自愈重建: %s", e)
        packs = raw.get("packs") if isinstance(raw, dict) else None
        self.data = ({str(k): dict(v) for k, v in packs.items() if isinstance(v, dict)}
                     if isinstance(packs, dict) else {})
        bs = raw.get("bundled_slots") if isinstance(raw, dict) else None
        self.bundled_slots = ({str(k): dict(v) for k, v in bs.items() if isinstance(v, dict)}
                              if isinstance(bs, dict) else {})
        if heal:
            self.heal(persist=persist)
        return self.data

    def persist(self) -> bool:
        """把当前内存清单落盘（启动链用；变更点内部已各自 save）。"""
        return self.save()

    def save(self) -> bool:
        """落盘（原子写）。失败返回 False 并告警——注册表写不进去不应该让主流程崩。"""
        try:
            from modules.storage import atomic_write_json
            doc = {"schema": 1, "updated_at": _now(), "packs": self.data}
            if self.bundled_slots:
                doc["bundled_slots"] = self.bundled_slots
            atomic_write_json(self.fp, doc)
            return True
        except Exception as e:
            logger.warning("packs.json 写入失败: %s", e)
            return False

    # ── 自愈：以**目录实况**为准重建/补齐 ──
    def heal(self, persist: bool = True) -> dict:
        """清单与目录比对：
        - 目录里有合法 preset.json 但清单没有 → 补登记（source=custom）；
        - 清单里有但目录已不存在 → 移除（包真没了，例如手工删目录）；
        - 清单字段缺失（旧清单/手改）→ 用目录里的 preset 补齐。
        差异写日志，便于用户/运维发现"包里多出来/少了什么"。
        persist=False 时只改内存（import 期调用走这条，见 load 的说明）。"""
        on_disk = _scan_custom_dirs()
        added, removed, fixed = [], [], []
        for pid, fp in on_disk.items():
            if pid in self.data:
                continue
            p = _parse_preset(fp, pid)
            if not p:
                continue
            self.data[pid] = self._entry(pid, p, source="custom")
            added.append(pid)
        for pid in list(self.data):
            if pid not in on_disk:
                self.data.pop(pid)
                removed.append(pid)
        for pid, fp in on_disk.items():
            meta = self.data.get(pid)
            if not meta or (meta.get("name") and meta.get("schema") is not None):
                continue
            p = _parse_preset(fp, pid)
            if p:
                self.data[pid] = self._entry(pid, p, source=meta.get("source", "custom"),
                                            created_at=meta.get("created_at"))
                fixed.append(pid)
        if added or removed or fixed:
            logger.warning("packs.json 自愈：新增 %s / 移除 %s / 补字段 %s",
                           added or "-", removed or "-", fixed or "-")
        if persist and (added or removed or fixed or not self.fp.exists()):
            self.save()
        return self.data

    @staticmethod
    def _entry(pid: str, parsed: dict, source: str = "custom",
               created_at: str | None = None) -> dict:
        return {
            "id": pid,
            "source": source,
            "schema": parsed.get("schema", PRESET_SCHEMA),
            "name": parsed.get("name") or pid,
            "char_name": parsed.get("char_name") or "",
            "capabilities": {"presentation": parsed.get("presentation", "sticker"),
                             "knowledge_dirs": parsed.get("knowledge_dirs")},
            "state": "active",
            "slots": {},          # 3.3 填三态（baseline/inherited/customized）
            "created_at": created_at or _now(),
            "updated_at": _now(),
        }

    # ── 变更 ──
    def register(self, pid: str, source: str = "custom", parsed: dict | None = None) -> dict | None:
        """登记（幂等）。parsed=None 时从 `USER_DIR/{pid}/character/preset.json` 解析。"""
        if parsed is None:
            fp = _paths.USER_DIR / pid / "character" / "preset.json"
            if not fp.exists():
                fp = _paths.USER_DIR / pid / "preset.json"
            parsed = _parse_preset(fp, pid) if fp.exists() else None
        if not parsed:
            logger.warning("注册包失败（preset.json 缺失或非法）: %s", pid)
            return None
        old = self.data.get(pid) or {}
        entry = self._entry(pid, parsed, source=source, created_at=old.get("created_at"))
        entry["slots"] = old.get("slots") or {}
        entry["state"] = old.get("state", "active")
        self.data[pid] = entry
        self.save()
        return entry

    def unregister(self, pid: str) -> bool:
        """注销（幂等）。返回是否真的删掉了一条。"""
        if pid in self.data:
            self.data.pop(pid)
            self.save()
            return True
        return False

    def get(self, pid: str) -> dict | None:
        return self.data.get(pid)

    def set_state(self, pid: str, state: str, persist: bool = True) -> bool:
        """改包状态（active|archived，任务 3.5）。包不在清单里返回 False（不是异常）。

        归档 = 清单里留条目 + 数据目录保留，只是 `state=archived` → 被 `active_ids()`
        挡在 MODES 之外（列表/聊天里消失）。**归档不是删除**：3.8 的快照白名单仍含 archived，
        所以归档包的数据照旧进备份。"""
        if state not in PACK_STATES or pid not in self.data:
            return False
        meta = self.data[pid]
        if meta.get("state", "active") == state:
            return False
        meta["state"] = state
        meta["updated_at"] = _now()
        if persist:
            self.save()
        return True

    def active_ids(self) -> list:
        return sorted(k for k, v in self.data.items() if v.get("state", "active") == "active")

    def all_ids(self) -> list:
        """含 archived（快照/同步白名单用：归档 ≠ 丢保险，数据仍要进备份）。"""
        return sorted(self.data)

    # ── 卡定义槽位三态（3.3）──
    def _slot_map(self, pid: str) -> dict | None:
        """取该包的槽位容器：用户包 → `packs[pid].slots`；内置包 → `bundled_slots[pid]`。
        包既不在清单、也不是当前模式 → None（调用方静默放过）。"""
        if pid in self.data:
            return self.data[pid].setdefault("slots", {})
        if pid in _modes():
            return self.bundled_slots.setdefault(pid, {})
        return None

    def get_slot(self, pid: str, rel: str) -> str | None:
        if pid in self.data:
            slots = (self.data[pid] or {}).get("slots")
        else:
            slots = self.bundled_slots.get(pid)
        v = slots.get(rel) if isinstance(slots, dict) else None
        return v if v in SLOT_STATES else None

    def mark_slot(self, pid: str, rel: str, state: str, persist: bool = True) -> bool:
        """记录一个槽位的三态（写入点专用：编辑→customized / 删除副本→inherited）。
        包不在清单里也不是当前模式（例如服务器版无用户包）返回 False —— 不是错误，调用方照常放行。"""
        if state not in SLOT_STATES:
            return False
        slots = self._slot_map(pid)
        if slots is None or slots.get(rel) == state:
            return False
        slots[rel] = state
        if pid in self.data:
            self.data[pid]["updated_at"] = _now()
        if persist:
            self.save()
        return True

    def refresh_slots(self, pid: str | None = None, persist: bool = True) -> int:
        """刷新槽位记录（run_startup_init 调用，也用于恢复包定义后的补齐）。

        规则：**有记录且用户副本仍在 → 一切照旧**（这是本卡的关键：bundled 更新不翻转
        inherited）；没记录、或副本已消失 → 按 `slot_state_from_disk` 重新推导。
        覆盖对象 = 清单里的全部包（含 archived） + 当前模式里的内置包。
        清单里已有的**未知槽位键**（更高版本/手工写的）原样保留，不删。
        返回变更条数。"""
        changed, touched = 0, []
        for _pid in ([pid] if pid else (list(self.data) + [m for m in _modes() if m not in self.data])):
            slots = self._slot_map(_pid)
            if slots is None:
                continue
            before = dict(slots)
            for rel in PACK_SLOT_FILES:
                prev = slots.get(rel)
                user_fp = _paths.pack_root(_pid) / "character" / rel
                if prev in SLOT_STATES and user_fp.exists():
                    continue
                st = slot_state_from_disk(_pid, rel, prev)
                if st != prev:
                    slots[rel] = st
            if slots != before:
                if _pid in self.data:
                    self.data[_pid]["updated_at"] = _now()
                changed += 1
                touched.append(_pid)
        if changed and persist:
            self.save()
        if changed:
            logger.warning("包槽位三态刷新：%s", touched)
        return changed


_REGISTRIES: dict[str, PackRegistry] = {}


def pack_registry(fp: Path | None = None) -> PackRegistry:
    """取当前数据根下的包注册表（按路径缓存：USER_DIR 可被测试替换，故用路径做键）。

    首次访问会 `load()`（**内存自愈**，不落盘）；落盘由启动链
    （`run_startup_init` → `persist()`）与登记/注销变更点负责。
    C8（2026-09-15）：缓存键默认值与 `__init__` 的默认 fp 同公式（用户上下文目录），
    服务器版按账号各自缓存独立实例。"""
    key = str(fp or ((_user_ctx_dir() or _paths.USER_DIR) / _PACKS_FILE))
    reg = _REGISTRIES.get(key)
    if reg is None:
        reg = PackRegistry(Path(key))
        reg.load(persist=False)
        _REGISTRIES[key] = reg
    return reg
