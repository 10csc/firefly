# -*- coding: utf-8 -*-
"""包注册表 packs.json（阶段 3 · 任务 3.2）验收

背景：包存在性原来是**进程态**（启动期扫目录）→ 孤儿目录不可见、换机恢复要两阶段特判、
快照/同步白名单只能靠 cfg.MODES 猜。本卡把"包存在性"升为**持久事实**：
`user_data/packs.json` 成为唯一权威，并以**目录自愈**保证老安装零迁移成本。

本测试守七件事（含卡片三条验收）：
1. 空环境：清单缺失 → 自愈建立（import 期只在内存，落盘由启动链/变更点负责）；
2. 目录有包但清单没有 → 自愈补登记；清单有但目录没了 → 自愈移除；
3. 注册/注销往返（幂等、保留 created_at）与 active/all 过滤；
4. `_discover_presets` 真的**从清单读**（archived 包不进 MODES，即使目录还在）；
5. 建包/删包走真实 handler 时清单同步（register/unregister）；
6. 快照恢复自建包后清单含该包（source=imported）；
7. 损坏的 packs.json 不致命（自愈重建）；内置包不进清单。

沙箱纪律：USER_DIR 指到临时目录，绝不触碰真实 user_data。
"""
import json
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_packreg_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

from core.presets import PackRegistry, pack_registry   # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


def _mk_pack(pid, name="测试包", char="测试角色", user="你"):
    d = _tmp / pid / "character"
    d.mkdir(parents=True, exist_ok=True)
    (d / "preset.json").write_text(json.dumps({
        "id": pid, "name": name, "char_name": char, "user_name": user,
        "presentation": "sticker", "schema": 1}, ensure_ascii=False), encoding="utf-8")
    return d


def _reload():
    """丢弃注册表缓存（模拟新进程/重新读盘）+ 重扫预设。"""
    from core import presets as P
    P._REGISTRIES.clear()
    cfg.reload_presets()
    return pack_registry()


print("=== A. 空环境：清单缺失 → 自愈建立（import 期不落盘） ===")
_reg = _reload()
check("A1 空环境清单为空", _reg.all_ids() == [])
check("A2 import/load 阶段**没有**写出 packs.json（落盘交给启动链）",
      not (_tmp / "packs.json").exists())
check("A3 persist() 后落盘", _reg.persist() and (_tmp / "packs.json").is_file())
_doc = json.loads((_tmp / "packs.json").read_text(encoding="utf-8"))
check("A4 落盘结构含 schema/updated_at/packs", _doc.get("schema") == 1
      and "updated_at" in _doc and isinstance(_doc.get("packs"), dict))

print("=== B. 目录自愈 ===")
_mk_pack("custom_a", "甲包")
_reg = _reload()
check("B1 目录里有、清单没有 → 自愈补登记", "custom_a" in _reg.all_ids())
check("B2 补登记带完整字段（source/state/name/slots/created_at）",
      _reg.get("custom_a").get("source") == "custom"
      and _reg.get("custom_a").get("state") == "active"
      and _reg.get("custom_a").get("name") == "甲包"
      and isinstance(_reg.get("custom_a").get("slots"), dict)
      and bool(_reg.get("custom_a").get("created_at")))
# 落盘由启动链（run_startup_init → persist()）负责；这里显式模拟一次
_reg.persist()
check("B3 启动链 persist() 后补登记落盘",
      "custom_a" in json.loads((_tmp / "packs.json").read_text(encoding="utf-8"))["packs"])
import shutil
shutil.rmtree(_tmp / "custom_a", ignore_errors=True)
_reg = _reload()
check("B4 目录没了 → 自愈移除", "custom_a" not in _reg.all_ids())

print("=== C. 注册/注销往返 ===")
_mk_pack("custom_b", "乙包")
_reg = _reload()
_e1 = _reg.register("custom_b", source="custom")
check("C1 注册成功且可读回", _e1 and _reg.get("custom_b")["name"] == "乙包")
_ca = _reg.get("custom_b")["created_at"]
_e2 = _reg.register("custom_b", source="custom")
check("C2 重复注册幂等且保留 created_at", _e2["created_at"] == _ca)
check("C3 注销返回 True / 再次注销 False",
      _reg.unregister("custom_b") is True and _reg.unregister("custom_b") is False)
