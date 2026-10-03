# -*- coding: utf-8 -*-
"""广场卡安装/卸载回归 —— 共创平台 M1

钉死的事：
- 装进**当前数据根**（服务器模式 = 该账号目录），且登记进该账号的包清单；
- **安装时剥离 preset.json 的 knowledge_dirs**（否则别人发布的卡能让你的检索器
  读仓库内任意目录并注入提示词）；
- 覆盖安装前留档、默认拒绝同名覆盖；
- 服务器模式配额（≤5 张、≤8MB）；本地模式不限额；
- 卸载只对**广场来源**的包生效，且删除前留档；
- 跨用户隔离（A 装的卡 B 看不见）。

沙箱：FIREFLY_PLAZA_DIR / USER_DIR 全指向临时目录。
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

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_plaza_install_"))
os.environ["FIREFLY_PLAZA_DIR"] = str(_tmp / "plaza")

import modules.app_config as cfg   # noqa: E402

cfg.USER_DIR = _tmp / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from core import presets as P          # noqa: E402
from core import userctx               # noqa: E402
from plaza import api as pa            # noqa: E402
from plaza import install as pl        # noqa: E402
from plaza import store as st          # noqa: E402

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


def publish(cid, name="卡片", knowledge_dirs=None):
    preset = {"id": cid, "name": name, "char_name": name, "user_name": "你",
              "presentation": "sticker", "desc": "d", "schema": 1}
    if knowledge_dirs:
        preset["knowledge_dirs"] = knowledge_dirs
    files = {"preset.json": json.dumps(preset, ensure_ascii=False).encode(),
             "core.md": b"# core",
             "assets/cover.png": PNG}
    st.publish_card({"id": cid, "name": name, "char_name": name, "user_name": "你",
                     "presentation": "sticker", "desc": "d", "category": "陪伴"},
                    files, uid_hash="a" * 64, display="作者")


USER_A = cfg.USER_DIR / "1"
USER_B = cfg.USER_DIR / "2"
USER_A.mkdir(parents=True, exist_ok=True)
USER_B.mkdir(parents=True, exist_ok=True)

def set_mode(server: bool):
    """两个模块各自在调用期取 `_is_server()`，测试要一起补丁。"""
    pa._is_server = lambda: server
    pl._is_server = lambda: server


set_mode(True)        # 服务器模式语义（不连网）

print("== 1. 正常安装 ==")
publish("card_one", "卡一", knowledge_dirs=["app"])     # 故意带内仓库路径
tok = userctx.set_user_context(USER_A)
ok, why, info = pl.install_card("card_one")
check(f"安装成功（{why}）", ok)
check("目录落在本人数据根", (USER_A / "card_one" / "character" / "preset.json").is_file())
check("卡定义文件写入", (USER_A / "card_one" / "character" / "core.md").is_file())
check("封面写入", (USER_A / "card_one" / "character" / "assets" / "cover.png").is_file())
check("登记来源为 plaza", (cfg.pack_registry().get("card_one") or {}).get("source") == "plaza")
check("装完即可用（valid_mode）", P.valid_mode("card_one"))
check("mode_root 指向本人目录", cfg.mode_root("card_one") == USER_A / "card_one")

print("== 2. 安全：knowledge_dirs 必须被剥离 ==")
on_disk = json.loads((USER_A / "card_one" / "character" / "preset.json").read_text(encoding="utf-8"))
check("落盘的 preset.json 不含 knowledge_dirs", "knowledge_dirs" not in on_disk)
check("其余字段保留", on_disk.get("char_name") == "卡一")
check("卡体里确实声明了 knowledge_dirs（证明是安装时剥的，不是卡里没有）",
      __import__("plaza.card_format", fromlist=["x"]).parse_card_zip(
          st.card_zip_path("card_one").read_bytes())["preset"].get("knowledge_dirs") == ["app"])
tok2 = userctx.set_user_context(USER_A)
check("运行时解析出的包也不带知识库", not (P.pack_meta("card_one").get("knowledge_dirs")))
userctx.reset_user_context(tok2)

print("== 3. 重复安装与覆盖 ==")
ok, why, _ = pl.install_card("card_one")
check("默认拒绝同名覆盖", not ok and "已安装" in why)
ok, why, info = pl.install_card("card_one", replace=True)
check("replace=True 覆盖成功", ok)
check("覆盖时留下留档", bool(info.get("backup")) and Path(info["backup"]).is_dir())

print("== 4. 跨用户隔离 ==")
userctx.reset_user_context(tok)
tokB = userctx.set_user_context(USER_B)
check("B 没装过这张卡", not P.valid_mode("card_one"))
check("B 的数据根下没有该卡目录", not (USER_B / "card_one").exists())
ok, _, _ = pl.install_card("card_one")
check("B 自己装一份也可以", ok)
check("B 的目录独立", (USER_B / "card_one" / "character" / "preset.json").is_file())
userctx.reset_user_context(tokB)

print("== 5. 服务器模式配额（≤5 张）==")
tokA = userctx.set_user_context(USER_A)
for i in range(2, 6):                      # 已有 1 张，再装 4 张 = 5 张
    publish(f"card_q{i}", f"配额卡{i}")
    ok, why, _ = pl.install_card(f"card_q{i}")
    if not ok:
        check(f"第 {i} 张安装失败：{why}", False)
u = pl.plaza_usage()
check(f"已装 5 张（实际 {u['count']}）", u["count"] == 5)
publish("card_q6", "配额卡6")
ok, why, _ = pl.install_card("card_q6")
check("第 6 张被配额拦住", not ok and "最多安装" in why)

print("== 5b. 管理员账号不受安装配额限制（2026-10-01 用户拍板）==")
# 单测口径：张数与容量双豁免
ok_a, why_a = pl.check_quota(10 * 1024 * 1024, is_admin=True)
check(f"admin：超量（10MB 大卡）也放行（{why_a or 'no error'}）", ok_a)
ok_n, why_n = pl.check_quota(0, replacing=False)
check(f"普通账号在同一状态下仍被拦（{why_n}）", not ok_n and "最多安装" in why_n)
# 端到端口径：此时 A 已装 5 张（满额），admin 再装一张必须成功
publish("card_admin1", "管理员卡")
ok, why, _ = pl.install_card("card_admin1", is_admin=True)
check(f"admin 在满额（5 张）后仍装成功（{why or 'ok'}）", ok)
check("admin 这张真的落盘了", (USER_A / "card_admin1" / "character" / "preset.json").is_file())
u2 = pl.plaza_usage()
check(f"用量如实 +1（实际 {u2['count']}）", u2["count"] == 6)
# 同一张卡若走"非 admin"口径，应立刻被拦（证明形参真的在起作用，而不是恒定放行）
publish("card_user6", "普通卡6")
ok, why, _ = pl.install_card("card_user6")
check(f"同一路径非 admin ⇒ 第 6 张被拦（{why}）", not ok and "最多安装" in why)
userctx.reset_user_context(tokA)

print("== 5c. /plaza/api/installed 配额载荷：admin ⇒ null（不限）==")


class _FakeH:
    """最小 handler 替身：只要 `_bearer_user()`（角色来源）与 `_json()`（收载荷）。"""

    def __init__(self, role):
        self._role = role
        self.out = None

    def _bearer_user(self):
        return {"role": self._role, "email": "x@example.com"}

    def _json(self, data, status=200):
        self.out = data


from plaza import api as _papi                      # noqa: E402
tokA = userctx.set_user_context(USER_A)             # 带上用户上下文，used_cards 才是非零真值
_h_admin = _FakeH("admin")
_papi.plaza_installed(_h_admin)
check(f"admin：max_cards = null（实际 {_h_admin.out.get('max_cards')!r}）",
      _h_admin.out.get("max_cards") is None)
check(f"admin：max_bytes = null（实际 {_h_admin.out.get('max_bytes')!r}）",
      _h_admin.out.get("max_bytes") is None)
check(f"admin：used_cards 仍如实上报（{_h_admin.out.get('used_cards')}）",
      _h_admin.out.get("used_cards") == pl.plaza_usage()["count"])
_h_user = _FakeH("user")
_papi.plaza_installed(_h_user)
check(f"普通账号：max_cards 仍是 5（实际 {_h_user.out.get('max_cards')!r}）",
      _h_user.out.get("max_cards") == pl.QUOTA_MAX_CARDS)
check(f"普通账号：max_bytes 仍是 8MB（实际 {_h_user.out.get('max_bytes')!r}）",
      _h_user.out.get("max_bytes") == pl.QUOTA_MAX_BYTES)

print("== 6. 本地模式不限额 ==")
set_mode(False)
ok, why = pl.check_quota(10 * 1024 * 1024)
check("本地模式放行超量大卡", ok)

print("== 7. 卸载 ==")
set_mode(True)
tokA = userctx.set_user_context(USER_A)
ok, why = pl.uninstall_card("card_q6")
check("未安装的卡卸载被拒", not ok)
ok, why = pl.uninstall_card("card_q2")
check(f"卸载成功（{why}）", ok)
check("目录已删除", not (USER_A / "card_q2").exists())
check("清单已注销", cfg.pack_registry().get("card_q2") is None)
check("卸载后不可用", not P.valid_mode("card_q2"))

# 自建包（source=custom）不允许从这里卸载
cfg.pack_registry().register("my_own", source="custom",
                             parsed={"id": "my_own", "name": "自建", "char_name": "自建",
                                     "user_name": "你", "presentation": "sticker"})
ok, why = pl.uninstall_card("my_own")
check("自建包拒绝从广场卸载口删除", not ok and "广场安装" in why)
userctx.reset_user_context(tokA)

print("== 8. 不存在的卡 ==")
tokA = userctx.set_user_context(USER_A)
ok, why, _ = pl.install_card("card_not_exist")
check("不存在的卡返回人话错误", not ok and "不存在" in why)
ok, why, _ = pl.install_card("../../etc")
check("非法 id 被拒", not ok)
userctx.reset_user_context(tokA)

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
