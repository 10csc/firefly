# -*- coding: utf-8 -*-
"""恢复引擎（阶段 2.2 自 routes_data 拆出）— 导入/恢复的落盘内核

职责：zip 条目安全审查（zip slip / 解压炸弹）、自动备份（含 C-3 滚动裁剪）、
「解压到临时目录 → 整体 rename」的原子换入、全包快照按包分发恢复。

这是全项目**唯一**能把用户数据目录整体替换掉的地方，改动前先看：
- `_swap_dir_into_place` 的三段式（旧目录 → .restore_old → rename + 失败回滚）
- `_restore_full_snapshot` 的阶段顺序铁律（自建包必须先落 preset.json 再落数据）
"""

import logging
import time
from pathlib import Path

from modules import app_config as cfg

logger = logging.getLogger(__name__)


# 导出/备份打包排除的内部目录（同步冲突备份/设定纠错中间态，不是用户数据）
_EXPORT_EXCLUDE_DIRS = {".sync_backups", ".setting_fix", ".sync_conflicts"}


# ══ 数据导入 / 本地备份 ════
# 导出复用 GET /export-data（zip 下载）；导入=multipart zip 覆盖（导入前自动备份 +
# zip slip 防御，见 _import_zip_to_mode）。备份本地化：/backup/* 端点管理
# {用户目录}/backups/（每模式留最近 _BACKUP_KEEP 份），手动云端备份（/sync/upload|download）已下线。
_IMPORT_MAX_BYTES = 60 * 1024 * 1024          # zip 上传上限（含 multipart 开销）


_IMPORT_MAX_FILE_BYTES = 20 * 1024 * 1024     # 包内单文件解压上限


_IMPORT_MAX_TOTAL_BYTES = 100 * 1024 * 1024   # 包内解压总量上限


_BACKUP_KEEP = 10                             # 每模式保留最近手动备份份数

# 自动备份（auto-/pre-restore-）的滚动配额（C-3，2026-09-13）。
# 原状：`_backup_current_mode` 只写不删，每次导入/恢复都在 backups/ 里堆一份全量 zip；
# 而裁剪逻辑只认 `{mode}-*`（手动备份），于是自动备份**永久累积**——用户完全看不出来
# （/backups 列表按设计不列自动备份），磁盘却一直被吃掉。
# 口径与既有实现对齐：auto 与手动同配额（每模式 10 份）；pre-restore 与 snapshot 侧
# 一致留 3 份（routes_snapshot.py 的 pre-restore 快照也是 3 份）。
_AUTO_KEEP = {"auto": _BACKUP_KEEP, "pre-restore": 3}


def _zip_safe_entries(zf) -> list[tuple[str, object]]:
    """zip slip 防御：拒绝绝对路径与 .. 穿越，只收普通文件；返回 [(name, info)]。
    超限抛 ValueError（调用方转人话文案）。"""
    out = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        # zip slip 防御：拒绝绝对路径（/ 或 Windows 盘符 C:/）、.. 穿越，只收普通文件
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":") or ".." in name.split("/"):
            raise ValueError("压缩包内含非法路径，已拒绝")
        if info.file_size > _IMPORT_MAX_FILE_BYTES:
            raise ValueError("压缩包内单个文件过大，已拒绝")
        total += info.file_size
        if total > _IMPORT_MAX_TOTAL_BYTES:
            raise ValueError("压缩包解压总量过大，已拒绝")
        out.append((name, info))
    return out


def _backup_dir(mode: str) -> Path:
    """本地备份目录：{用户数据根}/backups/（与模式目录平级，不进导出/同步循环）。
    服务器版经 _user_ctx 自动按用户目录隔离。"""
    return cfg.mode_root(mode).parent / "backups"


def _prune_auto_backups(bdir: Path, mode: str, prefix: str, keep: int, current: str) -> None:
    """滚动裁剪 {prefix}-{mode}-*.zip，保留最近 keep 份（刚写入的 current 不参与淘汰）。

    注意三点（都是踩过的坑）：
    1. 不看别的模式——backups/ 是各模式共用的一个目录（{用户根}/backups），
       若按 `{prefix}-*.zip` 全局裁剪，一个模式频繁导入会把别的模式的备份删掉；
    2. 排除 current：同一秒内连建时基底名会被淘汰再复用，不排除就会自删刚写的那份；
    3. 失败只告警——裁剪是清理动作，绝不能因为删不掉旧备份而让导入/恢复失败。"""
    try:
        olds = sorted(p.name for p in bdir.glob(f"{prefix}-{mode}-*.zip") if p.name != current)
        for old in olds[:max(len(olds) + 1 - keep, 0)]:
            try:
                (bdir / old).unlink()
            except OSError:
                pass
    except Exception as e:
        logger.warning("自动备份裁剪失败（不影响本次备份）: %s", e)


