# -*- coding: utf-8 -*-
"""安全加固回归测试（2026-08-25 安全审查配套）

覆盖本次修复点：
A. img_id 严格白名单（routes._load_image_data_url / get_image）——绝对路径 pattern
   不再抛未捕获异常、通配符/穿越形态一律拒绝
B. SSRF 防护 is_public_endpoint（api_client）——私网/环回/链路本地/CGNAT/localhost 拒绝，
   公网 IP 字面量放行，域名走 DNS 全记录校验（mock），结果缓存生效
C. db sessions token 哈希化——库内不存明文、明文行兼容并就地升级、delete/extend 双形式、
   v2 迁移批量改写
D. auth.verify_password 常数时间比较回归
E. db.prune_login_fail_keys 过期键清理
F. download_server MAX_PROXY_BODY 网关请求体上限常量
G. multipart 畸形 Content-Length 安全拒绝
"""
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


# ── 隔离 user_data ───────────────────────────────
import modules.app_config as cfg
_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_sec_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

import routes
from modules.api_client import is_public_endpoint, _ip_public

print("=== A. img_id 严格白名单 ===")
# 准备一张合法图片
from modules.storage import atomic_write_bytes
img_dir = cfg.mode_root("story") / "images"
img_dir.mkdir(parents=True, exist_ok=True)
atomic_write_bytes(img_dir / "img_abc123def456.png", b"\x89PNG\r\n\x1a\nfake")

ok_id = "img_abc123def456"
url = routes._load_image_data_url("story", ok_id)
check("A1 合法 img_id 正常读取", bool(url) and url.startswith("data:image/png"))

bad_ids = [
    "../../1/images/img_abc123def456",   # 穿越
    "..\\..\\1\\images\\img_x",          # Windows 反斜杠穿越
    "C:/evil",                            # 绝对路径（原会抛 NotImplementedError）
    "\\\\attacker\\share\\img",           # UNC
    "*",                                  # glob 通配符
    "img_*",                              # 前缀通配
    "",                                   # 空
    "img_x;rm -rf",                       # 杂字符
]
for bid in bad_ids:
    try:
        r = routes._load_image_data_url("story", bid)
        check(f"A2 拒绝非法 img_id {bid[:28]!r}", r is None)
    except Exception as e:
        check(f"A2 拒绝非法 img_id {bid[:28]!r}（抛异常 {type(e).__name__}）", False)

check("A3 白名单正则匹配正常格式", bool(routes._IMG_ID_RE.fullmatch("img_0123456789ab")))
check("A4 白名单拒绝超长", not routes._IMG_ID_RE.fullmatch("a" * 65))

# get_image 路由级校验（mock handler）
class _H:
    def __init__(self, path):
        self.path = path
        self.data = None
        self.status = None
        self.sent = []
    def _json(self, obj, status=200):
        self.data = obj
        self.status = status
    # 字节响应分支（合法 id 命中文件时）
    def send_response(self, code, *a):
        self.status = code
    def send_header(self, *a):
        pass
    def end_headers(self):
        pass
    wfile = __import__("io").BytesIO()   # 字节响应写目标

h = _H("/image?id=C:/evil&mode=story")
routes.get_image(h)
check("A5 get_image 绝对路径 400", h.status == 400)
h = _H("/image?id=..%2F..%2Fx&mode=story")   # urlencoded ../
routes.get_image(h)
check("A6 get_image 编码穿越 400", h.status == 400)
h = _H("/image?id=img_abc123def456&mode=story")
routes.get_image(h)
check("A7 get_image 合法 id 不拒（404 或 200 均可，不能 400）", h.status != 400)

