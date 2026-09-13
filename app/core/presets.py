# -*- coding: utf-8 -*-
"""预设包注册表 — 扫描 assets/character/*/preset.json 得到 PRESETS/MODES（任务 2.1 自 app_config 拆出）

`MODES` 会被 reload_presets() **重新赋值**（reload 后消费方必须重新读），
其他模块请经 `presets.MODES` 或 `cfg.MODES` 属性访问，不要按值 import。
"""

import json
import os
import re
import time
import logging
from pathlib import Path

from core import paths as _paths
from core.paths import BASE_DIR, ROOT

logger = logging.getLogger(__name__)


# ── 模式（预设包注册表）─────────────────────────────
# 一个预设包 = assets/character/{id}/ 一个目录（含 preset.json 清单），
# 打包三样东西：角色（人格/口吻）、剧本（知识库/核查口径）、演出形态（presentation）。
# 每个模式独立数据根：USER_DIR/{mode}/，其下 character/ data/ journal/ 各一份。
# story = 剧情模式（流萤·主线）；haruno = 春日手信（匹诺康尼黄金时刻·普通学生旅行AU）。
_PRESET_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# 预设包格式版本（R-05，2026-09-10）：当前版本。preset.json 可选声明 "schema"，
# 缺失视为 1；高于本值时只告警并按已知规则解析（前向兼容，不拒绝加载）。
PRESET_SCHEMA = 1


def _clean_knowledge_dirs(raw) -> list[str] | None:
    """knowledge_dirs 入口校验（R-10，2026-09-13）：只收**仓库内**的相对目录。

    原来只 `.strip().strip("/")`，于是 preset.json 里写 `../../任意目录` 或 `C:/x`
    会被原样收下，llm_retriever 再 `ROOT / d` 直接去读——自建包是用户可编辑的，
    等于把"读仓库外任意文件并注入提示词"的口子交给了包定义。
    非法项**逐项跳过并告警**（不整包拒绝）：一个坏目录不该让整个角色包不可用。
    判定与 sticker_picker 的 registry.json 路径校验同口径：拒绝绝对路径、盘符、`..`，
    以及 resolve 后落在 ROOT 之外的目录（防符号链接绕出仓库）。"""
    if not isinstance(raw, list):
        return None
    root = ROOT.resolve()
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        # 注意判定顺序：**先判绝对路径，再归一化首尾斜杠**。
        # 反过来（先 strip("/")）会把 "/etc/passwd" 变成 "etc/passwd"，
        # 绝对路径检查就永远命不中——这是本卡实现时踩到的真实坑，测试 E4 钉死。
        if s.startswith(("/", "\\")) or (len(s) >= 2 and s[1] == ":"):
            logger.warning("预设包声明的知识库目录是绝对路径，已跳过: %r", x)
            continue
        d = s.strip("/").replace("\\", "/")
        if not d or ".." in d.split("/"):
            logger.warning("预设包声明的知识库目录越界，已跳过: %r", x)
            continue
        try:
            (root / d).resolve().relative_to(root)
        except (OSError, ValueError):
            logger.warning("预设包声明的知识库目录不在仓库内，已跳过: %r", x)
            continue
        out.append(d)
    return out


