# -*- coding: utf-8 -*-
"""本地备份管理测试（/backup/* 端点 + export_data 打包口径）：
create / list / restore（含 pre-restore 自动备份）/ delete / 每模式保留 10 份 /
name 穿越拒绝 / export 与备份 zip 排除内部目录且 images/ 进包（正式消息数据）。"""
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
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_backup_"))
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


import routes


class FakeH:
    """最小 handler：_json 捕获 + zip 响应写入 wfile。"""

    def __init__(self, body=None, path="/"):
        self.data = None
        self.status = 200
        self.path = path
        self.headers = {}
        self.sent_headers = {}
        self.wfile = io.BytesIO()
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            self.headers = {"Content-Length": str(len(raw))}
            self.rfile = io.BytesIO(raw)

    def _json(self, data, status=200):
        self.data = data
        self.status = status

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass


def _zip_names(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return set(zf.namelist())


# ── 造数据：story 模式（含内部目录与图片） ──
root = cfg.mode_root("story")
(root / "data").mkdir(parents=True, exist_ok=True)
(root / "data" / "conversation.jsonl").write_text(
    '{"seq": 1, "who": "user", "type": "text", "content": "你好"}\n', encoding="utf-8")
(root / "images").mkdir(exist_ok=True)
(root / "images" / "img_a.png").write_bytes(b"\x89PNG\r\n\x1a\nbackup-test")
(root / ".sync_backups").mkdir(exist_ok=True)
(root / ".sync_backups" / "x.pre.bak").write_text("bak", encoding="utf-8")
(root / ".setting_fix").mkdir(exist_ok=True)
(root / ".setting_fix" / "manifest.json").write_text("{}", encoding="utf-8")
BACKUPS = root.parent / "backups"

print("=== A. export_data 打包口径（排除内部目录，images/ 进包） ===")
h = FakeH(path="/export-data?mode=story")
routes.export_data(h)
names = _zip_names(h.wfile.getvalue())
check("A1 export 含对话与图片", "data/conversation.jsonl" in names and "images/img_a.png" in names)
check("A2 export 排除内部目录",
      not any(n.startswith((".sync_backups/", ".setting_fix/", ".sync_conflicts/")) for n in names))
check("A3 export 附剥 Key 的 _config.json", "_config.json" in names)

print("=== B. /backup/create + /backups ===")
h = FakeH({"mode": "story"})
routes.backup_create(h)
check("B1 create 成功", h.data.get("ok") and h.data.get("mode") == "story")
bname = h.data.get("name", "")
check("B2 备份名格式 {mode}-{时间戳}.zip",
      bname.startswith("story-") and bname.endswith(".zip"))
bfp = BACKUPS / bname
check("B3 备份落盘", bfp.is_file() and bfp.stat().st_size == h.data.get("size"))
bnames = _zip_names(bfp.read_bytes())
check("B4 备份 zip 口径同 export（图片进包/内部目录排除）",
      "images/img_a.png" in bnames
      and not any(n.startswith((".sync_backups/", ".setting_fix/")) for n in bnames))

h = FakeH()
routes.backups_list(h)
items = h.data.get("backups", [])
check("B5 list 含新建备份", h.data.get("ok") and any(b["name"] == bname for b in items))
check("B6 list 字段完整（name/mode/size/time）",
      all(k in items[0] for k in ("name", "mode", "size", "time")) if items else False)

print("=== C. /backup/restore（含 pre-restore 自动备份） ===")
# 改动当前数据：覆盖对话 + 删图片
(root / "data" / "conversation.jsonl").write_text(
    '{"seq": 9, "who": "user", "type": "text", "content": "被改过的"}\n', encoding="utf-8")
(root / "images" / "img_a.png").unlink()
h = FakeH({"name": bname})
routes.backup_restore(h)
check("C1 restore 成功", h.data.get("ok") and h.data.get("restored") == bname)
restored = (root / "data" / "conversation.jsonl").read_text(encoding="utf-8")
check("C2 数据已恢复", "你好" in restored and "被改过的" not in restored)
check("C3 图片已恢复", (root / "images" / "img_a.png").is_file())
pre = sorted(BACKUPS.glob("pre-restore-story-*.zip"))
check("C4 恢复前自动打 pre-restore 备份", len(pre) >= 1)
pre_names = _zip_names(pre[-1].read_bytes()) if pre else set()
check("C5 pre-restore 含恢复前数据", "data/conversation.jsonl" in pre_names)
# 自动备份不进手动列表
h = FakeH()
routes.backups_list(h)
check("C6 pre-restore 不进 /backups 列表",
      not any(b["name"].startswith("pre-restore-") for b in h.data.get("backups", [])))

print("=== D. /backup/delete ===")
h = FakeH({"name": bname})
routes.backup_delete(h)
check("D1 delete 成功", h.data.get("ok") and not bfp.exists())
h = FakeH({"name": bname})
routes.backup_delete(h)
check("D2 重复删除报不存在", h.data.get("ok") is False and "不存在" in h.data.get("error", ""))

print("=== E. 每模式保留最近 10 份 ===")
last_name = ""
for _ in range(12):
    h = FakeH({"mode": "haruno"})
    routes.backup_create(h)
    last_name = h.data.get("name", "")
kept = sorted(BACKUPS.glob("haruno-*.zip"))
check("E1 12 次创建后只留 10 份", len(kept) == 10)
check("E2 最新一份保留", (BACKUPS / last_name).is_file())

print("=== F. name 穿越/非法拒绝 ===")
for bad in ("../x.zip", "..\\x.zip", "a/b.zip", "a\\b.zip", "x.zip/../y.zip",
            "..zip", "story-*.zip", "", None, "story-20260101.zip/"):
    h = FakeH({"name": bad})
    routes.backup_restore(h)
    r_ok = h.data.get("ok") is False
    h = FakeH({"name": bad})
    routes.backup_delete(h)
    d_ok = h.data.get("ok") is False
    if not (r_ok and d_ok):
        break
check("F1 restore/delete 拒绝穿越与非法名", r_ok and d_ok)
h = FakeH({"name": "story-19990101-000000.zip"})
routes.backup_restore(h)
check("F2 合法名但不存在 → 备份不存在", h.data.get("ok") is False and "不存在" in h.data.get("error", ""))
h = FakeH({"name": "xxx-20260101-000000.zip"})
routes.backup_restore(h)
check("F3 未知模式名拒绝", h.data.get("ok") is False and "模式" in h.data.get("error", ""))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