print("=== B. SSRF 防护 is_public_endpoint ===")
cases_false = [
    "http://127.0.0.1:8765/v1",
    "http://localhost/v1",
    "http://api.localhost/v1",
    "http://printer.local/v1",
    "http://192.168.1.1/v1",
    "http://10.0.0.5/v1",
    "http://172.16.0.1/v1",
    "http://169.254.169.254/latest/meta-data",   # 云元数据
    "http://100.64.0.1/v1",                       # CGNAT
    "http://0.0.0.0/v1",
    "http://[::1]:9000/v1",
    "http://[fe80::1]/v1",                        # IPv6 链路本地
    "http://[fd00::1]/v1",                        # IPv6 私有
    "ftp://example.com/v1",                       # 非 http 协议
    "not a url",
    "",
    "http://224.0.0.1/v1",                        # 组播
    "http://255.255.255.255/v1",                  # 广播/保留
]
for u in cases_false:
    check(f"B1 拒绝 {u[:40]}", not is_public_endpoint(u))

check("B2 公网 IPv4 字面量放行", is_public_endpoint("https://1.1.1.1/v1"))
check("B3 公网 IPv6 字面量放行", is_public_endpoint("https://[2606:4700:4700::1111]/v1"))
check("B4 IPv4-mapped IPv6 按私网拒", not is_public_endpoint("http://[::ffff:127.0.0.1]/v1"))

# 域名 DNS 路径（mock getaddrinfo，不依赖网络）
import modules.api_client as ac
_orig_gai = ac.socket.getaddrinfo if hasattr(ac, "socket") else None
import socket as _socket
# 注：不能用 203.0.113.x/192.0.2.x 等文档段——ipaddress 判 is_private（防御正确）
with patch.object(_socket, "getaddrinfo",
                  return_value=[(None, None, None, None, ("93.184.216.34", 0))]):
    check("B5 域名解析公网 IP 放行", is_public_endpoint("https://api.example.com/v1"))
with patch.object(_socket, "getaddrinfo",
                  return_value=[(None, None, None, None, ("8.8.8.8", 0)),
                                (None, None, None, None, ("192.168.0.1", 0))]):
    check("B6 多记录含私网 → 拒绝（DNS rebinding 面）",
          not is_public_endpoint("https://mixed.example.com/v1"))
with patch.object(_socket, "getaddrinfo", side_effect=_socket.gaierror("dns fail")):
    check("B7 DNS 失败 → 拒绝", not is_public_endpoint("https://nx.example.com/v1"))
# 缓存命中路径（同 host 再查，mock 已撤走仍应取缓存值）
check("B8 host 结果缓存生效", is_public_endpoint("https://api.example.com/v1"))
check("B9 _ip_public 单元：公网 True", _ip_public("8.8.8.8"))
check("B10 _ip_public 单元：保留段 False", not _ip_public("240.0.0.1"))

print("=== C. db token 哈希化 ===")
sys.path.insert(0, str(ROOT / "server"))
import db as sdb

_db = Path(tempfile.mkdtemp(prefix="firefly_test_secdb_")) / "t.db"
sdb.init_db(_db)
sdb.ensure_auth_columns()

uid = sdb.create_user("sec@qq.com", "h" * 64, "0" * 32, install_id="inst-" + "c" * 32)
sdb.create_session("tok-PLAIN-secret-01", uid, "test",
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400)))

# 库内必须是哈希而非明文
with sdb._DB_LOCK:
    row = sdb._conn().execute("SELECT token FROM sessions WHERE user_id=?", (uid,)).fetchone()
stored = row["token"]
check("C1 库内不存明文 token", stored != "tok-PLAIN-secret-01")
check("C2 存储值为 64 位 hex（sha256）", len(stored) == 64 and all(c in "0123456789abcdef" for c in stored))

sess = sdb.get_session("tok-PLAIN-secret-01")
check("C3 明文 token 可查回会话", sess is not None and sess["user_id"] == uid)
sdb.extend_session("tok-PLAIN-secret-01",
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 2 * 86400)))
check("C4 extend_session 哈希行可续期", sdb.get_session("tok-PLAIN-secret-01") is not None)

# 旧明文行兼容（模拟迁移前库）
with sdb._DB_LOCK:
    sdb._conn().execute(
        "INSERT INTO sessions(token, user_id, device, created_at, expires_at) VALUES(?,?,?,?,?)",
        ("tok-legacy-plain-row", uid, "old", "2026-01-01 00:00:00",
         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400))))
    sdb._conn().commit()