def _parse_preset(fp: Path, expect_id: str) -> dict | None:
    """解析单个 preset.json 并校验（id 须等于目录名）。非法返回 None（告警不阻塞）。"""
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("预设包 %s 的 preset.json 解析失败，跳过: %s", expect_id, e)
        return None
    pid = str(data.get("id", "")).strip()
    if pid != expect_id or not _PRESET_ID_RE.fullmatch(pid):
        logger.warning("预设包 %s 的 id 非法或与目录名不一致，跳过", expect_id)
        return None
    name = str(data.get("name", "")).strip()
    cname = str(data.get("char_name", "")).strip()
    uname = str(data.get("user_name", "")).strip()
    if not name or not cname or not uname:
        logger.warning("预设包 %s 缺必填字段（name/char_name/user_name），跳过", pid)
        return None
    presentation = str(data.get("presentation", "")).strip()
    if presentation not in ("sticker", "narration", "none"):
        logger.warning("预设包 %s 的 presentation 非法（%r），回退 sticker", pid, presentation)
        presentation = "sticker"
    # 知识库目录（可选）：包显式声明的仓库相对路径清单；不声明则用包内 knowledge/（存在才挂）
    # R-10：越界路径（../绝对/盘符/仓库外）逐项跳过并告警，见 _clean_knowledge_dirs
    knowledge_dirs = _clean_knowledge_dirs(data.get("knowledge_dirs"))
    # 包格式版本（R-05，2026-09-10）：**只读前向兼容**——缺失视为当前版本 1；
    # 高于已知版本只告警、仍按已知规则解析（宽进），避免"新客户端建的包在老版本打不开"。
    # 注意：该字段必须同时写进两处写盘点（pack_forge.forge_finish、routes_pack.create_pack），
    # 否则会退化成 `bans` 那样的死字段（写了没人读 / 读了没人写）。
    try:
        schema = int(data.get("schema", PRESET_SCHEMA))
    except (TypeError, ValueError):
        logger.warning("预设包 %s 的 schema 非整数，按 %d 处理", pid, PRESET_SCHEMA)
        schema = PRESET_SCHEMA
    if schema > PRESET_SCHEMA:
        logger.warning("预设包 %s 声明 schema=%d 高于本版支持的 %d，按已知规则解析（建议升级客户端）",
                       pid, schema, PRESET_SCHEMA)
    return {"id": pid, "name": name, "char_name": cname,
            "user_name": uname, "presentation": presentation,
            "desc": str(data.get("desc", "") or "").strip(),
            "tagline": str(data.get("tagline", "") or "").strip(),
            "knowledge_dirs": knowledge_dirs, "schema": schema}


# ── 包注册表（packs.json）：包存在性的唯一权威（阶段 3.2）──────────
# 背景：原来"包存在性" = 启动期扫目录（进程态）。后果：
#   ① 孤儿目录（建包中断残留）永远不可见、也没法清理；
#   ② "快照里有、恢复端还没注册"的自建包要两阶段特判（阶段 0 的 R-01 用例）；
#   ③ 哪些包该进快照/同步只能靠 cfg.MODES 猜。
# 现在改为持久事实：`user_data/packs.json`。
# 兼容与兜底：文件缺失/损坏/与目录不一致 → 从目录自愈重建（老安装零迁移成本）；
# 旧客户端不读该文件，照常按 MODES 工作。
_PACKS_FILE = "packs.json"
PACK_STATES = ("active", "archived")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _scan_custom_dirs() -> dict:
    """扫用户自建区，返回 {pid: preset.json 路径}（**目录实况**，供注册表自愈比对）。"""
    out = {}
    try:
        dirs = sorted(p for p in _paths.USER_DIR.iterdir() if p.is_dir())
    except OSError:
        return out
    for d in dirs:
        fp = d / "character" / "preset.json"
        if fp.exists():
            out[d.name] = fp
    return out


class PackRegistry:
    """`user_data/packs.json` 的读写与自愈（包存在性的唯一权威）。

    记录字段：id / source(bundled|custom|imported) / schema / name / char_name /
    capabilities / state(active|archived) / slots / created_at / updated_at。
    `slots` 供 3.3 维护卡定义三态（baseline/inherited/customized），本卡只建壳。

    **内置包不入清单**：它们由发行版提供，扫描即可（清单只描述用户区，避免"升级换了内置包
    却要迁移清单"的伪问题）。source=bundled 保留给将来"导入的内置副本"。
    """

    def __init__(self, fp: Path | None = None):
        self.fp = Path(fp) if fp else (_paths.USER_DIR / _PACKS_FILE)
        self.data: dict[str, dict] = {}

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
            atomic_write_json(self.fp, {"schema": 1, "updated_at": _now(), "packs": self.data})
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

    def active_ids(self) -> list:
        return sorted(k for k, v in self.data.items() if v.get("state", "active") == "active")

    def all_ids(self) -> list:
        """含 archived（快照/同步白名单用：归档 ≠ 丢保险，数据仍要进备份）。"""
        return sorted(self.data)


def backup_pack_ids() -> list:
    """**备份/快照白名单**（架构重构 3.8）：存在数据的**全部**包 id。

    = 内置/当前可用包（`MODES`，由发行版扫描） ∪ 清单全部条目（`packs.json`，**含 archived**）。

    为什么要有这个函数：`MODES` 是**运行期**语义（只有 active 的包能聊天），
    而快照/同步是**备份期**语义 —— 归档 ≠ 丢保险，用户按了归档的包，数据仍必须进快照，
    否则"归档"就成了静默的数据删除。旧实现三处白名单都拿 `MODES` 当全集，正是这个漏。
    默认模式排最前（顺序稳定，便于快照内容比对）。"""
    ids = set(MODES) | set(pack_registry().all_ids())
    return [DEFAULT_MODE] + sorted(i for i in ids if i != DEFAULT_MODE)


