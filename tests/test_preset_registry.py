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

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
