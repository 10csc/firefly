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

P4-7（2026-10-01）扩充：广场域 `panels/plaza.js`（844 行）同样按内聚拆成
shell + `plaza_list.js` + `plaza_detail.js`，A 组清单随之登记这三个文件（防止再单向增长）。
P4-7b（2026-10-01）**再扩充**：制卡域 `panels/plaza_forge.js`（1242 行，曾是全仓最大的前端文件）
拆成 shell + `plaza_forge_form.js` + `plaza_forge_review.js`，A 组清单随之登记这三个文件。
**有意不做** "panels/ 全目录硬门禁"：`plaza_admin.js` 尚未纳入，全目录门禁会当场触红。

真实浏览器端到端见 tests/manual_ui_panels.py（14 步全过、零 JS 报错）。
制卡页（共创平台 M2）另有一支：tests/manual_plaza_forge.py（按需跑，不进本套件）。
广场（M1/P4-3/P4-7）三支：tests/manual_plaza_ui.py、manual_plaza_forge.py、manual_plaza_admin.py。
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
PANELS_DIR = JS / "panels"          # 目录级 <500 行门禁扫这里（外壳 panels.js 在外，单独加）
SHELL = JS / "panels.js"
PACKS = JS / "panels" / "packs.js"
DATA = JS / "panels" / "data.js"
DEBUG = JS / "panels" / "debug.js"
# P4-7（2026-10-01）：广场域从单个 panels/plaza.js（844 行）拆成 shell + list + detail。
PLAZA = JS / "panels" / "plaza.js"
PLAZA_LIST = JS / "panels" / "plaza_list.js"
PLAZA_DETAIL = JS / "panels" / "plaza_detail.js"
# P4-7b（2026-10-01）：制卡域从单个 panels/plaza_forge.js（1242 行）拆成 shell + form + review。
FORGE = JS / "panels" / "plaza_forge.js"
FORGE_FORM = JS / "panels" / "plaza_forge_form.js"
FORGE_REVIEW = JS / "panels" / "plaza_forge_review.js"
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

print("=== A. 拆分产物都在，且 panels/ 下每个 js 都 <500 行 ===")
# 2026-10-01（P4-7 → P4-7b 完成后升级为目录级门禁）：原先只**逐个登记本次拆出的文件**，
# 于是没被登记的 `stickers.js`/`pack_tree.js`/`pack_assist.js` 仍可任意增长，门禁形同点名。
# P4-7/P4-7b 拆完后 `panels/` 下**全部文件都已 <500 行** ⇒ 改成目录级：
# 新增文件、旧文件增长都会当场触红（不再需要有人记得来登记）。
# 行数口径 = `str.splitlines()`；Windows PowerShell 5.1 的裸 `Get-Content` 按 ANSI 解码
# 会吃掉个别换行、同一文件少报几十行 —— 别用那个数当验收口径（见 docs/协作记忆.md #17）。
_panel_files = sorted(PANELS_DIR.glob("*.js"))
# 先断言清单本身没写错：glob 出错（路径改名/拼错）会让下面的循环空转、门禁假绿。
check(f"A0 panels/ 目录扫到 {len(_panel_files)} 个 js（≥10 才算有效）", len(_panel_files) >= 10)
for fp in [SHELL] + _panel_files:
    n = len(fp.read_text(encoding="utf-8").splitlines())
    check(f"A  {fp.name:24} 存在且 <500 行（{n} 行）", fp.is_file() and n < 500)

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
check("C3 资料面板在 data.js（状态/收藏/记忆/手账）",
      all(k in data for k in ("loadStateTab", "loadFavorites", "loadUserMemory",
                              "loadJournal")))
# C3b（2026-09-18）：loadCharFiles 已随「用户设定」编辑器从设定文件页一起去掉——
# 它属角色卡管理域（PACK_STRUCTURE 的 persona slot），且不是聊天产物。
# 断言用"定义/调用点"而非裸字符串（注释里提到这个名字是允许的）。
_C3B_DEAD = re.compile(r"function\s+loadCharFiles\b|\bloadCharFiles\s*\(|user-setting-editor")
check("C3b 设定文件页不再重复渲染用户设定（loadCharFiles 已移除）",
      not _C3B_DEAD.search(data) and not _C3B_DEAD.search(shell))
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
check("D7 _module_names() 排除的正是 NON_BUNDLE", bfb._module_names() == set(bfb.ORDER))
# D4（2026-09-19 改写）：原来断言 `NON_BUNDLE == {"bundle","pc_nav"}` 这种**写死集合**——
# PC 重构把 pc_nav 换成 pc_shell 就假失败了一次（同类问题见 docs/错误总结.md #12）。
# 现在断言的是**性质**：NON_BUNDLE 必须 = bundle ∪ index.html 里用独立 <script> 加载的那些模块。
_htm = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
_sep = set(re.findall(r'<script src="js/([A-Za-z0-9_/]+)\.js', _htm)) | {"bundle"}
check(f"D4 NON_BUNDLE = bundle + index.html 独立加载的模块 {sorted(_sep)}",
      bfb.NON_BUNDLE == _sep)
check("D5 index.html 引用的独立脚本都在盘上",
      all((ROOT / "app" / "static" / "js" / (n + ".js")).is_file() for n in _sep))

print("=== E. bundle 三副本 ===")
_markers = [f"/* ── 来源：js/panels/{n}.js ── */" for n in ("packs", "data", "debug")]
_pos = [bundle.find(m) for m in _markers]
check("E1 bundle 含三个面板模块的标记", all(p >= 0 for p in _pos))
check("E2 bundle 里顺序正确（packs → data → debug）", _pos == sorted(_pos))
check("E3 bundle 里外壳还在且在最前",
      0 <= bundle.find("/* ── 来源：js/panels.js ── */") < min(_pos))
# E4（2026-10-01 架构改造后改写）：**服务器只做后台**，server/frontend 下不得有 App 面板。
# 原来断言它"存在"（当时服务器=App 镜像），现在反过来 —— 变成"App 面板不得回潮"的守卫。
check("E4a android 侧 App 面板仍在（App 仍需要）",
      (ROOT / "android" / "app" / "src" / "main" / "assets" / "js" / "panels" / "packs.js").is_file())
check("E4b server/frontend **不得**有 App 面板（服务器只做后台）",
      not (ROOT / "server" / "frontend" / "js" / "panels").exists()
      and not (ROOT / "server" / "frontend" / "index.html").exists()
      and not (ROOT / "server" / "frontend" / "style.css").exists())

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
