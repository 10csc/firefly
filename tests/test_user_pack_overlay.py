# -*- coding: utf-8 -*-
"""按用户解析的包视图（共创平台 M1.5）回归

背景：`PRESETS/MODES` 是进程级全局，服务器模式每账号独立 `user_data/{uid}/`，
广场安装的角色卡必须**按请求上下文**解析，否则会出现"A 的卡出现在 B 的列表里"
或"合法 mode 被判非法 → 静默回退默认包 → 数据写进别的角色目录"。

钉死的事：
- 叠加层可见性与**跨用户隔离**；
- **`mode_root`/`pack_root` 落在该用户自己的目录**（本文件最重要的一条）；
- 无用户上下文（本地版 / 启动期线程）时行为与改动前完全一致；
- `reload_presets()` 清缓存（刚装的卡不用重启就能用）；
- 归档（archived）后从可用列表消失；
- 内置包在所有上下文下都可用。

沙箱：全部数据在临时目录，绝不触碰真实 user_data/。
"""
import json
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

import modules.app_config as cfg   # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_pack_overlay_"))
cfg.USER_DIR = _tmp / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from core import presets as P          # noqa: E402
from core import userctx               # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


def preset(pid, name, char_name):
    return {"id": pid, "name": name, "char_name": char_name, "user_name": "你",
            "presentation": "sticker", "desc": f"{name} 的卡", "schema": 1}


def install(user_dir: Path, pid, name, char_name):
    """模拟"广场安装"：写 {user}/{pid}/character/preset.json + 登记进该用户清单。"""
    d = user_dir / pid / "character"
    d.mkdir(parents=True, exist_ok=True)
    (d / "preset.json").write_text(json.dumps(preset(pid, name, char_name), ensure_ascii=False),
                                   encoding="utf-8")
    tok = userctx.set_user_context(user_dir)
    try:
        cfg.pack_registry().register(pid, source="plaza", parsed=preset(pid, name, char_name))
    finally:
        userctx.reset_user_context(tok)


def in_ctx(user_dir):
    return userctx.set_user_context(user_dir)


def out_ctx(tok):
    userctx.reset_user_context(tok)


USER_A = cfg.USER_DIR / "1"
USER_B = cfg.USER_DIR / "2"
USER_A.mkdir(parents=True, exist_ok=True)
USER_B.mkdir(parents=True, exist_ok=True)
install(USER_A, "plaza_a", "甲角色", "小甲")
install(USER_B, "plaza_b", "乙角色", "小乙")

print("== 1. 无用户上下文（本地版 / 启动期线程）行为不变 ==")
check("user_packs() 为空", P.user_packs() == {})
check("all_packs() 就是 PRESETS", P.all_packs() is P.PRESETS)
check("all_modes() 等于 MODES", P.all_modes() == P.MODES)
check("广场卡在无上下文时不可用", not P.valid_mode("plaza_a"))
check("内置包在无上下文时可用", P.valid_mode("story"))

print("== 2. 用户 A 上下文：只看得到自己的卡 ==")
tok = in_ctx(USER_A)
check("A 能看到 plaza_a", P.valid_mode("plaza_a"))
check("A 看不到 B 的 plaza_b", not P.valid_mode("plaza_b"))
check("plaza_a 不在全局 MODES 里（未污染全局）", "plaza_a" not in P.MODES)
check("all_modes() 含 plaza_a", "plaza_a" in P.all_modes())
check("pack_meta 取到卡元数据", P.pack_meta("plaza_a").get("char_name") == "小甲")
check("char_name 走卡自己的设定", cfg.char_name("plaza_a") == "小甲")
check("user_name 走卡自己的设定", cfg.user_name("plaza_a") == "你")
check("自定义标记存在", P.pack_meta("plaza_a").get("custom") is True)
check("内置包仍可用", P.valid_mode("story") and P.pack_meta("story").get("char_name"))
out_ctx(tok)

print("== 3. 用户 B 上下文：互不可见 ==")
tok = in_ctx(USER_B)
check("B 能看到自己的 plaza_b", P.valid_mode("plaza_b"))
check("B 看不到 A 的 plaza_a", not P.valid_mode("plaza_a"))
check("B 的 all_modes() 不含 plaza_a", "plaza_a" not in P.all_modes())
out_ctx(tok)

print("== 4. 路径解析必须落在本人目录（数据安全核心）==")
tok = in_ctx(USER_A)
check("A 的 mode_root 指向本人目录", cfg.mode_root("plaza_a") == USER_A / "plaza_a")
check("A 的 pack_root 指向本人目录", cfg.pack_root("plaza_a") == USER_A / "plaza_a")
check("A 的 character 目录指向本人", cfg.mode_character_dir("plaza_a") == USER_A / "plaza_a" / "character")
out_ctx(tok)
tok = in_ctx(USER_B)
check("B 用的 plaza_a 非法 → 回退默认包（不写进 A 的目录）",
      cfg.mode_root("plaza_a") == USER_B / P.DEFAULT_MODE)
check("B 的 mode_root 指向 B 自己", str(cfg.mode_root("plaza_b")).startswith(str(USER_B)))
out_ctx(tok)

print("== 5. 缓存与重扫 ==")
tok = in_ctx(USER_A)
_ = P.user_packs()                       # 先填一次缓存
install(USER_A, "plaza_c", "丙角色", "小丙")
check("未重扫时新卡还看不到（缓存生效）", not P.valid_mode("plaza_c"))
cfg.reload_presets()
check("reload_presets 后新卡立刻可见", P.valid_mode("plaza_c"))
out_ctx(tok)

print("== 6. 归档后从可用列表消失 ==")
tok = in_ctx(USER_A)
check("归档前可用", P.valid_mode("plaza_c"))
cfg.pack_registry().set_state("plaza_c", "archived")
cfg.reload_presets()
check("归档后不可用", not P.valid_mode("plaza_c"))
check("归档后不在 all_modes()", "plaza_c" not in P.all_modes())
cfg.pack_registry().set_state("plaza_c", "active")
cfg.reload_presets()
check("恢复后重新可用", P.valid_mode("plaza_c"))
out_ctx(tok)

print("== 7. 清单损坏不炸（退化为没有用户包）==")
tok = in_ctx(USER_A)
(cfg.USER_DIR / "1" / "packs.json").write_text("{坏掉的 json", encoding="utf-8")
cfg.reload_presets()
check("坏清单下依然可用内置包", P.valid_mode("story"))
check("坏清单下不抛异常", isinstance(P.all_modes(), tuple))
out_ctx(tok)

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
