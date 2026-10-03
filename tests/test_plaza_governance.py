# -*- coding: utf-8 -*-
"""广场举报与治理回归 —— 共创平台 M3（对应未完成事项 P4-3 的后端部分）

钉死的事：
- 举报**同一人只算一次**；不同举报人达阈值 → 自动转待复核（从公开列表消失，**不是删除**）；
- 举报理由截断、审计留痕；
- **公开投影永不暴露举报信息**；管理视图才带举报数与审核结论；
- 管理端点只认 admin 角色，其它账号 403；
- 本地模式：举报/管理端点走代理（改的是广场数据），不落本地实现。

沙箱：FIREFLY_PLAZA_DIR / USER_DIR 指向临时目录。
"""
import json
import os
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_plaza_gov_"))
os.environ["FIREFLY_PLAZA_DIR"] = str(_tmp / "plaza")

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = _tmp / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

import routes                      # noqa: E402
from plaza import api as pa        # noqa: E402
from plaza import store as st      # noqa: E402

pa._is_server = lambda: True
PASS = FAIL = 0
ADMIN = {"id": 1, "email": "owner@qq.com", "role": "admin"}
USER = {"id": 7, "email": "u@qq.com", "role": "user"}


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


def seed(cid, name):
    return st.publish_card(
        {"id": cid, "name": name, "char_name": name, "user_name": "你",
         "presentation": "sticker", "desc": "d", "category": "陪伴"},
        {"preset.json": json.dumps({"id": cid, "name": name, "char_name": name,
                                    "user_name": "你", "presentation": "sticker",
                                    "desc": "d", "schema": 1}, ensure_ascii=False).encode(),
         "core.md": b"# core"}, uid_hash="a" * 32, display="作者")


class FakeH:
    def __init__(self, path="/plaza/api/report", payload=None, user=None):
        self.path = path
        self.payload = payload or {}
        self.user = user
        self.status = None
        self.body = b""

    def _bearer_user(self):
        return self.user

    def _json(self, data, status=200):
        self.status = status
        self.body = json.dumps(data, ensure_ascii=False).encode()

    def data(self):
        return json.loads(self.body.decode())


seed("gov_card", "待举报卡")
seed("gov_card2", "另一张卡")

print("== 1. 举报记账与幂等 ==")
st.add_report("gov_card", "内容涉嫌违规", "reporter1")
meta = st._read_meta("gov_card")
check("举报已记录", len(meta["reports"]) == 1)
check("举报仍在公开列表（未达阈值）", any(v["id"] == "gov_card" for v in st.list_cards()["items"]))
st.add_report("gov_card", "重复举报", "reporter1")
check("同一举报人重复举报不重复计数", len(st._read_meta("gov_card")["reports"]) == 1)
check("非法 id 返回 None", st.add_report("../etc", "x", "r") is None)
check("不存在的卡返回 None", st.add_report("nope", "x", "r") is None)

print("== 2. 达阈值自动转待复核（不是删除）==")
st.add_report("gov_card", "理由二", "reporter2")
check("两人举报仍公开", any(v["id"] == "gov_card" for v in st.list_cards()["items"]))
st.add_report("gov_card", "理由三", "reporter3")
m2 = st._read_meta("gov_card")
check(f"三人举报 → 状态 {m2['status']}（应为 pending）", m2["status"] == "pending")
check("从公开列表消失", all(v["id"] != "gov_card" for v in st.list_cards()["items"]))
check("管理视角仍在（内容没删）", st.get_card("gov_card", statuses=None) is not None)
check("卡体文件仍在", st.card_zip_path("gov_card").is_file())

print("== 3. 理由截断与审计 ==")
st.add_report("gov_card2", "x" * 500, "reporter9")
check("理由截断到 120 字", len(st._read_meta("gov_card2")["reports"][0]["reason"]) == 120)
audit = st.audit_path("gov_card").read_text(encoding="utf-8")
check("审计记录了 report 动作", '"action": "report"' in audit or '"action":"report"' in audit)

print("== 4. 公开投影不暴露举报信息 ==")
pub = st.get_card("gov_card2")
check("公开视图无 reports 字段", "reports" not in pub)
check("公开视图无 report_count 字段", "report_count" not in pub)

print("== 5. 管理视图 ==")
al = st.admin_list(statuses=None)
ids = [v["id"] for v in al["items"]]
check("管理列表含未公开的卡", "gov_card" in ids)
row = next(v for v in al["items"] if v["id"] == "gov_card")
check("管理视图带举报数", row["report_count"] == 3)
check("管理视图带最近举报理由", row["reports"][0]["reason"] == "内容涉嫌违规")
check("管理视图带审核摘要字段", "review" in row)

print("== 6. 端点：举报 ==")
routes._read_json = lambda h: h.payload
h = FakeH(payload={"id": "nope", "reason": "x"}, user=USER)
pa.plaza_report(h)
check("举报不存在的卡 → 404", h.status == 404)
h = FakeH(payload={"id": "../x", "reason": "x"}, user=USER)
pa.plaza_report(h)
check("举报非法 id → 400", h.status == 400)
h = FakeH(payload={"id": "gov_card2", "reason": "触发阈值"}, user=USER)
pa.plaza_report(h)
check("正常举报 → 200", h.status == 200 and h.data()["ok"] is True)

print("== 7. 端点：管理权限边界 ==")
h = FakeH(path="/plaza/api/admin/list", user=USER)
pa.plaza_admin_list(h)
check("普通用户查管理列表 → 403", h.status == 403)
h = FakeH(path="/plaza/api/admin/list", user=None)
pa.plaza_admin_list(h)
check("未登录查管理列表 → 403", h.status == 403)
h = FakeH(path="/plaza/api/admin/list?status=all", user=ADMIN)
pa.plaza_admin_list(h)
check("管理员查列表 → 200 且含未公开卡",
      h.status == 200 and any(v["id"] == "gov_card" for v in h.data()["items"]))
h = FakeH(payload={"id": "gov_card2", "status": "不存在的状态"}, user=ADMIN)
pa.plaza_admin_status(h)
check("非法状态 → 400", h.status == 400)
h = FakeH(payload={"id": "gov_card2", "status": "archived"}, user=ADMIN)
pa.plaza_admin_status(h)
check("管理员下架 → 200", h.status == 200 and h.data()["status"] == "archived")
check("下架后公开列表消失", all(v["id"] != "gov_card2" for v in st.list_cards()["items"]))
check("下架写入审计", "admin-status" in st.audit_path("gov_card2").read_text(encoding="utf-8"))
h = FakeH(payload={"id": "gov_card2", "status": "published"}, user=USER)
pa.plaza_admin_status(h)
check("普通用户改状态 → 403", h.status == 403)

print("== 8. 本地模式走代理 ==")
pa._is_server = lambda: False
seen = []
pa._proxy = lambda h, sub, timeout=30: seen.append(("GET", sub))
pa._proxy_post = lambda h, sub, timeout=30: seen.append(("POST", sub))
pa.plaza_report(FakeH(payload={"id": "gov_card2"}))
pa.plaza_admin_list(FakeH(path="/plaza/api/admin/list"))
pa.plaza_admin_status(FakeH(payload={"id": "gov_card2", "status": "published"}))
check("三个治理端点本地模式都走代理",
      seen == [("POST", "report"), ("GET", "admin/list"), ("POST", "admin/status")])

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
