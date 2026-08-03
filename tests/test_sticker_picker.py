# -*- coding: utf-8 -*-
"""表情包选择器白盒测试"""

import sys, os, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tools.sticker_picker import (
    pick_sticker, StickerEntry,
    get_all_stickers, get_counters,
    add_sticker, StickerAddError, VALID_CATEGORIES,
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

