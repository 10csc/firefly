# -*- coding: utf-8 -*-
"""制卡草稿 → 审核 → 发布 回归 —— 共创平台 M2

钉死的事：
- 没通过审核**不能发布**；管理员免审但**必须留痕**；
- **"审 A 发 B"必须被拦**（审核结论绑定卡体 digest）；
- 草稿按作者隔离；草稿数上限；
- 作者展示名默认打码，**邮箱原文绝不进广场元数据**；
- 审核结论落草稿时**不含任何凭据**；
- 上架/下架的权限边界。

沙箱：FIREFLY_PLAZA_DIR / USER_DIR 均指向临时目录；不联网、不产生费用。
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_plaza_publish_"))
os.environ["FIREFLY_PLAZA_DIR"] = str(_tmp / "plaza")

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = _tmp / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

import routes                      # noqa: E402
from plaza import api as pa        # noqa: E402
from plaza import card_format as cf  # noqa: E402
from plaza import drafts as dr     # noqa: E402
from plaza import review as rv     # noqa: E402
from plaza import store as st      # noqa: E402

pa._is_server = lambda: True
PASS = FAIL = 0
KEY = "sk-" + "a1b2c3d4" * 4

USER_A = {"id": 11, "email": "alice@qq.com", "role": "user"}
USER_B = {"id": 22, "email": "bob@qq.com", "role": "user"}
ADMIN = {"id": 1, "email": "owner@qq.com", "role": "admin"}


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


PNG = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def card_payload(cid="craftcard", name="手工卡", core="核心设定"):
    return {"id": cid, "name": name, "char_name": "小萤", "user_name": "你",
            "desc": "简介", "category": "陪伴", "tags": ["治愈"],
            "core": core, "identity": "身份", "sms_samples": "短信", "opening": "你好",
            "cover": "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()}


def zip_of(payload):
    return pa._card_zip_from_payload(payload)


def review_pass(sk, manifest, files, **kw):
    return {"verdict": "pass", "risk": "low", "reasons": [], "model": "deepseek-flash",
            "usage": {"total_tokens": 10}, "signals": [], "reviewed_at": 0}


print("== 1. 存草稿 / 读草稿 ==")
oh = dr.owner_hash(USER_A)
meta = dr.save_draft(oh, zip_of(card_payload()), display=dr.clean_display("", USER_A))
check("草稿已保存", meta["id"] == "craftcard" and meta["status"] == "draft")
lst = dr.list_drafts(oh)
check("草稿列表含它", len(lst) == 1 and lst[0]["id"] == "craftcard")
m2, blob = dr.load_draft(oh, "craftcard")
check("草稿卡体可读回", len(blob) > 100 and m2["id"] == "craftcard")
check("默认展示名打码、不含完整邮箱", meta["display"] == "al***@qq.com")

print("== 2. 没审核不能发布 ==")
try:
    dr.publish_draft(oh, "craftcard", user=USER_A)
    check("未审核发布被拒", False)
except dr.DraftError as e:
    check(f"未审核发布被拒（{e.code}）", e.code == dr.DRAFT_NOT_REVIEWED)
check("广场里没有这张卡", st.get_card("craftcard", statuses=None) is None)

print("== 3. 审核通过 → 发布 ==")
data = zip_of(card_payload())
parsed = cf.parse_card_zip(data)
result = review_pass(KEY, parsed["manifest"], parsed["files"])
result["digest"] = parsed["digest"]
dr.save_draft(oh, data, display=dr.clean_display("我的笔名", USER_A),
              status="reviewed", review=result)
saved = dr._read_meta(oh, "craftcard")
check("审核结论已落草稿", (saved.get("review") or {}).get("verdict") == "pass")
check("草稿元数据不含任何 Key", "sk-" not in json.dumps(saved, ensure_ascii=False))
card = dr.publish_draft(oh, "craftcard", user=USER_A)
check("发布成功", card["id"] == "craftcard" and card["status"] == "published")
check("作者是匿名标识（非邮箱）", card["author"]["uid_hash"] == oh)
check("广场元数据不含邮箱", "alice@qq.com" not in json.dumps(card, ensure_ascii=False))
check("自填展示名生效", card["author"]["display"] == "我的笔名")
check("公开列表能看到", any(v["id"] == "craftcard" for v in st.list_cards()["items"]))
check("审计流水已写", st.audit_path("craftcard").is_file())
check("审计流水不含 Key", "sk-" not in st.audit_path("craftcard").read_text(encoding="utf-8"))

print("== 4. 「审 A 发 B」必须被拦 ==")
data2 = zip_of(card_payload(core="核心设定（审核后又改了）"))
dr.save_draft(oh, data2, display="我的笔名")          # 覆盖卡体但没有新审核
try:
    dr.publish_draft(oh, "craftcard", user=USER_A, replace=True)
    check("审核后改动仍可发布（错误）", False)
except dr.DraftError as e:
    check(f"审核后改动被拦（{e.code}）", e.code == dr.DRAFT_STALE_REVIEW)

print("== 5. 管理员免审但留痕 ==")
dr.save_draft(oh, zip_of(card_payload("admincard", "官方卡")), display="官方")
adcard = dr.publish_draft(oh, "admincard", user=ADMIN)
check("管理员免审可发布", adcard["id"] == "admincard")
check("带官方标识", adcard["author"]["official"] is True)
audit = st.audit_path("admincard").read_text(encoding="utf-8")
check("审计记录 bypass_review", '"bypass_review": true' in audit or '"bypass_review":true' in audit)

print("== 6. 草稿按作者隔离 ==")
oh_b = dr.owner_hash(USER_B)
check("B 看不到 A 的草稿", dr.load_draft(oh_b, "craftcard")[1] == b"")
try:
    dr.publish_draft(oh_b, "craftcard", user=USER_B)
    check("B 不能发布 A 的草稿", False)
except dr.DraftError as e:
    check("B 不能发布 A 的草稿", e.code == dr.DRAFT_NOT_FOUND)

print(f"== 7. 草稿数量上限（★2026-10-02：10 → {dr.DRAFT_MAX}，管理员不限）==")
for i in range(dr.DRAFT_MAX):
    dr.save_draft(oh_b, zip_of(card_payload(f"bcard{i}", f"B卡{i}")))
try:
    dr.save_draft(oh_b, zip_of(card_payload("bcard_over", "超限卡")))
    check("超过草稿上限被拒", False)
except dr.DraftError as e:
    check(f"超过草稿上限被拒（{e.code}）", e.code == dr.DRAFT_LIMIT)
check("覆盖已有草稿不受上限影响",
      dr.save_draft(oh_b, zip_of(card_payload("bcard0", "B卡0改")))["id"] == "bcard0")
# 管理员豁免：同一个 owner 在**已满额**状态下，以 is_admin=True 再存 5 份也应放行
for i in range(5):
    dr.save_draft(oh_b, zip_of(card_payload(f"acard{i}", f"管理卡{i}")), is_admin=True)
check(f"管理员不受草稿上限约束（满额后又存了 5 份）",
      dr.load_draft(oh_b, "acard4")[1] is not None)
check(f"DRAFT_MAX 常量 = {dr.DRAFT_MAX}（钉住，别再降回 10）", dr.DRAFT_MAX == 50)

print("== 8. 下架权限 ==")
check("非作者不能下架", dr.unpublish(oh_b, "craftcard", user=USER_B) is False)
check("作者可下架", dr.unpublish(oh, "craftcard", user=USER_A) is True)
check("下架后公开列表消失", all(v["id"] != "craftcard" for v in st.list_cards()["items"]))
check("下架后管理视角仍在", st.get_card("craftcard", statuses=None) is not None)

print("== 9. 端点：本地模式全部走代理 ==")


class FakeH:
    def __init__(self, path="/plaza/api/draft", payload=None, user=None):
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


routes._read_json = lambda h: h.payload
pa._is_server = lambda: False
seen = []
pa._proxy = lambda h, sub, timeout=30: seen.append(("GET", sub))
pa._proxy_post = lambda h, sub, timeout=30: seen.append(("POST", sub))
pa.plaza_draft_list(FakeH(path="/plaza/api/drafts"))
pa.plaza_draft_get(FakeH(path="/plaza/api/draft?id=x"))
pa.plaza_draft_save(FakeH(payload={"card": card_payload("d1")}))
pa.plaza_draft_delete(FakeH(payload={"id": "d1"}))
pa.plaza_publish(FakeH(payload={"id": "d1"}))
pa.plaza_unpublish(FakeH(payload={"id": "d1"}))
pa.plaza_review(FakeH(payload={"sk": KEY, "card": card_payload("d2")}))
check("本地模式 7 个制卡端点全部代理",
      seen == [("GET", "drafts"), ("GET", "draft"), ("POST", "draft"), ("POST", "draft/delete"),
               ("POST", "publish"), ("POST", "unpublish"), ("POST", "review")])

print("== 10. 端点：服务器模式自实现（含用户身份）==")
pa._is_server = lambda: True
pa._proxy = lambda *a, **k: (_ for _ in ()).throw(AssertionError("服务器模式不该代理"))
h = FakeH(path="/plaza/api/drafts", user=USER_A)
pa.plaza_draft_list(h)
check("服务器模式草稿列表自实现", h.status == 200 and h.data()["ok"] is True)
h = FakeH(payload={"card": card_payload("srvcard", "服务端卡")}, user=USER_A)
pa.plaza_draft_save(h)
check("服务器模式存草稿成功", h.status == 200 and h.data()["draft"]["id"] == "srvcard")

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
