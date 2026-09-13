# -*- coding: utf-8 -*-
"""import 期副作用清零守卫（阶段 2 · 任务 2.6）

两个缺陷：
1. `memory_manager` 在 **import 期**调用 `_migrate_legacy()` → 任何 `import memory_manager`
   都会在盘上建出 `user_data/{mode}/data/`（甚至拷历史文件）；
2. `paths.mode_root()` 自带 `mkdir` → 纯读端点（GET /modes、/config）也会凭空建目录，
   且"任何路径计算都落目录"让读代码看不出哪些操作会动盘。

本文件守四件事：
1. **import 零落盘**：子进程里用空数据根导入 routes（会拉起 app_config/orchestrator/
   memory_manager 等整条链），导入后数据根必须仍是空的；
2. **纯读端点零落盘**：/config、/modes 走完后数据根仍空；
3. 显式写路径照常工作（会话追加 / 手账保存 / 包级配置 / 记忆保存）——证明"不建目录"没有
   把写路径一起弄坏（写路径本就自建目录，本卡的验收要求）；
4. `ensure_mode_root()` 确实是那个"会建目录"的入口。

沙箱纪律：全部在临时目录里跑，绝不触碰真实 user_data。
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


def _tree(root: Path) -> set:
    """数据根下的全部条目（相对路径），用于断言"什么都没多出来"。"""
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


print("=== A. import 整条链不得落盘（子进程） ===")
_sandbox = Path(tempfile.mkdtemp(prefix="firefly_test_noside_"))
_data_root = _sandbox / "user_data"
_env = dict(os.environ)
_env.update({"FIREFLY_ANDROID": "1", "FIREFLY_DATA_DIR": str(_sandbox),
             "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
_CHILD = ("import sys; sys.path.insert(0, r'{app}');\n"
          "import routes, orchestrator, modules.memory_manager as mm;\n"
          "import modules.app_config as cfg;\n"
          "print('DATA', cfg.USER_DIR)").format(app=str(ROOT / "app"))
p = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, encoding="utf-8",
                   errors="replace", env=_env, cwd=str(ROOT))
check("A0 子进程导入成功", p.returncode == 0 and "DATA" in (p.stdout or ""))
if p.returncode != 0:
    print("   ", (p.stdout or "") + (p.stderr or "")[-400:])
check("A1 导入后数据根不存在或为空（import 期零落盘）", _tree(_data_root) == set())
if _tree(_data_root):
    print("    多出来的条目:", sorted(_tree(_data_root))[:8])

print("=== B. 纯读端点零落盘 ===")
sys.path.insert(0, str(ROOT / "app"))
import modules.app_config as cfg            # noqa: E402

cfg.USER_DIR = _data_root
cfg.CONFIG_FILE = _data_root / "config.json"


class FakeH:
    def __init__(self, path="/", body=None):
        self.path = path
        self.data = None
        self.status = 200
        self.wfile = __import__("io").BytesIO()
        self.headers = {}
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            self.headers = {"Content-Length": str(len(raw))}
            self.rfile = __import__("io").BytesIO(raw)

    def _json(self, data, status=200):
        self.data = data
        self.status = status

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


import routes   # noqa: E402

h = FakeH("/config")
routes.get_config(h)
check("B0 GET /config 返回成功", isinstance(h.data, dict) and h.data)
h = FakeH("/modes")
routes.get_modes(h)
check("B1 GET /modes 返回包清单", isinstance(h.data, dict) and h.data.get("modes"))
check("B2 两个纯读端点走完后数据根仍为空（无 user_data/{mode}/... 空目录）",
      _tree(_data_root) == set())
if _tree(_data_root):
    print("    多出来的条目:", sorted(_tree(_data_root))[:8])

print("=== C. ensure_mode_root 才是会建目录的入口 ===")
d = cfg.ensure_mode_root("story")
check("C1 ensure_mode_root 建出目录并返回该路径",
      d.is_dir() and d == cfg.mode_root("story") and d.name == "story")
check("C2 数据根现在才有内容", "story" in _tree(_data_root))

print("=== D. 写路径照常工作（自建目录，不依赖 mode_root 的旧 mkdir） ===")
_fresh = _sandbox / "fresh_user_data"
cfg.USER_DIR = _fresh          # 全新（不存在）的数据根：模拟首启
cfg.CONFIG_FILE = _fresh / "config.json"
try:
    from modules.conversation_store import append_message, load_recent
    append_message("user", {"type": "text", "content": "副作用清零后的第一句"}, mode="story")
    check("D1 会话追加成功且落盘", (_fresh / "story" / "data" / "conversation.jsonl").is_file()
          and load_recent(limit=5, mode="story"))

    from modules.favorite_store import add_favorite, list_favorites
    add_favorite("story", {"who": "firefly", "type": "text", "content": "收藏我", "seq": 1})
    check("D2 收藏写入成功", (_fresh / "story" / "data" / "favorites.json").is_file()
          and list_favorites("story"))

    from modules.app_config import set_pack_cfg, pack_cfg
    set_pack_cfg("story", {"enabled": False, "hard": 3})
    check("D3 包级配置写入成功", (_fresh / "story" / "data" / "proactive.json").is_file()
          and pack_cfg("story", "hard") == 3)

    from modules.memory_manager import _journal_file
    h = FakeH(body={"content": "# 手账\n\n副作用清零后的第一条。\n", "mode": "story"})
    routes.save_journal(h)
    check("D4 手账保存成功（api.chat 的写路径）",
          h.data.get("ok") is True and _journal_file("story").is_file())
except Exception as e:
    check(f"D 写路径异常: {type(e).__name__}: {e}", False)

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
