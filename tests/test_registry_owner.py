# -*- coding: utf-8 -*-
"""REGISTRY owner 声明一致性（阶段 3 · 任务 3.1）

背景：`storage.REGISTRY` 是"数据在哪、什么策略"的唯一真相源，但 13 条注册项**没写是谁在写它**
——于是"改数据策略前该找哪个模块""这份数据还有没有主"只能靠 grep 猜。

本卡只做**声明**（零行为变化）：每条加 `owner`（写这份数据的模块），并加一致性测试：
owner 必须是**真实存在的模块**；且每个 owner 都得是 app 内的模块（防写成"某人"这种空话）。
后续卡（3.2/3.8）会拿 owner 做工具链与文档的落点。
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
from modules import storage as st   # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


def _resolve(owner: str):
    """owner 字符串 → 源码文件路径（app/ 下）。找不到返回 None。

    ⚠️ 白名单 = **app/ 下的顶层包**。新增顶层包必须在这里登记，
    否则它的 owner（如 `voice.store`）会被当成"app/voice.store.py"而解析失败。
    2026-09-18 增补 `voice`（语音插件独立子系统，见 docs/工具/tts.md）。
    """
    parts = owner.split(".")
    if parts[0] in ("modules", "core", "api", "infra", "domain", "tools", "voice"):
        return ROOT / "app" / Path(*parts).with_suffix(".py")
    return ROOT / "app" / (owner + ".py")


print("=== A. 每条注册项都有 owner 声明 ===")
_missing = [k for k, v in st.REGISTRY.items() if not v.get("owner")]
check(f"A1 全部 {len(st.REGISTRY)} 条注册项都声明了 owner", not _missing)
if _missing:
    print("    缺 owner:", _missing)

print("=== B. owner 指向真实存在的模块 ===")
_bad = []
for key, spec in st.REGISTRY.items():
    owner = spec.get("owner") or ""
    fp = _resolve(owner)
    if not owner or fp is None or not fp.is_file():
        _bad.append((key, owner))
check("B1 每个 owner 都能解析到 app/ 下的真实 .py", not _bad)
if _bad:
    print("    解析失败:", _bad)

print("=== C. owner 不是泛称（必须精确到模块）===")
check("C1 owner 都带包前缀或为 app 顶层模块名（至少 1 个点或已存在同名单文件）",
      all(("." in (s.get("owner") or "")) or (ROOT / "app" / ((s.get("owner") or "x") + ".py")).is_file()
          for s in st.REGISTRY.values()))
check("C2 没有模糊词（如 misc/other/todo/某人）",
      not any((s.get("owner") or "").lower() in ("misc", "other", "todo", "unknown", "")
              for s in st.REGISTRY.values()))

print("=== D. 关键数据的所有权归属正确（防手滑写错模块）===")
_EXPECT = {
    "conversation": "modules.conversation_store",
    "memory": "modules.memory_manager",
    "favorites": "modules.favorite_store",
    "stickers": "domain.stickers.picker",
    "config": "core.config",
    "auth": "modules.auth_store",
    "setting_fix": "modules.setting_fix_store",
    "images": "routes_common",
    "character": "routes_pack",
}
_wrong = {k: (st.REGISTRY[k].get("owner"), v) for k, v in _EXPECT.items()
          if st.REGISTRY[k].get("owner") != v}
check("D1 9 条关键数据的 owner 与实现方一致", not _wrong)
if _wrong:
    print("    不一致(实际, 期望):", _wrong)

print("=== E. 声明不影响查询行为（零行为变化）===")
check("E1 sync 策略查询结果不变", st.sync_policy("data/conversation.jsonl") == "merge-jsonl"
      and st.sync_policy("character/core.md") == "newest"
      and st.sync_policy("config.json") == "exclude")
check("E2 媒体/敏感判定不变",
      st.is_media("stickers/a.webp") and st.is_sensitive("config.json")
      and not st.is_media("data/conversation.jsonl"))
check("E3 同步范围判定不变",
      st.is_syncable("data/conversation.jsonl") and not st.is_syncable("config.json"))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
