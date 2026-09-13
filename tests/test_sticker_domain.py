# -*- coding: utf-8 -*-
"""表情包领域归位 + tools 改名守卫（阶段 2 · 任务 2.7）

改动：
- `app/tools/sticker_picker.py`（428 行）→ `app/domain/stickers/picker.py`（领域层），
  旧路径保留为 re-export **兼容层**（带写转发）；
- `tools/sticker_selector.py` → `tools/sticker_dev_selector.py`（与 app 内表情包领域消歧义）。

守五件事：
1. 实现已在领域层，旧路径是转发用 `_ShimModule`；
2. 拆分前 30 个顶层名字经旧路径仍全部可达；
3. **写转发**：`sp._REGISTRY_FILE = 临时文件` 必须落到领域模块（tests 直接这样注入沙箱；
   只按值 re-export 会让补丁失效 → test_sticker_picker / test_sticker_upload 立刻红）；
4. **路径深度**：`_REGISTRY_LEGACY` 必须指向 `app/assets/stickers/registry.json`——
   原实现用 `Path(__file__).parent.parent`，归位后深度变化会指到 `app/domain/assets/...`（真实踩过）；
5. tools 改名彻底：旧文件不存在、新文件在、指向的 html 仍在、代码里无旧引用。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import modules.app_config as cfg   # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_sticker_domain_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import tools.sticker_picker as sp              # noqa: E402  （兼容层）
from domain.stickers import picker as picker   # noqa: E402  （领域层）

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
    "StickerAddError", "StickerDeleteError", "StickerEntry", "StickerUpdateError",
    "VALID_CATEGORIES", "_PICK_COUNT", "_REGISTRY_FILE", "_REGISTRY_LEGACY",
    "_STICKERS_DEFAULT", "_char_overlap", "_load_registry", "_lock",
    "_migrate_enabled_defaults", "_migrate_legacy_registry", "_read_registry_items",
    "_save_user_entry", "_user_registry_file", "_write_registry_all", "add_sticker",
    "delete_sticker", "editable_ids", "get_all_stickers", "get_counters",
    "get_enabled_stickers", "list_all_stickers", "logger", "pick_sticker",
    "pick_sticker_by_label", "pick_sticker_by_meaning", "update_sticker",
]

print("=== A. 归位与兼容层身份 ===")
check("A1 实现在 app/domain/stickers/picker.py",
      (ROOT / "app" / "domain" / "stickers" / "picker.py").is_file())
check("A2 旧路径是转发用 _ShimModule", type(sp).__name__ == "_ShimModule")
check("A3 领域包有 __init__.py（domain / domain.stickers）",
      (ROOT / "app" / "domain" / "__init__.py").is_file()
      and (ROOT / "app" / "domain" / "stickers" / "__init__.py").is_file())
_missing = [n for n in BEFORE_NAMES if not hasattr(sp, n)]
check(f"A4 拆分前 {len(BEFORE_NAMES)} 个顶层名字经兼容层全部可达", not _missing)
if _missing:
    print("    缺失:", _missing)
check("A5 名字由领域模块持有（兼容层不留实现）",
      all(hasattr(picker, n) for n in BEFORE_NAMES))

print("=== B. 写转发（tests 直接改模块级全局量） ===")
_orig = picker._REGISTRY_FILE
_injected = Path(tempfile.mkdtemp(prefix="firefly_test_sticker_inject_")) / "registry.json"
try:
    sp._REGISTRY_FILE = _injected
    check("B1 写落到领域模块（函数读得到）", picker._REGISTRY_FILE == _injected)
    check("B2 兼容层读到同一个值", sp._REGISTRY_FILE == _injected)
    check("B3 领域函数确实使用被注入的路径（_user_registry_file 无用户上下文时用它）",
          str(sp._user_registry_file()) == str(_injected))
finally:
    sp._REGISTRY_FILE = _orig
check("B4 还原一致", sp._REGISTRY_FILE == picker._REGISTRY_FILE == _orig)

print("=== C. 路径深度（归位的真实坑） ===")
check("C1 _REGISTRY_LEGACY 指向 app/assets/stickers/registry.json",
      picker._REGISTRY_LEGACY == cfg.BASE_DIR / "assets" / "stickers" / "registry.json")
check("C2 该文件真实存在（旧实现归位后指到 app/domain/assets/ 就是这里翻车）",
      picker._REGISTRY_LEGACY.is_file())
_picker_src = (ROOT / "app" / "domain" / "stickers" / "picker.py").read_text(encoding="utf-8")
_code_lines = [ln for ln in _picker_src.split("\n") if not ln.lstrip().startswith("#")]
check("C3 代码里不再用 __file__ 相对深度定位 assets（注释里说明可以）",
      not any("__file__" in ln for ln in _code_lines))

print("=== D. 消费方指向领域层 ===")
_app_srcs = list((ROOT / "app").rglob("*.py"))
_old_refs = [p.relative_to(ROOT).as_posix() for p in _app_srcs
             if "from tools.sticker_picker import" in p.read_text(encoding="utf-8")
             and p.name != "sticker_picker.py"]
check("D1 app/ 内已无 `from tools.sticker_picker import`（除兼容层自身）", not _old_refs)
if _old_refs:
    print("    仍引用:", _old_refs)
_new_cnt = sum(1 for p in _app_srcs if "from domain.stickers.picker import" in p.read_text(encoding="utf-8"))
check(f"D2 领域层导入已就位（{_new_cnt} 个文件）", _new_cnt >= 6)

print("=== E. tools 改名 ===")
check("E1 旧名 tools/sticker_selector.py 已不存在",
      not (ROOT / "tools" / "sticker_selector.py").exists())
check("E2 新名 tools/sticker_dev_selector.py 在",
      (ROOT / "tools" / "sticker_dev_selector.py").is_file())
_dev = (ROOT / "tools" / "sticker_dev_selector.py").read_text(encoding="utf-8")
check("E3 新脚本仍指向它用的 html（sticker_selector.html）",
      'sticker_selector.html' in _dev and (ROOT / "tools" / "sticker_selector.html").is_file())
check("E4 脚本内用法提示已改名", "tools/sticker_dev_selector.py" in _dev)
# 全仓（排除历史记录的协作记忆）不应再有对旧脚本名的引用
_hits = []
for p in list((ROOT / "tools").rglob("*")) + list((ROOT / "docs").rglob("*.md")):
    if p.is_file() and p.suffix in (".py", ".md", ".bat", ".vbs", ".json"):
        try:
            if "sticker_selector.py" in p.read_text(encoding="utf-8"):
                _hits.append(p.relative_to(ROOT).as_posix())
        except Exception:
            pass
check("E5 仅剩历史记录/计划书提及旧名，无活引用",
      all(("协作记忆" in h or "架构重构计划" in h) for h in _hits))
if _hits:
    print("    提及处:", _hits)

print("=== F. 端点 oracle 未受影响 ===")
p = subprocess.run([sys.executable, str(ROOT / "tests" / "test_routes_oracle.py")],
                   capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
check("F1 路由 oracle 仍绿（57 POST / 28 GET）", p.returncode == 0)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
