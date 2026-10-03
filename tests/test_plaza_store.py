# -*- coding: utf-8 -*-
"""广场存储层回归 —— 共创平台 M1（docs/设计/角色卡共创平台/01 + 00）

钉死的事：
- 发布是**先校验后落盘**：坏卡不允许留下半个目录（孤儿目录是这个项目踩过的坑）；
- 同名卡默认拒绝、显式 replace 才覆盖，且**下载数不被覆盖清零**；
- 列表筛选（分类/标签/关键词/官方）、排序（new/hot/name）、分页夹取；
- 状态门：非 published 不进公开列表；
- **目录是唯一真相源**：meta 在而 card.zip 没了 → 该卡不可用（不许"列表有、下载 404"）；
- 一张坏 meta 不能让整个广场 500（跳过 + 告警）。

沙箱纪律：`FIREFLY_PLAZA_DIR` 指向临时目录，绝不触碰真实数据。
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_plaza_store_"))
os.environ["FIREFLY_PLAZA_DIR"] = str(_tmp / "plaza")

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = _tmp / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from plaza import card_format as cf   # noqa: E402
from plaza import store as st         # noqa: E402

PASS = FAIL = 0


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


def make(cid="card_a", name="卡片A", category="陪伴", tags=("治愈",), official=False,
         with_cover=True, desc="简介A", char_name="小萤"):
    files = {
        "preset.json": json.dumps({"id": cid, "name": name, "char_name": char_name,
                                   "user_name": "你", "presentation": "sticker",
                                   "desc": desc, "schema": 1}, ensure_ascii=False).encode(),
        "core.md": f"# {name}".encode(),
    }
    if with_cover:
        files["assets/cover.png"] = PNG
    manifest = {"id": cid, "name": name, "char_name": char_name, "user_name": "你",
                "presentation": "sticker", "desc": desc, "category": category,
                "tags": list(tags), "created_at": 1759290000}
    return manifest, files, {"uid_hash": "u" * 64, "display": "作者", "official": official}


print("== 1. 发布与读取 ==")
m, f, a = make()
view = st.publish_card(m, f, **a)
check("发布返回投影", view["id"] == "card_a" and view["name"] == "卡片A")
check("card.zip 落盘", st.card_zip_path("card_a").is_file())
check("meta.json 落盘", st.meta_path("card_a").is_file())
check("封面被抽出到 assets/", view["cover"] == "cover.png")
check("封面文件确实存在", (st.assets_dir("card_a") / "cover.png").is_file())
check("size_bytes 记录", view["size_bytes"] > 0)
check("get_card 取到", st.get_card("card_a")["id"] == "card_a")
check("get_card 不返回卡正文", "files" not in st.get_card("card_a"))
check("非法 id 取不到", st.get_card("../../etc") is None)
check("不存在的卡取不到", st.get_card("nope") is None)

print("== 2. 先校验后落盘（坏卡不留孤儿目录）==")
bad_files = {"core.md": b"x"}          # 缺 preset.json
try:
    st.publish_card({"id": "bad_card", "name": "坏", "char_name": "x", "user_name": "y",
                     "desc": "d", "category": "陪伴"}, bad_files)
    check("坏卡被拒", False)
except cf.CardError as e:
    check(f"坏卡被拒（{e.code}）", e.code in (cf.E_BAD_PRESET, cf.E_BAD_MANIFEST))
check("坏卡没有留下目录", not st.card_dir("bad_card").exists())

print("== 3. 同名卡：默认拒绝，replace 才覆盖且保留下载数 ==")
try:
    st.publish_card(m, f, **a)
    check("默认拒绝覆盖", False)
except st.StoreError as e:
    check(f"默认拒绝覆盖（{e.code}）", e.code == st.S_EXISTS)
st.bump_download("card_a")
st.bump_download("card_a")
check("下载数累加到 2", st.get_card("card_a")["downloads"] == 2)
m2, f2, a2 = make(name="卡片A·改")
v2 = st.publish_card(m2, f2, replace=True, **a2)
check("replace 覆盖成功", v2["name"] == "卡片A·改")
check("覆盖后下载数保留", v2["downloads"] == 2)

print("== 4. 列表：筛选 / 排序 / 分页 ==")
st.publish_card(*make(cid="card_b", name="卡片B", category="剧情", tags=("战斗", "热血"))[:2],
                **make(cid="card_b", name="卡片B", category="剧情", tags=("战斗", "热血"))[2])
st.publish_card(*make(cid="card_c", name="官方卡", category="剧情", tags=("治愈",),
                      official=True, desc="官方出品")[:2],
                **make(cid="card_c", name="官方卡", category="剧情", tags=("治愈",),
                      official=True, desc="官方出品")[2])
st.bump_download("card_b")
st.bump_download("card_b")
st.bump_download("card_b")

allv = st.list_cards()
check("总数 3", allv["total"] == 3)
check("返回分类枚举", allv["categories"] == list(cf.CATEGORIES))
check("默认按 new 排序（官方卡最新在前）", allv["items"][0]["id"] == "card_c")
check("按 hot 排序", st.list_cards(sort="hot")["items"][0]["id"] == "card_b")
check("按 name 排序（中文按码位：卡<官）", [v["id"] for v in st.list_cards(sort="name")["items"]] == ["card_a", "card_b", "card_c"])
check("分类筛选", [v["id"] for v in st.list_cards(category="剧情")["items"]] == ["card_c", "card_b"])
check("标签筛选", [v["id"] for v in st.list_cards(tag="战斗")["items"]] == ["card_b"])
check("官方筛选", [v["id"] for v in st.list_cards(official=True)["items"]] == ["card_c"])
check("关键词搜简介", [v["id"] for v in st.list_cards(q="官方出品")["items"]] == ["card_c"])
check("关键词搜角色名", st.list_cards(q="小萤")["total"] == 3)
check("分页 size=2 第 1 页", len(st.list_cards(size=2)["items"]) == 2)
check("分页第 2 页剩 1", len(st.list_cards(size=2, page=2)["items"]) == 1)
check("size 夹取上限 50", st.list_cards(size=9999)["size"] == st.MAX_PAGE_SIZE)
check("page 非法夹到 1", st.list_cards(page=0)["page"] == 1)
check("未知分类回退其他（不报错）", st.list_cards(category="外星")["total"] == 0)

print("== 5. 状态门与治理 ==")
st.publish_card(*make(cid="card_d", name="等待发布")[:2], **make(cid="card_d", name="等待发布")[2],
                status="pending")
check("pending 不进公开列表", all(v["id"] != "card_d" for v in st.list_cards()["items"]))
check("pending 不进公开详情", st.get_card("card_d") is None)
check("管理视角能看到 pending", st.get_card("card_d", statuses=None) is not None)
check("管理视角列表含 pending",
      any(v["id"] == "card_d" for v in st.list_cards(statuses=None)["items"]))
check("置为 published 后公开可见", st.set_status("card_d", "published") and st.get_card("card_d"))
check("置为 archived 后公开消失", st.set_status("card_d", "archived") and st.get_card("card_d") is None)
try:
    st.set_status("card_b", "不存在的状态")
    check("非法状态被拒", False)
except ValueError:
    check("非法状态被拒", True)

print("== 6. 唯一真相源与健壮性 ==")
st.card_zip_path("card_c").unlink()
check("meta 在而 zip 没了 → 详情不可用", st.get_card("card_c") is None)
check("meta 在而 zip 没了 → 列表也不出现", all(v["id"] != "card_c" for v in st.list_cards()["items"]))
st.publish_card(*make(cid="card_e", name="卡E")[:2], **make(cid="card_e", name="卡E")[2])
st.meta_path("card_e").write_text("{坏掉的 json", encoding="utf-8")
before = st.list_cards()["total"]
check("坏 meta 被跳过、不炸整个广场", isinstance(before, int))
check("坏 meta 的卡不出现", all(v["id"] != "card_e" for v in st.list_cards()["items"]))
st.meta_path("card_e").unlink()

print("== 7. 下载计数、统计、删除、审计 ==")
st.bump_download("card_a")
check("bump_download 返回新值", st.bump_download("card_a") == 4)
check("非法 id 的 bump 返回 0", st.bump_download("../x") == 0)
s = st.stats()
check("stats 计数一致", s["cards"] == st.list_cards(statuses=None)["total"])
check("stats 有分类分布", isinstance(s["by_category"], dict) and s["by_category"])
st.append_audit("card_a", {"action": "publish", "by": "u" * 8})
check("审计流水落盘", st.audit_path("card_a").is_file())
check("审计流水不含 sk", "sk-" not in st.audit_path("card_a").read_text(encoding="utf-8"))
check("删除成功", st.delete_card("card_a"))
check("删除后取不到", st.get_card("card_a") is None)
check("删除后资产目录也没了", not st.assets_dir("card_a").exists())
check("重复删除返回 False", st.delete_card("card_a") is False)

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
