# -*- coding: utf-8 -*-
"""`/assets/` 的**账号作用域**回归 —— 2026-10-01 修复（clientpack 实测缺口）

现象：服务器模式下**用户自己包里的图片取不到** —— `/assets/character/<用户包>/assets/*`
与 `/assets/stickers/*` 一律 **404**。
根因：`server/app.py` 的 `/assets/` 分支在 `_setup_user_context()` **之前**分发 ⇒
`_user_ctx_dir()` 恒为 None ⇒ 只在全局根里找；而且 `cfg.resolve_asset()` 的"包副本"分支被
`parts[2] in _presets.MODES` 挡住（服务器版注册表**只有内置包**）⇒ 自建包永远解析不到。

本测试在真实 `server_app.py` 进程上跑，正/负例都要：
  ① 登录用户能取到**自己**的包图与表情包（200 + 字节与落盘文件一致）；
  ② **取不到别人**的（404，负例必测）；
  ③ 未登录取账号内资产 ⇒ **401**（不是 200）；
  ④ 全局/bundled 资产未登录仍 **200**（现有 App 依赖，不能变 401）；
  ⑤ `/assets/index`、`/assets/raw` 仍走认证路由（未登录 401）；
  ⑥ `..` 穿越仍 404。
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
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_asset_scope_"))
USER_DIR = SANDBOX / "user_data"
PLAZA_DIR = SANDBOX / "plaza"
USER_DIR.mkdir(parents=True, exist_ok=True)
PLAZA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "FIREFLY_ANDROID": "1", "FIREFLY_DATA_DIR": str(SANDBOX),
    "FIREFLY_PLAZA_DIR": str(PLAZA_DIR), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
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
        print(f"  FAIL {desc}  {note}", flush=True)


import db as srv_db                              # noqa: E402

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID_A = srv_db.create_user("scope_a@qq.com", "h", "s", install_id="inst-" + "a" * 32, role="user")
UID_B = srv_db.create_user("scope_b@qq.com", "h", "s", install_id="inst-" + "b" * 32, role="user")
TOK_A, TOK_B = "tok-scope-a", "tok-scope-b"
srv_db.create_session(TOK_A, UID_A, "scope-a", EXP)
srv_db.create_session(TOK_B, UID_B, "scope-b", EXP)

PNG = bytes.fromhex(                     # 头部合法的最小 PNG（内容只需要可区分）
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082")
PACK_A, PACK_B = "mypack_a", "mypack_b"
BLOB_A_PACK = b"AAAA-package-cover-" + PNG
BLOB_A_STICK = b"AAAA-sticker-" + PNG
BLOB_A_MIRROR = b"AAAA-mirror-" + PNG
BLOB_B_PACK = b"BBBB-package-cover-" + PNG
BLOB_B_STICK = b"BBBB-sticker-" + PNG

# ① 用户自建包：{USER_DIR}/{uid}/{包}/character/assets/{文件}（App 的布局）
def _put(uid, rel, blob):
    p = USER_DIR / str(uid) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)
    return p


_put(UID_A, f"{PACK_A}/character/assets/cover.png", BLOB_A_PACK)
_put(UID_A, "stickers/astick.png", BLOB_A_STICK)
_put(UID_A, "assets/stickers/mirror.png", BLOB_A_MIRROR)      # 直接镜像布局（另一条解析路径）
_put(UID_B, f"{PACK_B}/character/assets/cover.png", BLOB_B_PACK)
_put(UID_B, "stickers/bstick.png", BLOB_B_STICK)
GLOBAL = ROOT / "app" / "assets" / "character" / "story" / "assets" / "cover.png"
check(f"前置：全局/bundled 资产样本存在（{GLOBAL.relative_to(ROOT)}）", GLOBAL.is_file())


def pick_port():
    s = _sk.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = pick_port()
env = dict(os.environ)
env.update({"FIREFLY_PORT": str(PORT), "PYTHONUNBUFFERED": "1"})
log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=env, stdout=log, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"


def get(path, tok=None, timeout=15):
    """只读 GET；`tok=None` = **不带** Authorization（图片类客户端就是这样）。"""
    url = base + urllib.parse.quote(path, safe="/?=&%:#")
    req = urllib.request.Request(url, method="GET")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


try:
    for _ in range(80):
        try:
            if get("/health")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        print("服务器未起来：", (SANDBOX / "server.log").read_text("utf-8", "replace")[-1200:])
        sys.exit(2)

    print("== ① 登录用户取**自己**包里的图（修复前 404）==")
    for path, blob, desc in (
        (f"/assets/character/{PACK_A}/assets/cover.png", BLOB_A_PACK, "自己的包封面（包副本布局）"),
        ("/assets/stickers/astick.png", BLOB_A_STICK, "自己的表情包（stickers/ 布局）"),
        ("/assets/stickers/mirror.png", BLOB_A_MIRROR, "自己的表情包（assets/stickers/ 镜像布局）"),
    ):
        s, b = get(path, TOK_A)
        check(f"{desc} ⇒ 200 且字节一致（{path}）", s == 200 and b == blob, f"status={s} len={len(b)}")

    print("== ② 负例：取**别人**的包图/表情包必须取不到 ==")
    s, b = get(f"/assets/character/{PACK_B}/assets/cover.png", TOK_A)
    check(f"A 取 B 的包封面 ⇒ 404（实际 {s}，{len(b)}B）", s == 404)
    s, b = get("/assets/stickers/bstick.png", TOK_A)
    check(f"A 取 B 的表情包 ⇒ 404（实际 {s}）", s == 404)
    s, b = get(f"/assets/character/{PACK_A}/assets/cover.png", TOK_B)
    check(f"B 取 A 的包封面 ⇒ 404（实际 {s}）", s == 404)
    s, b = get("/assets/stickers/astick.png", TOK_B)
    check(f"B 取 A 的表情包 ⇒ 404（实际 {s}）", s == 404)
    # 正面对照：B 自己的能取到（证明上面的 404 不是"整类不可用"）
    s, b = get(f"/assets/character/{PACK_B}/assets/cover.png", TOK_B)
    check(f"B 取自己的包封面 ⇒ 200 且字节一致（实际 {s}）", s == 200 and b == BLOB_B_PACK)

    print("== ③ 未登录取账号内资产 ⇒ 401（不是 200，也不是 404 混过去）==")
    for path in (f"/assets/character/{PACK_A}/assets/cover.png", "/assets/stickers/astick.png"):
        s, b = get(path)
        check(f"未登录 {path} ⇒ 401（实际 {s}）", s == 401)

    print("== ④ 全局/bundled 资产未登录仍 200（现有 App 依赖，不能变 401）==")
    s, b = get("/assets/character/story/assets/cover.png")
    check(f"未登录取全局包封面 ⇒ 200（实际 {s}，{len(b)}B）", s == 200 and len(b) > 0)
    check("返回的确实是 bundled 那份字节", s == 200 and b == GLOBAL.read_bytes())
    s, b = get("/assets/character/story/assets/cover.png", TOK_A)
    check(f"登录用户取全局包封面 ⇒ 200（实际 {s}）", s == 200 and b == GLOBAL.read_bytes())

    print("== ⑤ /assets/index、/assets/raw 仍走认证路由（不能被我这条分支吞掉）==")
    s, _ = get("/assets/index?mode=story")
    check(f"未登录 /assets/index ⇒ 401（实际 {s}）", s == 401)
    s, _ = get("/assets/raw?name=x&mode=story")
    check(f"未登录 /assets/raw ⇒ 401（实际 {s}）", s == 401)
    s, _ = get("/assets/index?mode=story", TOK_A)
    check(f"登录 /assets/index 不回 401（实际 {s}）", s != 401)

    print("== ⑥ 穿越仍被挡（不能靠新分支绕过）==")
    s, _ = get("/assets/../user_data/%s/stickers/astick.png" % UID_A, TOK_A)
    check(f"含 .. 的路径 ⇒ 404（实际 {s}）", s == 404)
    s, _ = get("/assets/character/..%2F..%2Fetc/passwd", TOK_A)
    check(f"编码后的穿越 ⇒ 非 200（实际 {s}）", s != 200)
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
