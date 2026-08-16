# -*- coding: utf-8 -*-
"""表情包选择器白盒测试（含默认启用集合 50/35 的回归校验）"""

import sys, os, json, tempfile, shutil
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# 隔离 user_data：重定向必须在 import sticker_picker 之前——其模块级
# _migrate_legacy_registry()（sticker_picker.py:61）会把 app 注册表拷进真实 user_data
import modules.app_config as cfg
_tmp = tempfile.mkdtemp(prefix="firefly_test_stk_")
cfg.USER_DIR = __import__("pathlib").Path(_tmp)
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from tools.sticker_picker import (
    pick_sticker, StickerEntry,
    get_all_stickers, get_enabled_stickers, list_all_stickers, get_counters,
    add_sticker, update_sticker, StickerAddError, VALID_CATEGORIES,
)

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


# ══════════════════════════════════════════════════
# 1. pick_sticker — 正常路径（可爱/帅气两类）
# ══════════════════════════════════════════════════
print("=== pick_sticker 正常 ===")

for cat in VALID_CATEGORIES:
    s = pick_sticker(cat)
    if s:
        check(f"category={cat}→有结果", isinstance(s, StickerEntry))
        check(f"category={cat}→id非空", len(s.id) > 0)
        check(f"category={cat}→file非空", len(s.file) > 0)
        check(f"category={cat}→分类匹配", s.category == cat)
    else:
        print(f"  - category={cat}→无匹配表情包")


# ══════════════════════════════════════════════════
# 2. pick_sticker — 边界
# ══════════════════════════════════════════════════
print("\n=== pick_sticker 边界 ===")

# 非法 category → 降级到可爱，不抛异常
s = pick_sticker("")
check("空category→不抛异常", s is not None)
s = pick_sticker("不存在的分类")
check("不存在分类→不抛异常", s is not None)


# ══════════════════════════════════════════════════
# 2.5 默认集合缩减回归（用户已拍板：50 项 / 启用 35 / 停用 15）
# ══════════════════════════════════════════════════
print("\n=== 默认集合缩减回归 ===")

all_list = list_all_stickers()
enabled_dict = get_enabled_stickers()
bundled_ids = set(enabled_dict.keys())
total, enabled_n, disabled_n = len(all_list), len(enabled_dict), sum(1 for s in all_list if not s.enabled)
check("全量条目=50", total == 50)
check("默认启用=35", enabled_n == 35)
check("默认停用=15", disabled_n == 15)
check("全量=启用+停用", total == enabled_n + disabled_n)
check("get_all_stickers 只含启用项", all(s.enabled for s in get_all_stickers().values()))
check("get_enabled_stickers 与 get_all_stickers 一致", set(get_all_stickers()) == set(get_enabled_stickers()))
check("停用项不会出现在启用列表", all(not get_all_stickers().get(s.id) for s in all_list if not s.enabled))

import tools.sticker_picker as sp
bundled_raw = json.loads(sp._REGISTRY_LEGACY.read_text(encoding="utf-8"))["stickers"]
check("bundled registry=50", len(bundled_raw) == 50)
check("bundled enabled=35", sum(1 for i in bundled_raw if i.get("enabled", True)) == 35)
# 每个 bundled id 都出现在合并结果里（含老安装补齐逻辑）
merged_ids = {s.id for s in all_list}
check("bundled 全部合并", all(i["id"] in merged_ids for i in bundled_raw))

