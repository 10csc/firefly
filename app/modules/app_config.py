# -*- coding: utf-8 -*-
"""应用配置与路径 — 全局唯一的 user_data 公式来源 + 配置读写点

server 拆分产物：路径引导、目录创建、默认文件拷贝、运行时配置状态。
其他模块（llm_base/sticker_picker/routes）统一从这里取 USER_DIR，
避免各自复制 frozen 判断公式导致路径分裂。
"""

import json, os, sys
from pathlib import Path

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
ASSETS_DIR = ROOT / "assets"

PORT = 8765
API_BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"

VALID_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
VALID_EFFORTS = ("none", "low", "high", "max")

CONFIG_FILE = USER_DIR / "config.json"

# ── 模式（多模式隔离）─────────────────────────────
# 每个模式独立数据根：USER_DIR/{mode}/，其下 character/ data/ journal/ 各一份。
# story = 剧情模式（现有闭环，数据迁移自旧平铺目录）；haruno = 春日手信（待设计）。
MODES = ("story", "haruno")
DEFAULT_MODE = "story"


def mode_root(mode: str = DEFAULT_MODE) -> Path:
    """模式数据根：USER_DIR/{mode}/。非法 mode 回退 story（审查约束）。"""
    m = mode if mode in MODES else DEFAULT_MODE
    d = USER_DIR / m
    d.mkdir(parents=True, exist_ok=True)
    return d


def mode_character_dir(mode: str = DEFAULT_MODE) -> Path:
    return mode_root(mode) / "character"


def mode_data_dir(mode: str = DEFAULT_MODE) -> Path:
    return mode_root(mode) / "data"


def mode_journal_dir(mode: str = DEFAULT_MODE) -> Path:
    return mode_root(mode) / "journal"


def bundled_character_dir(mode: str = DEFAULT_MODE) -> Path:
    """bundled 默认设定目录：{BASE_DIR}/assets/character/{mode}/（只读，退回路径）。

    注意用 BASE_DIR 而非 ASSETS_DIR（ROOT/assets）：
    - 开发：app/assets/character/；安卓：backend/app/assets/character/；frozen：_internal/assets/character/
    - ASSETS_DIR 公式在安卓/开发下指向不存在的 backend/assets、仓库根/assets，历史遗留错误
    """
    return BASE_DIR / "assets" / "character" / mode


# ── 老数据迁移（一次性，幂等）──────────────────────
# 目录级隔离前：user_data/character/ data/ story/手账.md 平铺。
# 迁移到 story 模式：user_data/story/{character,data,journal}/。
# 用 move（同盘 rename 原子），迁移后旧位置不再读写，避免新旧双份分裂。
# 顺序铁律：迁移必须先于默认拷贝——否则默认文件占位导致用户数据迁移被跳过丢失。
def _migrate_legacy_layout():
    import shutil as _sh
    targets = (
        (USER_DIR / "character", mode_character_dir("story")),
        (USER_DIR / "data", mode_data_dir("story")),
    )
    for _src, _dst in targets:
        if not _src.exists():
            continue
        _dst.mkdir(parents=True, exist_ok=True)
        for _f in _src.iterdir():
            if _f.is_file() and not (_dst / _f.name).exists():
                try:
                    _sh.move(str(_f), str(_dst / _f.name))
                except OSError:
                    pass
    # 旧手账位置 user_data/story/手账.md → story/journal/手账.md
    _old_journal = USER_DIR / "story" / "手账.md"
    _new_journal = mode_journal_dir("story") / "手账.md"
    if _old_journal.exists() and not _new_journal.exists():
        _new_journal.parent.mkdir(parents=True, exist_ok=True)
        try:
            _sh.move(str(_old_journal), str(_new_journal))
        except OSError:
            pass


_migrate_legacy_layout()


# ── 首次启动引导：建目录 + 拷贝默认文件（在迁移之后）──
USER_DIR.mkdir(parents=True, exist_ok=True)
for _sub in ("stickers",):
    (USER_DIR / _sub).mkdir(exist_ok=True)

# 默认源路径：frozen 时数据在 _internal/，开发时在 app/；BASE_DIR 已指向对应位置
_DEFAULTS = {
    USER_DIR / "config.json": ROOT / "config.json",
    mode_character_dir() / "core.md": bundled_character_dir() / "core.md",
    mode_character_dir() / "identity.md": bundled_character_dir() / "identity.md",
    mode_character_dir() / "sms_samples.md": bundled_character_dir() / "sms_samples.md",
}
for _dst, _src in _DEFAULTS.items():
    if not _dst.exists() and _src.exists():
        _dst.parent.mkdir(parents=True, exist_ok=True)
        _dst.write_text(_src.read_text(encoding="utf-8"), encoding="utf-8")


