# -*- coding: utf-8 -*-
"""设定纠错助手路由测试 — 新端点接线 / 旧反馈路由移除 / 数据导出包含 .setting_fix"""
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_setting_routes_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes
from modules import setting_fix as sf
from modules import setting_fix_store as store

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


class JsonHandler:
    def __init__(self, path="/", body=None):
        self.path = path
        self.body = body
        raw = (body or "").encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.data = None
        self.status = None

    def _json(self, data, status=200):
        self.data = data
        self.status = status


class ExportHandler:
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.wfile = io.BytesIO()
        self.sent = []

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass


print("=== A. 路由接线 ===")
check("A1 新 POST 端点齐备",
      all(k in routes.POST_ROUTES for k in ("/setting-fix/message", "/setting-fix/start",
                                            "/setting-fix/apply", "/setting-fix/dismiss",
                                            "/setting-fix/rollback", "/setting-fix/reset")))
check("A2 新 GET 端点存在", "/setting-fix/status" in routes.GET_ROUTES)
check("A3 旧反馈/harness 路由全部移除",
      all(k not in routes.POST_ROUTES for k in ("/feedback", "/chat/reroll",
                                                "/prompt-apply", "/prompt-dismiss",
                                                "/prompt-rollback"))
      and "/prompt-candidates" not in routes.GET_ROUTES)

print("=== B. status 与 message ===")
h = JsonHandler("/setting-fix/status?mode=story")
routes.setting_fix_status(h)
check("B1 status 返回 ok", h.data and h.data.get("ok") and h.data.get("stage") == "idle")

_store_client = cfg.get_client
_orig_align = sf.run_alignment
try:
    cfg.get_client = lambda: "fake-client"
    sf.run_alignment = lambda client, mode, conv, text, model, effort: {
        "stage": "ready", "text": "明白了", "options": []}
    h = JsonHandler("/setting-fix/message", json.dumps({"mode": "story", "text": "她说自己还在医疗舱"}))
    routes.setting_fix_message(h)
    check("B2 message 成功", h.data and h.data.get("ok") and h.data.get("stage") == "ready")
    check("B3 对话落盘", len(store.load_conversation("story")) == 2)

    h = JsonHandler("/setting-fix/message", json.dumps({"mode": "story", "text": "   "}))
    routes.setting_fix_message(h)
    check("B4 空描述拒绝", h.data and not h.data.get("ok"))
finally:
    cfg.get_client = _store_client
    sf.run_alignment = _orig_align

print("=== C. start / apply / rollback 链路 ===")
old = "不是因为萤火虫短暂，是因为萤火虫很美。"
_orig_proposal = sf.run_proposal
try:
    cfg.get_client = lambda: "fake-client"
    sf.run_proposal = lambda client, mode, conv, model, effort: {
        "ok": True, "kind": "proposal", "diagnosis": "d",
        "changes": [{"file": "core.md", "op": "replace", "old": old,
                     "new": "不是因为萤火虫短暂，是因为萤火虫很美，也为了纪念那个夜晚。", "reason": "r"}]}
    h = JsonHandler("/setting-fix/start", json.dumps({"mode": "story"}))
    routes.setting_fix_start(h)
    check("C1 start 生成 pending", h.data and h.data.get("ok") and h.data.get("stage") == "proposal")

    h = JsonHandler("/setting-fix/apply", json.dumps({"mode": "story", "session_id": "s1"}))
    routes.setting_fix_apply(h)
    check("C2 apply 成功", h.data and h.data.get("ok") and h.data.get("version") == 1)

    h = JsonHandler("/setting-fix/rollback", json.dumps({"mode": "story"}))
    routes.setting_fix_rollback(h)
    check("C3 rollback 成功", h.data and h.data.get("ok") and h.data.get("version") == 0)
finally:
    cfg.get_client = _store_client
    sf.run_proposal = _orig_proposal

print("=== D. 数据导出包含 .setting_fix（不影响数据同步） ===")
fix_dir = cfg.mode_root("story") / ".setting_fix"
fix_dir.mkdir(parents=True, exist_ok=True)
(fix_dir / "conversation.jsonl").write_text("x", encoding="utf-8")
h = ExportHandler("/export-data?mode=story")
routes.export_data(h)
buf = h.wfile
buf.seek(0)
with zipfile.ZipFile(buf) as zf:
    names = zf.namelist()
check("D1 导出 zip 含 .setting_fix/conversation.jsonl",
      ".setting_fix/conversation.jsonl" in names)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
