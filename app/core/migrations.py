# -*- coding: utf-8 -*-
"""一次性迁移与首启初始化 — 老数据归位、默认文件拷贝、供应商结构迁移（任务 2.1 自 app_config 拆出）

全部幂等；`run_legacy_migration()` 只在本地版入口显式调用（服务器版永不调用，
因为 server 与本地共用同一个 user_data 根）。
"""

import time
import logging
from pathlib import Path

from core import paths as _paths
from core.config import API_BASE, _CONFIG_BAK_KEEP, _clean_provider, _deepseek_preset, _valid_http_base
from core.paths import ROOT, bundled_character_dir, mode_character_dir

logger = logging.getLogger(__name__)


def _migrate_legacy_providers(data: dict) -> dict:
    """旧结构（顶层 api_key/api_base）→ providers。返回 {providers, active_provider} 或 None。
    迁移前备份 config.json；迁移后旧字段不再读取/落盘。幂等：新结构存在则不迁。"""
    if "providers" in data:
        return None
    if "api_key" not in data and "api_base" not in data:
        return None
    try:
        _bak = _paths.CONFIG_FILE.with_name(_paths.CONFIG_FILE.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        _bak.write_bytes(_paths.CONFIG_FILE.read_bytes())
        logger.info("多供应商迁移：已备份旧配置 -> %s", _bak.name)
        # 保留策略：只留最新 _CONFIG_BAK_KEEP 份（文件名时间戳序，按名排序删旧）；
        # 失败静默——清理失败不影响迁移主流程
        try:
            _baks = sorted(_paths.CONFIG_FILE.parent.glob(_paths.CONFIG_FILE.name + ".bak-*"))
            for _old in _baks[:max(len(_baks) - _CONFIG_BAK_KEEP, 0)]:
                _old.unlink(missing_ok=True)
        except OSError:
            pass
    except OSError:
        pass
    old_key = str(data.get("api_key", "") or "").strip()
    old_base = str(data.get("api_base", "") or API_BASE).strip()
    if not _valid_http_base(old_base):
        old_base = API_BASE
    old_base = old_base.rstrip("/")
    if old_base == API_BASE:
        providers = [_deepseek_preset(old_key, API_BASE)]
        active = "deepseek"
    else:
        # 旧状态 = 非官方端点（如 OpenCode Go）：保留原名端口供用户选用，行为等价
        p1 = _deepseek_preset("", API_BASE)
        p2 = _clean_provider({"id": "legacy", "name": "原接口地址",
                              "base_url": old_base, "api_key": old_key,
                              "models": [], "caps": {}})
        providers = [p for p in (p2, p1) if p]   # legacy 置顶 = 与旧行为一致
        active = providers[0]["id"] if providers else "deepseek"
    return {"providers": providers, "active_provider": active}


# ── 老数据迁移（一次性，幂等）──────────────────────
# 目录级隔离前：user_data/character/ data/ story/手账.md 平铺。
# 迁移到 story 模式：user_data/story/{character,data,journal}/。
# 用 move（同盘 rename 原子），迁移后旧位置不再读写，避免新旧双份分裂。
# 顺序铁律：迁移必须先于默认拷贝——否则默认文件占位导致用户数据迁移被跳过丢失。
#
# 2026-09-10 修复（三项）：
#   ① **不再在 import 期自动执行**。原来顶层直接调用，而服务器版入口 server_app.py
#      也 import 本模块 → 服务器每次启动都会对**共享** user_data 根执行移动式迁移。
#      现改为显式调用：本地版入口 app/server.py 的 main() 调 run_legacy_migration()
#      （服务器版永不调用；运维如需迁移自建包，需手工调用并在迁移前先备份）。
#   ② 迁移前对涉及源打一份 user_data/.legacy_pre_move.zip 备份（失败即中止迁移）。
#   ③ move 失败不再静默：逐条 logger.warning + 汇总，返回统计供调用方展示。
_LEGACY_PRE_MOVE_ZIP = "legacy_pre_move.zip"


def _legacy_targets():
    """迁移映射：(源, 目标路径字符串) 列表（含旧手账位置）。

    注意：这里**只做纯路径计算**，不能调用 mode_root/mode_character_dir 等派生函数——
    它们内部有 mkdir 副作用，会让"无待迁移内容即零副作用"的承诺失效
    （2026-09-10 自测发现：原写法在无旧布局时也会凭空建出 user_data/story/）。"""
    return [
        (_paths.USER_DIR / "character", _paths.USER_DIR / "story" / "character"),
        (_paths.USER_DIR / "data", _paths.USER_DIR / "story" / "data"),
        (_paths.USER_DIR / "story" / "手账.md", _paths.USER_DIR / "story" / "journal" / "手账.md"),
    ]


def _backup_legacy_sources() -> Path:
    """迁移前把涉及源打包到 user_data/{_LEGACY_PRE_MOVE_ZIP}（失败抛异常，由调用方中止迁移）。"""
    import zipfile
    out = _paths.USER_DIR / _LEGACY_PRE_MOVE_ZIP
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, _dst in _legacy_targets():
            if not src.exists():
                continue
            if src.is_file():
                zf.write(src, src.name)
                n += 1
            else:
                for fp in sorted(src.rglob("*")):
                    if fp.is_file():
                        zf.write(fp, f"{src.name}/{fp.relative_to(src).as_posix()}")
                        n += 1
    if n == 0:
        try:
            out.unlink()
        except OSError:
            pass
        return out
    logger.warning("旧布局迁移：已备份 %d 个待移动文件 → %s", n, out)
    return out


def run_legacy_migration() -> dict:
    """显式执行旧布局迁移（幂等）。返回统计 dict。

    仅由本地版入口 app/server.py 的 main() 调用——**服务器版不得调用**
    （服务器 model 下 user_data 根是多账号共享的，迁移会产生跨账号归属歧义）。
    无待迁移内容时立即返回且不产生任何副作用（含不写备份）。

    2026-09-10（R-04）：目标已存在时不再一律跳过，改为**内容感知**——
      内容摘要相同 → 该源是 bundled 的陈旧副本，**丢弃**（否则它会永久遮蔽新版内置内容，
      实测线上 user_data/story/character/ 就留了这样 3 个文件，其中 2 个与 bundled 逐字相同）；
      内容不同    → 可能是用户数据，**保留原处**并告警（宁可留冗余，不误删）。"""
    import shutil as _sh
    moved, failed, skipped, discarded = 0, 0, 0, 0
    pending = [t for t in _legacy_targets() if t[0].exists()]
    if not pending:
        return {"pending": 0, "moved": 0, "failed": 0, "skipped": 0,
                "discarded": 0, "aborted": False}

    # ③ 迁移前先备份；备份失败即中止（不再"无备份地移动用户数据"）
    try:
        _backup_legacy_sources()
    except Exception as e:
        logger.error("旧布局迁移已中止：迁移前备份失败 %s", e)
        return {"pending": len(pending), "moved": 0, "failed": 0, "skipped": 0,
                "discarded": 0, "aborted": True, "error": str(e)}

    def _same_content(a: Path, b: Path) -> bool:
        try:
            return a.read_bytes().replace(b"\r\n", b"\n") == b.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            return False

    def _handle(src_file: Path, dst_file: Path) -> str:
        """返回 'moved' | 'discarded' | 'skipped' | 'failed'"""
        if dst_file.exists():
            if _same_content(src_file, dst_file):
                try:
                    src_file.unlink()
                    return "discarded"
                except OSError:
                    return "skipped"
            logger.warning("旧布局迁移：目标已存在且内容不同，保留源文件不删: %s", src_file)
            return "skipped"
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            _sh.move(str(src_file), str(dst_file))
            return "moved"
        except OSError as e:
            logger.warning("旧布局迁移失败（文件留在原位）: %s → %s: %s", src_file, dst_file, e)
            return "failed"

    for _src, _dst in pending:
        if _src.is_file():
            r = _handle(_src, _dst)
            moved += r == "moved"; discarded += r == "discarded"
            skipped += r == "skipped"; failed += r == "failed"
            continue
        for _f in _src.iterdir():
            if not _f.is_file():
                continue
            r = _handle(_f, _dst / _f.name)
            moved += r == "moved"; discarded += r == "discarded"
            skipped += r == "skipped"; failed += r == "failed"
    if moved or failed or skipped or discarded:
        logger.warning("旧布局迁移完成：moved=%d discarded=%d skipped=%d failed=%d",
                       moved, discarded, skipped, failed)
    return {"pending": len(pending), "moved": moved, "failed": failed,
            "skipped": skipped, "discarded": discarded, "aborted": False}


# ── 首次启动引导：建目录 + 拷贝默认文件 ──
# 顺序铁律：迁移必须先于默认拷贝——否则默认文件占位导致用户数据迁移被跳过丢失。
# 2026-09-10：整段收敛进 run_startup_init()，由入口在**迁移之后**调用；不再在 import 期
# 执行。原实现的问题：服务器版入口 server_app.py 也 import 本模块 → 每次启动都在
# **共享** user_data 根建目录并写入 config.json / story/character/*.md（实测线上
# user_data/story/character/ 下的 3 个孤文件即由此产生，且不属于任何账号）。
# 默认源路径：frozen 时数据在 _internal/，开发时在 app/；BASE_DIR 已指向对应位置
# 注意：这是**模块级快照**，绑定 import 时的 USER_DIR。运行时改 USER_DIR 的场景
# （测试注入、将来多根部署）必须用 _defaults_map() 重算，不要直接用这个字典。
_DEFAULTS = {
    _paths.USER_DIR / "config.json": ROOT / "config.json",
    mode_character_dir() / "core.md": bundled_character_dir() / "core.md",
    mode_character_dir() / "identity.md": bundled_character_dir() / "identity.md",
    mode_character_dir() / "sms_samples.md": bundled_character_dir() / "sms_samples.md",
}


def _defaults_map() -> dict:
    """按**当前** USER_DIR / bundled 路径重算默认文件映射（{目标: 源}）。

    mode_character_dir 会 mkdir，故放在函数内、仅由 run_startup_init 调用（那里本就要建目录）。
    2026-09-10：原实现直接用模块级 _DEFAULTS，在 USER_DIR 被运行时替换的场景下会指向旧路径
    （自测发现：首启拷贝落到了上一个 USER_DIR）。"""
    return {
        _paths.USER_DIR / "config.json": ROOT / "config.json",
        mode_character_dir() / "core.md": bundled_character_dir() / "core.md",
        mode_character_dir() / "identity.md": bundled_character_dir() / "identity.md",
        mode_character_dir() / "sms_samples.md": bundled_character_dir() / "sms_samples.md",
    }


def _cleanup_stale_defaults() -> int:
    """启动时清理"未被用户修改过的"设定副本，让 bundled 的后续更新能送达（R-04）。

    背景：首启把 bundled 的 core/identity/sms_samples 拷进 user_data/{mode}/character/，
    而 resolve_character_file 无条件优先用户副本 → 此后**内置内容更新永远到不了老用户**
    （已实测：改 bundled 后 load_slot 读到的仍是旧副本）。
    判定算法与 routes_pack.get_pack_files 的 customized 完全一致（strip 后逐字比较）：
      相同 → 该副本无信息量，删除即可让读取回落到 bundled（下次更新自然跟随）；
      不同 → 用户改过，**保留**（决定权在用户，UI 的"恢复默认"可删）。
    幂等：删完即无副本可比，重复启动零动作。返回删除数量。"""
    removed = 0
    try:
        for dst, src in _defaults_map().items():
            if not dst.exists() or not src.exists():
                continue
            try:
                if dst.read_text(encoding="utf-8").strip() == src.read_text(encoding="utf-8").strip():
                    dst.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception as e:
        logger.warning("陈旧副本清理失败（不影响启动）: %s", e)
    if removed:
        logger.warning("已清理 %d 个未修改的设定副本（让内置包更新生效）", removed)
    return removed


def run_startup_init() -> dict:
    """本地版首启引导：建目录 + 清理"未修改的"设定副本。**服务器版不得调用**。

    必须在 run_legacy_migration() **之后**调用（迁移未跑就处理副本会占位，
    导致旧数据迁移被跳过而丢失）。

    2026-09-10（R-04）：**不再预拷贝 bundled 设定到用户区**。原实现首启把
    core/identity/sms_samples 拷进 user_data/{mode}/character/，而 resolve_character_file
    无条件优先用户副本 → 此后内置内容更新永远到不了老用户（已实测）。
    读取链路本身就是「用户副本 → bundled」回落，文件不存在也能正常读到 bundled，
    而用户一旦在软件内编辑就由 character_file_update 写出真正的用户副本
    ——预拷贝纯属多余，还制造了"陈旧遮蔽"。
    因此这里只做两件事：建目录 + 清理"与 bundled 逐字相同"的历史副本
    （这些副本无信息量，删掉即可让内置更新自然跟随；用户改过的副本一律保留）。"""
    created = 0
    _paths.USER_DIR.mkdir(parents=True, exist_ok=True)
    for _sub in ("stickers",):
        p = _paths.USER_DIR / _sub
        if not p.exists():
            p.mkdir(exist_ok=True)
            created += 1
    # 包注册表（阶段 3.2）：启动时落一次盘（import 期只做内存自愈，避免"import 即写盘"）
    try:
        from core.presets import pack_registry
        pack_registry().persist()
    except Exception as e:
        logger.warning("packs.json 初始化失败（继续启动）: %s", e)
    cleaned = _cleanup_stale_defaults()
    # 包槽位三态（阶段 3.3）：必须在清理**之后**刷新 —— 清理删掉的正是"与 bundled 逐字相同"
    # 的副本，那些槽位应落到 inherited（副本存在过），而不是被当成"从未有过副本"的 baseline。
    try:
        from core.presets import pack_registry
        pack_registry().refresh_slots()
    except Exception as e:
        logger.warning("包槽位三态刷新失败（继续启动）: %s", e)
    return {"dirs_created": created, "files_copied": 0, "stale_removed": cleaned}
