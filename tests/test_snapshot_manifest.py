# -*- coding: utf-8 -*-
"""快照 `_manifest.json`（阶段 3 · 任务 3.4）验收

卡片：打包时写入 `{snapshot_version:1, app_version, created_at, packs:[{id,name,source,schema,
files,sha256 汇总}]}`；恢复端有 manifest → 按 manifest 驱动并回传摘要；无 manifest（旧 zip）→ 走现逻辑。

要修的病：旧快照 zip 只有数据、没有清单 —— "这份备份里到底有哪些包、是不是完整、有没有被改过"
只能靠解压猜；恢复是**破坏性覆盖**，却对内容零核对。

守六件事：
1. 新快照含 `_manifest.json`，字段齐备（含归档包）；
2. 清单里的 sha256 与 zip 内容**同口径可复算**（打包/核对共用 `infra.sync.restore._pack_digest`）；
3. 恢复端回填并回传摘要，`verified=True`；
4. 旧格式 zip（手工删 manifest）仍可恢复，且摘要里没有 manifest 键（向后兼容）；
5. 内容被改过 → `verified=False`，但**不中止**恢复（宁可恢复 + 明确告知不可信）；
6. 清单与 zip 不一致（缺包 / 清单外包）都要报出来。

沙箱纪律：USER_DIR/CONFIG_FILE 全指临时目录；不打真网络请求（未登录 → 不推送）。
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_snapman_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes_snapshot as rs                                   # noqa: E402
from infra.sync import restore as rst                          # noqa: E402
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


class FakeH:
    def __init__(self, body=None, path="/snapshot/create"):
        raw = json.dumps(body or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = __import__("io").BytesIO(raw)
        self.path = path
        self.data = None
        self.status = 200
        self.wfile = BytesIO()

    def _json(self, data, status=200):
        self.data = data
        self.status = status


def _mk_pack(pid, name="清单包"):
    d = _tmp / pid / "character"
    d.mkdir(parents=True, exist_ok=True)
    (d / "preset.json").write_text(json.dumps({
        "id": pid, "name": name, "char_name": "角色", "user_name": "你",
        "presentation": "sticker", "schema": 1}, ensure_ascii=False), encoding="utf-8")
    return d


def _reload():
    from core import presets as P
    P._REGISTRIES.clear()
    cfg.reload_presets()
    return pack_registry()


def _names(data: bytes) -> list:
    with zipfile.ZipFile(BytesIO(data)) as zf:
        return zf.namelist()


def _read(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(BytesIO(data)) as zf:
        return zf.read(name)


# ── 造数据：在册包 + 归档包 + 表情包 ───────────────────────────────
_mk_pack("custom_m1", "清单甲包")
(_tmp / "custom_m1" / "data").mkdir(parents=True, exist_ok=True)
(_tmp / "custom_m1" / "data" / "conversation.jsonl").write_text(
    '{"seq": 1, "content": "甲包对话"}\n', encoding="utf-8")
_mk_pack("custom_m2", "清单乙包（归档）")
(_tmp / "custom_m2" / "data").mkdir(parents=True, exist_ok=True)
(_tmp / "custom_m2" / "data" / "memory.md").write_text("乙包记忆", encoding="utf-8")
_reg = _reload()
_reg.register("custom_m1", source="custom")
_reg.register("custom_m2", source="custom")
_reg.get("custom_m2")["state"] = "archived"
_reg.save()
_reg = _reload()
(_tmp / "stickers").mkdir(exist_ok=True)
(_tmp / "stickers" / "user_x.webp").write_bytes(b"WEBP")
# 默认包也有数据（验证内置包在清单里标 bundled、名字取自预设表）
(_tmp / cfg.DEFAULT_MODE / "data").mkdir(parents=True, exist_ok=True)
(_tmp / cfg.DEFAULT_MODE / "data" / "conversation.jsonl").write_text(
    '{"seq": 1, "content": "默认包对话"}\n', encoding="utf-8")

print("=== A. 新快照含 manifest ===")
snap = rs.build_full_snapshot_zip()
names = _names(snap)
check("A1 zip 里有 _manifest.json", "_manifest.json" in names)
man = json.loads(_read(snap, "_manifest.json").decode("utf-8"))
check("A2 snapshot_version=1", man.get("snapshot_version") == 1)
check("A3 app_version 与 cfg.APP_VERSION 一致",
      man.get("app_version") == getattr(cfg, "APP_VERSION", ""))
check("A4 created_at 形如 YYYY-MM-DD HH:MM:SS",
      len(str(man.get("created_at", ""))) == 19 and str(man["created_at"])[4] == "-")
_ids = {p["id"] for p in man.get("packs", [])}
check("A5 清单含在册包与**归档包**（3.8 的白名单语义）",
      {"custom_m1", "custom_m2", cfg.DEFAULT_MODE} <= _ids)
check("A5b 内置包标 bundled 且名字取自预设表",
      next(p for p in man["packs"] if p["id"] == cfg.DEFAULT_MODE)["source"] == "bundled"
      and next(p for p in man["packs"] if p["id"] == cfg.DEFAULT_MODE)["name"])
check("A6 每个包条目字段齐备",
      all({"id", "name", "source", "schema", "files", "sha256"} <= set(p)
          for p in man["packs"]))
_p1 = next(p for p in man["packs"] if p["id"] == "custom_m1")
_p2 = next(p for p in man["packs"] if p["id"] == "custom_m2")
check("A7 source/schema/name 取自注册表",
      _p1["source"] == "custom" and _p1["schema"] == 1 and _p1["name"] == "清单甲包")
check("A8 files 计数与哈希非空", _p1["files"] >= 2 and len(_p1["sha256"]) == 64)
check("A9 归档包条目也在且哈希不同（各包独立汇总）",
      _p2["files"] >= 2 and _p2["sha256"] != _p1["sha256"])
check("A10 stickers 汇总也在",
      man.get("stickers", {}).get("files") == 1
      and len(man["stickers"]["sha256"]) == 64)

print("=== B. 哈希口径可复算 ===")


def _digest_from_zip(data: bytes, top: str) -> str:
    """按同口径从 zip 复算某顶层的汇总（模拟恢复端的核对）。"""
    import hashlib
    out = []
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/")
            if len(parts) < 2 or parts[0] != top:
                continue
            h = hashlib.sha256(zf.read(info)).hexdigest()
            out.append(("/".join(parts[1:]), h))
    return rst._pack_digest(out)


check("B1 清单 sha256 与 zip 内容复算一致",
      _digest_from_zip(snap, "custom_m1") == _p1["sha256"])
check("B2 打包端与恢复端用同一实现（_pack_digest 单一来源）",
      rs._pack_digest([("a", "b")]) == rst._pack_digest([("a", "b")]))
check("B3 路径参与哈希（改名必变）",
      rst._pack_digest([("a/x.md", "h")]) != rst._pack_digest([("b/x.md", "h")]))
check("B4 空集合也能算（合法状态，不报错）", len(rst._pack_digest([])) == 64)

print("=== C. 恢复端：摘要回填 + verified ===")
_sum = {}
_ok, _err, _n = rst._restore_full_snapshot(snap, backup=False, summary=_sum)
check("C1 恢复成功", _ok is True and _n > 0)
_m = _sum.get("manifest")
check("C2 摘要回填", isinstance(_m, dict))
check("C3 verified=True（内容未被改动）", _m.get("verified") is True)
check("C4 摘要列出各包核对结果",
      all({"id", "in_manifest", "in_zip", "files", "ok"} <= set(p) for p in _m["packs"]))
check("C5 数据真的恢复了",
      (_tmp / "custom_m1" / "data" / "conversation.jsonl").is_file())

print("=== D. 旧格式 zip（无声清单）向后兼容 ===")
_old = BytesIO()
with zipfile.ZipFile(BytesIO(snap)) as src, zipfile.ZipFile(_old, "w") as dst:
    for info in src.infolist():
        if info.filename == "_manifest.json":
            continue
        dst.writestr(info, src.read(info))
_oldsnap = _old.getvalue()
check("D1 删掉 manifest 后仍被认作快照",
      rst._is_snapshot_zip(zipfile.ZipFile(BytesIO(_oldsnap))) is True)
_sum2 = {}
_ok2, _err2, _n2 = rst._restore_full_snapshot(_oldsnap, backup=False, summary=_sum2)
check("D2 旧 zip 仍可恢复", _ok2 is True and _n2 > 0)
check("D3 旧 zip 不回填 manifest 键（前端据此判断有无清单）", "manifest" not in _sum2)

print("=== E. 内容被改过 → verified=False，但不中止 ===")


def _tamper(data: bytes, target: str) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            raw = src.read(info)
            if info.filename == target:
                raw = raw.replace("甲包对话".encode("utf-8"), "被篡改的".encode("utf-8"))
            dst.writestr(info, raw)
    return buf.getvalue()


_bad = _tamper(snap, "custom_m1/data/conversation.jsonl")
_sum3 = {}
_ok3, _err3, _n3 = rst._restore_full_snapshot(_bad, backup=False, summary=_sum3)
check("E1 仍恢复（不因核对失败而拒绝用户自己的备份）", _ok3 is True)
check("E2 verified=False", _sum3.get("manifest", {}).get("verified") is False)
_p1v = next(p for p in _sum3["manifest"]["packs"] if p["id"] == "custom_m1")
check("E3 逐包标出未通过的是哪个包", _p1v["ok"] is False)
check("E4 满足哈希的包仍标 ok",
      all(p["ok"] for p in _sum3["manifest"]["packs"] if p["id"] != "custom_m1"))

print("=== F. 清单与 zip 不一致要报出来 ===")
_man_only = json.loads(json.dumps(man))
_man_only["packs"].append({"id": "ghost_pack", "name": "只写在清单里", "source": "custom",
                           "schema": 1, "files": 1, "sha256": "0" * 64})
_buf = BytesIO()
with zipfile.ZipFile(BytesIO(snap)) as src, zipfile.ZipFile(_buf, "w") as dst:
    for info in src.infolist():
        if info.filename == "_manifest.json":
            dst.writestr(info, json.dumps(_man_only, ensure_ascii=False))
        else:
            dst.writestr(info, src.read(info))
_sum4 = {}
rst._restore_full_snapshot(_buf.getvalue(), backup=False, summary=_sum4)
check("F1 清单声明但 zip 里没有 → missing_in_zip + verified=False",
      "ghost_pack" in _sum4["manifest"]["missing_in_zip"]
      and _sum4["manifest"]["verified"] is False)

_extra = BytesIO()
with zipfile.ZipFile(BytesIO(snap)) as src, zipfile.ZipFile(_extra, "w") as dst:
    for info in src.infolist():
        if info.filename == "_manifest.json":
            _m2 = json.loads(json.dumps(man))
            _m2["packs"] = [p for p in _m2["packs"] if p["id"] != "custom_m2"]
            dst.writestr(info, json.dumps(_m2, ensure_ascii=False))
        else:
            dst.writestr(info, src.read(info))
_sum5 = {}
rst._restore_full_snapshot(_extra.getvalue(), backup=False, summary=_sum5)
check("F2 zip 里有、清单里没有 → extra_in_zip（手改 zip 不被静默过滤）",
      "custom_m2" in _sum5["manifest"]["extra_in_zip"])
check("F3 清单外的包数据仍被恢复（宁可多恢复，不可静默丢）",
      (_tmp / "custom_m2" / "data" / "memory.md").is_file())

print("=== G. 结构守卫 ===")
_src_rs = (ROOT / "app" / "routes_snapshot.py").read_text(encoding="utf-8")
_src_rst = (ROOT / "app" / "infra" / "sync" / "restore.py").read_text(encoding="utf-8")
check("G1 哈希实现只在 restore.py 一处（routes_snapshot 直接 import，不各写一份）",
      "hashlib" not in _src_rs and "def _pack_digest" not in _src_rs
      and "from infra.sync.restore import _pack_digest" in _src_rs)
check("G2 打包遍历白名单仍是 backup_pack_ids（3.8 不被本卡回退）",
      "cfg.backup_pack_ids()" in _src_rs and "for mode in cfg.MODES" not in _src_rs)
check("G3 _config.json 仍不参与恢复（Key 不随快照回灌）",
      "Key 绝不随快照回灌" in _src_rst)
check("G4 manifest 顶层文件名不在包分发白名单里（不会被当包数据解压）",
      rst._pack_restorable("_manifest.json") is False)

print("=== H. 端到端 handler ===")
h = FakeH({}, path="/snapshot/create")
rs.snapshot_create(h)
check("H1 create 成功", h.data.get("ok") is True and h.data.get("pushed") is False)
_sname = h.data.get("name", "")
check("H2 落盘的快照含 manifest",
      bool(_sname) and "_manifest.json" in _names((rs._snapshots_dir() / _sname).read_bytes()))
h = FakeH({"name": _sname}, path="/snapshot/restore")
rs.snapshot_restore(h)
check("H3 restore 响应带回 manifest 摘要",
      h.data.get("ok") is True and isinstance(h.data.get("manifest"), dict))
check("H4 摘要 verified=True", h.data["manifest"].get("verified") is True)

print(f"\n结果：{PASS}/{PASS + FAIL} 通过")
sys.exit(1 if FAIL else 0)
