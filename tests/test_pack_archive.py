# -*- coding: utf-8 -*-
"""删除两级化（阶段 3 · 任务 3.5）验收

卡片：`POST /pack-archive`（state→archived，数据保留，包从 /modes 消失）、
`POST /pack-restore`（archived→active）、`delete_pack` 改为**仅 archived 包可删**且删除前
强制打一份快照落 `backups/`（失败即中止）、贴纸 `pack=该id` 条目转全局共享；
前端「归档」主按钮 + 归档区「彻底删除」（输入包名确认）。

要修的病：旧 `delete_pack` 一次 confirm 就 `rmtree` 连数据一起抹掉 —— 用户手滑一次，
人设/记忆/聊天记录全没。本卡把"删除"拆成**可反悔的归档**与**有保险的抹除**两级。

守七件事：
1. 归档：MODES 不含该包、数据目录与清单条目都在；
2. 归档包的贴纸条目降级为全局共享（只动该包的条目）；
3. 取消归档：回到 MODES；
4. 活跃包不能被直接抹除（必须先归档）——旧行为的关键收紧；
5. 抹除前强制备份：`backups/pack-erase-{id}-*.zip` 必出现，且**备份失败即中止**（数据不动）；
6. 孤儿目录清理（R-09）不受影响；内置包/未注册包该拒还是拒；
7. 前端与路由表真的接上了（归档按钮、归档区、输入包名确认、两个新端点）。

沙箱纪律：USER_DIR/CONFIG_FILE 全指临时目录；不打网络。
"""
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_archive_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes_pack as rp                                       # noqa: E402
import routes_config as rc                                     # noqa: E402
import routes_snapshot as rs                                   # noqa: E402
import domain.stickers.picker as sp                            # noqa: E402
from api import router                                         # noqa: E402
from core.presets import pack_registry                         # noqa: E402
from infra.sync.restore import _is_snapshot_zip               # noqa: E402

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


def _post(fn, body, path="/"):
    h = FakeH(body, path)
    fn(h)
    return h.data or {}


def _get(fn, path):
    h = FakeH({}, path)
    fn(h)
    return h.data or {}


def _mk_pack(pid, name="存档包"):
    d = _tmp / pid / "character"
    d.mkdir(parents=True, exist_ok=True)
    (d / "preset.json").write_text(json.dumps({
        "id": pid, "name": name, "char_name": "角色", "user_name": "你",
        "presentation": "sticker", "schema": 1}, ensure_ascii=False), encoding="utf-8")
    (d / "core.md").write_text("核心设定", encoding="utf-8")
    return d


def _mk_data(pid, text="聊过的话"):
    d = _tmp / pid / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / "conversation.jsonl").write_text(
        json.dumps({"seq": 1, "content": text}, ensure_ascii=False) + "\n", encoding="utf-8")
    return d / "conversation.jsonl"


def _reload():
    from core import presets as P
    P._REGISTRIES.clear()
    cfg.reload_presets()
    return pack_registry()


def _reg_file() -> Path:
    sp._REGISTRY_FILE = _tmp / "stickers" / "registry.json"
    return sp._REGISTRY_FILE


