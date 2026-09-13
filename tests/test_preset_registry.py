# -*- coding: utf-8 -*-
"""预设包注册表（角色预设化阶段 1）：_discover_presets 扫描/校验/兜底 + MODES 派生不变。"""
import json
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


def _mk_pack(parent, name, data=None, raw=None):
    d = parent / name
    d.mkdir(parents=True)
    if raw is not None:
        (d / "preset.json").write_text(raw, encoding="utf-8")
    elif data is not None:
        (d / "preset.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return d


_VALID = {"id": None, "name": "测试", "char_name": "流萤", "user_name": "开拓者",
          "presentation": "sticker"}

print("=== A. 真实注册表（仓库现状） ===")
check("A1 MODES 内容与顺序不变", cfg.MODES == ("story", "haruno"))
check("A2 DEFAULT_MODE=story", cfg.DEFAULT_MODE == "story")
check("A3 story 包 presentation=sticker", cfg.PRESETS["story"]["presentation"] == "sticker")
check("A4 haruno 包 presentation=narration", cfg.PRESETS["haruno"]["presentation"] == "narration")
check("A5 包角色名/用户称呼",
      cfg.PRESETS["story"]["char_name"] == "流萤" and cfg.PRESETS["haruno"]["user_name"] == "开拓者")
check("A6 story 包 knowledge_dirs 声明",
      cfg.PRESETS["story"]["knowledge_dirs"] == ["knowledge", "database/dialogues_compiled"])
check("A7 haruno 包无 knowledge_dirs", cfg.PRESETS["haruno"]["knowledge_dirs"] is None)

print("=== B. 扫描校验（临时目录注入） ===")
with tempfile.TemporaryDirectory(prefix="firefly_test_preset_") as tmp:
    base = Path(tmp)
    _mk_pack(base, "aaa", dict(_VALID, id="aaa"))
    _mk_pack(base, "badjson", raw="{not json")
    _mk_pack(base, "idmismatch", dict(_VALID, id="other"))
    _mk_pack(base, "badpres", dict(_VALID, id="badpres", presentation="xxx"))
    _mk_pack(base, "nofield", {"id": "nofield"})
    (base / "emptydir").mkdir()              # 无 preset.json 的目录
    (base / "afile").write_text("x", encoding="utf-8")  # 文件不是目录
    presets = cfg._discover_presets(base)
    check("B1 合法包被发现", presets.get("aaa", {}).get("name") == "测试")
    check("B2 损坏 json 跳过", "badjson" not in presets)
    check("B3 id 与目录名不一致跳过", "idmismatch" not in presets and "other" not in presets)
    check("B4 presentation 非法回退 sticker",
          presets.get("badpres", {}).get("presentation") == "sticker")
    check("B5 缺必填字段跳过", "nofield" not in presets)
    check("B6 无 preset.json 目录跳过", "emptydir" not in presets)
    check("B7 扫描结果恰好 2 包", len(presets) == 2)

print("=== C. 兜底 ===")
with tempfile.TemporaryDirectory(prefix="firefly_test_preset_empty_") as tmp:
    presets = cfg._discover_presets(Path(tmp))
    check("C1 空目录回退内置两包", set(presets) == {"story", "haruno"})
with tempfile.TemporaryDirectory(prefix="firefly_test_preset_missing_") as tmp:
    presets = cfg._discover_presets(Path(tmp) / "不存在")
    check("C2 目录不存在同样兜底", set(presets) == {"story", "haruno"})

print("=== D. 现有行为保持 ===")
check("D1 非法 mode 回退默认", cfg.mode_root("不存在的模式") == cfg.mode_root("story"))

print("=== E. knowledge_dirs 越界校验（R-10，2026-09-13） ===")
print("--- E1 单元：_clean_knowledge_dirs 逐项判定 ---")
_c = cfg._clean_knowledge_dirs
check("E1 合法相对目录保留", _c(["knowledge", "database/dialogues_compiled"]) ==
      ["knowledge", "database/dialogues_compiled"])
check("E2 `..` 穿越被拒", _c(["../../etc"]) == [])
check("E3 反斜杠穿越同样被拒", _c(["..\\..\\windows"]) == [])
check("E4 绝对路径被拒", _c(["/etc/passwd"]) == [])
check("E5 盘符路径被拒", _c(["C:/Windows"]) == [])
check("E6 空串/纯斜杠丢弃", _c(["", "   ", "/"]) == [])
check("E7 非字符串项丢弃（不炸）", _c(["knowledge", 123, None, {"a": 1}]) == ["knowledge"])
check("E8 混合清单只留合法项（逐项跳过，不整包拒绝）",
      _c(["knowledge", "../../etc", "/etc", "database/dialogues_compiled"]) ==
      ["knowledge", "database/dialogues_compiled"])
check("E9 前后空白被归一化", _c(["  knowledge  "]) == ["knowledge"])
# 注意：**前导斜杠按"绝对路径"拒绝**，不再像旧实现那样 strip 掉。
# 理由（实测）：Windows 下 `ROOT / "/etc/passwd"` 会得到 `\etc\passwd`（跳到盘根）——
# 绝对路径确实能逃出仓库，必须拒；旧实现靠 strip("/") 变成相对路径"碰巧"安全。
# 代价：手写 "/knowledge" 这种声明会失效（真实预设包都不带前导斜杠，story 用的是 "knowledge"）。
check("E9b 前导斜杠=绝对路径，拒绝", _c(["/database/dialogues_compiled/"]) == [])
check("E9c 尾部斜杠（相对路径）仍被归一化", _c(["database/dialogues_compiled/"]) ==
      ["database/dialogues_compiled"])
check("E10 非 list 返回 None（既有语义：=未声明）", _c("knowledge") is None and _c(None) is None)
check("E11 空 list 返回 []（=声明了但没有可用目录）", _c([]) == [])

print("--- E2 集成：坏声明不影响包加载 ---")
with tempfile.TemporaryDirectory(prefix="firefly_test_preset_kd_") as tmp:
    base = Path(tmp)
    _mk_pack(base, "evil", dict(_VALID, id="evil",
                               knowledge_dirs=["../../etc", "knowledge", "/etc", "C:/x"]))
    _mk_pack(base, "good", dict(_VALID, id="good", knowledge_dirs=["knowledge"]))
    _mk_pack(base, "nokd", dict(_VALID, id="nokd"))
    presets = cfg._discover_presets(base)
    check("E12 含越界声明的包仍能加载（不整包拒绝）", "evil" in presets)
    check("E13 越界项被剔除，合法项保留",
          presets.get("evil", {}).get("knowledge_dirs") == ["knowledge"])
    check("E14 合法声明原样保留", presets.get("good", {}).get("knowledge_dirs") == ["knowledge"])
    check("E15 未声明仍是 None", presets.get("nokd", {}).get("knowledge_dirs") is None)

print("--- E3 真实注册表未被误伤 ---")
check("E16 story 的知识库声明逐字不变（含空格/顺序）",
      cfg.PRESETS["story"]["knowledge_dirs"] == ["knowledge", "database/dialogues_compiled"])
check("E17 越界校验未把目录改成绝对/改序",
      all(not d.startswith("/") and ".." not in d.split("/")
          for d in (cfg.PRESETS["story"]["knowledge_dirs"] or [])))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