def _backup_current_mode(mode: str, prefix: str) -> None:
    """把当前模式数据打成 zip 存到 backups/（导入/恢复前自动备份，防误操作）。空目录跳过。
    写入后按前缀滚动裁剪（C-3）：auto 留 _BACKUP_KEEP 份、pre-restore 留 3 份。"""
    root = cfg.mode_root(mode)
    if not any(root.rglob("*")):
        return
    import io as _io
    import zipfile as _zipfile
    _backup_dir(mode).mkdir(parents=True, exist_ok=True)
    fp = _backup_dir(mode) / f"{prefix}-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with _zipfile.ZipFile(fp, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(root).as_posix())
    keep = _AUTO_KEEP.get(prefix)
    if keep:
        _prune_auto_backups(_backup_dir(mode), mode, prefix, keep, fp.name)


def _swap_dir_into_place(tmp: Path, root: Path) -> str:
    """把已就绪的临时目录原子换入 root（同盘 rename）。

    2026-09-10 加：导入/恢复原来走「rmtree(root) → 逐个解压」，中途失败会留下
    已清空/半写的目标目录且不可回滚。改为「先解压到临时目录 → 整体 rename」，
    失败时目标目录保持原样。返回 "" 表示成功，否则为错误文案。"""
    import shutil as _sh
    try:
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            tmp.rename(root)
            return ""
        old = root.with_name(root.name + ".restore_old")
        try:
            if old.exists():
                _sh.rmtree(old)
            root.rename(old)
        except OSError as e:
            return f"旧数据暂存失败（未改动任何数据）: {e}"
        try:
            tmp.rename(root)
        except OSError as e:
            try:
                old.rename(root)      # 回滚：把旧数据放回原位
            except OSError:
                pass
            return f"换入失败（旧数据仍在 {old.name}/）: {e}"
        _sh.rmtree(old, ignore_errors=True)
        return ""
    except Exception as e:
        return f"目录切换失败（未改动任何数据）: {e}"


def _import_zip_to_mode(data: bytes, mode: str, backup_prefix: str = "auto") -> tuple[bool, str, int]:
    """zip 数据覆盖导入到指定模式（导入前把当前模式备份到 backups/，前缀 backup_prefix）。

    返回 (ok, error, 文件数)。

    2026-09-10 修复两处数据安全问题：
      ① 原来「先 rmtree(root) 再逐个解压」，中途失败（磁盘满/进程被杀/zip 后段损坏）
         会留下空目录或半写状态且无法回滚。现改为解压到同盘临时目录后整体 rename。
      ② 原来「导入前自动备份失败」被 `except Exception: pass` 静默吞掉，导致在没有任何
         备份的情况下执行破坏性覆盖。现在备份失败即中止导入（宁可导不进，不可丢数据）。"""
    import io as _io
    import shutil as _sh
    import zipfile as _zipfile
    if not data.startswith(b"PK"):
        return False, "不是有效的 zip 备份文件", 0
    try:
        zf = _zipfile.ZipFile(_io.BytesIO(data))
    except Exception:
        return False, "zip 解析失败（文件损坏？）", 0
    try:
        entries = _zip_safe_entries(zf)
    except ValueError as e:
        return False, str(e), 0

    # 导入前自动备份现有数据；备份失败即中止（不再静默继续覆盖）
    try:
        _backup_current_mode(mode, backup_prefix)
    except Exception as e:
        logger.warning("导入前自动备份失败，已中止导入: %s", e)
        return False, f"导入前自动备份失败，为避免数据丢失已中止：{e}", 0

    root = cfg.mode_root(mode)
    tmp = root.with_name(root.name + ".import_tmp")
    n = 0
    try:
        if tmp.exists():
            _sh.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        for name, info in entries:
            dst = tmp / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dst, "wb") as out:
                _sh.copyfileobj(src, out)
            n += 1
    except Exception as e:
        _sh.rmtree(tmp, ignore_errors=True)
        return False, f"写入临时目录失败（原数据未改动）: {e}", 0

    err = _swap_dir_into_place(tmp, root)
    if err:
        _sh.rmtree(tmp, ignore_errors=True)
        return False, err, 0
    # 清缓存：设定/手账/短信样本的内存缓存必须重载（旧内容会串进新数据）
    try:
        from modules.llm_base import clear_cache, reload_journal
        from modules.polisher import clear_samples_cache
        clear_cache()
        clear_samples_cache()
        reload_journal(mode)
    except Exception:
        pass
    return True, "", n


