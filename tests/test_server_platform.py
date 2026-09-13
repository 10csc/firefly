# -*- coding: utf-8 -*-
"""服务器版平台标记硬置回归 —— 阶段 1 · 任务 1.2（E-2b，P0）

缺陷：`server/server_app.py` 原来用 `os.environ.setdefault("FIREFLY_SERVER", "1")`。
环境里只要存在 `FIREFLY_SERVER=""`（systemd 写空、容器透传空值、手滑 export 成空串），
setdefault 因"键已存在"而什么都不做，而 `app_config._discover_presets()` 判的是
`os.environ.get("FIREFLY_SERVER")` 的**真值** → 服务器版静默按本地版跑：
把各账号用户目录当角色包扫进全局注册表、平台标记回落 local。全程无报错。

本测试用**子进程**复现（平台标记必须在 app_config 导入前生效，同进程改不了），
在 `FIREFLY_SERVER=""` 下启动，断言：
1. 模块导入后标记被硬置为 "1"；
2. 用户数据目录里的自建包**不会**进入服务器版注册表（只认内置包）。

沙箱纪律：子进程 USER_DIR 指向临时目录（FIREFLY_ANDROID+FIREFLY_DATA_DIR 注入），
绝不触碰真实 user_data/。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_server_platform_"))
_user_root = _tmp / "user_data"
# 造一个"本地版才会有"的自建角色包（服务器版绝不允许它进全局注册表）
_pack = _user_root / "custom_t01" / "character"
_pack.mkdir(parents=True, exist_ok=True)
(_pack / "preset.json").write_text(json.dumps({
    "id": "custom_t01", "name": "自建测试包", "char_name": "测试角色",
    "user_name": "测试用户", "presentation": "sticker"}, ensure_ascii=False), encoding="utf-8")

_CHILD = r'''
import os, sys
sys.path.insert(0, r"{app}")
sys.path.insert(0, r"{server}")
import server_app                      # 触发模块级平台标记硬置
import modules.app_config as cfg       # 由 server_app 间接导入，此处已带正确标记
print("MARK=" + repr(os.environ.get("FIREFLY_SERVER")))
print("USERDIR=" + str(cfg.USER_DIR))
print("PRESETS=" + ",".join(sorted(cfg.PRESETS)))
print("MODES=" + ",".join(cfg.MODES))
'''.format(app=str(ROOT / "app"), server=str(ROOT / "server"))

print("=== A. 子进程以 FIREFLY_SERVER=\"\"（空串）启动 ===")
env = dict(os.environ)
env["FIREFLY_SERVER"] = ""            # 关键：空串而不是删除（删除时 setdefault 反而能生效）
env["FIREFLY_ANDROID"] = "1"          # 沙箱注入：USER_DIR = FIREFLY_DATA_DIR/user_data
env["FIREFLY_DATA_DIR"] = str(_tmp)
env.pop("FIREFLY_PROXY_KEY", None)
proc = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True,
                      encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
_out = (proc.stdout or "") + "\n" + (proc.stderr or "")
check("A0 子进程正常退出", proc.returncode == 0)
if proc.returncode != 0:
    print(_out[-1500:])

_lines = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in (proc.stdout or "").splitlines()
          if "=" in ln}

print("=== B. 平台标记被硬置 ===")
check("B1 FIREFLY_SERVER == '1'（空串被覆盖）", _lines.get("MARK") == "'1'")
check("B2 子进程 USER_DIR 是临时沙箱",
      _lines.get("USERDIR", "").replace("\\", "/").endswith("user_data")
      and str(_tmp).split("\\")[-1] in _lines.get("USERDIR", ""))

print("=== C. 服务器版注册表只认内置包 ===")
presets = [x for x in _lines.get("PRESETS", "").split(",") if x]
check("C1 自建包未进入注册表（未降级为本地版）", "custom_t01" not in presets)
check("C2 内置包照常在（story/haruno）", "story" in presets and "haruno" in presets)
check("C3 MODES 同样不含自建包", "custom_t01" not in _lines.get("MODES", "").split(","))

print("=== D. 源码守卫：硬置取代 setdefault、main() 不再重复置 ===")
src = (ROOT / "server" / "server_app.py").read_text(encoding="utf-8")
app_src = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
check("D1 不再使用 setdefault 设平台标记",
      'setdefault("FIREFLY_SERVER"' not in src)
check("D2 模块级硬置存在", 'os.environ["FIREFLY_SERVER"] = "1"' in src)
# 2026-09-13（阶段 2.4 拆分后）：入口只做硬置 + re-export，app_config 的导入搬进了 server/app.py。
# 守卫意图不变 = **硬置必须早于任何会拉进 app_config 的导入**（现在是 `from app import ...`）。
_hard = src.index('os.environ["FIREFLY_SERVER"] = "1"')
_pull = src.find("from app import")
check("D3 硬置行在拉入 app（进而 app_config）的导入之前", 0 <= _hard < _pull)
check("D3b 入口不再直接导入 app_config（由 app.py 持有）",
      "from modules import app_config" not in src)
check("D3c app.py 确实持有 app_config 导入（硬置必须先于它）",
      "from modules import app_config" in app_src)
_main = app_src[app_src.index("def main():"):]
check("D4 main() 里不再重复硬置（只留确认打印）",
      'os.environ["FIREFLY_SERVER"] = "1"' not in _main and "[平台] FIREFLY_SERVER=" in _main)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
