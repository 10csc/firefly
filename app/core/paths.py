# -*- coding: utf-8 -*-
"""路径与平台常量 — 全局唯一的 user_data 公式来源（任务 2.1 自 app_config 拆出）

BASE_DIR / ROOT / APP_DIR / USER_DIR / STATIC_DIR / PORT / CONFIG_FILE 在此定义；
各 mode_* 目录访问器与 resolve_asset 也归这里（路径公式只有一处，避免分裂）。

注意：USER_DIR / CONFIG_FILE 是**可变全局量**（测试与运行期会替换数据根），
其他模块一律经 `paths.USER_DIR` 属性访问，不要 `from core.paths import USER_DIR`。
"""

import os
import sys
from pathlib import Path

from core.userctx import _user_ctx_dir


# ── 路径 ────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent          # app/（打包后 _internal/，安卓下=解压 backend/app/）

# 安卓（Chaquopy 内嵌）：FIREFLY_ANDROID=1 + FIREFLY_DATA_DIR=内部存储数据根
#   ROOT（设定资料，只读） = 解压目录 backend/（BASE_DIR.parent，含 knowledge/memory/static/assets）
#   USER_DIR（用户数据，可写） = 内部存储 /firefly_data（升级保留）
if os.environ.get("FIREFLY_ANDROID"):
    ROOT = BASE_DIR.parent                                   # 解压根 backend/（只读设定）
    USER_DIR = Path(os.environ["FIREFLY_DATA_DIR"]) / "user_data"
elif getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent        # dist/firefly/
    ROOT = APP_DIR / "_internal"                           # 数据文件根
    USER_DIR = APP_DIR / "user_data"                       # 用户数据（exe 同级）
else:
    APP_DIR = BASE_DIR.parent                              # 仓库根
    ROOT = APP_DIR                                         # 开发时数据根=仓库根
    USER_DIR = ROOT / "user_data"

# 前端静态文件：开发时 app/static/，frozen 时 _internal/static/（BASE_DIR 两者皆指向）
STATIC_DIR = BASE_DIR / "static"

PORT = 8765


CONFIG_FILE = USER_DIR / "config.json"

# ── A7：认证/同步服务器（登录前置化；本地版经本地后端代理转发）──
# 公网入口（8787 网关）；发版时若切换服务器/域名，改此处 + docs/服务器管理规范.md
AUTH_SERVER_DEFAULT = "http://101.200.14.126:8787"


# 默认参数用 None 哨兵而不是 `= DEFAULT_MODE`：DEFAULT_MODE 属于 core.presets，而 presets
# 又需要本模块的 USER_DIR —— 若在 def 期求值默认参数，import 期就成环。改为调用期解析，
# 语义逐条等价（None 与"未传"都落到 DEFAULT_MODE，显式非法值仍回退默认包）。
def _presets_mod():
    """延迟取 core.presets（只有模式校验需要它；延迟导入打破模块级循环）。"""
    from core import presets as _presets
    return _presets


def _resolve_mode(mode: str | None) -> str:
    """非法/空 mode → 默认包（审查约束：调用方传坏值不炸，静默回退默认包）。"""
    _presets = _presets_mod()
    return mode if mode in _presets.MODES else _presets.DEFAULT_MODE


def mode_root(mode: str = None) -> Path:
    """模式数据根：{user_dir}/{mode}/（服务器版按用户隔离；本地版 = USER_DIR/{mode}）。

    **纯路径计算，不创建目录**（阶段 2.6）。历史上这里自带 mkdir，副作用是：
    ① 纯读端点（GET /modes、/config）也会凭空建出 `user_data/{mode}/...`；
    ② 任何 import 链路过它一次就在盘上落一堆空目录（"读操作改盘"）。
    写路径请显式调 `ensure_mode_root()`；读路径直接用它。
    非法 mode 仍按原语义回退默认包。"""
    m = _resolve_mode(mode)
    base = _user_ctx_dir() or USER_DIR
    return base / m


def ensure_mode_root(mode: str = None) -> Path:
    """取模式数据根并**确保目录存在**（写路径专用，阶段 2.6 新增）。

    把"创建目录"从"读路径的隐藏副作用"改成"写路径的显式一步"，
    这样读代码时就能看出哪些操作会动盘。"""
    d = mode_root(mode)
    d.mkdir(parents=True, exist_ok=True)
    return d


def mode_character_dir(mode: str = None) -> Path:
    return mode_root(mode) / "character"


def mode_data_dir(mode: str = None) -> Path:
    return mode_root(mode) / "data"


def mode_journal_dir(mode: str = None) -> Path:
    return mode_root(mode) / "journal"


def pack_root(pid: str) -> Path:
    """包数据根：与 mode_root 同公式，但**不做非法回退**（阶段 3.3 新增）。

    为什么需要它：`mode_root()` 对不在 `MODES` 里的 id 会静默换成默认包（老语义：传坏值不炸）。
    而"包"的概念比 MODES 宽 —— 归档包（3.5）在 packs.json 里但不在 MODES。若拿 `mode_root(归档id)`
    去定位，就会读到/写坏 **默认包** 的目录（3.8 已在同步/快照链路上踩到同一个坑）。
    调用方负责保证 pid 合法（注册表/`_PRESET_ID_RE` 校验过）。"""
    return (_user_ctx_dir() or USER_DIR) / str(pid)


def bundled_character_dir(mode: str = None) -> Path:
    """bundled 默认设定目录：{BASE_DIR}/assets/character/{mode}/（只读，退回路径）。
    开发：app/assets/character/；安卓：backend/app/assets/character/；frozen：_internal/assets/character/。
    空 mode → 默认包；**非空原样使用、不做合法性校验**（保持拆分前的既有语义）。"""
    if mode is None:
        mode = _presets_mod().DEFAULT_MODE
    return BASE_DIR / "assets" / "character" / mode


def resolve_asset(path: str) -> Path:
    """静态资源解析：当前用户目录优先（服务器版账号隔离），退回 bundled；表情包再查 stickers/。
    包资产（assets/character/{包}/assets/{文件}）：用户副本 {user}/{包}/character/assets/ 优先
    （软件内编辑头像/封面的产物），再退回 bundled 包目录。"""
    from core import presets as _presets   # 延迟导入：presets 需要本模块的 USER_DIR（单向循环，只在调用期解析）
    u = (_user_ctx_dir() or USER_DIR) / path
    if u.exists():
        return u
    parts = Path(path).parts
    if (len(parts) == 5 and parts[0] == "assets" and parts[1] == "character"
            and parts[3] == "assets" and parts[2] in _presets.MODES):
        ub = (_user_ctx_dir() or USER_DIR) / parts[2] / "character" / "assets" / parts[4]
        if ub.exists():
            return ub
    b = BASE_DIR / path
    if b.exists():
        return b
    if path.startswith("assets/"):
        alt = (_user_ctx_dir() or USER_DIR) / "stickers" / Path(path).name
        if alt.exists():
            return alt
    return b
