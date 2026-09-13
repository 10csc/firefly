# -*- coding: utf-8 -*-
"""db v3 迁移（阶段 3 · 任务 3.7）的**行为守卫**

卡片要求：v3 = `sessions.expires_at` 索引 + `notices` 索引；`ensure_auth_columns` 保留（旧库兼容）
但注明其历史地位，新列一律走 `_MIGRATIONS`。

守五件事：
1. 新库：`init_db` 后 `schema_version=3`，两个索引都在；
2. 旧 v2 库升级：数据不丢，版本推到 3，索引补上；
3. 更旧的 v1 库：v2（明文 token 哈希化）与 v3 顺序都跑到（迁移清单真按版本递增执行）；
4. 幂等：v3 库再 init 不报错、不再重复"迁移"、索引仍在；
5. `ensure_auth_columns` 仍在（旧库兼容路径），且能真的重建 phone NOT NULL 表；
   源码里该函数带"历史地位"注释（新列禁止加进去）。

`server/` 不进 git，本卡改动无 commit 回滚点，靠 `_harden/` tar + 本文件。
"""
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import db  # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {desc}")
    else:
        FAIL += 1
        print(f"  [FAIL] {desc}")


# ── 旧库 fixture：复制"当时"的 DDL，故意不跟着 db.SCHEMA 走 ─────────────
# 这样以后 SCHEMA 再演进，本测试仍然在测真正的历史升级路径。
_V2_DDL = """
CREATE TABLE users(
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE, email TEXT UNIQUE,
    password_hash TEXT NOT NULL, salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user', quota_api_proxy INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', install_id TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE sessions(
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, device TEXT,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE TABLE proxy_usage(day TEXT NOT NULL, user_id INTEGER NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, user_id));
CREATE TABLE proxy_usage_failed(day TEXT NOT NULL, user_id INTEGER NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, user_id));
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE notices(user_id INTEGER NOT NULL, kind TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (user_id, kind));
"""


