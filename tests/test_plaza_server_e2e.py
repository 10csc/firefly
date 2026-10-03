# -*- coding: utf-8 -*-
"""服务器模式「广场卡」端到端 —— 共创平台 M1/M1.5 的真实进程验证（未完成事项 P4-6）

为什么必须单独有它：`plaza/install.py` 的 29 项单测与 `test_user_pack_overlay.py` 的 30 项
都是**进程内模拟**（手工 `set_user_context`）。而 D1 决策（服务器版允许广场卡落盘）真正依赖的是：
**真实服务器入口 + 真实账号 + 真实 token** 下，
① 卡确实落到**该账号自己**的目录；② 该账号的 `/modes` 能看到它；③ **别的账号看不到**；
④ 管理端点只认 admin；⑤ 卸载后目录与列表都干净。

做法：沙箱数据根 + 直接建库造用户/会话（不走邮件注册流程），起 `server_app.py`，全程真 HTTP。
**不连外网、不碰真实 user_data、不动生产。**

退出码：0 全过；1 有断言失败；2 服务没起来；3 端口被占。
"""
import json
import os
import shutil
import socket as _sk
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_plaza_e2e_"))
USER_DIR = SANDBOX / "user_data"
PLAZA_DIR = SANDBOX / "plaza"
USER_DIR.mkdir(parents=True, exist_ok=True)
PLAZA_DIR.mkdir(parents=True, exist_ok=True)

# ⚠ 必须先把环境摆好再 import：app/core/paths.py 在 import 期就按 FIREFLY_* 定 USER_DIR
os.environ.update({
    "FIREFLY_ANDROID": "1",
    "FIREFLY_DATA_DIR": str(SANDBOX),        # USER_DIR = 该值 / "user_data"（paths.py:26）
    "FIREFLY_PLAZA_DIR": str(PLAZA_DIR),
    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
})
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"  {note}" if note else ""), flush=True)


# ── 1. 沙箱里造账号 + 种卡（都在 Lead 进程内完成，随后由服务器进程读同一份盘）──
import db as srv_db                    # noqa: E402  （server/db.py）
from plaza import store as st          # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID_A = srv_db.create_user("alice@qq.com", "h", "s", install_id="inst-" + "a" * 32, role="user")
UID_B = srv_db.create_user("bob@qq.com", "h", "s", install_id="inst-" + "b" * 32, role="user")
UID_ADMIN = srv_db.create_user("owner@qq.com", "h", "s", install_id="inst-" + "c" * 32, role="admin")
TOK_A, TOK_B, TOK_ADMIN = "tok-alice", "tok-bob", "tok-admin"
for tok, uid in ((TOK_A, UID_A), (TOK_B, UID_B), (TOK_ADMIN, UID_ADMIN)):
    srv_db.create_session(tok, uid, "e2e", EXP)
check("沙箱建了 3 个账号（含 1 个 admin）", all((UID_A, UID_B, UID_ADMIN)))

CARD = {"id": "e2e_card", "name": "端到端卡", "char_name": "小端", "user_name": "你",
        "presentation": "sticker", "desc": "服务器模式端到端验证用", "category": "陪伴"}
st.publish_card(CARD, {
    "preset.json": json.dumps({"id": CARD["id"], "name": CARD["name"], "char_name": CARD["char_name"],
                               "user_name": CARD["user_name"], "presentation": "sticker",
                               "desc": CARD["desc"], "schema": 1}, ensure_ascii=False).encode(),
    "core.md": "# 核心设定".encode(),
}, uid_hash="o" * 32, display="官方", official=True)
check("广场里已有一张已发布卡", st.stats()["cards"] == 1)


# ── 2. 起真实服务器入口 ──────────────────────────────────────────
def free_port(lo=8911, hi=8940):
    for p in range(lo, hi):
        s = _sk.socket()
        s.settimeout(0.3)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return 0


PORT = free_port()
check(f"空闲端口 {PORT}", PORT > 0)