s = sdb.get_session("tok-legacy-plain-row")
check("C5 旧明文行仍可认证", s is not None)
with sdb._DB_LOCK:
    row2 = sdb._conn().execute(
        "SELECT token FROM sessions WHERE token=?", ("tok-legacy-plain-row",)).fetchone()
check("C6 旧明文行命中后就地升级为哈希", row2 is None)
sdb.delete_session("tok-legacy-plain-row")
check("C7 delete_session 清除升级后的行", sdb.get_session("tok-legacy-plain-row") is None)

# v2 迁移：存量明文批量改写
with sdb._DB_LOCK:
    for i in range(3):
        sdb._conn().execute(
            "INSERT INTO sessions(token, user_id, device, created_at, expires_at) VALUES(?,?,?,?,?)",
            (f"tok-mig-plain-{i}-abcdefgh", uid, "mig", "2026-01-01 00:00:00",
             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400))))
    sdb._conn().commit()
sdb._migrate_sessions_token_hash()
with sdb._DB_LOCK:
    left = sdb._conn().execute(
        "SELECT COUNT(*) FROM sessions WHERE token LIKE 'tok-mig-plain-%'").fetchone()[0]
check("C8 v2 迁移批量改写明文", left == 0)
check("C9 迁移后原 token 仍可认证", sdb.get_session("tok-mig-plain-1-abcdefgh") is not None)

print("=== D. verify_password 常数时间比较回归 ===")
import auth as auth_svc
salt_hex = "ab" * 16
good = auth_svc.hash_password("pass12345", bytes.fromhex(salt_hex))
check("D1 正确密码通过", auth_svc.verify_password("pass12345", salt_hex, good))
check("D2 错误密码拒绝", not auth_svc.verify_password("pass123456", salt_hex, good))
check("D3 畸形盐拒绝", not auth_svc.verify_password("x", "zz", good))
check("D4 空 hash 拒绝", not auth_svc.verify_password("x", salt_hex, ""))

print("=== E. loginfail 键清理 ===")
sdb.set_setting("loginfail:a@qq.com", "[1000000000.0]")        # 远古（必过期）
sdb.set_setting("loginfail:b@qq.com", f"[{time.time()}]")       # 新鲜（保留）
sdb.set_setting("loginfail:c@qq.com", "corrupt{")               # 损坏（清）
removed = sdb.prune_login_fail_keys(15 * 60)
check("E1 清理过期/损坏键", removed == 2)
check("E2 新鲜键保留", sdb.get_setting("loginfail:b@qq.com") is not None)

print("=== F. download_server 网关体上限 ===")
_argv = sys.argv
sys.argv = ["download_server.py"]           # 防 argv 被测试参数污染 BASE/PORT
try:
    import importlib
    ds = importlib.import_module("download_server")
finally:
    sys.argv = _argv
check("F1 MAX_PROXY_BODY 已定义且 >11MB（图片上传上限）",
      ds.MAX_PROXY_BODY > 11 * 1024 * 1024)
check("F2 MAX_PROXY_BODY ≤ 32MB（防上限过宽形同虚设）",
      ds.MAX_PROXY_BODY <= 32 * 1024 * 1024)

print("=== G. multipart 畸形 Content-Length ===")
from modules.multipart import parse_multipart
class _MH:
    def __init__(self, cl):
        self.headers = {"Content-Type": "multipart/form-data; boundary=x",
                        "Content-Length": cl}
        self.rfile = b""
try:
    f, files = parse_multipart(_MH("99999999999999999999x"))
    check("G1 非数字 Content-Length 安全拒绝", f == {} and files == {})
except (TypeError, ValueError):
    check("G1 非数字 Content-Length 安全拒绝", True)

print()
print("=" * 50)
print(f"  通过: {PASS}  失败: {FAIL}")
print("=" * 50)
sys.exit(1 if FAIL else 0)