check("C4 注销后清单不含该包（目录仍在，下次自愈会重新补登记）",
      "custom_b" not in _reg.all_ids())

print("=== D. active / archived 过滤（为 3.5 预留语义） ===")
_mk_pack("custom_c", "丙包")
_reg = _reload()
_reg.register("custom_c", source="custom")
_reg.data["custom_c"]["state"] = "archived"
_reg.save()
cfg.reload_presets()          # 改完状态要重扫，cfg.MODES 才是当前结论
check("D1 active_ids 不含 archived", "custom_c" not in _reg.active_ids())
check("D2 all_ids 含 archived（归档 ≠ 丢保险，快照/同步仍要覆盖它）",
      "custom_c" in _reg.all_ids())
check("D3 _discover_presets 只收 active → archived 不进 MODES",
      "custom_c" not in cfg.MODES and "custom_c" not in cfg.PRESETS)
_reg.data["custom_c"]["state"] = "active"
_reg.save()
cfg.reload_presets()
check("D4 恢复 active 后回到 MODES", "custom_c" in cfg.MODES and cfg.PRESETS["custom_c"].get("custom"))

print("=== E. 建包/删包走真实 handler 时清单同步 ===")
import routes   # noqa: E402


class FakeH:
    def __init__(self, body):
        raw = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = BytesIO(raw)
        self.data = None
        self.status = 200

    def _json(self, data, status=200):
        self.data = data
        self.status = status


h = FakeH({"id": "custom_new", "name": "新包", "char_name": "角色", "user_name": "你"})
routes.create_pack(h)
check("E1 create_pack 后清单含该包", h.data.get("ok") is True
      and "custom_new" in pack_registry().all_ids() and "custom_new" in cfg.MODES)
h = FakeH({"id": "custom_new"})
routes.delete_pack(h)
# 3.5（删除两级化）改口径：活跃包直接删不掉了，先归档再抹除；两步都要与清单同步
check("E2 活跃包直接删除被拒（3.5：先归档）",
      h.data.get("ok") is False and "custom_new" in pack_registry().all_ids())
h = FakeH({"id": "custom_new"})
routes.archive_pack(h)
check("E2b 归档后清单仍在（只是 state=archived，且不在 MODES）",
      h.data.get("ok") is True and "custom_new" in pack_registry().all_ids()
      and "custom_new" not in cfg.MODES)
h = FakeH({"id": "custom_new"})
routes.delete_pack(h)
check("E2c 抹除后清单同步移除", h.data.get("ok") is True
      and "custom_new" not in pack_registry().all_ids() and "custom_new" not in cfg.MODES)

print("=== F. 快照恢复自建包 → 清单含该包（source=imported） ===")
_buf = BytesIO()
with zipfile.ZipFile(_buf, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("custom_t01/character/preset.json", json.dumps({
        "id": "custom_t01", "name": "快照包", "char_name": "角色", "user_name": "你",
        "presentation": "sticker"}, ensure_ascii=False))
    zf.writestr("custom_t01/data/conversation.jsonl",
                '{"seq": 1, "who": "user", "type": "text", "content": "来自快照"}\n')
    zf.writestr("_config.json", '{"providers": []}')
import routes_data as rd   # noqa: E402
_ok, _err, _n = rd._restore_full_snapshot(_buf.getvalue(), backup=False)
check("F1 恢复成功", _ok is True and not _err)
_reg = pack_registry()
check("F2 清单含恢复的自建包且 source=imported",
      _reg.get("custom_t01") is not None and _reg.get("custom_t01")["source"] == "imported")
check("F3 恢复的包进了 MODES", "custom_t01" in cfg.MODES)

print("=== G. 损坏清单自愈 + 内置包不入清单 ===")
(_tmp / "packs.json").write_text("{ 这不是 json", encoding="utf-8")
_reg = _reload()
check("G1 损坏清单不致命且能从目录重建",
      "custom_t01" in _reg.all_ids() and "custom_c" in _reg.all_ids())
check("G2 清单只描述用户区（内置 story/haruno 不在其中）",
      "story" not in _reg.all_ids() and "haruno" not in _reg.all_ids())
check("G3 内置包仍在注册表扫描结果里（story/haruno 照常可用）",
      "story" in cfg.MODES and "haruno" in cfg.MODES)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