def resolve_asset(path: str) -> Path:
    """静态资源解析：user_data/ 优先，退回 bundled；表情包再查 user_data/stickers/。"""
    u = USER_DIR / path
    if u.exists():
        return u
    b = BASE_DIR / path
    if b.exists():
        return b
    if path.startswith("assets/"):
        alt = USER_DIR / "stickers" / Path(path).name
        if alt.exists():
            return alt
    return b


# ── 配置状态 ─────────────────────────────────────
def _load_config() -> dict:
    """加载配置。缺失字段用默认值。兼容旧 reply_* 字段自动映射到 polisher_*。"""
    cfg = {
        "api_key": "", "analyzer_model": "deepseek-v4-flash",
        "organizer_model": "deepseek-v4-flash", "polisher_model": "deepseek-v4-flash",
        "retriever_model": "deepseek-v4-flash",
        "retriever_effort": "none", "analyzer_effort": "high",
        "polisher_effort": "high", "organizer_effort": "none",
        "retriever_temperature": 0.0, "polisher_temperature": 0.5,
        # 主动性强度：0-100，0=关闭。值越大流萤越主动（间隔越短、触发概率越高）。
        "initiative_level": 0,
    }
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg["api_key"] = data.get("api_key", "") or ""
            for key in ("analyzer_model", "organizer_model", "polisher_model", "retriever_model"):
                val = data.get(key, "deepseek-v4-flash")
                cfg[key] = val if val in VALID_MODELS else "deepseek-v4-flash"
            _effort_defaults = {"retriever_effort": "none", "analyzer_effort": "high",
                                "polisher_effort": "high", "organizer_effort": "none"}
            for key in _effort_defaults:
                val = data.get(key, _effort_defaults[key])
                cfg[key] = val if val in VALID_EFFORTS else _effort_defaults[key]
            if "reply_model" in data and "polisher_model" not in data:
                rm = data.get("reply_model", "deepseek-v4-flash")
                cfg["polisher_model"] = rm if rm in VALID_MODELS else "deepseek-v4-flash"
            eff = data.get("polisher_effort", data.get("reply_effort", "high"))
            cfg["polisher_effort"] = eff if eff in VALID_EFFORTS else "high"
            try:
                t = float(data.get("polisher_temperature", data.get("reply_temperature", 0.5)))
                cfg["polisher_temperature"] = max(0.0, min(2.0, t))
            except (TypeError, ValueError):
                cfg["polisher_temperature"] = 0.5
            try:
                rt = float(data.get("retriever_temperature", 0.0))
                cfg["retriever_temperature"] = max(0.0, min(2.0, rt))
            except (TypeError, ValueError):
                cfg["retriever_temperature"] = 0.0
            try:
                ii = int(data.get("initiative_level", 0))
                cfg["initiative_level"] = max(0, min(100, ii))
            except (TypeError, ValueError):
                cfg["initiative_level"] = 0
    except Exception:
        pass
    if not cfg["api_key"]:
        cfg["api_key"] = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return cfg


# 模块级共享状态：routes 直接改 dict 字段后调 save_config()
config = _load_config()


def save_config() -> None:
    CONFIG_FILE.write_text(
        json.dumps({
            "api_key": config.get("api_key", ""),
            "analyzer_model": config.get("analyzer_model", "deepseek-v4-flash"),
            "organizer_model": config.get("organizer_model", "deepseek-v4-flash"),
            "polisher_model": config.get("polisher_model", "deepseek-v4-flash"),
            "retriever_model": config.get("retriever_model", "deepseek-v4-flash"),
            "retriever_effort": config.get("retriever_effort", "none"),
            "analyzer_effort": config.get("analyzer_effort", "high"),
            "polisher_effort": config.get("polisher_effort", "high"),
            "organizer_effort": config.get("organizer_effort", "none"),
            "retriever_temperature": config.get("retriever_temperature", 0.0),
            "polisher_temperature": config.get("polisher_temperature", 0.5),
            "initiative_level": config.get("initiative_level", 0),
        }, ensure_ascii=False),
        encoding="utf-8")


def get_api_key() -> str:
    """当前生效的 API Key：配置优先，环境变量兜底。"""
    return config.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "").strip()


def get_client():
    """获取当前 API 客户端，若未设置 Key 则返回 None。
    requests 实现（api_client），安卓 Chaquopy 兼容，无 openai 依赖。"""
    key = get_api_key()
    if not key:
        return None
    from modules.api_client import _CompatClient
    return _CompatClient(api_key=key, base_url=API_BASE, timeout=30.0)
