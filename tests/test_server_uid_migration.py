# -*- coding: utf-8 -*-
"""服务器版"按 uid 迁移"回归测试（对应缺口：线上老账号数据停在旧布局会"看起来消失"）。

要证的四件事：
  A 每个 uid 的旧布局被搬进 {uid}/story/…（对话/记忆/手账三类都搬）
  B **每个账号各自留一份备份** {uid}/legacy_pre_move.zip（备份失败即中止该账号）
  C 只碰**纯数字** uid 目录；非数字目录/普通文件一律不动
  D 幂等：再跑一次不搬任何东西、也不新增备份
  E 目标已存在且内容**不同**时：保留源文件（宁可留冗余，不误删用户数据）
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
TMP = Path(tempfile.mkdtemp(prefix="firefly_test_uidmig_"))
os.environ["FIREFLY_DATA_DIR"] = str(TMP)
os.environ["FIREFLY_SERVER"] = "1"          # 服务器形态
os.environ.pop("FIREFLY_ANDROID", None)

from core import paths as _paths          # noqa: E402
_paths.USER_DIR = TMP / "user_data"
_paths.USER_DIR.mkdir(parents=True, exist_ok=True)

# 造现场：两个老账号（旧布局）+ 一个非 uid 目录 + 一个普通文件
for uid in ("1", "10"):
    d = _paths.USER_DIR / uid
    (d / "character").mkdir(parents=True, exist_ok=True)
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "story").mkdir(parents=True, exist_ok=True)
    (d / "character" / "用户设定.md").write_text(f"uid{uid} 的用户设定", encoding="utf-8")
    (d / "data" / "conversation.jsonl").write_text(
        json.dumps({"seq": 1, "who": "user", "content": f"uid{uid} 的对话"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (d / "story" / "手账.md").write_text(f"uid{uid} 的手账", encoding="utf-8")
# 非 uid 目录（绝不能被搬）
(_paths.USER_DIR / "shared_assets").mkdir(parents=True, exist_ok=True)
(_paths.USER_DIR / "shared_assets" / "a.txt").write_text("不该动", encoding="utf-8")
(_paths.USER_DIR / "README.txt").write_text("根下文件也不该动", encoding="utf-8")

from core import migrations as MIG        # noqa: E402

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


print("=== A/B. 两个账号各搬各的，并各留一份备份 ===")
r1 = MIG.run_legacy_migration_all_users()
print("   返回:", r1)
for uid in ("1", "10"):
    d = _paths.USER_DIR / uid
    check(f"A  uid={uid} 设定搬到 story/character/",
          (d / "story" / "character" / "用户设定.md").is_file())
    check(f"A  uid={uid} 对话搬到 story/data/",
          (d / "story" / "data" / "conversation.jsonl").is_file())
    check(f"A  uid={uid} 手账搬到 story/journal/",
          (d / "story" / "journal" / "手账.md").is_file())
    # 旧位置的**文件**必须已不在（目录可能是空的，且现在会被迁移顺手清掉）
    check(f"A  uid={uid} 旧位置不再有文件",
          not (d / "character" / "用户设定.md").exists()
          and not (d / "data" / "conversation.jsonl").exists()
          and not (d / "story" / "手账.md").exists())
    check(f"B  uid={uid} 有自己的迁移前备份",
          (d / "legacy_pre_move.zip").is_file())
    if (d / "legacy_pre_move.zip").is_file():
        names = zipfile.ZipFile(d / "legacy_pre_move.zip").namelist()
        check(f"B  uid={uid} 备份里含对话文件",
              any("conversation.jsonl" in n for n in names), str(names[:3]))

print("\n=== C. 非 uid 目录/文件一律不动 ===")
check("C  shared_assets/ 未被搬动",
      (_paths.USER_DIR / "shared_assets" / "a.txt").is_file())
check("C  根下 README.txt 未被搬动",
      (_paths.USER_DIR / "README.txt").is_file())
check("C  汇总里只统计了 2 个账号", r1.get("users") == 2, str(r1))

print("\n=== D. 幂等：再跑一次不搬、不新增备份 ===")
before = {u: (_paths.USER_DIR / u / "legacy_pre_move.zip").stat().st_mtime
          for u in ("1", "10")}
r2 = MIG.run_legacy_migration_all_users()
missing = [u for u in ("1", "10")
           if not (_paths.USER_DIR / u / "legacy_pre_move.zip").is_file()]
after = {u: (_paths.USER_DIR / u / "legacy_pre_move.zip").stat().st_mtime
         for u in ("1", "10") if u not in missing}
check("D  第二次没有任何账号待迁移", r2.get("users") == 0, str(r2))
check("D  ★ 第一次的迁移备份**没有被删掉**（曾出现的 bug）", not missing, f"丢失={missing}")
check("D  备份文件未被重写（幂等，无副作用）", before == after)

print("\n=== E. 目标已存在且内容不同 → 保留源文件，不误删 ===")
d = _paths.USER_DIR / "1"
(d / "data").mkdir(parents=True, exist_ok=True)
(d / "data" / "conversation.jsonl").write_text("这是新的、与现目标不同的内容\n", encoding="utf-8")
(d / "story" / "data" / "conversation.jsonl").write_text("目标里的旧内容，必须保住\n",
                                                          encoding="utf-8")
r3 = MIG.run_legacy_migration_all_users()
check("E  目标文件内容未被覆盖",
      "必须保住" in (d / "story" / "data" / "conversation.jsonl").read_text(encoding="utf-8"))
check("E  冲突的源文件保留在原处（宁可冗余不误删）",
      (d / "data" / "conversation.jsonl").is_file(), str(r3))

print("\n=== F. 不变量：服务器版仍不得在共享根上搬 ===")
src = (ROOT / "app" / "core" / "migrations.py").read_text(encoding="utf-8")
i = src.find("def run_legacy_migration(")
check("F  run_legacy_migration 的 docstring 仍写明服务器版不得直接调用",
      "服务器版不得调用" in src[i:i + 700])

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
