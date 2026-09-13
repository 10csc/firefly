# -*- coding: utf-8 -*-
"""panels.js 按面板拆分（阶段 2 · 任务 2.5）的结构守卫

拆分：`app/static/js/panels.js`（842 行）→ 保留 `js/panels.js`（面板外壳：头像选择 + 汉堡菜单/
抽屉 + tab 切换）+ `js/panels/{packs,data,debug}.js`：
- packs  角色包面板（表情包管理 + 包详情页：人设/主动消息/专属表情包/头像封面）
- data   资料面板（状态 / 收藏 / 用户记忆 / 设定文件 / 手账）
- debug  调试面板（请求记录 / 流水线）

**与计划书的冲突（以代码为准，已记入执行日志）**：卡片写 `panels/data.js`=备份/快照/同步，
但代码事实是那部分 UI 在 `chat.js`（导入/导出/快照）与 `sync.js`（增量同步），panels.js 内没有。
故 data.js 落为上面的"资料面板"。

守四件事：
1. 对外导出仍在外壳（6 个模块从 `./panels.js` import 的 8 个名字，少一个就是 ESM 报错）；
2. 各面板内容归属正确（外壳不含包详情页/请求记录；packs 含包详情页；data/debug 各就各位）；
3. **门禁覆盖子目录**：`build_frontend_bundle._module_names()` 必须含 `panels/*`，
   且与 ORDER 逐字相等（否则拆进子目录的模块会被门禁"看不见"）；
4. bundle 三副本含新模块且顺序正确（漂移由 sync_frontends --check 兜底）。

真实浏览器端到端见 tests/manual_ui_panels.py（14 步全过、零 JS 报错）。
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js"
SHELL = JS / "panels.js"
PACKS = JS / "panels" / "packs.js"
DATA = JS / "panels" / "data.js"
DEBUG = JS / "panels" / "debug.js"
BUNDLE = JS / "bundle.js"

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "app"))
import build_frontend_bundle as bfb   # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}")
    else:
        FAIL += 1
        print(f"  X {desc}")


shell = SHELL.read_text(encoding="utf-8")
packs = PACKS.read_text(encoding="utf-8")
data = DATA.read_text(encoding="utf-8")
debug = DEBUG.read_text(encoding="utf-8")
bundle = BUNDLE.read_text(encoding="utf-8")

print("=== A. 四个文件都在，且各自 <500 行 ===")
for fp, name in ((SHELL, "shell"), (PACKS, "packs"), (DATA, "data"), (DEBUG, "debug")):
    n = len(fp.read_text(encoding="utf-8").splitlines())
    check(f"A  {name:6} 存在且行数合理（{n} 行）", fp.is_file() and n < 500)

print("=== B. 对外导出仍在外壳（其他 6 个模块 import 的 8 个名字）===")
_EXPORTS = ["openMenu", "openSettings", "closeMenu", "TB_AVATARS", "openAvatarPicker",
            "tbChoice", "closeFeedback", "closeSettings"]
_missing = [n for n in _EXPORTS
            if not re.search(r"export\s+(?:const|let|function)\s+" + n + r"\b", shell)]
check("B1 8 个对外导出全在外壳", not _missing)
if _missing:
    print("    缺失:", _missing)
check("B2 面板模块不再重复导出这些名字（避免 ESM 歧义）",
      not any(re.search(r"export\s+(?:const|let|function)\s+" + n + r"\b", packs + data + debug)
              for n in _EXPORTS))

print("=== C. 内容归属 ===")
check("C1 包详情页在 packs.js（loadPackView/_packViewMode/专属表情包/资产上传）",
      all(k in packs for k in ("loadPackView", "_packViewMode", "_loadPackStickers", "_packAssetUpload")))
check("C2 外壳只**调用**面板 loader，不含它们的定义",
      "function loadPackView" not in shell and "function loadPackView" not in shell
      and not re.search(r"function\s+load(RequestLog|Pipeline|Favorites|CharFiles|Journal|UserMemory)\b", shell))
check("C3 资料面板在 data.js（状态/收藏/记忆/设定文件/手账）",
      all(k in data for k in ("loadStateTab", "loadFavorites", "loadUserMemory",
                              "loadCharFiles", "loadJournal")))
check("C4 调试面板在 debug.js（请求记录/流水线）",
      "loadRequestLog" in debug and "loadPipeline" in debug and "_stageBlock" in debug)
check("C5 外壳保留 tab 切换与菜单骨架",
      "menu-tab" in shell and "openAvatarPicker" in shell and "closeMenu" in shell)
check("C6 面板模块仍挂 window.*（内联 onclick 与 views.js 桥依赖）",
      "window.loadFavorites" in data and "window.loadRequestLog" in debug
      and "window.loadPackView" in packs)

print("=== D. 门禁覆盖子目录（任务 2.5 的关键配套）===")
_names = bfb._module_names()
check("D1 _module_names() 递归到子目录（含 panels/packs 等）",
      {"panels/packs", "panels/data", "panels/debug"} <= _names)
check("D2 ORDER 与盘上模块集合逐字相等（门禁核心断言）", set(bfb.ORDER) == _names)
check("D3 三个面板模块紧跟外壳之后（拼接顺序）",
      bfb.ORDER.index("panels") + 1 == bfb.ORDER.index("panels/packs")
      and bfb.ORDER.index("panels/data") == bfb.ORDER.index("panels/packs") + 1
      and bfb.ORDER.index("panels/debug") == bfb.ORDER.index("panels/data") + 1)
check("D4 NON_BUNDLE 仍只排除 bundle/pc_nav", bfb.NON_BUNDLE == {"bundle", "pc_nav"})

print("=== E. bundle 三副本 ===")
_markers = [f"/* ── 来源：js/panels/{n}.js ── */" for n in ("packs", "data", "debug")]
_pos = [bundle.find(m) for m in _markers]
check("E1 bundle 含三个面板模块的标记", all(p >= 0 for p in _pos))
check("E2 bundle 里顺序正确（packs → data → debug）", _pos == sorted(_pos))
check("E3 bundle 里外壳还在且在最前",
      0 <= bundle.find("/* ── 来源：js/panels.js ── */") < min(_pos))
check("E4 服务端/安卓副本同步（由 sync_frontends --check 兜底，这里只确认文件在）",
      (ROOT / "server" / "frontend" / "js" / "panels" / "packs.js").is_file()
      and (ROOT / "android" / "app" / "src" / "main" / "assets" / "js" / "panels" / "packs.js").is_file())

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