env = dict(os.environ)
env.update({"FIREFLY_PORT": str(PORT), "FIREFLY_NO_BROWSER": "1", "PYTHONUNBUFFERED": "1"})
log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def req(method, path, token=None, body=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


try:
    ready = False
    for _ in range(80):
        s, _b = req("GET", "/health")
        if s == 200:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        print("服务器未起来", flush=True)
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)

    print("== 1. 登录态与广场可见性 ==")
    s, b = req("GET", "/plaza/api/list")
    check(f"① 未登录 → 401（实际 {s}）", s == 401)
    s, b = req("GET", "/plaza/api/list", TOK_A)
    d = json.loads(b.decode("utf-8"))
    check("② 登录后能看到广场列表", s == 200 and d["total"] == 1)
    check("②b 卡在列表里", d["items"][0]["id"] == CARD["id"])

    s, b = req("GET", "/plaza/api/installed", TOK_A)
    check("③ alice 初始未安装任何卡", s == 200 and json.loads(b.decode())["used_cards"] == 0)

    print("== 2. 安装 → 落到**该账号自己**的目录 ==")
    s, b = req("POST", "/plaza/api/install", TOK_A, {"id": CARD["id"]})
    check(f"④ 安装成功（实际 {s}）", s == 200 and json.loads(b.decode())["ok"] is True)
    own = USER_DIR / str(UID_A) / CARD["id"] / "character" / "preset.json"
    other = USER_DIR / str(UID_B) / CARD["id"]
    check(f"④b 卡落在 user_data/{UID_A}/ 下", own.is_file())
    check("④c 没有落到别人的账号目录", not other.exists())
    check("④d 也没有落到全局根（必须按账号隔离）", not (USER_DIR / CARD["id"]).exists())

    s, b = req("GET", "/plaza/api/installed", TOK_A)
    u = json.loads(b.decode())
    check(f"⑤ 已安装计数与配额（{u['used_cards']}/{u['max_cards']}）",
          u["used_cards"] == 1 and u["server_mode"] is True and u["max_cards"] > 0)

    print("== 3. M1.5 按用户包视图：本账号 /modes 能看到它 ==")
    s, b = req("GET", "/modes", TOK_A)
    modes = json.loads(b.decode())
    ids = [m.get("id") for m in (modes.get("modes") or modes.get("items") or [])] \
        if isinstance(modes, dict) else []
    check(f"⑥ alice 的 /modes 含新装的卡（实际 {ids}）", CARD["id"] in ids)

    s, b = req("GET", f"/pack-files?mode={CARD['id']}", TOK_A)
    check(f"⑦ 能通过包端点读到这张卡（实际 {s}）", s == 200)

    print("== 4. 跨账号隔离 ==")
    s, b = req("GET", "/modes", TOK_B)
    modes_b = json.loads(b.decode())
    ids_b = [m.get("id") for m in (modes_b.get("modes") or modes_b.get("items") or [])] \
        if isinstance(modes_b, dict) else []
    check(f"⑧ bob 的 /modes 不含 alice 的卡（实际 {ids_b}）", CARD["id"] not in ids_b)
    s, b = req("GET", "/plaza/api/installed", TOK_B)
    check("⑨ bob 的已安装数为 0", json.loads(b.decode())["used_cards"] == 0)
    s, b = req("GET", f"/pack-files?mode={CARD['id']}", TOK_B)
    # 注意：这里**不该**断言状态码——`_query_mode_ex` 的既有约定（R-07）是
    # "未知 mode → 回退默认包并在响应里体现"，所以 bob 会拿到 200 + **story 的内容**。
    # 真正要断言的性质是"**读不到对方的卡**"：回退后的 mode 不是那张卡，且响应里没有它的内容。
    body_b = b.decode("utf-8", "replace")
    d_b = json.loads(body_b) if s == 200 and body_b.startswith("{") else {}
    check(f"⑨b bob 读他人的卡只会回退默认包、拿不到内容（回退到 {d_b.get('mode')!r}）",
          d_b.get("mode") != CARD["id"] and "小端" not in body_b and CARD["id"] not in body_b)

    print("== 5. 管理端点权限 ==")
    s, _b = req("GET", "/plaza/api/admin/list?status=all", TOK_A)
    check(f"⑩ 普通账号查管理列表 → 403（实际 {s}）", s == 403)
    s, b = req("GET", "/plaza/api/admin/list?status=all", TOK_ADMIN)
    d = json.loads(b.decode())
    check("⑪ admin 查得到（含未公开状态）", s == 200 and any(v["id"] == CARD["id"] for v in d["items"]))

    print("== 6. 举报链路（真实落盘 + 管理可见）==")
    s, _b = req("POST", "/plaza/api/report", TOK_A, {"id": CARD["id"], "reason": "端到端测试举报"})
    check(f"⑫ 举报成功（实际 {s}）", s == 200)
    s, b = req("GET", "/plaza/api/admin/list?status=all", TOK_ADMIN)
    row = next(v for v in json.loads(b.decode())["items"] if v["id"] == CARD["id"])
    check(f"⑬ 管理视图看到举报数（{row['report_count']}）", row["report_count"] == 1)

    print("== 7. 卸载后干净 ==")
    s, _b = req("POST", "/plaza/api/uninstall", TOK_A, {"id": CARD["id"]})
    check(f"⑭ 卸载成功（实际 {s}）", s == 200)
    check("⑭b 账号目录已删", not (USER_DIR / str(UID_A) / CARD["id"]).exists())
    s, b = req("GET", "/modes", TOK_A)
    modes2 = json.loads(b.decode())
    ids2 = [m.get("id") for m in (modes2.get("modes") or modes2.get("items") or [])] \
        if isinstance(modes2, dict) else []
    check("⑭c /modes 里也不再有它", CARD["id"] not in ids2)
    s, b = req("GET", "/plaza/api/installed", TOK_A)
    check("⑭d 已安装计数回到 0", json.loads(b.decode())["used_cards"] == 0)
    # 广场本体不受卸载影响（作者发的卡还在）
    s, b = req("GET", "/plaza/api/list", TOK_A)
    check("⑭e 广场里那张卡仍在（卸载只删本地副本）", json.loads(b.decode())["total"] == 1)

    print("== 8. ★受登录防护：**所有**广场端点未登录都拒绝（含写端点）==")
    # 需求原话是"服务器端提供**受登录防护**的角色卡制作页"。
    # 既有的 `manual_plaza_login_gate.py` 只覆盖了**读**端点（7 项）；
    # 这里把**写**端点也逐个钉死 —— 发布/审核/举报若未登录可达，等于谁都能往广场塞卡。
    gate_get = ["/plaza/api/list", "/plaza/api/card?id=gate_x",
                "/plaza/api/asset?id=gate_x&slot=cover", "/plaza/api/download?id=gate_x",
                "/plaza/api/installed", "/plaza/api/drafts", "/plaza/api/draft?id=gate_x",
                "/plaza/api/admin/list"]
    gate_post = [("/plaza/api/install", {"id": "gate_x"}),
                 ("/plaza/api/uninstall", {"id": "gate_x"}),
                 ("/plaza/api/review", {"id": "gate_x", "sk": "sk-" + "a" * 24}),
                 ("/plaza/api/draft", {"id": "gate_x", "name": "n", "char_name": "c",
                                       "user_name": "u", "desc": "d"}),
                 ("/plaza/api/draft/delete", {"id": "gate_x"}),
                 ("/plaza/api/publish", {"id": "gate_x"}),
                 ("/plaza/api/unpublish", {"id": "gate_x"}),
                 ("/plaza/api/report", {"id": "gate_x", "reason": "r"}),
                 ("/plaza/api/admin/status", {"id": "gate_x", "status": "published"})]
    leaked = []
    for p in gate_get:
        s, _b = req("GET", p)
        if s != 401:
            leaked.append((p, s))
    for p, payload in gate_post:
        s, _b = req("POST", p, None, payload)     # 注意签名是 (method, path, token, body)
        if s != 401:
            leaked.append((p, s))
    check(f"⑮ 未登录访问 {len(gate_get) + len(gate_post)} 个广场端点（8 读 + 9 写）"
          f"**全部 401**", not leaked, f"漏网的：{leaked}")

    print("== 9. ★普通用户不能冒充「官方」（official 只能由服务端按 role 判定）==")
    from plaza import card_format as _cf
    n_draft = len(list((PLAZA_DIR / "drafts").rglob("*.json"))) if (PLAZA_DIR / "drafts").exists() else 0
    s, b = req("POST", "/plaza/api/draft", TOK_A,
               {"card": {"id": "priv_card", "name": "越权验证卡", "char_name": "小萤",
                         "user_name": "你", "desc": "普通用户自查能否自称官方"}})
    check(f"⑯ 普通用户能建自己的草稿（实际 {s}）", s == 200, b[:120].decode("utf-8", "replace"))

    # 让草稿"通过审核"：真实审核要花用户自己的 Key，这里按**真实形状**把结论写进沙箱草稿
    # （与 tests/manual_plaza_forge.py 同一手法；**产品闸门一行没改**）
    patched = False
    for meta_fp in (PLAZA_DIR / "drafts").rglob("priv_card.json"):
        zip_fp = meta_fp.with_suffix(".zip")
        if not zip_fp.is_file():
            continue
        digest = _cf.parse_card_zip(zip_fp.read_bytes())["digest"]
        m = json.loads(meta_fp.read_text(encoding="utf-8"))
        m["review"] = {"verdict": "pass", "risk": "low", "reasons": ["桩：合规"],
                       "model": "deepseek-stub", "digest": digest}
        m["status"] = "reviewed"
        meta_fp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        patched = True
        break
    check("⑯b 已按真实形状把审核结论写进沙箱草稿", patched)

    # ★ 2026-10-01：普通用户**显式**传 official ⇒ 现在直接 403（原先静默忽略，见 lead 口径）
    s, b = req("POST", "/plaza/api/publish", TOK_A, {"id": "priv_card", "official": True})
    check(f"⑰ 普通用户带 official ⇒ **403**（实际 {s}）", s == 403,
          b[:160].decode("utf-8", "replace"))
    # 去掉 official 后照常发布 ⇒ 验证"不是普通用户一律发不出去"，只挡冒充官方
    s, b = req("POST", "/plaza/api/publish", TOK_A, {"id": "priv_card"})
    check(f"⑰a 去掉 official 后发布成功（实际 {s}）", s == 200, b[:160].decode("utf-8", "replace"))
    priv_meta = PLAZA_DIR / "cards" / "priv_card" / "meta.json"
    pub = json.loads(priv_meta.read_text(encoding="utf-8")) if priv_meta.is_file() else {}
    au = pub.get("author") or {}
    check(f"⑰b ★不带 official 时落盘 author.official={au.get('official')!r}（冒充不了）",
          au.get("official") is False)
    check("⑰c 作者记的是**他自己**的 uid_hash（不是别人的）",
          bool(au.get("uid_hash")) and au.get("uid_hash") != "o" * 32)

    # 正对照：admin 发布走同一条路 → official 为真（证明上一条不是"永远为假"）
    s, _b = req("POST", "/plaza/api/draft", TOK_ADMIN,
                {"card": {"id": "admin_card", "name": "官方卡", "char_name": "小萤",
                          "user_name": "你", "desc": "正对照"}})
    s, b = req("POST", "/plaza/api/publish", TOK_ADMIN, {"id": "admin_card"})
    a_meta = PLAZA_DIR / "cards" / "admin_card" / "meta.json"
    a_pub = json.loads(a_meta.read_text(encoding="utf-8")) if a_meta.is_file() else {}
    check(f"⑱ 正对照：admin 发布 → official={((a_pub.get('author') or {}).get('official'))!r}",
          ((a_pub.get("author") or {}).get("official")) is True)
finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log.close()
    shutil.rmtree(SANDBOX, ignore_errors=True)

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
