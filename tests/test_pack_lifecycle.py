# -*- coding: utf-8 -*-
"""自建角色包生命周期回归 —— 阶段 1 · 任务 1.10（R-09，P2）

两个缺陷：
1. `create_pack` 中途失败不回滚已建目录 → 留下**孤儿包目录**：没进 PRESETS、列表里看不见、
   没有任何管理入口，却占着 id 让用户无法同名重建（真实案例 user_data/custom_f447527f/，
   只剩一个空 character/）。
2. `delete_pack` 要求"必须已注册"，于是孤儿目录**永远删不掉**（前端也没有入口）。

本测试在沙箱 USER_DIR 里造各类目录，钉死：
- 建包失败必回滚（且回滚干净：目录不存在、未进注册表）；
- 孤儿目录（空 / 只剩空 character/）可删；
- **未注册但非空的目录一律拒绝删**（保护用户手工放进来的东西）；
- 内置包与非法 id 依旧拒绝。

沙箱纪律：绝不触碰真实 user_data/（真实孤儿目录的清理需用户另行确认）。
"""
import json
import shutil
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_pack_lifecycle_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes
import routes_pack as rp

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


class FakeH:
    def __init__(self, body):
        raw = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = __import__("io").BytesIO(raw)
        self.data = None
        self.status = 200

    def _json(self, data, status=200):
        self.data = data
        self.status = status


print("=== A. 建包成功（既有行为不变） ===")
h = FakeH({"id": "custom_ok", "name": "正常包", "char_name": "角色", "user_name": "你",
           "presentation": "sticker"})
routes.create_pack(h)
check("A1 返回 ok", h.data.get("ok") is True and h.data.get("id") == "custom_ok")
_pdir = _tmp / "custom_ok"
check("A2 目录与 preset.json 落位", (_pdir / "character" / "preset.json").is_file())
check("A3 进了注册表且标 custom", "custom_ok" in cfg.PRESETS and cfg.PRESETS["custom_ok"].get("custom"))
check("A4 模板文件都在", all((_pdir / "character" / f).is_file()
                            for f in ("core.md", "identity.md", "sms_samples.md")))

print("=== B. 建包中途失败必须回滚（R-09 ①） ===")
# B1：最后一步 reload_presets 抛异常
_orig_reload = cfg.reload_presets


def _boom_reload():
    raise RuntimeError("模拟注册表重扫失败")


cfg.reload_presets = _boom_reload
try:
    h = FakeH({"id": "custom_fail1", "name": "失败包1", "char_name": "角色", "user_name": "你"})
    routes.create_pack(h)
finally:
    cfg.reload_presets = _orig_reload
check("B1 返回 ok=False", h.data.get("ok") is False)
check("B2 残留目录已删除（不留孤儿）", not (_tmp / "custom_fail1").exists())
check("B3 未进注册表", "custom_fail1" not in cfg.PRESETS)

# B2：文件写入阶段（模板）就炸 → 半写状态也必须整体清掉
_orig_tpl = rp._PACK_IDENTITY_TPL
rp._PACK_IDENTITY_TPL = None      # None.format(...) → AttributeError
try:
    h = FakeH({"id": "custom_fail2", "name": "失败包2", "char_name": "角色", "user_name": "你"})
    routes.create_pack(h)
finally:
    rp._PACK_IDENTITY_TPL = _orig_tpl
check("B4 半写失败同样回滚", h.data.get("ok") is False and not (_tmp / "custom_fail2").exists())
check("B5 半写失败后 registry 干净", "custom_fail2" not in cfg.PRESETS)

print("=== C. 孤儿目录可清理（R-09 ②） ===")
# C1：空目录
(_tmp / "custom_orphan_empty").mkdir()
check("C1 空目录判定为孤儿", rp._is_orphan_pack_dir(_tmp / "custom_orphan_empty") is True)
# C2：只剩空 character/（真实案例 custom_f447527f 的形状）
(_tmp / "custom_orphan_char" / "character").mkdir(parents=True)
check("C2 只剩空 character/ 判定为孤儿",
      rp._is_orphan_pack_dir(_tmp / "custom_orphan_char") is True)
h = FakeH({"id": "custom_orphan_char"})
routes.delete_pack(h)
check("C3 孤儿包可被删除", h.data.get("ok") is True and not (_tmp / "custom_orphan_char").exists())
h = FakeH({"id": "custom_orphan_empty"})
routes.delete_pack(h)
check("C4 空目录孤儿也可删", h.data.get("ok") is True and not (_tmp / "custom_orphan_empty").exists())

print("=== D. 保护：未注册但非空的目录一律拒绝 ===")
(_tmp / "custom_data" / "character").mkdir(parents=True)
(_tmp / "custom_data" / "character" / "core.md").write_text("用户手写的设定", encoding="utf-8")
check("D1 含文件的目录不算孤儿", rp._is_orphan_pack_dir(_tmp / "custom_data") is False)
h = FakeH({"id": "custom_data"})
routes.delete_pack(h)
check("D2 拒绝删除且目录仍在",
      h.data.get("ok") is False and (_tmp / "custom_data" / "character" / "core.md").is_file())
(_tmp / "custom_data2" / "data").mkdir(parents=True)
(_tmp / "custom_data2" / "data" / "conversation.jsonl").write_text("x\n", encoding="utf-8")
h = FakeH({"id": "custom_data2"})
routes.delete_pack(h)
check("D3 只有 data/ 无 preset 的目录也拒绝",
      h.data.get("ok") is False and (_tmp / "custom_data2" / "data").is_dir())
(_tmp / "custom_deep" / "character" / "prompts").mkdir(parents=True)
check("D4 character/ 里有子目录（可能是半成品）拒绝",
      rp._is_orphan_pack_dir(_tmp / "custom_deep") is False)

print("=== E. 既有拒绝分支不变 ===")
h = FakeH({"id": "story"})
routes.delete_pack(h)
check("E1 内置包 story 不能删", h.data.get("ok") is False)
h = FakeH({"id": "../evil"})
routes.delete_pack(h)
check("E2 非法 id 拒绝", h.data.get("ok") is False)
h = FakeH({"id": "custom_nonexistent"})
routes.delete_pack(h)
check("E3 不存在的未注册 id 拒绝（不是孤儿）", h.data.get("ok") is False)
h = FakeH({"id": "custom_ok"})
routes.delete_pack(h)
# 3.5（删除两级化）改口径：活跃包不再能一步删掉，必须先归档——原断言"直接可删"已不成立
check("E4 活跃包直接删除被拒（3.5：先归档再抹除）",
      h.data.get("ok") is False and _pdir.exists()
      and "custom_ok" in cfg.PRESETS)
h = FakeH({"id": "custom_ok"})
routes.archive_pack(h)
check("E4b 归档成功且数据仍在", h.data.get("ok") is True and _pdir.exists()
      and "custom_ok" not in cfg.MODES)
h = FakeH({"id": "custom_ok"})
routes.delete_pack(h)
check("E4c 归档后可抹除（清单与目录都没了）",
      h.data.get("ok") is True and not _pdir.exists() and "custom_ok" not in cfg.PRESETS)

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
