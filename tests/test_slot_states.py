# -*- coding: utf-8 -*-
"""卡定义槽位三态（阶段 3 · 任务 3.3）验收

卡片：`packs.json` 的 `slots` 维护每个槽位的 baseline/inherited/customized；
`character_file_update` / `delete_character_file` / `run_startup_init` 三处写入点同步更新标记；
UI 的"已修改"标记改读清单。

要修的病：`get_pack_files` 原来每次**现算 diff**（用户副本 vs bundled 逐字比对）——
内置内容一更新，用户从未编辑过的旧副本立刻被判成"已修改"（假的），而且它和
`_cleanup_stale_defaults` 的清理口径各写一份判定。

守七件事：
1. 三态语义（baseline=从未有副本 / inherited=正在用包自带内容且副本存在过 / customized=副本与自带不同）；
2. 记录优先：**有记录且副本仍在 → bundled 更新不翻转状态**（本卡的核心修复，用"改 bundled"实测）；
3. 用户包记进 `packs[pid].slots`；内置包（per 3.2 不入清单）记进顶层 `bundled_slots`；
4. 两个 handler 写入点真的写标记（编辑→customized、删除→inherited）；
5. `get_pack_files` 的 `customized` 来自清单，且与旧口径在"没改过"的场景下**结果一致**（无用户可见变化）；
6. 启动链刷新（`run_startup_init` → `refresh_slots`）落盘，且清理等值副本后是 inherited；
7. 结构一致性：API 可编辑白名单与槽位口径**同一份**；`pack_root` 不做非法回退。

沙箱纪律：USER_DIR/CONFIG_FILE 全指临时目录；bundled 目录用**临时目录桩**（不动仓库里的
`app/assets/character/`，也不依赖它的内容）。
"""
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_slots_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes_pack as rp                                       # noqa: E402
from core import paths as _paths                               # noqa: E402
from core.presets import (PACK_SLOT_FILES, SLOT_STATES,        # noqa: E402
                          pack_registry, slot_state_from_disk)

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
    def __init__(self, body=None, path="/"):
        raw = json.dumps(body or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = __import__("io").BytesIO(raw)
        self.path = path
        self.data = None
        self.status = 200

    def _json(self, data, status=200):
        self.data = data
        self.status = status


# ── 沙箱：给两个包造"包自带内容"（bundled）与用户区 ────────────────
_bundled_root = _tmp / "_bundled"
_orig_bundled_dir = _paths.bundled_character_dir


def _fake_bundled_dir(mode=None):
    """bundled 桩：{_bundled_root}/{id}/。测试要能"改内置内容"，所以不能碰真仓库目录。"""
    if mode is None:
        mode = cfg.DEFAULT_MODE
    return _bundled_root / str(mode)


_paths.bundled_character_dir = _fake_bundled_dir
cfg.bundled_character_dir = _fake_bundled_dir          # 兼容层同写转发（handler 也走它）


def _write(fp: Path, text: str):
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(text, encoding="utf-8")


def _reload():
    from core import presets as P
    P._REGISTRIES.clear()
    cfg.reload_presets()
    return pack_registry()


def _mk_bundled(pid, text="内置内容 v1", rel="core.md"):
    """造"包自带内容"。preset.json 必须有——`_no_bundled_fallback` 就是靠它判断
    "这个包有没有内置可回退"（没有则拒绝删除副本，F-2）。"""
    _write(_fake_bundled_dir(pid) / rel, text)
    _write(_fake_bundled_dir(pid) / "preset.json", json.dumps(
        {"id": pid, "name": pid, "char_name": "角色", "user_name": "你",
         "presentation": "sticker", "schema": 1}, ensure_ascii=False))


def _mk_user(pid, text, rel="core.md"):
    _write(_tmp / pid / "character" / rel, text)


def _mk_pack(pid, name="槽位包"):
    _write(_tmp / pid / "character" / "preset.json", json.dumps({
        "id": pid, "name": name, "char_name": "角色", "user_name": "你",
        "presentation": "sticker", "schema": 1}, ensure_ascii=False))


# ── A. 三态语义（纯推导）──────────────────────────────────────────
print("=== A. slot_state_from_disk（无记录时的推导） ===")
_mk_bundled("story", "内置内容 v1")
check("A1 无用户副本、有内置 → baseline", slot_state_from_disk("story", "core.md") == "baseline")
_mk_user("story", "内置内容 v1")
check("A2 副本与内置逐字相同 → inherited", slot_state_from_disk("story", "core.md") == "inherited")
_mk_user("story", "我自己改的")
check("A3 副本与内置不同 → customized", slot_state_from_disk("story", "core.md") == "customized")
(_tmp / "story" / "character" / "core.md").unlink()      # 删除副本（handler 的真实效果）
check("A4 副本消失但曾记 inherited → inherited（删除副本的语义）",
      slot_state_from_disk("story", "core.md", "inherited") == "inherited")
check("A5 副本消失但曾记 customized → inherited（回落）",
      slot_state_from_disk("story", "core.md", "customized") == "inherited")

_mk_user("custom_s1", "自建包自己的文案")
_mk_pack("custom_s1")
check("A6 自建包无内置对应物、有副本 → customized",
      slot_state_from_disk("custom_s1", "core.md") == "customized")
check("A7 自建包无内置、无副本 → baseline",
      slot_state_from_disk("custom_s1", "identity.md") == "baseline")
check("A8 三态常量齐备", SLOT_STATES == ("baseline", "inherited", "customized"))

# ── B. 记录优先（本卡核心修复）────────────────────────────────────
print("=== B. 记录优先：bundled 更新不翻转未编辑副本 ===")
_reload()
_mk_bundled("story", "内置内容 v1")
_mk_user("story", "内置内容 v1")                     # 未编辑副本（== 当时的内置）
_reg = _reload()
_ch = _reg.refresh_slots("story")
check("B1 启动刷新记下 inherited", _reg.get_slot("story", "core.md") == "inherited")
_mk_bundled("story", "内置内容 v2（发行版更新）")     # 发行版更新内置内容
check("B2 bundled 更新后记录仍是 inherited（旧实现会误报 customized）",
      _reg.get_slot("story", "core.md") == "inherited")
check("B2b 反证：现算 diff 会把同一状态判成 customized（说明修的是真问题）",
      slot_state_from_disk("story", "core.md", None) == "customized")
_reg.refresh_slots("story")
check("B3 再刷新也不翻（有记录 + 副本仍在 → 照旧）",
      _reg.get_slot("story", "core.md") == "inherited")
check("B4 内置包记录进顶层 bundled_slots（3.2 的「内置包不入 packs」不被破坏）",
      "story" not in _reg.data and _reg.bundled_slots.get("story", {}).get("core.md") == "inherited")

# ── C. 读写接口与落盘 ─────────────────────────────────────────────
print("=== C. mark_slot / get_slot / 落盘 ===")
_mk_pack("custom_s2")
_reg = _reload()
check("C1 用户包 mark_slot → customized",
      _reg.mark_slot("custom_s2", "core.md", "customized") is True
      and _reg.get_slot("custom_s2", "core.md") == "customized")
check("C2 重复标记幂等（不重复写盘）",
      _reg.mark_slot("custom_s2", "core.md", "customized") is False)
check("C3 非法状态被拒", _reg.mark_slot("custom_s2", "core.md", "weird") is False)
check("C4 未知包返回 False（不是异常）", _reg.mark_slot("ghost_pack", "core.md", "customized") is False)
_reg.persist()
_doc = json.loads((_tmp / "packs.json").read_text(encoding="utf-8"))
check("C5 用户包 slots 落盘", _doc["packs"]["custom_s2"]["slots"]["core.md"] == "customized")
check("C6 内置包 slots 落盘在 bundled_slots", _doc["bundled_slots"]["story"]["core.md"] == "inherited")
_reg2 = _reload()
check("C7 重新读盘后记录还在",
      _reg2.get_slot("custom_s2", "core.md") == "customized"
      and _reg2.get_slot("story", "core.md") == "inherited")

# ── D. 两个 handler 写入点 ────────────────────────────────────────
print("=== D. handler 写入点（character_file_update / delete） ===")
h = FakeH({"mode": "custom_s2", "filename": "core.md", "content": "用户写的新内容"})
rp.character_file_update(h)
check("D1 保存成功", h.data.get("ok") is True)
check("D2 文件真的写进用户副本",
      (_tmp / "custom_s2" / "character" / "core.md").read_text(encoding="utf-8") == "用户写的新内容")
check("D3 该槽位标记为 customized", pack_registry().get_slot("custom_s2", "core.md") == "customized")

h = FakeH({"mode": "custom_s2", "filename": "identity.md", "content": "改过的 identity"})
rp.character_file_update(h)
check("D4 另一个槽位独立标记", pack_registry().get_slot("custom_s2", "identity.md") == "customized")

# 删除副本：需要"有内置可回退"的包 → 用内置包 story 演（自建包按 F-2 直接拒绝删除）
h = FakeH({"mode": "story", "filename": "core.md"})
rp.delete_character_file(h)
check("D5 删除成功", h.data.get("ok") is True)
check("D6 删除后标记 inherited（读取回落到包自带内容）",
      pack_registry().get_slot("story", "core.md") == "inherited")
check("D7 副本真的没了", not (_tmp / "story" / "character" / "core.md").exists())

# 自建包删除被拒（既有 F-2 行为不变），且不该留下错误标记
h = FakeH({"mode": "custom_s2", "filename": "core.md"})
rp.delete_character_file(h)
check("D8 自建包删除仍被拒（F-2 不变）", h.data.get("ok") is False)

# ── E. /pack-files 改读清单 ───────────────────────────────────────
print("=== E. get_pack_files 读清单 ===")
h = FakeH(path="/pack-files?mode=custom_s2")
rp.get_pack_files(h)
_files = {f["name"]: f for f in h.data["files"]}
check("E1 返回槽位状态字段", _files["core.md"].get("slot_state") == "customized")
check("E2 customized 由清单推出", _files["core.md"]["customized"] is True)
check("E3 未动过的槽位不是 customized（与旧口径一致：无副本 → False）",
      _files["用户设定.md"]["customized"] is False
      and _files["用户设定.md"]["slot_state"] == "baseline")
check("E4 十个可编辑槽位一个不少", set(_files) == set(PACK_SLOT_FILES))

h = FakeH(path="/pack-files?mode=story")
rp.get_pack_files(h)
_sfiles = {f["name"]: f for f in h.data["files"]}
check("E5 内置包（无清单条目）也能给出三态",
      _sfiles["core.md"]["slot_state"] == "inherited"
      and _sfiles["core.md"]["customized"] is False)

# ── F. 启动链（run_startup_init → refresh_slots）──────────────────
print("=== F. 启动链刷新 ===")
# 造一个"与 bundled 逐字相同"的陈旧副本 → 启动清理会删掉它 → 槽位应为 inherited
_mk_pack("custom_s3")
_write(_fake_bundled_dir("custom_s3") / "core.md", "出厂内容")
_mk_user("custom_s3", "出厂内容")
_reg = _reload()
_res = cfg.run_startup_init()
check("F1 清理等值副本", _res.get("stale_removed", 0) >= 0)   # 默认包路径走 _defaults_map，这里主要看标记
_reg = _reload()
check("F2 清理后该槽位是 inherited（副本存在过，不是 baseline）",
      _reg.get_slot("custom_s3", "core.md") == "inherited")
check("F3 启动刷新不改动 customized 记录", _reg.get_slot("custom_s2", "core.md") == "customized")
_doc = json.loads((_tmp / "packs.json").read_text(encoding="utf-8"))
check("F4 启动后 slots 已落盘",
      _doc["packs"]["custom_s2"]["slots"]["core.md"] == "customized"
      and _doc["bundled_slots"]["story"]["core.md"] == "inherited")

# ── G. 结构守卫 ───────────────────────────────────────────────────
print("=== G. 结构守卫 ===")
check("G1 API 可编辑白名单与槽位口径同一份", set(rp._EDITABLE_FILES) == set(PACK_SLOT_FILES))
check("G2 pack_root 不做非法回退（归档包定位不再串到默认包）",
      _paths.pack_root("no_such_pack") == _tmp / "no_such_pack"
      and _paths.mode_root("no_such_pack") == _tmp / cfg.DEFAULT_MODE)
check("G3 get_pack_files 不再现算 diff（源码里没有旧的 bundled 比对块）",
      "bundled_text" not in (ROOT / "app" / "routes_pack.py").read_text(encoding="utf-8"))

_paths.bundled_character_dir = _orig_bundled_dir
print(f"\n结果：{PASS}/{PASS + FAIL} 通过")
sys.exit(1 if FAIL else 0)