def _write_stickers(items):
    fp = _reg_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps({"stickers": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def _packs_of(fp):
    return {i["id"]: i for i in json.loads(fp.read_text(encoding="utf-8"))["stickers"]}


# ── 造包 ──────────────────────────────────────────────────────────
_mk_pack("custom_ar1", "归档甲包")
_data1 = _mk_data("custom_ar1")
_mk_pack("custom_ar2", "归档乙包")
_reg = _reload()
_reg.register("custom_ar1", source="custom")
_reg.register("custom_ar2", source="custom")
_reg = _reload()
check("A0 前置：两个包都在 MODES 里",
      {"custom_ar1", "custom_ar2"} <= set(cfg.MODES))

print("=== A. 归档 ===")
_r = _post(rp.archive_pack, {"id": "custom_ar1"})
check("A1 归档成功", _r.get("ok") is True and _r.get("state") == "archived")
check("A2 清单里 state=archived",
      (pack_registry().get("custom_ar1") or {}).get("state") == "archived")
check("A3 MODES 不再含该包（从列表/聊天消失）", "custom_ar1" not in cfg.MODES)
check("A4 数据目录与文件**都还在**（归档 ≠ 删除）",
      _data1.is_file() and "聊过的话" in _data1.read_text(encoding="utf-8"))
check("A5 另一个包不受影响", "custom_ar2" in cfg.MODES)

print("=== B. 贴纸降级为全局共享 ===")
_fp = _write_stickers([
    {"id": "s_a", "file": "stickers/a.webp", "category": "可爱", "label": "甲", "pack": "custom_ar1"},
    {"id": "s_b", "file": "stickers/b.webp", "category": "可爱", "label": "乙", "pack": "custom_ar2"},
    {"id": "s_c", "file": "stickers/c.webp", "category": "可爱", "label": "全局", "pack": ""},
])
_r2 = _post(rp.archive_pack, {"id": "custom_ar2"})
check("B1 归档返回降级条数", _r2.get("ok") is True and _r2.get("stickers_detached") == 1)
_after = _packs_of(_fp)
check("B2 该包条目 pack 置空（转全局共享）", _after["s_b"]["pack"] == "")
check("B3 别的包条目不动", _after["s_a"]["pack"] == "custom_ar1")
check("B4 本来就是全局的条目不受影响", _after["s_c"]["pack"] == "")
check("B5 降级后该条目仍可用（可见性只由 pack/ enabled 决定）",
      sp.get_enabled_stickers("")["s_b"].id == "s_b")
check("B6 重复归档幂等（changed=False，不重复降级）",
      _post(rp.archive_pack, {"id": "custom_ar2"}).get("changed") is False)
check("B7 未注册包归档被拒", _post(rp.archive_pack, {"id": "ghost_zz"}).get("ok") is False)
check("B8 内置包归档被拒（内置包由发行版提供）",
      _post(rp.archive_pack, {"id": cfg.DEFAULT_MODE}).get("ok") is False)

print("=== C. 取消归档 ===")
_r = _post(rp.restore_pack, {"id": "custom_ar1"})
check("C1 恢复成功", _r.get("ok") is True and _r.get("state") == "active")
check("C2 MODES 又有该包", "custom_ar1" in cfg.MODES)
check("C3 数据原样（恢复不动数据）",
      "聊过的话" in _data1.read_text(encoding="utf-8"))
# 定义缺失 → 拒绝（否则会造出一个"在册但没有 preset.json"的坏包）
_bak = (_tmp / "custom_ar1" / "character" / "preset.json").read_text(encoding="utf-8")
(_tmp / "custom_ar1" / "character" / "preset.json").unlink()
_post(rp.archive_pack, {"id": "custom_ar1"})
check("C4 包定义缺失时拒绝恢复",
      _post(rp.restore_pack, {"id": "custom_ar1"}).get("ok") is False)
(_tmp / "custom_ar1" / "character" / "preset.json").write_text(_bak, encoding="utf-8")
_post(rp.restore_pack, {"id": "custom_ar1"})
check("C5 定义补回后可恢复", "custom_ar1" in cfg.MODES)

print("=== D. 抹除（第二级）：必须先归档 ===")
_r = _post(rp.delete_pack, {"id": "custom_ar1"})
check("D1 活跃包直接删除被拒（旧行为的关键收紧）",
      _r.get("ok") is False and "归档" in str(_r.get("error", "")))
check("D2 被拒后数据完好", _data1.is_file() and (_tmp / "custom_ar1").is_dir())

_post(rp.archive_pack, {"id": "custom_ar1"})
_bdir = _tmp / "backups"
_before = set(p.name for p in _bdir.glob("pack-erase-*.zip")) if _bdir.exists() else set()
_r = _post(rp.delete_pack, {"id": "custom_ar1"})
check("D3 归档后抹除成功", _r.get("ok") is True)
check("D4 目录真的没了", not (_tmp / "custom_ar1").exists())
check("D5 清单条目也没了", pack_registry().get("custom_ar1") is None)
_after_b = set(p.name for p in _bdir.glob("pack-erase-*.zip"))
_new = _after_b - _before
check("D6 抹除前强制出新备份 pack-erase-*.zip", len(_new) == 1)
check("D7 响应回传备份名且与落盘一致",
      _r.get("backup") in _after_b and _r.get("backup", "").startswith("pack-erase-custom_ar1-"))
_snap_fp = _bdir / sorted(_new)[0]
_buf = _snap_fp.read_bytes()
check("D8 备份是**全量快照**格式（能被快照恢复链路识别）",
      _buf.startswith(b"PK") and _is_snapshot_zip(zipfile.ZipFile(io.BytesIO(_buf))))
_zn = zipfile.ZipFile(io.BytesIO(_buf)).namelist()
check("D9 备份里含被抹除包的数据（3.8 的 archived 白名单让这份保险真的有用）",
      any(n.startswith("custom_ar1/data/") for n in _zn))

print("=== E. 备份失败 → 中止删除（数据一个字节都不动） ===")
_mk_pack("custom_ar3", "备份失败包")
_d3 = _mk_data("custom_ar3")
_reg = _reload()
_reg.register("custom_ar3", source="custom")
_reload()
_post(rp.archive_pack, {"id": "custom_ar3"})
_orig_build = rs.build_full_snapshot_zip


def _boom():
    raise OSError("模拟磁盘满")


rs.build_full_snapshot_zip = _boom
try:
    _r = _post(rp.delete_pack, {"id": "custom_ar3"})
finally:
    rs.build_full_snapshot_zip = _orig_build
check("E1 备份失败 → 拒绝删除并说明原因",
      _r.get("ok") is False and "备份失败" in str(_r.get("error", "")))
check("E2 目录仍在", _d3.is_file())
check("E3 清单条目仍在（没留下删一半的状态）", pack_registry().get("custom_ar3") is not None)
check("E4 仍可恢复使用", "custom_ar3" not in cfg.MODES and
      _post(rp.restore_pack, {"id": "custom_ar3"}).get("ok") is True)

print("=== F. 孤儿目录清理（R-09）不受影响 ===")
(_tmp / "orphan_x" / "character").mkdir(parents=True, exist_ok=True)
check("F1 空孤儿目录可清", _post(rp.delete_pack, {"id": "orphan_x"}).get("ok") is True
      and not (_tmp / "orphan_x").exists())
(_tmp / "orphan_y").mkdir(parents=True, exist_ok=True)
(_tmp / "orphan_y" / "data").mkdir(parents=True, exist_ok=True)
(_tmp / "orphan_y" / "data" / "x.jsonl").write_text("{}", encoding="utf-8")
check("F2 非空未注册目录仍拒绝（免误删用户数据）",
      _post(rp.delete_pack, {"id": "orphan_y"}).get("ok") is False)
check("F3 内置包删除仍拒绝",
      _post(rp.delete_pack, {"id": cfg.DEFAULT_MODE}).get("ok") is False)

print("=== G. /modes 归档区 + /pack-files state ===")
_post(rp.archive_pack, {"id": "custom_ar2"})
_m = _get(rc.get_modes, "/modes")
check("G1 归档包不在 modes 里", "custom_ar2" not in {x["id"] for x in _m["modes"]})
check("G2 归档区列出了它（含名字与 has_data）",
      any(x["id"] == "custom_ar2" and x["name"] == "归档乙包" and x["has_data"]
          for x in _m.get("archived", [])))
check("G3 无归档时 archived 是空表而不是缺字段",
      isinstance(_m.get("archived"), list))
_pf = _get(rp.get_pack_files, "/pack-files?mode=custom_ar2")
check("G4 归档包没有详情页入口（/pack-files 的 mode 白名单仍是 MODES → 回退默认包；"
      "管理走首页归档区）",
      _pf.get("mode") == cfg.DEFAULT_MODE and _pf.get("state") == "active")
check("G5 活跃包回传 state=active", _get(rp.get_pack_files, "/pack-files?mode=custom_ar3").get("state") == "active")

print("=== H. 结构守卫（路由 + 前端接线） ===")
check("H1 路由表含两个新端点",
      "/pack-archive" in router.POST_ROUTES and "/pack-restore" in router.POST_ROUTES)
check("H2 两个端点指向真实 handler",
      router.POST_ROUTES["/pack-archive"] is rp.archive_pack
      and router.POST_ROUTES["/pack-restore"] is rp.restore_pack)
_views = (ROOT / "app" / "static" / "js" / "views.js").read_text(encoding="utf-8")
_packsjs = (ROOT / "app" / "static" / "js" / "panels" / "packs.js").read_text(encoding="utf-8")
_bundle = (ROOT / "app" / "static" / "js" / "bundle.js").read_text(encoding="utf-8")
check("H3 归档区渲染 + 两个出口都在（views.js）",
      "_renderArchivedPacks" in _views and "/pack-restore" in _views
      and "/pack-delete" in _views and "彻底删除" in _views)
check("H4 彻底删除要输入包名确认（不是一次 confirm 就删）",
      "prompt(" in _views and "包名不匹配" in _views)
check("H5 详情页主按钮改成归档，且走共用动作函数（packs.js 不带任何删除端点）",
      '__packLifecycle("archive"' in _packsjs and "归档" in _packsjs
      and "/pack-delete" not in _packsjs and "/pack-archive" not in _packsjs)
check("H5b 三个生命周期动作只有一处实现（views.js 的 packLifecycle）",
      _views.count("packLifecycle") >= 4 and "/pack-archive" in _views)
check("H6 bundle 已同步新代码（门禁会逐字校验，这里给个快速信号）",
      "archived-packs" in _bundle and "/pack-archive" in _bundle)
check("H7 归档包单独存一份清单（不进 PRESET_MODES）",
      "ARCHIVED_PACKS" in _views and "ARCHIVED_PACKS.splice" in _views)

print(f"\n结果：{PASS}/{PASS + FAIL} 通过")
sys.exit(1 if FAIL else 0)