def _is_snapshot_zip(zf) -> bool:
    """全包快照格式识别：顶层出现注册模式目录（{mode}/…）或 stickers/。
    单包导出 zip 的条目相对模式根（data/…、character/…），不会出现这两个顶层。"""
    tops = set()
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = info.filename.replace("\\", "/").split("/")
        if len(parts) > 1:
            tops.add(parts[0])
    return bool(tops & set(cfg.MODES)) or "stickers" in tops


def _pack_restorable(top: str) -> bool:
    """快照顶层目录名是否可作为一个「自建角色包」恢复（R-01 修复的审查口）。

    条件：目录名符合包 id 规则（^[a-z0-9_-]{1,32}$）**且**不在当前注册表 PRESETS 中。
    后者是护栏：内置包（story/haruno）由发行版提供，不允许被快照覆盖
    （否则一份构造的 zip 就能改写内置角色的设定）。
    真正的合法性强校验在阶段 1 —— 暂存后必须存在 character/preset.json 并能被
    _parse_preset 通过，才会换入。"""
    from modules.app_config import _PRESET_ID_RE
    pid = str(top or "").strip()
    if not pid or not _PRESET_ID_RE.fullmatch(pid):
        return False
    return pid not in cfg.PRESETS


def _restore_full_snapshot(data: bytes, backup: bool = True) -> tuple[bool, str, int]:
    """恢复全包快照 zip（所有角色包 + 用户表情包；_config.json 不自动恢复）。

    白名单分发：顶层 {mode}/ → 该包数据根；stickers/ → 用户表情包目录；
    其它顶层（_config.json / stickers-meta.json）忽略——Key 绝不随快照回灌。
    覆盖前按包自动备份（backup=True；/snapshot/restore 已先打全量 pre-restore
    快照，传 False 避免重复备份）。返回 (ok, error, 文件数)。"""
    import io as _io
    import shutil as _sh
    import zipfile as _zipfile
    if not data.startswith(b"PK"):
        return False, "不是有效的 zip 快照", 0
    try:
        zf = _zipfile.ZipFile(_io.BytesIO(data))
    except Exception:
        return False, "zip 解析失败（文件损坏？）", 0
    try:
        entries = _zip_safe_entries(zf)
    except ValueError as e:
        return False, str(e), 0

    by_mode: dict[str, list] = {}
    pack_entries: dict[str, list] = {}      # 自建包：顶层 {custom_id}/ → USER_DIR/{id}/
    sticker_entries: list = []
    ignored_packs: list = []
    for name, info in entries:
        parts = name.split("/")
        top = parts[0]
        if len(parts) < 2:
            continue
        rel = "/".join(parts[1:])
        if any(p in _EXPORT_EXCLUDE_DIRS for p in parts[1:]):
            continue            # 内部目录（同步冲突/纠错中间态）不进恢复
        if top == "stickers":
            sticker_entries.append((rel, info))
            continue
        if rel == "character/preset.json" and _pack_restorable(top):
            # 2026-09-10（R-01）：自建包 = 顶层以包 id 命名的目录（含 character/preset.json）。
            # 旧实现只认 cfg.MODES（**启动期**扫描结果），导致"快照里有、恢复端还没注册"的
            # 自建包被整包丢弃 —— 换机/重装场景下静默丢角色卡与对话。
            pack_entries.setdefault(top, []).append((rel, info))
            continue
        if top in cfg.MODES:
            by_mode.setdefault(top, []).append((rel, info))
        elif _pack_restorable(top):
            pack_entries.setdefault(top, []).append((rel, info))
        else:
            ignored_packs.append(top)
    if not by_mode and not pack_entries and not sticker_entries:
        return False, "快照里没有可恢复的数据", 0

    n = 0
    # ── 阶段 1：自建包整体暂存 + 先落地**包定义**，再重扫注册表 ──
    # 顺序铁律：包定义必须先于其数据落地——否则 cfg.mode_root(mode) 会因 mode 未注册而
    # 静默回退到 DEFAULT_MODE，把自建包的数据写进 story 包里。
    # 注意：包定义（character/）与包数据（data/journal/images/）同处 {id}/ 一个目录，
    # 因此阶段 1 只把 character/ 换入；其余条目暂存在 stage 目录，阶段 2 再搬。
    pack_stage: dict[str, Path] = {}
    user_root = Path(cfg._user_ctx_dir() or cfg.USER_DIR)
    for pid, items in pack_entries.items():
        stage = user_root / (pid + ".pack_stage")
        pack_dir = user_root / pid
        try:
            if stage.exists():
                _sh.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True, exist_ok=True)
            for rel, info in items:
                dst = stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dst, "wb") as out:
                    _sh.copyfileobj(src, out)
                n += 1
            if not (stage / "character" / "preset.json").exists():
                _sh.rmtree(stage, ignore_errors=True)
                ignored_packs.append(pid)
                continue
            char_tmp = user_root / (pid + ".pack_tmp")
            if char_tmp.exists():
                _sh.rmtree(char_tmp, ignore_errors=True)
            (stage / "character").rename(char_tmp)
            err = _swap_dir_into_place(char_tmp, pack_dir / "character")
            if err:
                _sh.rmtree(char_tmp, ignore_errors=True)
                _sh.rmtree(stage, ignore_errors=True)
                return False, f"[{pid}] {err}", n
            pack_stage[pid] = stage
        except Exception as e:
            _sh.rmtree(stage, ignore_errors=True)
            return False, f"[{pid}] 自建包解压失败（未改动该包）: {e}", n
    if pack_stage:
        try:
            cfg.reload_presets()      # 让新包进入 PRESETS/MODES，后续 mode_root 才认得它们
        except Exception as e:
            logger.warning("恢复自建包后重扫注册表失败（该包数据将跳过）: %s", e)
        # 未成功注册的包：清理暂存，避免半恢复状态
        for pid in list(pack_stage):
            if pid not in cfg.MODES:
                logger.warning("自建包 %s 未通过注册表校验，跳过其用户数据", pid)
                _sh.rmtree(pack_stage.pop(pid), ignore_errors=True)

    # ── 阶段 2a：模式包用户数据（原子换目录）──
    for mode, items in by_mode.items():
        root = cfg.mode_root(mode)
        if backup:
            try:
                _backup_current_mode(mode, "pre-restore")
            except Exception:
                pass    # 备份失败不阻塞恢复（快照本身即数据源）；调用方可传 backup=False 跳过
        tmp = root.with_name(root.name + ".restore_tmp")
        try:
            if tmp.exists():
                _sh.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            for rel, info in items:
                dst = tmp / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dst, "wb") as out:
                    _sh.copyfileobj(src, out)
                n += 1
        except Exception as e:
            # 2026-09-10：解压改到临时目录，失败时该包数据保持原样（原来会先 rmtree 清空）
            _sh.rmtree(tmp, ignore_errors=True)
            return False, f"[{mode}] 解压失败（该包数据未改动）: {e}", n
        err = _swap_dir_into_place(tmp, root)
        if err:
            _sh.rmtree(tmp, ignore_errors=True)
            return False, f"[{mode}] {err}", n

    # ── 阶段 2b：自建包用户数据（逐子目录换入，保留阶段 1 的 character/ 定义）──
    for pid, stage in pack_stage.items():
        pack_dir = user_root / pid
        if backup:
            try:
                _backup_current_mode(pid, "pre-restore")
            except Exception:
                pass
        try:
            for sub in sorted(p for p in stage.iterdir() if p.is_dir()):
                if sub.name == "character":
                    continue
                target = pack_dir / sub.name
                tmp = pack_dir / (sub.name + ".restore_tmp")
                if tmp.exists():
                    _sh.rmtree(tmp, ignore_errors=True)
                sub.rename(tmp)
                err = _swap_dir_into_place(tmp, target)
                if err:
                    _sh.rmtree(tmp, ignore_errors=True)
                    _sh.rmtree(stage, ignore_errors=True)
                    return False, f"[{pid}/{sub.name}] {err}", n
        except Exception as e:
            _sh.rmtree(stage, ignore_errors=True)
            return False, f"[{pid}] 用户数据恢复失败: {e}", n
        _sh.rmtree(stage, ignore_errors=True)
    if ignored_packs:
        logger.warning("快照恢复：忽略 %d 个无法识别为角色包的顶层目录: %s",
                       len(set(ignored_packs)), sorted(set(ignored_packs))[:5])

    if sticker_entries:
        sdir = Path(cfg._user_ctx_dir() or cfg.USER_DIR) / "stickers"
        try:
            sdir.mkdir(parents=True, exist_ok=True)
            for rel, info in sticker_entries:
                dst = sdir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dst, "wb") as out:
                    _sh.copyfileobj(src, out)
                n += 1
        except Exception as e:
            return False, f"表情包恢复失败: {e}", n

    # 缓存重载：设定/手账/短信样本的内存缓存必须重建（旧内容会串进新数据）
    try:
        from modules.llm_base import clear_cache, reload_journal
        from modules.polisher import clear_samples_cache
        clear_cache()
        clear_samples_cache()
        for m in by_mode:
            reload_journal(m)
    except Exception:
        pass
    return True, "", n
