# -*- coding: utf-8 -*-
"""预设包解析层（阶段 2.8 自 core/presets.py 拆出，纯移动无行为变化）

职责：preset.json 的解析与校验（`_parse_preset`）、knowledge_dirs 入口校验（R-10）、
卡定义槽位三态的常量与盘上推导（`slot_state_from_disk`，阶段 3.3）。
本模块只依赖 core.paths，不依赖 presets/pack_registry —— 是包体系的最底层，
供 presets.py（注册表扫描）与 pack_registry.py（清单读写）共同使用。
"""

import json
import logging
import re
import time
from pathlib import Path

from core import paths as _paths
from core.paths import ROOT

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
    # B8（审计 2026-09-15）：JSON 合法但不是对象（内容为 [] / "x" / 数字）时，下方
    # data.get 直接 AttributeError——_discover_presets 在 import 期执行，一个畸形
    # 自建包 preset.json（可经导入 zip 落进用户区）即可启动即崩，用户只能删文件自救
    if not isinstance(data, dict):
        logger.warning("预设包 %s 的 preset.json 不是 JSON 对象，跳过", expect_id)
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


# ── 包注册表常量与槽位三态（阶段 3.2 / 3.3）──────────
# 背景：原来"包存在性" = 启动期扫目录（进程态）。后果：
#   ① 孤儿目录（建包中断残留）永远不可见、也没法清理；
#   ② "快照里有、恢复端还没注册"的自建包要两阶段特判（阶段 0 的 R-01 用例）；
#   ③ 哪些包该进快照/同步只能靠 cfg.MODES 猜。
# 现在改为持久事实：`user_data/packs.json`。
# 兼容与兜底：文件缺失/损坏/与目录不一致 → 从目录自愈重建（老安装零迁移成本）；
# 旧客户端不读该文件，照常按 MODES 工作。
_PACKS_FILE = "packs.json"
PACK_STATES = ("active", "archived")

# ── 卡定义槽位三态（阶段 3.3）────────────────────────────────────────
# 病根：routes_pack.get_pack_files 原来每次都**现算** customized（用户副本 vs bundled 逐字比对）。
# 于是"内置内容更新了、用户从未编辑过副本"会被误报成"已修改"——用户看到一堆莫名其妙的
# "（已修改）"，而 _cleanup_stale_defaults 的清理口径又和它各写一份。
# 现在把"是否用户改过"变成**记录**（packs.json 的 slots），比对只在没有记录时用来 bootstrap。
#
# 三态定义（与 3.3 卡/架构图 view⑤ 一致，语义边界写死在这里）：
#   baseline   该槽位没有用户副本（用包自带内容，出厂状态）
#   inherited  正在用包自带内容，但**曾经有/现在有**等价副本（删除副本、清理陈旧副本后回落）
#   customized 用户副本与包自带内容不同 —— 用户真改过
# 「未编辑副本 + bundled 后续更新」仍判 inherited：这正是本卡要修的那个误报。
SLOT_STATES = ("baseline", "inherited", "customized")

# 可编辑槽位（与 routes_pack 的 API 白名单同一份口径；键是**角色目录下的相对路径**）。
# 架构图示例写的是短名（"core"），实现用相对路径：`prompts/` 下可能出现与根目录同名的文件，
# 短名会歧义，而相对路径既能当键也能直接拼路径。
PACK_SLOT_FILES = ("core.md", "identity.md", "sms_samples.md", "用户设定.md",
                   "prompts/polisher.md", "prompts/analyzer_extra.md",
                   "prompts/organizer_sticker.md", "prompts/organizer_narration.md",
                   "prompts/proactive_context.md", "prompts/env_suffix.md")


def _read_text(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8")
    except OSError:
        return ""


def slot_state_from_disk(pid: str, rel: str, prev: str | None = None) -> str:
    """按**盘上实况**推一个槽位的三态（只在没有记录时用作 bootstrap 判据）。

    规则（顺序有意义）：
    1. 用户副本存在、包自带副本不存在 → 自建包，用户副本即唯一定义 → customized；
    2. 用户副本存在、两者都有 → 逐字（strip 后）相同即 inherited，否则 customized；
    3. 用户副本不存在：曾经有过等价副本（prev=inherited/customized，例如"删除副本"或
       启动清理）→ inherited；否则 baseline。"""
    user_fp = _paths.pack_root(pid) / "character" / rel
    bundled_fp = _paths.bundled_character_dir(pid) / rel
    user_exists, bundled_exists = user_fp.exists(), bundled_fp.exists()
    if user_exists:
        if not bundled_exists:
            return "customized"
        return ("inherited" if _read_text(user_fp).strip() == _read_text(bundled_fp).strip()
                else "customized")
    if bundled_exists and prev in ("inherited", "customized"):
        return "inherited"
    return "baseline"


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