_REGISTRIES: dict[str, PackRegistry] = {}


def pack_registry(fp: Path | None = None) -> PackRegistry:
    """取当前数据根下的包注册表（按路径缓存：USER_DIR 可被测试替换，故用路径做键）。

    首次访问会 `load()`（**内存自愈**，不落盘）；落盘由启动链
    （`run_startup_init` → `persist()`）与登记/注销变更点负责。"""
    key = str(fp or (_paths.USER_DIR / _PACKS_FILE))
    reg = _REGISTRIES.get(key)
    if reg is None:
        reg = PackRegistry(Path(key))
        reg.load(persist=False)
        _REGISTRIES[key] = reg
    return reg


def _discover_presets(base: Path | None = None) -> dict:
    """扫描发现预设包：bundled（assets/character/{id}/preset.json）+ 本地版追加用户自建区
    （USER_DIR/{id}/character/preset.json；服务器版跳过——用户区属各账号，自定义整包不入全局注册表）。
    内置包优先（用户区同 id 跳过）；一个都没发现回退两内置包硬编码兜底。base 参数供测试注入。"""
    presets = {}
    roots = [base or (BASE_DIR / "assets" / "character")]
    for root in roots:
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            fp = d / "preset.json"
            if not fp.exists():
                continue
            p = _parse_preset(fp, d.name)
            if p:
                presets[p["id"]] = p
    # 用户自建区（仅本地版；测试注入 base 时跳过）——**从 packs.json 读**（阶段 3.2）
    if base is None and not os.environ.get("FIREFLY_SERVER"):
        reg = pack_registry()
        # 只取 active 的（archived 包不进 MODES：3.5 的归档语义）；目录多出未注册的包
        # 由 PackRegistry.heal() 补齐（自愈），不会因为"忘了登记"就消失
        for pid in reg.active_ids():
            fp = _paths.USER_DIR / pid / "character" / "preset.json"
            if not fp.exists():
                continue
            p = _parse_preset(fp, pid)
            if not p:
                continue
            if p["id"] in presets:
                logger.warning("用户自建包 %s 与内置包同 id，跳过（内置优先）", p["id"])
                continue
            p["custom"] = True
            presets[p["id"]] = p
    if not presets:
        logger.error("预设包发现为空，回退内置 story/haruno 兜底")
        presets["story"] = {"id": "story", "name": "剧情模式", "char_name": "流萤",
                            "user_name": "开拓者", "presentation": "sticker",
                            "desc": "她的故事，与你共同推进",
                            "tagline": "会找到的，属于我的梦...",
                            "knowledge_dirs": ["knowledge", "database/dialogues_compiled"]}
        presets["haruno"] = {"id": "haruno", "name": "春日手信", "char_name": "流萤",
                             "user_name": "开拓者", "presentation": "narration",
                             "desc": "流萤想象的普通学生生活",
                             "tagline": "会找到的，属于我的梦...",
                             "knowledge_dirs": None}
    return presets


PRESETS = _discover_presets()
DEFAULT_MODE = "story" if "story" in PRESETS else next(iter(PRESETS), "story")
# 模式元组（兼容既有几百处 `mode in cfg.MODES` 校验）：默认模式在前，其余按 id 排序
MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))


def reload_presets() -> None:
    """重新扫描预设包（新建/删除自定义包后调用）。
    PRESETS 原地清空重建（dict 引用共享，各模块运行时再取）；MODES 重新赋值——
    消费方须用 cfg.MODES 运行时访问（from-import 的值拷贝会陈旧）。"""
    global PRESETS, MODES
    fresh = _discover_presets()
    PRESETS.clear()
    PRESETS.update(fresh)
    MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))


def char_name(mode: str = DEFAULT_MODE) -> str:
    """当前模式角色名（预设包声明；非法 mode 回退默认包，兜底"流萤"）。"""
    p = PRESETS.get(mode) or PRESETS.get(DEFAULT_MODE) or {}
    return p.get("char_name") or "流萤"


def user_name(mode: str = DEFAULT_MODE) -> str:
    """当前模式用户称呼（同 char_name）。"""
    p = PRESETS.get(mode) or PRESETS.get(DEFAULT_MODE) or {}
    return p.get("user_name") or "开拓者"
