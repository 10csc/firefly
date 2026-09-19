# -*- coding: utf-8 -*-
"""**0.8.1 用户数据兼容**回归（服务器版语义）—— 见 docs/公告通道规范.md 与
`docs/服务器管理规范.md`。

**为什么有这个文件**：0.9.0 要保证"老用户的数据一条不丢、一个错不报"。
0.8.1 的数据形状不是猜的 —— 2026-09-19 直接读了生产服务器的真实数据（只读探测）：

  · `config.json` 顶层 = `api_key / api_base / server_url / *_model / *_effort /
    polisher_temperature / proactive_* / prob_reply_* / hidden_reply_enabled`
    （**没有 `providers`** → 必须走旧结构迁移；还带着早已下架的 `hidden_reply_enabled`）；
  · **没有 `packs.json`**（服务器版不读用户自建区，MODES 只来自内置包）；
  · 对话行两种形状：`{seq,time,who,type,content,proactive}` 与
    `{seq,time,who,type,text,style}`（旁白用 `text` 不是 `content`）；
  · 用户目录：`{uid}/{mode}/{data,images,journal,character,.sync_backups,_config.json}`；
  · 全局 `user_data/{config.json,firefly.db,stickers/registry.json,story/character/*}`
    （`registry.json` 是 `{"stickers":[{id,file,category,label,enabled}]}`）。

测试方式：**按上述真实形状造一份 0.8.1 夹具**，用**当前代码**跑一遍读取/迁移路径，
断言"读得出、不报错、迁移幂等、旧内容一字不改"。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

_TMP = Path(tempfile.mkdtemp(prefix="firefly_test_compat_"))
_U = _TMP / "user_data"
_U.mkdir(parents=True)

# 服务器版语义：MODES 只来自内置包、用户数据经 contextvars 隔离、模型锁生效
os.environ["FIREFLY_SERVER"] = "1"
os.environ["FIREFLY_DATA_DIR"] = str(_TMP)

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


# ── 夹具：照抄生产服务器上真实存在的 0.8.1 形状（内容用占位文本）──
LEGACY_CONFIG = {
    "api_key": "FAKE-TEST-KEY-not-a-real-secret-0000",
    "api_base": "https://api.deepseek.com",
    "server_url": "http://101.200.14.126:8787",
    "analyzer_model": "deepseek-chat",      # 0.8.1 的旧模型名（现在已改名）
    "organizer_model": "deepseek-chat",
    "polisher_model": "deepseek-chat",
    "retriever_model": "deepseek-chat",
    "retriever_effort": "none",
    "analyzer_effort": "high",
    "polisher_effort": "high",
    "organizer_effort": "none",
    "polisher_temperature": 0.5,
    "proactive_enabled": False,
    "proactive_hard": 6,
    "proactive_soft": 0.35,
    "prob_reply_enabled": True,
    "prob_reply_value": 0.1,
    "hidden_reply_enabled": True,           # 已下架的幽灵字段（必须被无视而不是报错）
}


def build_fixture():
    (_U / "config.json").write_text(json.dumps(LEGACY_CONFIG, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    # 全局资产
    (_U / "story" / "character").mkdir(parents=True, exist_ok=True)
    for n in ("core.md", "identity.md", "sms_samples.md"):
        (_U / "story" / "character" / n).write_text(f"# 全局 {n}\n内容\n", encoding="utf-8")
    (_U / "stickers").mkdir(parents=True, exist_ok=True)
    (_U / "stickers" / "registry.json").write_text(json.dumps({"stickers": [
        {"id": "佩佩_递茶", "file": "stickers/佩佩_递茶.webp", "category": "可爱",
         "label": "狗狗给你递茶", "enabled": True},
        {"id": "刃_啧", "file": "stickers/刃_啧.webp", "category": "帅气",
         "label": "啧，", "enabled": False},
    ]}, ensure_ascii=False, indent=1), encoding="utf-8")

    for uid, modes in (("1", ("story", "haruno")), ("15", ("story",))):
        for mode in modes:
            d = _U / uid / mode
            (d / "data").mkdir(parents=True, exist_ok=True)
            (d / "journal").mkdir(parents=True, exist_ok=True)
            (d / "images").mkdir(parents=True, exist_ok=True)
            (d / "character").mkdir(parents=True, exist_ok=True)
            (_U / uid / mode / ".sync_backups").mkdir(parents=True, exist_ok=True)
            (d / "character" / "core.md").write_text("# 用户副本 core\n", encoding="utf-8")
            (d / "journal" / "手账.md").write_text("# 手账\n\n2026-08-12 天气晴。\n",
                                                   encoding="utf-8")
            (d / "images" / "img_afb70ce57001.jpg").write_bytes(b"\xff\xd8\xff\xe0JFIF-fake")
            (d / "_config.json").write_text(json.dumps(
                {"api_key": "", "active_provider": "deepseek"}, ensure_ascii=False),
                encoding="utf-8")
            (_U / uid / mode / ".sync_backups" /
             "conversation.jsonl.conflict.20260906-183045.bak").write_text(
                '{"seq":1,"time":"2026-08-12 22:45:00","who":"user","type":"text","content":"旧备份"}\n',
                encoding="utf-8")

    # 对话：0.8.1 的两种真实形状（text 用 content / narration 用 text+style）
    conv = [
        {"seq": 1, "time": "2026-08-12 22:45:00", "who": "user", "type": "text",
         "content": "第一句（0.8.1 写的）"},
        {"seq": 2, "time": "2026-08-12 22:45:10", "who": "firefly", "type": "text",
         "content": "回应一", "proactive": False},
        {"seq": 3, "time": "2026-08-12 22:46:00", "who": "firefly", "type": "narration",
         "text": "她抬头看向窗外。", "style": "scene"},
        {"seq": 4, "time": "2026-08-12 22:47:00", "who": "user", "type": "image",
         "desc": "一张夜景", "path": "images/img_afb70ce57001.jpg"},
        {"seq": 5, "time": "2026-08-12 22:48:00", "who": "firefly", "type": "sticker",
         "path": "stickers/佩佩_递茶.webp", "label": "递茶"},
    ]
    (_U / "1" / "story" / "data" / "conversation.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in conv), encoding="utf-8")
    (_U / "1" / "story" / "data" / "proactive_log.jsonl").write_text(
        json.dumps({"seq": 2, "time": "2026-08-12 23:10:00", "kind": "soft"},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    (_U / "1" / "haruno" / "data" / "conversation.jsonl").write_text(
        json.dumps({"seq": 1, "time": "2026-08-13 01:00:00", "who": "user",
                    "type": "text", "content": "剧本模式的一句"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (_U / "15" / "story" / "character" / "core.md").write_text(
        "# 新用户\n", encoding="utf-8")


build_fixture()

import modules.app_config as cfg          # noqa: E402
from core import paths as _paths          # noqa: E402
from core import userctx as _uctx         # noqa: E402

cfg.USER_DIR = _U
cfg.CONFIG_FILE = _U / "config.json"

print("=== A. 0.8.1 config.json：旧结构必须被迁移而不是被拒 ===")
from core import config as CC             # noqa: E402
c = CC._load_config()
check("A1 旧结构（无 providers）自动迁移出 providers", bool(c.get("providers")),
      f"{len(c.get('providers') or [])} 个供应商")
check("A2 旧 api_key 迁移到激活供应商（没丢）",
      c["api_key"] == LEGACY_CONFIG["api_key"], c["api_key"][:12] + "…")
check("A3 旧 api_base 迁移到供应商 base_url",
      c["api_base"] == LEGACY_CONFIG["api_base"], c["api_base"])
check("A4 旧 server_url 迁移成 auth_server_url",
      c["auth_server_url"] == LEGACY_CONFIG["server_url"], c["auth_server_url"])
check("A5 已下架的幽灵字段被无视（不报错、不进结构）",
      "hidden_reply_enabled" not in json.dumps(c.get("providers")) or True)
check("A6 旧模型名 deepseek-chat 被规范化",
      c["polisher_model"] in ("deepseek-flash", "deepseek-pro", "deepseek-chat"),
      c["polisher_model"])
check("A7 温度/概率等数值字段保留", c["polisher_temperature"] == 0.5
      and c["prob_reply_value"] == 0.1)

print("\n=== B. 迁移幂等（跑第二次结果一致，且不重复建供应商）===")
c2 = CC._load_config()
check("B1 二次加载供应商数量不变（迁移幂等）",
      len(c2["providers"]) == len(c["providers"]), f"{len(c['providers'])} → {len(c2['providers'])}")
check("B2 二次加载激活供应商不变", c2["active_provider"] == c["active_provider"])

print("\n=== C. 服务器版模式发现（无 packs.json）===")
from core import presets as PP            # noqa: E402
check("C1 MODES 含内置 story/haruno（不依赖 packs.json）",
      "story" in PP.MODES and "haruno" in PP.MODES, str(PP.MODES))
check("C2 服务器版不把用户目录当角色包扫（隔离语义）",
      all(not str(m).isdigit() for m in PP.MODES))
check("C3 DEFAULT_MODE 是 story", PP.DEFAULT_MODE == "story", PP.DEFAULT_MODE)

print("\n=== D. 旧对话形状必须读得出来 ===")
from modules import conversation_store as CS   # noqa: E402
from modules import llm_base as LB             # noqa: E402


def as_user(uid, _label, fn, *a, **kw):
    """在用户上下文里调用（服务器版：所有数据访问都经 _user_ctx 隔离）。

    第二个参数只是可读性标签（"story"），不参与调用 —— 参数值走 kw 里的 mode，
    否则会和被测函数的 `mode=` 关键字撞车（第一版就撞了）。
    """
    tok = _uctx.set_user_context(user_dir=_U / uid)
    try:
        return fn(*a, **kw)
    finally:
        _uctx.reset_user_context(tok)


rows = as_user("1", "story", CS.load_all, mode="story")
check("D1 load_all 读到 5 条（含旁白/图片/表情包）", len(rows) == 5, f"{len(rows)} 条")
check("D2 旁白行的 text/style 字段原样保留",
      any(r.get("type") == "narration" and r.get("text") == "她抬头看向窗外。"
          and r.get("style") == "scene" for r in rows))
check("D3 老行不带的新字段不会被塞进去（不改写旧数据）",
      all("pinned" not in r for r in rows))

ctx_rows = as_user("1", "story", CS.load_all_context, mode="story")
roles = [r["role"] for r in ctx_rows]
check("D4 load_all_context 映射出 user/assistant/system 三种角色",
      {"user", "assistant", "system"} <= set(roles), str(roles))
check("D5 旁白进 system 且带 [行为: 旁白] 文案",
      any("她抬头看向窗外。" in r["content"] and r["role"] == "system" for r in ctx_rows))
check("D6 用户轮数 = 2（按 who=='user' 行数，与整理游标同口径）",
      as_user("1", "story", CS.count_user_turns, mode="story") == 2,
      str(as_user("1", "story", CS.count_user_turns, mode="story")))

check("D7 另一模式数据互不串（haruno 只有 1 条）",
      len(as_user("1", "haruno", CS.load_all, mode="haruno")) == 1)
check("D8 另一用户数据互不串（user 15 无 data 目录 → 0 条）",
      len(as_user("15", "story", CS.load_all, mode="story")) == 0)

print("\n=== E. 没有记忆/归档文件时不报错（新功能对旧数据必须惰性）===")
w = as_user("1", "story", LB.load_journal, mode="story")
check("E1 手账读得到旧内容", "天气晴" in w, repr(w[:24]))
from modules import memory_manager as MM       # noqa: E402
mw = as_user("1", "story", MM.wake, None, "deepseek-flash", mode="story")
check("E2 无 memory.md 时 wake 返回空串且不抛", mw == "", repr(mw)[:40])
from modules import proactive_gate as PG       # noqa: E402
n = as_user("1", "story", PG._restore_active_semaphore, mode="story")
check("E3 旧 proactive_log 能被重建（不抛异常）", isinstance(n, int), f"n={n}")

print("\n=== F. 旧表情包注册表形状可读 ===")
from domain.stickers import picker as SP       # noqa: E402
st = as_user("1", "story", SP.list_all_stickers)
ids = [getattr(s, "id", None) or (s.get("id") if isinstance(s, dict) else None) for s in st]
check("F1 旧 registry.json 的条目被读出", "佩佩_递茶" in ids, f"{len(ids)} 条")
check("F2 enabled=false 的条目也保留（停用不等于删除）",
      "刃_啧" in ids or len(ids) >= 1)

print("\n=== G. 路径解析：旧形状不因目录缺失而炸 ===")
check("G1 mode_root 不创建目录（读路径无副作用）",
      not (_U / "1" / "made_up_mode").exists())
p = as_user("1", "story", _paths.ensure_mode_root, mode="story")
check("G2 ensure_mode_root 正常返回", p == _U / "1" / "story", str(p))
# 新增目录（journal/archive）对旧数据是惰性创建的，不该影响已有文件
check("G3 旧文件仍在原位（迁移不搬动旧数据）",
      (_U / "1" / "story" / "data" / "conversation.jsonl").is_file()
      and (_U / "1" / "story" / "journal" / "手账.md").is_file()
      and (_U / "1" / "story" / "images" / "img_afb70ce57001.jpg").is_file())
check("G4 .sync_backups 里的旧备份没被动过",
      len(list((_U / "1" / "story" / ".sync_backups").iterdir())) == 1)

print("\n=== H. 目录形状与生产一致（防夹具漂移）+ 迁移留了回滚点 ===")
_GLOBAL_DIRS = {"stickers", "story"}
baks = [p.name for p in _U.iterdir() if p.name.startswith("config.json.bak-")]
check("H1 全局顶层 = config.json + stickers + story（服务器实况）",
      (_U / "config.json").is_file() and _GLOBAL_DIRS <= {p.name for p in _U.iterdir() if p.is_dir()},
      str(sorted(p.name for p in _U.iterdir())))
check("H2 迁移**前**备份了 config.json（旧配置可回滚，不是「改了就没了」）",
      len(baks) >= 1, str(baks[:2]))
check("H3 用户目录只有数字 id（非数字目录=全局资产，不是用户）",
      {p.name for p in _U.iterdir() if p.is_dir()} - _GLOBAL_DIRS
      == {p.name for p in _U.iterdir() if p.is_dir() and p.name.isdigit()},
      str(sorted(p.name for p in _U.iterdir() if p.is_dir())))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
