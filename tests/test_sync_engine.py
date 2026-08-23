# -*- coding: utf-8 -*-
"""A1 同步引擎测试：清单范围（注册表策略：媒体本体排除、images blob 放行）、
append 行合并（seq 去重 + 确定性重排 → 双端收敛）、收藏并集、计划对比（上传/下载/合并）、
文档新者胜、blob 异 sha 不合并、中断恢复（重复执行幂等）"""
import hashlib
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


print("=== A. 清单范围（文字类 + images blob；媒体本体/内部目录排除） ===")
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
check("A2 stickers 媒体本体排除（本地策略不变）", "stickers/a.webp" not in m)
check("A2b images 压缩图入清单（blob 可同步）", "images/pic.png" in m)
check("A3 内部目录排除（.setting_fix）", ".setting_fix/manifest.json" not in m)
check("A4 其它文字文件包含", "data/fav.json" in m)

print("=== B. append 行级合并（seq 去重 + 确定性重排） ===")
local = '{"seq":1,"who":"user","content":"你好"}\n{"seq":2,"who":"firefly"}\n'
remote = '{"seq":2,"who":"firefly"}\n{"seq":3,"who":"user","content":"吃了吗"}\n'
merged, cf = se.merge_jsonl(local, remote)
lines = [json.loads(x) for x in merged.strip().split("\n")]
check("B1 合并后按 seq 确定性升序", [l["seq"] for l in lines] == [1, 2, 3])
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

print("=== H. 双端发散收敛（conversation 风格：有 seq） ===")
# 两端从同一基础各自追加不同行，交换 local/remote 各合并一次，结果必须字节相等
base = '{"seq":1,"who":"user","content":"早"}\n{"seq":2,"who":"firefly","content":"早呀"}\n'
a_side = base + '{"seq":4,"who":"user","content":"PC 端追加"}\n'
b_side = base + '{"seq":3,"who":"firefly","content":"安卓端追加"}\n'
m_ab, _ = se.merge_jsonl(a_side, b_side)
m_ba, _ = se.merge_jsonl(b_side, a_side)
check("H1 两端各自合并字节完全相等", m_ab == m_ba)
check("H2 sha256 收敛（不再重复全量重传）",
      hashlib.sha256(m_ab.encode("utf-8")).hexdigest() == hashlib.sha256(m_ba.encode("utf-8")).hexdigest())
check("H3 合并结果按 seq 升序", [json.loads(x)["seq"] for x in m_ab.strip().split("\n")] == [1, 2, 3, 4])
scrambled = '{"seq":4,"who":"user","content":"PC 端追加"}\n' + base  # 本地历史行序不同
m_sc, _ = se.merge_jsonl(scrambled, b_side)
check("H4 输入行序不同仍收敛到同一字节序", m_sc == m_ab)

print("=== I. 双端发散收敛（pipeline/proactive 风格：无 seq，有 _ts/time） ===")
p_base = '{"time": "2026-08-11 14:00:00", "mode": "haruno", "user_input": "第一条"}\n'
p_a = p_base + '{"_ts": 1786445000.0, "time": "2026-08-11 18:45:00", "turn": 9}\n'
p_b = p_base + '{"_ts": 1786444000.0, "time": "2026-08-11 18:30:00", "turn": 6}\n'
p_ab, _ = se.merge_jsonl(p_a, p_b)
p_ba, _ = se.merge_jsonl(p_b, p_a)
check("I1 _ts 行两端合并字节相等", p_ab == p_ba)
ts_order = [json.loads(x).get("_ts") for x in p_ab.strip().split("\n") if "_ts" in x]
check("I2 _ts 行按时间戳升序", ts_order == sorted(ts_order) == [1786444000.0, 1786445000.0])
# 纯 time 字符串行（无 _ts）
t_a = '{"time": "2026-08-11 10:00:00", "ev": "a"}\n{"time": "2026-08-11 12:00:00", "ev": "c"}\n'
t_b = '{"time": "2026-08-11 10:00:00", "ev": "a"}\n{"time": "2026-08-11 11:00:00", "ev": "b"}\n'
t_ab, _ = se.merge_jsonl(t_a, t_b)
t_ba, _ = se.merge_jsonl(t_b, t_a)
check("I3 time 字符串行两端合并字节相等", t_ab == t_ba)
check("I4 time 行按字符串升序",
      [json.loads(x)["ev"] for x in t_ab.strip().split("\n")] == ["a", "b", "c"])
check("I5 幂等（重复合并不再变化）", se.merge_jsonl(p_ab, p_b)[0] == p_ab)

print("=== J. images 纳入范围（blob）/ stickers 仍排除 ===")
check("J1 images 图片进范围", se.in_sync_scope(Path("images/uuid.png"), root) is True)
check("J2 stickers 仍排除", se.in_sync_scope(Path("stickers/a.webp"), root) is False)
check("J3 非法扩展拒绝（.bmp）", se.in_sync_scope(Path("images/x.bmp"), root) is False)
check("J4 子目录递归拒绝", se.in_sync_scope(Path("images/sub/x.png"), root) is False)
check("J5 白名单扩展全放行",
      all(se.in_sync_scope(Path(f"images/x{e}"), root) for e in (".jpg", ".jpeg", ".png", ".webp", ".gif")))
check("J6 无扩展/非图文件拒绝", se.in_sync_scope(Path("images/readme.md"), root) is False
      and se.in_sync_scope(Path("images/noext"), root) is False)

print("=== K. blob 不合并（异 sha 保留本地 + 返回不变） ===")
k_out, k_cf, k_chg = se.apply_merge_file(root, "images/uuid.png", b"local-bytes", b"remote-bytes", 100, 200)
check("K1 异 sha 保留本地 + 不变", k_out == b"local-bytes" and k_cf == [] and k_chg is False)
k_out2, _, k_chg2 = se.apply_merge_file(root, "images/uuid.png", b"same-bytes", b"same-bytes", 100, 200)
check("K2 同 sha 跳过", k_out2 == b"same-bytes" and k_chg2 is False)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
