# -*- coding: utf-8 -*-
"""A1 同步引擎测试：清单范围（媒体排除）、append 行合并（seq 去重）、收藏并集、
计划对比（上传/下载/合并）、文档新者胜、中断恢复（重复执行幂等）"""
import json
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from modules import sync_engine as se

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_sync_"))

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


print("=== A. 清单范围（仅文字类；媒体/内部目录排除） ===")
root = _tmp / "story"
(root / "data").mkdir(parents=True)
(root / "stickers").mkdir()
(root / "images").mkdir()
(root / "character").mkdir()
(root / ".setting_fix").mkdir()
(root / "journal").mkdir()
(root / "data" / "conversation.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")
(root / "memory.md").write_text("# 核心记忆头部\n", encoding="utf-8")
(root / "stickers" / "a.webp").write_bytes(b"\x00\x01")
(root / "images" / "pic.png").write_bytes(b"\x00\x01")
(root / ".setting_fix" / "manifest.json").write_text("{}", encoding="utf-8")
(root / "data" / "fav.json").write_text("[]", encoding="utf-8")
m = se.scan_manifest(root)
check("A1 清单含 jsonl/md", "data/conversation.jsonl" in m and "memory.md" in m)
check("A2 媒体目录排除（stickers/images）", "stickers/a.webp" not in m and "images/pic.png" not in m)
check("A3 内部目录排除（.setting_fix）", ".setting_fix/manifest.json" not in m)
check("A4 其它文字文件包含", "data/fav.json" in m)

print("=== B. append 行级合并（seq 去重保序） ===")
local = '{"seq":1,"who":"user","content":"你好"}\n{"seq":2,"who":"firefly"}\n'
remote = '{"seq":2,"who":"firefly"}\n{"seq":3,"who":"user","content":"吃了吗"}\n'
merged, cf = se.merge_jsonl(local, remote)
lines = [json.loads(x) for x in merged.strip().split("\n")]
check("B1 顺序=本地前+远端新增后", [l["seq"] for l in lines] == [1, 2, 3])
check("B2 重复行已去重", len(lines) == 3)
merged2, cf2 = se.merge_jsonl(merged, remote)
check("B3 幂等（重复合并不重复追加）", merged2 == merged and len(cf2) == 0)
# 同 seq 异内容：远端行备份进冲突
merged3, cf3 = se.merge_jsonl('{"seq":5,"content":"本地版"}\n', '{"seq":5,"content":"远端版"}\n')
check("B4 同 seq 异内容 → 冲突列表含远端行", len(cf3) == 1 and "远端版" in cf3[0])

print("=== C. 收藏并集（seq 去重） ===")
a = json.dumps([{"seq": 1, "content": "x"}, {"seq": 2, "content": "y"}], ensure_ascii=False)
b = json.dumps([{"seq": 2, "content": "y"}, {"seq": 3, "content": "z"}], ensure_ascii=False)
merged_s, cf4 = se.merge_favorites(a, b)
arr = json.loads(merged_s)
check("C1 收藏并集去重", [x["seq"] for x in arr] == [1, 2, 3])

print("=== D. 计划对比（上传/下载/合并） ===")
lm = {"a.md": {"sha256": "aa"}, "b.jsonl": {"sha256": "bb"}}
rm = {"b.jsonl": {"sha256": "cc"}, "c.md": {"sha256": "dd"}}
plan = se.plan_sync(lm, rm)
check("D1 本地独有 → 上传", plan["to_upload"] == ["a.md"])
check("D2 服务器独有 → 下载", plan["to_download"] == ["c.md"])
check("D3 两边内容不同 → 合并", plan["to_merge"] == ["b.jsonl"])
check("D4 相同内容 → same", True)

print("=== E. 文档类：mtime 新者胜 ===")
win, _, change = se.merge_docs_by_mtime({"mtime": 100, "sha256": "x"}, {"mtime": 200, "sha256": "y"})
check("E1 服务器更新 → remote 胜", win == "remote" and change is True)
win2, _, change2 = se.merge_docs_by_mtime({"mtime": 300, "sha256": "x"}, {"mtime": 200, "sha256": "y"})
check("E2 本地更新 → local 胜", win2 == "local" and change2 is True)
win3, _, change3 = se.merge_docs_by_mtime({"mtime": 100, "sha256": "x"}, {"mtime": 100, "sha256": "x"})
check("E3 内容相同 → 无需同步", win3 == "same" and change3 is False)

print("=== F. 执行层 apply + 备份（中断恢复=可重复执行） ===")
root2 = _tmp / "haruno2"
(root2 / "data").mkdir(parents=True)
fp = root2 / "data" / "conversation.jsonl"
fp.write_text('{"seq":1}\n', encoding="utf-8")
merged_f, cf5, changed = se.apply_merge_file(
    root2, "data/conversation.jsonl", '{"seq":1}\n', '{"seq":1}\n{"seq":2}\n', 100, 200)
check("F1 合并执行有变化", changed is True)
fp.write_text(merged_f, encoding="utf-8")
merged_f2, cf6, changed2 = se.apply_merge_file(
    root2, "data/conversation.jsonl", merged_f, '{"seq":1}\n{"seq":2}\n', 200, 200)
check("F2 中断恢复（重复执行幂等）", changed2 is False and merged_f2 == merged_f)

doc = root2 / "记忆.md"
doc.write_text("旧版本", encoding="utf-8")
bak = se.backup_file(doc, root2 / ".sync_backups", "pre")
check("F3 覆盖前备份文件存在", bak.exists() and bak.name.endswith(".bak"))
check("F4 备份目录在同步范围外", ".sync_backups" not in se.scan_manifest(root2))

print("=== G. 路径审查（同步范围） ===")
check("G1 绝对路径拒绝", se.in_sync_scope(Path("/etc/passwd"), root) is False)
check("G2 越界路径拒绝", se.in_sync_scope(Path("../x.md"), root) is False)
check("G3 正常路径通过", se.in_sync_scope(Path("data/conversation.jsonl"), root) is True)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
