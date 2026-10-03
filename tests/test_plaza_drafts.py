# -*- coding: utf-8 -*-
"""制卡 payload → 草稿 → 回读 的往返回归 —— 共创平台 V1（docs/设计/角色卡共创平台/06）

钉死的事：
- `_card_zip_from_payload()` 收 V1 `files:[{path,text}]` + `images:{avatar,thumb,display,stickers:[{data,label}]}`；
- **老字段等价映射**：`core`/`identity`/`sms_samples`/平铺 `cover`/`avatar`/字符串 `opening`
  → 与 V1 新写法产出**同一份卡内容**（老客户端与老草稿不能坏）；
- `label` 落 `character/stickers.json`；缺 label 合法；第 9 张表情包与超 12 字标签被拒；
- `GET /plaza/api/draft` 回的新形状 `files[]`/`images{}` 能**原样再提交**（往返 digest 不变）；
- 老草稿（只有 cover、没有 thumb）仍能读出来继续编辑（thumb 必填只掐新格式提交）。

沙箱：临时数据根 + 临时广场目录，**不碰真实 user_data**、不连网。
"""
import base64
import io
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_plaza_drafts_"))
os.environ["FIREFLY_PLAZA_DIR"] = str(_tmp / "plaza")

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = _tmp / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from plaza import api as pa        # noqa: E402
from plaza import card_format as cf  # noqa: E402
from plaza import drafts as dr     # noqa: E402
from plaza import store as st      # noqa: E402

pa._is_server = lambda: True        # 服务器模式：端点走本端实现（本测试不连网）
USER = {"id": 7, "email": "tester@example.com"}

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


def expect_code(desc, code, fn):
    global PASS, FAIL
    try:
        fn()
    except cf.CardError as e:
        if e.code == code:
            PASS += 1
            print(f"  ok   {desc} [{code}]")
        else:
            FAIL += 1
            print(f"  FAIL {desc} —— 期望 {code}，实际 {e.code}（{e.message}）")
    except Exception as e:
        FAIL += 1
        print(f"  FAIL {desc} —— 期望 CardError({code})，实际 {type(e).__name__}: {e}")
    else:
        FAIL += 1
        print(f"  FAIL {desc} —— 期望 {code}，但没抛异常")


class FakeH:
    """最小 handler 桩：路由层只用到这几个成员（照 test_plaza_api.py 的先例）。

    `body`（可选）= JSON 请求体 ⇒ 同时补出 `Content-Length` 与 `rfile`，
    这样 `routes._read_json(h)` 能真读到（用来端到端驱动 POST 端点）。
    """

    def __init__(self, path, body=None, role=None):
        self.path = path
        self.headers = {}
        self.status = None
        self.body = b""
        self.role = role
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.headers["Content-Length"] = str(len(raw))
            self.rfile = io.BytesIO(raw)

    def _bearer_user(self):
        if self.role:
            return {"id": 8 if self.role == "admin" else 7, "email": f"{self.role}@example.com",
                    "role": self.role}
        return USER

    def _json(self, data, status=200):
        self.status = status
        self.body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def data(self):
        return json.loads(self.body.decode("utf-8"))