# ── 老用户 registry 迁移逻辑（不改真实文件，用临时 registry）──
orig_reg = sp._REGISTRY_FILE
mig_dir = tempfile.mkdtemp(prefix="firefly_test_mig_")
try:
    mig_file = __import__("pathlib").Path(mig_dir) / "registry.json"
    bundled_by_id = {i["id"]: i for i in bundled_raw}
    # 找两个 bundled：一个启用、一个停用，验证老文件无 enabled 字段时按新默认覆盖
    enabled_bundled = next(i for i in bundled_raw if i.get("enabled", True))
    disabled_bundled = next(i for i in bundled_raw if not i.get("enabled", True))
    # 再拿一个用户手动停用过的 bundled，验证迁移不会把它重新启用
    manual_off = next(i for i in bundled_raw if i.get("enabled", True) and i["id"] != enabled_bundled["id"])
    old_items = [
        {"id": enabled_bundled["id"], "file": enabled_bundled["file"],
         "category": enabled_bundled["category"], "label": "旧描述"},
        {"id": disabled_bundled["id"], "file": disabled_bundled["file"],
         "category": disabled_bundled["category"], "label": "旧描述2"},
        {"id": manual_off["id"], "file": manual_off["file"],
         "category": manual_off["category"], "label": "用户改过", "enabled": False},
        {"id": "user_legacy1", "file": "stickers/x.png",
         "category": "可爱", "label": "用户上传"},
    ]
    mig_file.write_text(json.dumps({"stickers": old_items}, ensure_ascii=False), encoding="utf-8")
    sp._REGISTRY_FILE = mig_file
    sp._migrate_enabled_defaults()
    migrated = json.loads(mig_file.read_text(encoding="utf-8"))["stickers"]
    by_id = {i["id"]: i for i in migrated}
    check("迁移→启用默认项补enabled=true", by_id[enabled_bundled["id"]]["enabled"] is True)
    check("迁移→停用默认项补enabled=false", by_id[disabled_bundled["id"]]["enabled"] is False)
    check("迁移→启用项描述词覆盖为bundled", by_id[enabled_bundled["id"]]["label"] == enabled_bundled["label"])
    check("迁移→用户手动停用不被重新启用", by_id[manual_off["id"]]["enabled"] is False)
    check("迁移→用户上传项补enabled=true", by_id["user_legacy1"]["enabled"] is True)
    check("迁移→补齐缺失bundled条目", len(migrated) >= len(bundled_raw))
    sp._REGISTRY_FILE = mig_file
    reloaded = list_all_stickers()
    reloaded_ids = {s.id: s for s in reloaded}
    check("迁移后合并→总数仍=50+用户项", len(reloaded) == 50 + 1)
    check("迁移后→停用默认仍停用", reloaded_ids[disabled_bundled["id"]].enabled is False)
    check("迁移后→用户手动停用仍停用", reloaded_ids[manual_off["id"]].enabled is False)
finally:
    sp._REGISTRY_FILE = orig_reg
    shutil.rmtree(mig_dir, ignore_errors=True)
    # 让后续用例回到主隔离目录的注册表
    sp._REGISTRY_FILE = sp._USER_DIR / "stickers" / "registry.json" if hasattr(sp, "_USER_DIR") else orig_reg


# ══════════════════════════════════════════════════
# 3. add_sticker — 用户添加（用临时 registry.json 避免污染）
# ══════════════════════════════════════════════════
print("\n=== add_sticker ===")

import tools.sticker_picker as sp
orig_registry = sp._REGISTRY_FILE
tmpdir = tempfile.mkdtemp()
try:
    sp._REGISTRY_FILE = __import__("pathlib").Path(tmpdir) / "registry.json"
    # 正常添加
    entry = add_sticker("stickers/test_可爱.png", "可爱", "测试可爱")
    check("添加→返回StickerEntry", isinstance(entry, StickerEntry))
    check("添加→id以user_开头", entry.id.startswith("user_"))
    check("添加→分类可爱", entry.category == "可爱")
    check("添加→写入registry.json", sp._REGISTRY_FILE.exists())
    # 加载后能看到
    all_s = get_all_stickers()
    check("添加后→注册表含新项", entry.id in all_s)
    # 非法分类
    try:
        add_sticker("stickers/x.png", "强势", "x")  # 旧分类应被拒
        check("非法分类→应抛异常", False)
    except StickerAddError:
        check("非法分类→抛StickerAddError", True)
    # 空 label
    try:
        add_sticker("stickers/x.png", "可爱", "")
        check("空label→应抛异常", False)
    except StickerAddError:
        check("空label→抛StickerAddError", True)
finally:
    sp._REGISTRY_FILE = orig_registry
    shutil.rmtree(tmpdir, ignore_errors=True)


# ══════════════════════════════════════════════════
# 4. 注册表
# ══════════════════════════════════════════════════
print("\n=== 注册表 ===")

all_s = get_all_stickers()
check("至少1张", len(all_s) >= 1)
for sid, entry in all_s.items():
    check(f"{sid}→StickerEntry", isinstance(entry, StickerEntry))
    check(f"{sid}→分类合法", entry.category in VALID_CATEGORIES)


# ══════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)

