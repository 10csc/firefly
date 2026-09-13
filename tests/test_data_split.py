# -*- coding: utf-8 -*-
"""routes_data 拆分（阶段 2 · 任务 2.2）的**纯移动 oracle**

拆分把 `app/routes_data.py`（974 行）拆成：
- `app/api/data_export.py`       导出/导入/本地备份端点
- `app/infra/sync/restore.py`    恢复引擎（原子换目录、zip 安全审查、自动备份、快照分发）
- `app/api/data_sync.py`         增量同步（manifest/import/export/now/_sync_one_mode）
原文件退化为兼容层（re-export + 写转发）。

守五件事：
1. 拆分前 32 个顶层名字，全部仍能经 `routes_data` 拿到，且实现都在新模块；
2. 兼容层身份：`routes_data` 是 `_ShimModule`，自身不留业务函数；
3. **monkeypatch 语义**：`rd._restore_full_snapshot = stub` 要落到 restore 模块
   （tests/test_snapshot.py L 段靠它），`rd.time = 假时钟` 要落到**所有**持有 time 的新模块
   （tests/test_backup.py G 段靠它——只落第一个模块会让自动备份裁剪断言失效）；
4. 导入顺序无关、各新模块可独立导入（2.2 实现中踩过 `restore ↔ data_export` 的环）；
5. 关键常量与端点集合不变（oracle 由 test_routes_oracle.py 覆盖，这里守常量值）。
"""
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import routes_data as rd
from api import data_export as dex
from api import data_sync as dsy
from infra.sync import restore as rst

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


BEFORE_NAMES = [
    "_AUTO_KEEP", "_BACKUP_KEEP", "_BACKUP_NAME_RE", "_EXPORT_EXCLUDE_DIRS", "_IMPORT_MAX_BYTES", "_IMPORT_MAX_FILE_BYTES",
    "_IMPORT_MAX_TOTAL_BYTES", "_backup_current_mode", "_backup_dir", "_backup_mode_of", "_backup_name_ok", "_build_backup_zip",
    "_import_zip_to_mode", "_is_snapshot_zip", "_pack_restorable", "_prune_auto_backups", "_restore_full_snapshot", "_swap_dir_into_place",
    "_sync_mode_root", "_sync_one_mode", "_zip_safe_entries", "backup_create", "backup_delete", "backup_restore",
    "backups_list", "export_data", "import_data", "logger", "sync_export", "sync_import",
    "sync_manifest", "sync_now",
]

SOURCES = (dex, rst, dsy)

print("=== A. 名字一个不少 ===")
_missing = [n for n in BEFORE_NAMES if not hasattr(rd, n)]
check(f"A1 拆分前 {len(BEFORE_NAMES)} 个名字全部可达", not _missing)
if _missing:
    print("    缺失:", _missing)
_not_in_new = [n for n in BEFORE_NAMES if n != "logger" and not any(hasattr(m, n) for m in SOURCES)]
check("A2 每个名字都由新模块持有（兼容层不留实现）", not _not_in_new)
if _not_in_new:
    print("    未在新模块找到:", _not_in_new)

print("=== B. 兼容层身份 ===")
check("B1 routes_data 是转发用 _ShimModule", type(rd).__name__ == "_ShimModule")
_own = [k for k, v in vars(rd).items()
        if not k.startswith("__") and callable(v)
        and getattr(v, "__module__", "") == "routes_data"]
check("B2 兼容层只定义转发类，不留业务函数", _own == ["_ShimModule"])
if _own != ["_ShimModule"]:
    print("    兼容层自定义定义:", _own)

print("=== C. monkeypatch 语义（拆分前是同一个命名空间，补丁必须一并生效） ===")
_old_restore = rst._restore_full_snapshot
_stub = lambda *a, **k: (True, "", 0)          # noqa: E731
try:
    rd._restore_full_snapshot = _stub
    check("C1 函数补丁落到 restore 模块（模块内部调用也吃到）",
          rst._restore_full_snapshot is _stub)
    check("C2 兼容层读到的是补丁（消费方运行时 from-import 取值）", rd._restore_full_snapshot is _stub)
    check("C3 未被补丁影响的邻居保持原样",
          rd._swap_dir_into_place is rst._swap_dir_into_place)
finally:
    rd._restore_full_snapshot = _old_restore
check("C4 还原后一致", rd._restore_full_snapshot is rst._restore_full_snapshot)


class _FakeClock:
    def strftime(self, _fmt):
        return "20260101-000000"


_old_time = rd.time
try:
    rd.time = _FakeClock()
    _hit = [m.__name__ for m in SOURCES if getattr(m, "time", None) is rd.time or
            type(getattr(m, "time", None)).__name__ == "_FakeClock"]
    check("C5 模块名补丁（rd.time）落到所有持有 time 的新模块",
          all(type(getattr(m, "time", None)).__name__ == "_FakeClock"
              for m in SOURCES if hasattr(m, "time")))
    check("C6 至少一个模块吃到了 time 补丁（G 段裁剪测试的前提）", bool(_hit))
finally:
    rd.time = _old_time
check("C7 还原后 time 恢复为真模块", rd.time.strftime("%Y") >= "2025")

print("=== D. 独立导入 / 导入顺序无关（2.2 实现中出过 restore↔data_export 环） ===")
_CHILD = ("import sys; sys.path.insert(0, r'{app}');\n"
          "import {first};\n"
          "import api.data_export; import api.data_sync; import infra.sync.restore;\n"
          "import routes_data as rd; print('OK', rd._IMPORT_MAX_BYTES)")
for first in ("api.data_export", "api.data_sync", "infra.sync.restore"):
    p = subprocess.run([sys.executable, "-c", _CHILD.format(app=str(ROOT / "app"), first=first)],
                       capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    check(f"D1 先导入 {first} 也能跑通", p.returncode == 0 and "OK" in (p.stdout or ""))
    if p.returncode != 0:
        print("   ", (p.stdout or "") + (p.stderr or "")[-300:])

print("=== E. 常量与关键语义不变 ===")
check("E1 上传上限 60MB", rd._IMPORT_MAX_BYTES == 60 * 1024 * 1024)
check("E2 包内单文件 20MB / 总量 100MB",
      rd._IMPORT_MAX_FILE_BYTES == 20 * 1024 * 1024 and rd._IMPORT_MAX_TOTAL_BYTES == 100 * 1024 * 1024)
check("E3 手动备份留 10 份、自动备份配额表",
      rd._BACKUP_KEEP == 10 and rd._AUTO_KEEP == {"auto": 10, "pre-restore": 3})
check("E4 内部目录排除集合", rd._EXPORT_EXCLUDE_DIRS == {".sync_backups", ".setting_fix", ".sync_conflicts"})
check("E5 备份名正则与 C-2/C-3 修复保持", rd._BACKUP_NAME_RE.pattern == r"^[a-z0-9_\-\.]+\.zip$")
check("E6 恢复引擎的三段式函数在 restore 模块",
      all(hasattr(rst, n) for n in ("_swap_dir_into_place", "_restore_full_snapshot",
                                    "_import_zip_to_mode", "_zip_safe_entries")))
check("E7 同步编排在 data_sync 模块",
      all(hasattr(dsy, n) for n in ("sync_manifest", "sync_import", "sync_export",
                                    "sync_now", "_sync_one_mode")))
check("E8 导出/备份端点在 data_export 模块",
      all(hasattr(dex, n) for n in ("export_data", "import_data", "backup_create",
                                    "backups_list", "backup_restore", "backup_delete")))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