def _make_legacy_db(path: Path, version: int, plaintext_token: str | None = None) -> None:
    """造一个停在 `version` 的历史库（带一行数据，用于验证迁移不丢数据）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(_V2_DDL)
    conn.execute("INSERT INTO users(phone,email,password_hash,salt,role,status,install_id,created_at)"
                 " VALUES('13800000000','a@b.c','h','s','user','active','INST-1','2026-01-01 00:00:00')")
    conn.execute("INSERT INTO sessions(token,user_id,device,created_at,expires_at)"
                 " VALUES(?,1,'app/android','2026-01-01 00:00:00','2099-01-01 00:00:00')",
                 (plaintext_token or "x" * 64,))
    conn.execute("INSERT INTO notices(user_id,kind,created_at) VALUES(1,'old-client','2026-01-01 00:00:00')")
    conn.execute("INSERT INTO settings(key,value) VALUES('schema_version',?)", (str(version),))
    conn.commit()
    conn.close()


def _sql(path: Path, sql: str, args=()):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _idx_names(path: Path, table: str) -> set:
    return {r[1] for r in _sql(path, f"PRAGMA index_list({table})")}


def _schema_version(path: Path) -> int:
    rows = _sql(path, "SELECT value FROM settings WHERE key='schema_version'")
    return int(rows[0][0]) if rows else 0


tmp = Path(tempfile.mkdtemp(prefix="ff_dbv3_"))

# ── A. 新库直接到 v3 ────────────────────────────────────────────────
print("A. 新库直接到 v3")
fresh = tmp / "fresh.db"
db.init_db(fresh)
check("A1 新库 schema_version=3", _schema_version(fresh) == 3)
check("A2 新库有 idx_sessions_expires", "idx_sessions_expires" in _idx_names(fresh, "sessions"))
check("A3 新库有 idx_notices_user", "idx_notices_user" in _idx_names(fresh, "notices"))
check("A4 新库保留原有 idx_sessions_user", "idx_sessions_user" in _idx_names(fresh, "sessions"))

# ── B. v2 库升级（数据不丢）─────────────────────────────────────────
print("B. v2 库升级")
v2 = tmp / "v2.db"
_make_legacy_db(v2, 2)
check("B0 升级前版本=2 且无 v3 索引",
      _schema_version(v2) == 2 and "idx_sessions_expires" not in _idx_names(v2, "sessions"))
db.init_db(v2)
check("B1 升级后 schema_version=3", _schema_version(v2) == 3)
check("B2 升级后 sessions 索引补上", "idx_sessions_expires" in _idx_names(v2, "sessions"))
check("B3 升级后 notices 索引补上", "idx_notices_user" in _idx_names(v2, "notices"))
check("B4 迁移不动数据（users/sessions/notices 各 1 行）",
      len(_sql(v2, "SELECT id FROM users")) == 1
      and len(_sql(v2, "SELECT token FROM sessions")) == 1
      and len(_sql(v2, "SELECT kind FROM notices")) == 1)

# ── C. v1 库：v2 与 v3 顺序都跑到 ───────────────────────────────────
print("C. v1 库顺序迁移")
v1 = tmp / "v1.db"
_plain = "AbCd-_" + "e" * 37          # token_urlsafe(32) 形态：43 长且含非 hex 字符
_make_legacy_db(v1, 1, plaintext_token=_plain)
db.init_db(v1)
check("C1 v1 直升 v3", _schema_version(v1) == 3)
tok = _sql(v1, "SELECT token FROM sessions")[0][0]
check("C2 v2 迁移生效（明文 token 已哈希化）",
      len(tok) == 64 and re.fullmatch(r"[0-9a-f]{64}", tok) is not None and tok != _plain)
check("C3 v3 迁移也跑到（索引存在）",
      "idx_sessions_expires" in _idx_names(v1, "sessions")
      and "idx_notices_user" in _idx_names(v1, "notices"))

# ── D. 幂等：v3 库再 init ──────────────────────────────────────────
print("D. 幂等")
ok = True
try:
    db.init_db(fresh)
    db.init_db(v2)
except Exception as e:                       # pragma: no cover - 失败即报
    ok = False
    print(f"    异常：{e!r}")
check("D1 v3 库重复 init 不报错", ok)
check("D2 重复 init 后版本仍为 3", _schema_version(fresh) == 3 and _schema_version(v2) == 3)
check("D3 重复 init 后索引仍在且不重复",
      _idx_names(fresh, "sessions") >= {"idx_sessions_user", "idx_sessions_expires"}
      and _idx_names(fresh, "notices") >= {"idx_notices_user"})

# ── E. 迁移清单结构 ────────────────────────────────────────────────
print("E. 迁移清单")
vers = [v for v, _ in db._MIGRATIONS]
check("E1 版本号严格递增", vers == sorted(vers) and len(vers) == len(set(vers)))
check("E2 每项可调用", all(callable(fn) for _, fn in db._MIGRATIONS))
check("E3 清单含 v3 索引迁移", vers[-1] == 3)

# ── F. ensure_auth_columns 保留（旧库兼容）─────────────────────────
print("F. ensure_auth_columns（历史兼容路径）")
legacy = tmp / "legacy_users.db"
conn = sqlite3.connect(str(legacy))
conn.executescript("""
CREATE TABLE users(
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user', quota_api_proxy INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
CREATE TABLE sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, device TEXT,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE notices(user_id INTEGER NOT NULL, kind TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (user_id, kind));
""")
conn.execute("INSERT INTO users(phone,password_hash,salt,created_at)"
             " VALUES('13900000000','h','s','2026-01-01 00:00:00')")
conn.commit()
conn.close()
db.init_db(legacy)
db.ensure_auth_columns()
cols = {r[1]: r for r in _sql(legacy, "PRAGMA table_info(users)")}
check("F1 补出 install_id / email 列", "install_id" in cols and "email" in cols)
check("F2 重建成 phone 可空表（notnull 标志清零）", cols["phone"][3] == 0)
check("F3 重建不丢用户行", len(_sql(legacy, "SELECT id FROM users")) == 1)
check("F4 users 唯一索引已建",
      {"idx_users_install", "idx_users_email"} <= _idx_names(legacy, "users"))
src = (ROOT / "server" / "db.py").read_text(encoding="utf-8")
check("F5 ensure_auth_columns 带历史地位注释（新列走 _MIGRATIONS）",
      "历史地位" in src and "禁止再往本函数里加东西" in src)

# ── G. 索引服务的功能仍正常 ────────────────────────────────────────
print("G. 功能回归（索引不影响语义）")
db.init_db(fresh)
uid = db.create_user("v3@test.local", "hash", "salt", "INST-V3")
db.create_session("tok-alive", uid, "app/android", "2099-01-01 00:00:00")
db.create_session("tok-dead", uid, "app/android", "2000-01-01 00:00:00")
db.delete_expired_sessions()
toks = {r[0] for r in _sql(fresh, "SELECT token FROM sessions")}
check("G1 delete_expired_sessions 清掉过期行",
      db._hash_token("tok-dead") not in toks and db._hash_token("tok-alive") in toks)
first = db.mark_notice(uid, "old-client")
second = db.mark_notice(uid, "old-client")
check("G2 notices 去重仍生效（首记 True / 重复 False / 仅 1 行）",
      first is True and second is False
      and len(_sql(fresh, "SELECT kind FROM notices WHERE user_id=?", (uid,))) == 1)

print(f"\n结果：{PASS}/{PASS + FAIL} 通过")
sys.exit(1 if FAIL else 0)
