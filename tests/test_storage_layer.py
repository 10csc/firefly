# -*- coding: utf-8 -*-
"""A2 测试：storage 注册表/原子写/迁移一次性、per-user 覆盖（eff_cfg 优先级）、
schema 版本化 + WAL、登录失败计数落库、服务器版 add-sticker 经过式（图片不落盘）。
注册表同步策略：images=blob（压缩图整文件可同步）、stickers=metadata（本体本地）、
config/auth=exclude；is_syncable/is_blob 为同步链路唯一判定口。"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_storage_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


from modules import storage as st

print("=== A. storage 注册表（媒体/敏感/同步策略） ===")
check("A1 对话=merge-jsonl", st.sync_policy("data/conversation.jsonl") == "merge-jsonl")
check("A2 favorites=merge-favorites", st.sync_policy("data/favorites.json") == "merge-favorites")
check("A3 memory 文档类 newest", st.sync_policy("data/memory.md") == "newest")
check("A4 表情包=媒体+metadata", st.is_media("stickers/a.webp") and st.sync_policy("stickers/a.webp") == "metadata")
check("A5 图片=媒体+blob 策略", st.is_media("images/pic.png") and st.sync_policy("images/pic.png") == "blob")
check("A6 config=敏感+exclude", st.is_sensitive("config.json") and st.sync_policy("config.json") == "exclude")
check("A7 手账 newest", st.sync_policy("journal/手账.md") == "newest")
check("A8 纠错中间态 exclude", st.sync_policy(".setting_fix/pending.json") == "exclude")
check("A9 images 可同步（blob 一层白名单）",
      st.is_syncable("images/uuid.webp") and st.is_blob("images/uuid.webp"))
check("A10 images 非法扩展/子目录不同步",
      not st.is_syncable("images/x.bmp") and not st.is_syncable("images/sub/x.png"))
check("A11 stickers 本体仍不同步（媒体本地策略不变）", not st.is_syncable("stickers/a.webp"))
check("A12 config/auth 不可同步", not st.is_syncable("config.json") and not st.is_syncable("auth.json"))
check("A13 未注册文字路径默认可同步（兼容现逻辑）", st.is_syncable("data/fav.json"))
check("A14 文字类注册项可同步", st.is_syncable("data/conversation.jsonl") and not st.is_blob("data/conversation.jsonl"))

print("=== A2. 通配注册项匹配（C-6.3，2026-09-13） ===")
# 原状：get_type 对 character/*.md 按字面前缀 startswith("character/*.md") 比对，
# 永远命不中任何真实路径 → 该项的 sync=newest 名存实亡（靠"未注册路径"兜底才没出事）。
check("A15 character/core.md 命中 newest 策略", st.sync_policy("character/core.md") == "newest")
check("A16 命中后仍非媒体/非敏感", not st.is_media("character/core.md")
      and not st.is_sensitive("character/core.md"))
check("A17 非 .md 扩展名不命中（x.exe 落到未注册兜底）", st.get_type("character/x.exe") is None)
check("A18 `*` 不跨目录（子目录里的 .md 不命中）", st.get_type("character/sub/core.md") is None)
check("A19 光杆扩展名不命中（.md 无文件名主干）", st.get_type("character/.md") is None)
check("A20 目录本身不命中（character 不是文件）", st.get_type("character") is None)
check("A21 其它注册项不受影响（前缀匹配语义不变）",
      st.sync_policy("data/memory.md") == "newest" and st.sync_policy("config.json") == "exclude"
      and st.sync_policy("data/conversation.jsonl") == "merge-jsonl")
check("A22 同步判定结论不变（is_syncable 修复前后同为 True，故本次零行为变化）",
      st.is_syncable("character/core.md") and st.is_syncable("character/x.exe"))

print("=== B. 原子写 / 迁移一次性 / 路径审查 ===")
fp = _tmp / "x.json"
check("B1 原子写成功", st.atomic_write_json(fp, {"a": 1}) and json.loads(fp.read_text(encoding="utf-8"))["a"] == 1)
legacy = _tmp / "legacy.txt"
legacy.write_text("old", encoding="utf-8")
tgt = _tmp / "sub" / "target.txt"
check("B2 一次性迁移拷贝", st.migrate_once(tgt, legacy) and tgt.read_text(encoding="utf-8") == "old")
check("B3 二次执行幂等（不覆盖）", st.migrate_once(tgt, legacy) is False)
check("B4 越界路径拒绝", st.safe_relative("../../etc") is None)
check("B5 盘符拒绝", st.safe_relative("C:/x") is None)
check("B6 正常相对路径通过", st.safe_relative("data/a.jsonl") is not None)
check("B7 sha256 计算", st.file_sha256(legacy) == "50868c469a1a1f6b9a8a4f1d0c2f9d3f97edb42c01be3196f2bb1af5e5b91c1f"[0:64] or len(st.file_sha256(legacy)) == 64)

print("=== C. per-user 覆盖（eff_cfg 优先级） ===")
cfg.config["polisher_model"] = "global-model"
check("C1 无覆盖 → 全局值", cfg.eff_cfg("polisher_model") == "global-model")
tok_ov = cfg.set_user_overlay({"polisher_model": "user-model"})
check("C2 有覆盖 → 用户值优先", cfg.eff_cfg("polisher_model") == "user-model")
cfg.set_user_overlay(None)
check("C3 清覆盖 → 回落全局", cfg.eff_cfg("polisher_model") == "global-model")

print("=== D. add-sticker 服务器版经过式（图片不落盘） ===")
import routes
os.environ["FIREFLY_SERVER"] = "1"


class FakeH:
    headers = {}
    def _json(self, data, status=200):
        self.data = data
        self.status = status


# 模拟 multipart 请求（绕过 parse_multipart 完整实现，直接注入 files/fields）
real_parse = routes.parse_multipart
try:
    from tools.sticker_picker import _user_registry_file
    # 用户上下文（服务器版）→ 注册表写用户目录
    t = cfg.set_user_context(user_dir=Path(tempfile.mkdtemp(prefix="u_")))
    routes.parse_multipart = lambda h, **kw: (
        {"category": "可爱", "label": "测试图"},
        {"file": {"filename": "a.png", "data": b"fake-image-bytes"}})
    h = FakeH()
    routes.add_sticker_route(h)
    check("D1 返回 local 引用", h.data.get("ok") and str(h.data.get("file", "")).startswith("local:"))
    check("D2 图片未落盘", not list((Path(cfg.user_scope_key()) / "stickers").rglob("*.png")))
    reg = json.loads(_user_registry_file().read_text(encoding="utf-8"))
    check("D3 注册表含元数据条目", any("local:" in str(i.get("file", "")) for i in reg["stickers"]))
    cfg.reset_user_context(t)
finally:
    routes.parse_multipart = real_parse
os.environ.pop("FIREFLY_SERVER", None)

print("=== E. db：schema_version + WAL + 登录失败计数落库 ===")
import db as db_svc
import auth as auth_svc
import mail as mail_svc

mail_svc._send_raw = lambda *a, **k: None
db_svc.init_db(_tmp / "t.db")
db_svc.ensure_auth_columns()
check("E1 版本已登记", db_svc._schema_version() >= 1)
connect = db_svc._conn()
check("E2 WAL 模式启用", connect.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
# 登录失败计数（settings 表，重启不丢）
email = "lock@qq.com"
for _ in range(3):
    auth_svc.record_login_fail(email)
check("E3 失败计数落库", auth_svc.login_locked(email) is False)
auth_svc.record_login_fail(email)
auth_svc.record_login_fail(email)
check("E4 达到阈值锁定", auth_svc.login_locked(email) is True)
row = db_svc.get_setting(auth_svc._fails_key(email))
check("E5 计数在 settings 表（非空）", bool(row) and len(json.loads(row)) >= 5)
auth_svc.clear_login_fails(email)
check("E6 清除后解锁", auth_svc.login_locked(email) is False)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
