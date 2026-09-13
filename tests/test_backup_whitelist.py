# -*- coding: utf-8 -*-
"""快照/同步白名单切 `PackRegistry`（阶段 3 · 任务 3.8）验收

卡片：三处白名单原来都拿 `cfg.MODES` 当"全部包"——而 MODES 是**运行期**语义（只有 active
能聊天）。后果：用户点了「归档」的包，数据既不进快照也不进同步 = **静默的数据删除**。

本卡把白名单换成 `cfg.backup_pack_ids()` = `MODES` ∪ `packs.json` 全部条目（**含 archived**），
并把"非法 mode 静默回退默认包"这条老语义挡在包目录定位之外（否则归档包会被当作 story 同步）。

守七件事：
1. `backup_pack_ids()` 语义（含 archived、默认包在前、顺序稳定）；
2. 归档包数据**真的进快照**（卡片验收点）；
3. `_is_snapshot_zip` 认归档包顶层（旧写法会把归档快照误判成单包导出）；
4. `_pack_restorable` 三态判定（归档放行 / 内置与在册包不放行 / 非法 id 不放行）；
5. 归档包快照恢复端到端：数据落 `USER_DIR/{id}`，**不落到 story**，且仍保持归档；
6. `_sync_mode_root` 对归档包用**包目录本身**，对未知 mode 仍回退默认包（老语义兼容）；
7. `_sync_one_mode` 服务端 mode 不一致 → 报错跳过（不写坏数据）。

沙箱纪律：USER_DIR/CONFIG_FILE 全指临时目录，绝不触碰真实 user_data。
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_bakwhitelist_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes_snapshot as rs                                  # noqa: E402
from infra.sync.restore import (_is_snapshot_zip, _pack_restorable,   # noqa: E402
                                _restore_full_snapshot)
import routes_data as rd                                      # noqa: E402
from core.presets import pack_registry                         # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


def _mk_pack(pid, name="测试包"):
    d = _tmp / pid / "character"
    d.mkdir(parents=True, exist_ok=True)
    (d / "preset.json").write_text(json.dumps({
        "id": pid, "name": name, "char_name": "测试角色", "user_name": "你",
        "presentation": "sticker", "schema": 1}, ensure_ascii=False), encoding="utf-8")
    return d


def _reload():
    from core import presets as P
    P._REGISTRIES.clear()
    cfg.reload_presets()
    return pack_registry()


def _names(data: bytes) -> set:
    with zipfile.ZipFile(BytesIO(data)) as zf:
        return set(zf.namelist())


# ── A. backup_pack_ids 语义 ────────────────────────────────────────
print("=== A. backup_pack_ids（MODES ∪ 清单含 archived） ===")
_mk_pack("active_a", "在册甲包")
_reg = _reload()
_reg.register("active_a", source="custom")
_check = _reload()
check("A1 在册包进入白名单且也在 MODES",
      "active_a" in _check.all_ids() and "active_a" in cfg.MODES
      and "active_a" in cfg.backup_pack_ids())
_mk_pack("arch_b", "归档乙包")
_check.register("arch_b", source="custom")
_check.get("arch_b")["state"] = "archived"
_check.save()
_check = _reload()
check("A2 归档包**不在** MODES（运行期语义：不能聊天）", "arch_b" not in cfg.MODES)
check("A3 归档包**在白名单里**（备份期语义：数据仍要备）",
      "arch_b" in _check.all_ids() and "arch_b" in cfg.backup_pack_ids())
check("A4 默认模式排首位且顺序稳定",
      cfg.backup_pack_ids()[0] == cfg.DEFAULT_MODE
      and cfg.backup_pack_ids() == cfg.backup_pack_ids())
check("A5 白名单 = MODES ∪ all_ids（无多余项）",
      set(cfg.backup_pack_ids()) == set(cfg.MODES) | set(_check.all_ids()))

# ── B. 归档包数据真的进快照 ────────────────────────────────────────
print("=== B. 快照打包口径（卡片验收点） ===")
(_tmp / "arch_b" / "data").mkdir(parents=True, exist_ok=True)
(_tmp / "arch_b" / "data" / "conversation.jsonl").write_text(
    '{"seq": 1, "who": "user", "content": "归档包里的对话"}\n', encoding="utf-8")
(_tmp / "active_a" / "data").mkdir(parents=True, exist_ok=True)
(_tmp / "active_a" / "data" / "memory.md").write_text("在册包记忆", encoding="utf-8")
snap = rs.build_full_snapshot_zip()
names = _names(snap)
check("B1 归档包数据进快照（旧实现会漏）",
      "arch_b/data/conversation.jsonl" in names)
check("B2 在册包数据仍进快照", "active_a/data/memory.md" in names)
check("B3 内置包目录结构未被污染（无 story/story 嵌套）",
      not any(n.startswith(f"{cfg.DEFAULT_MODE}/{cfg.DEFAULT_MODE}/") for n in names))

# ── C. _is_snapshot_zip 认归档 ─────────────────────────────────────
print("=== C. 快照格式识别 ===")


def _zip(entries: dict) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in entries.items():
            zf.writestr(k, v)
    return buf.getvalue()


def _is_snap(data: bytes) -> bool:
    return _is_snapshot_zip(zipfile.ZipFile(BytesIO(data)))


check("C1 顶层只有归档包 → 认作快照（修：旧写法不认）",
      _is_snap(_zip({"arch_b/data/conversation.jsonl": "x"})))
check("C2 顶层有 stickers → 认作快照", _is_snap(_zip({"stickers/a.webp": "x"})))
check("C3 单包导出（条目相对模式根）→ 不是快照",
      not _is_snap(_zip({"data/conversation.jsonl": "x", "character/preset.json": "{}"})))
check("C4 未知名顶层 → 不是快照", not _is_snap(_zip({"unknown_zzz/data/a.jsonl": "x"})))

# ── D. _pack_restorable 三态判定 ───────────────────────────────────
print("=== D. _pack_restorable ===")
check("D1 归档包 → 可恢复（走自建包两阶段路径）", _pack_restorable("arch_b") is True)
check("D2 内置包 → 不可恢复（防构造 zip 改写内置角色）",
      _pack_restorable(cfg.DEFAULT_MODE) is False)
check("D3 在册（MODES 内）包 → 不走本分支（保持 by_mode 老语义）",
      _pack_restorable("active_a") is False)
check("D4 非法 id → 不可恢复",
      _pack_restorable("../evil") is False and _pack_restorable("Bad Id") is False
      and _pack_restorable("") is False)

# ── E. 归档包快照恢复端到端 ────────────────────────────────────────
print("=== E. 恢复归档包快照（不落到 story） ===")
_story_sentinel = _tmp / cfg.DEFAULT_MODE / "data"
_story_sentinel.mkdir(parents=True, exist_ok=True)
(_story_sentinel / "sentinel.txt").write_text("默认包原有数据", encoding="utf-8")
# 清掉归档包数据，模拟"换机后从快照恢复"
for _p in sorted((_tmp / "arch_b" / "data").glob("*")):
    _p.unlink()
_arch_snap = _zip({
    "arch_b/character/preset.json": json.dumps(
        {"id": "arch_b", "name": "归档乙包", "char_name": "测试角色",
         "user_name": "你", "presentation": "sticker", "schema": 1}, ensure_ascii=False),
    "arch_b/data/conversation.jsonl": '{"seq": 2, "who": "user", "content": "恢复回来的"}\n',
})
_ok, _err, _n = _restore_full_snapshot(_arch_snap, backup=False)
check("E1 恢复成功", _ok is True and _n >= 2)
check("E2 数据落在归档包自己目录",
      (_tmp / "arch_b" / "data" / "conversation.jsonl").is_file()
      and "恢复回来的" in (_tmp / "arch_b" / "data" / "conversation.jsonl").read_text(encoding="utf-8"))
check("E3 **没有**写进默认包（非法 mode 回退会让数据串包）",
      not (_tmp / cfg.DEFAULT_MODE / "data" / "conversation.jsonl").exists())
check("E4 默认包原有数据未被动",
      (_story_sentinel / "sentinel.txt").is_file())
check("E5 归档包仍在清单里（source 保留）",
      (pack_registry().get("arch_b") or {}).get("id") == "arch_b")
check("E6 归档状态保持（恢复 ≠ 自动取消归档）",
      (pack_registry().get("arch_b") or {}).get("state") == "archived"
      and "arch_b" not in cfg.MODES)

# ── F. 同步白名单与寻址 ────────────────────────────────────────────
print("=== F. 同步白名单 ===")
_root, _m = rd._sync_mode_root("arch_b")
check("F1 归档包同步根 = 包目录本身（不是默认包）",
      _m == "arch_b" and _root == _tmp / "arch_b")
_root2, _m2 = rd._sync_mode_root("active_a")
check("F2 在册包仍走 mode_root 公式",
      _m2 == "active_a" and _root2 == cfg.mode_root("active_a"))
_root3, _m3 = rd._sync_mode_root("no_such_pack")
check("F3 未知 mode 仍回退默认包（老语义兼容）",
      _m3 == cfg.DEFAULT_MODE and _root3 == cfg.mode_root(cfg.DEFAULT_MODE))


def _call_mismatch(path, payload=None, binary=False):
    return {"files": {}, "mode": cfg.DEFAULT_MODE}          # 服务端把归档包解析成了默认包


def _call_ok(path, payload=None, binary=False):
    return {"files": {}}                                    # 老服务端不回 mode → 视为一致


_reports = {"uploaded": [], "downloaded": [], "merged": [], "skipped": [], "conflicts": 0}
_err_mm = rd._sync_one_mode(_call_mismatch, "arch_b", dict(_reports, skipped=[]))
check("F4 服务端 mode 不一致 → 报错跳过（防串包写坏）",
      isinstance(_err_mm, str) and "解析成" in _err_mm)


# 老服务端不回 mode：应放行到正常同步流程（上传分支要 stub urlopen，别真联网）
class _FakeResp:
    def read(self):
        return json.dumps({"applied": ["data/conversation.jsonl"],
                           "conflicts": 0, "skipped": []}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


import urllib.request as _ur                              # noqa: E402

_orig_urlopen = _ur.urlopen
_ur.urlopen = lambda req, timeout=None: _FakeResp()
try:
    _err_ok = rd._sync_one_mode(_call_ok, "arch_b", dict(_reports, skipped=[]))
finally:
    _ur.urlopen = _orig_urlopen
check("F5 服务端不回 mode（老服务端）→ 不拦，正常走同步（向后兼容）", _err_ok is None)

print(f"\n结果：{PASS}/{PASS + FAIL} 通过")
sys.exit(1 if FAIL else 0)