# ── 图片样本（真头部、无 Pillow）──
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def data_url(blob, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


THUMB, AVATAR, DISPLAY, STICK = png(320, 200), png(256, 256), png(1024, 768), png(160, 160)


def base_card(**over):
    c = {
        "id": "draft_card", "name": "草稿卡", "char_name": "小萤", "user_name": "你",
        "presentation": "sticker", "desc": "一张用于草稿往返的卡", "tagline": "副标",
        "category": "陪伴", "tags": ["治愈", "日常"],
        "opening": {"narrations": ["夜色落下来"], "first_messages": ["你回来啦"]},
    }
    c.update(over)
    return c


def v1_payload():
    return base_card(files=[
        {"path": "core.md", "text": "# 核心\n她是流萤"},
        {"path": "knowledge/世界观.md", "text": "扁平知识库"},
        {"path": "knowledge/world/地理.md", "text": "分组知识库"},
    ], images={
        "thumb": data_url(THUMB),
        "avatar": data_url(AVATAR),
        "display": data_url(DISPLAY),
        "stickers": [{"data": data_url(STICK), "label": "开心"},
                     {"data": data_url(STICK), "label": "疑惑"}],
    })


print("== 1. V1 payload：files[]/images{} 全量组装 ==")
zip1 = pa._card_zip_from_payload(v1_payload())
p1 = cf.parse_card_zip(zip1, require_thumb=True)
check("卡 id 与必填字段带上", p1["manifest"]["id"] == "draft_card"
      and p1["manifest"]["char_name"] == "小萤")
check("扁平知识库进卡", p1["files"]["knowledge/世界观.md"] == "扁平知识库".encode())
check("一层分组知识库进卡", p1["files"]["knowledge/world/地理.md"] == "分组知识库".encode())
check("thumb/avatar/display 三个槽位都在",
      all(f"assets/{s}.png" in p1["files"] for s in ("thumb", "avatar", "display")))
check("两张表情包都在", "assets/sticker-1.png" in p1["files"] and "assets/sticker-2.png" in p1["files"])
check("label 落 stickers.json", json.loads(p1["files"]["stickers.json"].decode()) ==
      {"sticker-1.png": "开心", "sticker-2.png": "疑惑"})
check("opening 结构化进卡", json.loads(p1["files"]["opening.json"].decode())["first_messages"] == ["你回来啦"])
check("preset.json 由后端生成（不是用户提交的）",
      json.loads(p1["files"]["preset.json"].decode())["id"] == "draft_card")

print("== 2. 老字段等价映射（老客户端/老草稿不能坏）==")
legacy = pa._card_zip_from_payload(base_card(
    core="# 核心\n她是流萤", identity="身份", sms_samples="样本",
    cover=data_url(AVATAR)))
pl = cf.parse_card_zip(legacy)
check("core/identity/sms_samples → 同名文件",
      pl["files"]["core.md"] == "# 核心\n她是流萤".encode()
      and pl["files"]["identity.md"] == "身份".encode()
      and pl["files"]["sms_samples.md"] == "样本".encode())
check("平铺 cover → assets/cover.png（老槽位）", "assets/cover.png" in pl["files"])
check("老格式没有 thumb 也放行（兼容通道，不报 CARD_MISSING_THUMB）",
      cf.thumb_name(pl["files"]) is None)
legacy_open = cf.parse_card_zip(pa._card_zip_from_payload(base_card(opening="你回来啦")))
check("字符串 opening → first_messages",
      json.loads(legacy_open["files"]["opening.json"].decode())["first_messages"] == ["你回来啦"])
new_same = cf.parse_card_zip(pa._card_zip_from_payload(base_card(files=[
    {"path": "core.md", "text": "# 核心\n她是流萤"},
    {"path": "identity.md", "text": "身份"},
    {"path": "sms_samples.md", "text": "样本"},
], images={"cover": data_url(AVATAR), "thumb": data_url(THUMB)})))
_a = {k: v for k, v in pl["files"].items() if k != "preset.json"}
_b = {k: v for k, v in new_same["files"].items() if k != "preset.json"}
check("老写法与新 files[]/images{} 写法**内容等价**（新写法只多一个必填 thumb）",
      {k: v for k, v in _b.items() if k != "assets/thumb.png"} == _a
      and set(_b) - set(_a) == {"assets/thumb.png"})
both = pa._card_zip_from_payload(base_card(
    core="老文本", files=[{"path": "core.md", "text": "新文本"}],
    images={"thumb": data_url(THUMB)}))
check("同路径以 files[] 为准", cf.parse_card_zip(both)["files"]["core.md"] == "新文本".encode())

print("== 3. 新格式必须带 thumb（老格式豁免）==")
try:
    pa._card_zip_from_payload(base_card(
        files=[{"path": "core.md", "text": "x"}], images={"avatar": data_url(AVATAR)}))
    check("新格式缺 thumb 被拒", False)
except cf.CardError as e:
    check(f"新格式缺 thumb 被拒 [{e.code}]", e.code == cf.E_MISSING_THUMB and "thumb" in e.message)
expect_code("显式 require_thumb=True 也拦得住", cf.E_MISSING_THUMB,
            lambda: pa._card_zip_from_payload(base_card(core="x", cover=data_url(AVATAR)),
                                              require_thumb=True))

print("== 4. payload 的安全边界与超限 ==")
expect_code("用户提交 preset.json 被拒", cf.E_UNKNOWN_FILE,
            lambda: pa._card_zip_from_payload(base_card(files=[{"path": "preset.json", "text": "{}"}])))
expect_code("用户提交 opening.json 被拒（走 opening 字段）", cf.E_UNKNOWN_FILE,
            lambda: pa._card_zip_from_payload(base_card(files=[{"path": "opening.json", "text": "{}"}])))
expect_code("用户提交 stickers.json 被拒（走 label）", cf.E_UNKNOWN_FILE,
            lambda: pa._card_zip_from_payload(base_card(files=[{"path": "stickers.json", "text": "{}"}])))
expect_code("越界目录 prompts/x.md 被拒", cf.E_UNKNOWN_FILE,
            lambda: pa._card_zip_from_payload(base_card(files=[{"path": "prompts/x.md", "text": "x"}])))
expect_code("files[] 条目缺 text 被拒", cf.E_BAD_MANIFEST,
            lambda: pa._card_zip_from_payload(base_card(files=[{"path": "core.md"}])))
expect_code("images 未知槽位被拒（不静默丢弃）", cf.E_UNKNOWN_FILE,
            lambda: pa._card_zip_from_payload(base_card(images={"banner": data_url(AVATAR),
                                                                "thumb": data_url(THUMB)})))
expect_code("第 9 张表情包被拒", cf.E_TOO_BIG,
            lambda: pa._card_zip_from_payload(base_card(images={
                "thumb": data_url(THUMB),
                "stickers": [{"data": data_url(STICK)} for _ in range(9)]})))
expect_code("表情包标签超 12 字被拒", cf.E_BAD_MANIFEST,
            lambda: pa._card_zip_from_payload(base_card(images={
                "thumb": data_url(THUMB),
                "stickers": [{"data": data_url(STICK), "label": "这个标签明显超过了十二个字"}]})))
p_nolabel = cf.parse_card_zip(pa._card_zip_from_payload(base_card(images={
    "thumb": data_url(THUMB), "stickers": [data_url(STICK)]})))
check("裸 data URL 表情包（无 label）合法且不生成 stickers.json",
      "assets/sticker-1.png" in p_nolabel["files"] and "stickers.json" not in p_nolabel["files"])

print("== 5. 草稿保存 → 新形状回读 → 原样再提交（往返 digest 不变）==")
ohash = dr.owner_hash(USER)
meta = dr.save_draft(ohash, zip1, display="小萤", status="draft")
check("草稿已保存", meta["id"] == "draft_card" and meta["size_bytes"] > 0)
m2, blob2 = dr.load_draft(ohash, "draft_card")
check("草稿能读回且字节一致", blob2 == zip1 and m2["status"] == "draft")

h = FakeH("/plaza/api/draft?id=draft_card")
pa.plaza_draft_get(h)
check("draft_get 200", h.status == 200)
card = h.data()["draft"]["card"]
paths = {f["path"] for f in card["files"]}
check("回读 files[] 含扁平与分组知识库",
      {"core.md", "knowledge/世界观.md", "knowledge/world/地理.md"} <= paths)
check("回读 files[] 不含 preset/opening/stickers.json",
      not ({"preset.json", "opening.json", "stickers.json"} & paths))
check("回读 images 有 thumb/avatar/display", all(card["images"].get(s) for s in ("thumb", "avatar", "display")))
check("回读 stickers 是 [{data,label}] 且标签保住",
      [s["label"] for s in card["images"]["stickers"]] == ["开心", "疑惑"]
      and all(s["data"].startswith("data:image/") for s in card["images"]["stickers"]))
check("回读 opening 结构化", card["opening"]["first_messages"] == ["你回来啦"])
check("回读仍带老形状字段（core/cover 兜底）", card.get("core", "").startswith("# 核心"))

zip_round = pa._card_zip_from_payload(card)      # ★ 把回读结果原样再提交
p_round = cf.parse_card_zip(zip_round, require_thumb=True)
check("往返后文件集合一致", set(p_round["files"]) == set(p1["files"]))
check("往返后整卡 digest 一致（内容零漂移）", p_round["digest"] == p1["digest"])

print("== 6. 老草稿（无 thumb）能读出来，且老形状提交不被掐死 ==")
old_zip = cf.build_card_zip(
    {"id": "old_card", "name": "老草稿", "char_name": "小萤", "user_name": "你",
     "presentation": "sticker", "desc": "V1 之前存的草稿", "category": "陪伴"},
    {"preset.json": json.dumps({"id": "old_card", "name": "老草稿", "char_name": "小萤",
                                "user_name": "你", "presentation": "sticker",
                                "desc": "V1 之前存的草稿", "schema": 1}, ensure_ascii=False).encode(),
     "core.md": "# 老".encode(), "assets/cover.png": PNG})
dr.save_draft(ohash, old_zip, display="小萤")
h2 = FakeH("/plaza/api/draft?id=old_card")
pa.plaza_draft_get(h2)
check("老草稿能读（不因缺 thumb 报错）", h2.status == 200)
card2 = h2.data()["draft"]["card"]
check("老草稿的 cover 出现在 images.cover（老形状兜底）",
      card2["images"].get("cover", "").startswith("data:image/"))
check("老草稿 files[] 里有 core.md", {f["path"] for f in card2["files"]} == {"core.md"})
# 老**客户端**发的老形状（平铺字段、没有 files/images）→ 兼容通道，照旧能存
old_again = pa._card_zip_from_payload({"id": "old_card", "name": "老草稿", "char_name": "小萤",
                                       "user_name": "你", "desc": "V1 之前存的草稿",
                                       "category": "陪伴",
                                       "core": "# 老", "cover": card2["images"]["cover"]})
check("老客户端（平铺字段）再存不被 thumb 必填掐死", cf.thumb_name(cf.parse_card_zip(old_again)["files"]) is None)
# 新 UI 打开老草稿再存：**必须先补 thumb**（契约 §3.3）——这是有意为之的迁移闸门
expect_code("新 UI 原样回存老草稿 → 明确要求补 thumb", cf.E_MISSING_THUMB,
            lambda: pa._card_zip_from_payload(card2))
ok_card = dict(card2)
ok_card["images"] = dict(card2["images"], thumb=data_url(THUMB))
check("补上 thumb 后老草稿可保存（内容不丢）",
      "assets/cover.png" in cf.parse_card_zip(pa._card_zip_from_payload(ok_card))["files"])

print("== 7. draft_get 的 404 ==")
h3 = FakeH("/plaza/api/draft")
pa.plaza_draft_get(h3)
check("缺 id → 404（不是 500）", h3.status == 404)
h4 = FakeH("/plaza/api/draft?id=../../etc")
pa.plaza_draft_get(h4)
check("非法 id → 404", h4.status == 404)

print("== 8. 官方标记 official：只有管理员能传；管理员发布的卡默认官方（2026-10-01）==")
_h_admin = FakeH("/plaza/api/publish", body={"id": "draft_card", "official": True}, role="admin")
_h_user = FakeH("/plaza/api/publish", body={"id": "draft_card", "official": True}, role="user")
_h_user_plain = FakeH("/plaza/api/publish", body={"id": "draft_card"}, role="user")
_ok_a, _off_a = pa._check_official(_h_admin, {"official": True})
check(f"管理员显式 official:true ⇒ 放行且 official=True（{_off_a}）", _ok_a and _off_a is True)
_ok_u, _off_u = pa._check_official(_h_user, {"official": True})
check(f"普通用户显式 official:true ⇒ **被拒 403**（status={_h_user.status}，{_h_user.data().get('error')}）",
      (not _ok_u) and _h_user.status == 403 and "管理员" in _h_user.data().get("error", ""))
_ok_p, _off_p = pa._check_official(_h_user_plain, {})
check(f"普通用户不传 official ⇒ 放行且 official=False（{_off_p}）", _ok_p and _off_p is False)
_ok_d, _off_d = pa._check_official(FakeH("/x", role="admin"), {})
check(f"管理员不传 official ⇒ 默认官方（{_off_d}）", _ok_d and _off_d is True)
# 端到端：普通用户带 official 打 /plaza/api/publish ⇒ 403（**先**于审核闸门，与草稿状态无关）
pa.plaza_publish(_h_user)
check(f"端点级：普通用户 + official ⇒ 403（status={_h_user.status}）",
      _h_user.status == 403 and "管理员" in _h_user.data().get("error", ""))
# 端到端：普通用户不带 official ⇒ 过官方闸门，但被**审核**闸门挡（400「没有通过审核」）——证明没被一刀切
pa.plaza_publish(_h_user_plain)
check(f"端点级：普通用户不带 official ⇒ 不是 403（status={_h_user_plain.status}，"
      f"{_h_user_plain.data().get('error')}）", _h_user_plain.status == 400)
# 端到端：管理员发布 ⇒ 成功且卡片带官方标记（admin 免审，与既有行为一致）
_admin_user = {"id": 8, "email": "admin@example.com", "role": "admin"}
_admin_ohash = dr.owner_hash(_admin_user)
dr.save_draft(_admin_ohash, zip1, display="小萤", is_admin=True)
_h_admin2 = FakeH("/plaza/api/publish", body={"id": "draft_card", "official": True}, role="admin")
_publish_calls = []
_orig_publish_draft = dr.publish_draft          # api.py 里是函数内 `from plaza import drafts as dr` ⇒ 同一模块对象


def _spy_publish_draft(ohash, did, **kw):
    _publish_calls.append(kw)
    return _orig_publish_draft(ohash, did, **kw)


dr.publish_draft = _spy_publish_draft
try:
    pa.plaza_publish(_h_admin2)
finally:
    dr.publish_draft = _orig_publish_draft
check(f"端点级：管理员 + official:true ⇒ 200/ok（official 透传={_publish_calls and _publish_calls[0].get('official')}）",
      _h_admin2.status == 200 and _h_admin2.data().get("ok") is True
      and bool(_publish_calls) and _publish_calls[0].get("official") is True)


# ① 三态口径（2026-10-01 收口）：管理员**显式 false** ⇒ 真的发非官方卡（勾选框不再是装饰）；不传 ⇒ 默认官方
def _admin_publish(cid, body):
    _z = pa._card_zip_from_payload(base_card(id=cid, images={"thumb": data_url(THUMB)}), is_admin=True)
    dr.save_draft(_admin_ohash, _z, display="小萤", is_admin=True)
    _h = FakeH("/plaza/api/publish", body={"id": cid, **body}, role="admin")
    pa.plaza_publish(_h)
    _mp = st.meta_path(cid)
    _m = json.loads(_mp.read_text(encoding="utf-8")) if _mp.is_file() else {}
    return _h, (_m.get("author") or {})


_h_false, _au_false = _admin_publish("off_false_card", {"official": False})
check(f"①a 管理员显式 official:false ⇒ 落盘 author.official={_au_false.get('official')!r}"
      f"（status={_h_false.status}）",
      _h_false.status == 200 and _au_false.get("official") is False)
_h_none, _au_none = _admin_publish("off_default_card", {})
check(f"①b 管理员不传 official ⇒ 默认官方（author.official={_au_none.get('official')!r}）",
      _h_none.status == 200 and _au_none.get("official") is True)
_h_true, _au_true = _admin_publish("off_true_card", {"official": True})
check(f"①c 管理员显式 official:true ⇒ 官方（author.official={_au_true.get('official')!r}）",
      _h_true.status == 200 and _au_true.get("official") is True)

# 管理员走**草稿→发布**流程也拿得到"文字总量不限"（平台的真实流程就是这条，不是直连 build）
_big = base_card(id="big_card", files=[{"path": "knowledge/huge.md", "text": "知识" * 1500000}],
                 images={"thumb": data_url(THUMB)})
try:
    _bz = pa._card_zip_from_payload(_big, is_admin=True)
    dr.save_draft(_admin_ohash, _bz, display="小萤", is_admin=True)
    check("管理员：≈9MB 文字卡经**草稿流程**保存成功（文字总量不限）", len(_bz) > 0)
except (cf.CardError, dr.DraftError) as e:
    check(f"管理员草稿流程保存超量卡（实际被拒：{getattr(e, 'code', '?')} {e}）", False)
try:
    pa._card_zip_from_payload(_big)          # 普通账号：同一张卡必须被拒
    check("普通账号：同一张超量卡被拒（文字总量 8MB）", False)
except cf.CardError as e:
    check(f"普通账号：同一张超量卡被拒（{e.code}：{e.message[:42]}…）", e.code == cf.E_TOO_BIG)

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
