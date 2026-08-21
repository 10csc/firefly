# -*- coding: utf-8 -*-
"""应用配置与路径 — 全局唯一的 user_data 公式来源 + 配置读写点

server 拆分产物：路径引导、目录创建、默认文件拷贝、运行时配置状态。
其他模块（llm_base/sticker_picker/routes）统一从这里取 USER_DIR，
避免各自复制 frozen 判断公式导致路径分裂。
"""

import json, os, sys, time, logging
import contextvars
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 用户上下文（服务器版多用户隔离；本地版不设置，行为与之前完全一致）──
# 每个请求一个上下文：user_dir（该用户数据根）、api_key/api_base（用户自己的 Key）。
# 用 contextvars（Flask/Werkzeug 同款标准模式）：set 返回 Token，请求结束 reset 恢复。
# ThreadingHTTPServer 每请求新线程 + reset 双保险，无跨请求泄漏。
_user_ctx: contextvars.ContextVar = contextvars.ContextVar("firefly_user_ctx", default=None)


def set_user_context(user_dir=None, api_key=None, api_base=None, proxy=False) -> contextvars.Token:
    """设置当前请求的用户上下文，返回 Token（请求结束 reset_user_context 恢复）。
    proxy=True：服务器托管 API 模式（用户不提供 Key，服务器用运营者 Key 直发）。"""
    data = dict(_user_ctx.get() or {})
    if user_dir is not None:
        data["user_dir"] = Path(user_dir)
    if api_key is not None:
        data["api_key"] = api_key
    if api_base is not None:
        data["api_base"] = api_base
    data["proxy"] = bool(proxy)
    return _user_ctx.set(data)


def reset_user_context(token) -> None:
    """请求结束恢复上下文（try/finally 中调用，LLM 异常也会正确恢复）。"""
    _user_ctx.reset(token)


def _user_ctx_dir():
    ctx = _user_ctx.get()
    return ctx.get("user_dir") if ctx else None


def user_scope_key() -> str:
    """当前用户作用域标识（缓存 key 用）：服务器版 = 用户目录路径，本地版 = 空串。

    各模块的按 mode 缓存（角色设定/手账/短信样本）必须带上此维度，
    否则多用户并发下 A 的设定/手账会被 B 读到（缓存串扰）。
    """
    d = _user_ctx_dir()
    return str(d) if d else ""


def user_dir_id() -> int:
    """当前用户 id（= 数据目录名，服务器版；本地版无上下文返回 0）。配额记账用。"""
    d = _user_ctx_dir()
    if d is None:
        return 0
    try:
        return int(d.name)
    except ValueError:
        return 0


def user_has_key() -> bool:
    """当前请求是否具备可用 Key：服务器版看用户请求头（env/全局配置不算用户的），
    本地版看 config/env。"""
    ctx = _user_ctx.get()
    if ctx:
        return bool(ctx.get("api_key"))
    _p = _active_provider()
    return bool((_p.get("api_key") if _p else "") or os.environ.get("DEEPSEEK_API_KEY", "").strip())


def relay_needs_key() -> bool:
    """服务器版 relay 模式且用户未带 Key（非托管）→ True：chat 应立即返回 need_key，
    避免流水线空跑 4×120s relay 超时（本地版无上下文恒 False）。"""
    ctx = _user_ctx.get()
    if not ctx:
        return False
    if ctx.get("proxy"):
        return False
    return not bool(ctx.get("api_key"))


# ── 托管模式配额钩子（服务器注册；本地版不注册 = 不限制）──
_proxy_quota_checker = None
_proxy_quota_counter = None
_proxy_quota_failer = None


def set_proxy_quota_checker(fn) -> None:
    """注册托管模式配额检查函数：fn() 返回错误文案（""=放行）。
    A3（默认拍板）：检查与记账分离——检查放行后才调用，成功再记数（失败单独记失败表）。"""
    global _proxy_quota_checker
    _proxy_quota_checker = fn


def set_proxy_quota_counter(fn) -> None:
    """注册成功记账函数：每次调用成功（无异常）后调用。"""
    global _proxy_quota_counter
    _proxy_quota_counter = fn


def set_proxy_quota_failer(fn) -> None:
    """注册失败记账函数：调用抛异常时调用（失败不占额度，单独记录）。"""
    global _proxy_quota_failer
    _proxy_quota_failer = fn

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
# ── 版本号（唯一权威源）──────────────────────────
# 发版铁律：改这里必须同步改 4 处：
#   1. 本文件 APP_VERSION
#   2. app/static/js/panels.js 的 CURRENT_VERSION（0.9.0 起 app.js 已拆分为 js/ 模块）
#   3. android/app/build.gradle.kts 的 versionName
#   4. package/firefly.iss 的 AppVersion
# 用 tools/check_version.py 一键校验四者一致；格式 x.y.z 纯数字点分，
# 禁止 -beta/-rc 后缀（Gitee 无 prerelease 概念，后缀会污染 releases/latest）。
APP_VERSION = "0.8.0"
API_BASE = "https://api.deepseek.com/v1"
# OpenCode Go 兼容端点（OpenAI 兼容 chat/completions，模型 ID 与 DeepSeek 一致）
GO_BASE = "https://opencode.ai/zen/go/v1"
MODEL = "deepseek-v4-flash"

# ── 供应商（A8 多供应商；2026-08-21）────────────────
# 结构：providers=[{id,name,base_url,api_key,models[],caps{}}] + active_provider=id
# caps 能力位（DeepSeek 私有能力按供应商分支）：
#   thinking=支持 extra_body thinking/reasoning_effort；reasoning=解析 reasoning_content；
#   vision=支持 image_url 多模态；prompt_cache=回传缓存命中统计；models_endpoint=GET /models
# 内置建议清单（v1 时代 VALID_MODELS/端点白名单的替代物：建议 + 用户自由输入，不做硬白名单）
SUGGESTED_PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"],
     "caps": {"thinking": True, "reasoning": True, "vision": True,
              "prompt_cache": True, "models_endpoint": True}},
    {"id": "opencode-go", "name": "OpenCode Go", "base_url": "https://opencode.ai/zen/go/v1",
     "models": ["mimo-v2.5", "mimo-v2.5-free"],
     "caps": {"thinking": False, "reasoning": False, "vision": False,
              "prompt_cache": False, "models_endpoint": False}},
]
# 模型名：官方英文名（UI 下拉建议用 deepseek 官方清单；不再用「快速/更强」中文档位）
SUGGESTED_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]
# 兼容别名（旧代码/测试仍引用 VALID_MODELS；模型校验已改为自由输入，见 _clean_model）
VALID_MODELS = tuple(SUGGESTED_MODELS)
VALID_EFFORTS = ("none", "low", "high", "max")

_MODEL_MAX_LEN = 100
_BASE_MAX_LEN = 300


def _valid_http_base(url: str) -> bool:
    """base_url 安全校验：必须 http(s)，长度受限（防注入/畸形配置）。"""
    return url.startswith(("http://", "https://")) and len(url) <= _BASE_MAX_LEN


def _clean_model(val, default: str = MODEL) -> str:
    """模型名校验：非空字符串 + 长度上限；非法回退默认（自由输入，无白名单）。"""
    if not isinstance(val, str):
        return default
    v = val.strip()
    return v if v and len(v) <= _MODEL_MAX_LEN else default


def _clean_provider(p) -> dict | None:
    """供应商记录校验/净化：缺关键字段或 base_url 非法 → 拒绝（审查约束）。"""
    if not isinstance(p, dict):
        return None
    pid = str(p.get("id", "")).strip()
    if not pid or len(pid) > 40:
        return None
    base = str(p.get("base_url", "") or "").strip().rstrip("/")
    if not _valid_http_base(base):
        return None
    name = str(p.get("name", "") or "").strip() or pid
    models = [m for m in (str(x).strip() for x in p.get("models", []) if isinstance(x, str))
              if m and len(m) <= _MODEL_MAX_LEN]
    caps = {k: bool(v) for k, v in (p.get("caps") or {}).items()
            if k in ("thinking", "reasoning", "vision", "prompt_cache", "models_endpoint")}
    return {"id": pid, "name": name, "base_url": base,
            "api_key": str(p.get("api_key", "") or "").strip(),
            "models": models, "caps": caps}


def normalize_providers(raw) -> list:
    """整表净化：非法条目剔除；去重（id）。"""
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for p in raw:
        c = _clean_provider(p)
        if c and c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


def _deepseek_preset(api_key: str = "", base_url: str = API_BASE) -> dict:
    p = {"id": "deepseek", "name": "DeepSeek", "base_url": base_url,
         "api_key": api_key, "models": list(SUGGESTED_PROVIDERS[0]["models"]),
         "caps": dict(SUGGESTED_PROVIDERS[0]["caps"])}
    return p


def _migrate_legacy_providers(data: dict) -> dict:
    """旧结构（顶层 api_key/api_base）→ providers。返回 {providers, active_provider} 或 None。
    迁移前备份 config.json；迁移后旧字段不再读取/落盘。幂等：新结构存在则不迁。"""
    if "providers" in data:
        return None
    if "api_key" not in data and "api_base" not in data:
        return None
    try:
        _bak = CONFIG_FILE.with_name(CONFIG_FILE.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        _bak.write_bytes(CONFIG_FILE.read_bytes())
        logger.info("多供应商迁移：已备份旧配置 -> %s", _bak.name)
    except OSError:
        pass
    old_key = str(data.get("api_key", "") or "").strip()
    old_base = str(data.get("api_base", "") or API_BASE).strip()
    if not _valid_http_base(old_base):
        old_base = API_BASE
    old_base = old_base.rstrip("/")
    if old_base == API_BASE:
        providers = [_deepseek_preset(old_key, API_BASE)]
        active = "deepseek"
    else:
        # 旧状态 = 非官方端点（如 OpenCode Go）：保留原名端口供用户选用，行为等价
        p1 = _deepseek_preset("", API_BASE)
        p2 = _clean_provider({"id": "legacy", "name": "原接口地址",
                              "base_url": old_base, "api_key": old_key,
                              "models": [], "caps": {}})
        providers = [p for p in (p2, p1) if p]   # legacy 置顶 = 与旧行为一致
        active = providers[0]["id"] if providers else "deepseek"
    return {"providers": providers, "active_provider": active}

CONFIG_FILE = USER_DIR / "config.json"

# ── A7：认证/同步服务器（登录前置化；本地版经本地后端代理转发）──
# 公网入口（8787 网关）；发版时若切换服务器/域名，改此处 + docs/服务器管理规范.md
AUTH_SERVER_DEFAULT = "http://101.200.14.126:8787"

# ── 模式（多模式隔离）─────────────────────────────
# 每个模式独立数据根：USER_DIR/{mode}/，其下 character/ data/ journal/ 各一份。
# story = 剧情模式（现有闭环，数据迁移自旧平铺目录）；haruno = 春日手信（匹诺康尼黄金时刻·普通学生旅行AU）。
MODES = ("story", "haruno")
DEFAULT_MODE = "story"


def mode_root(mode: str = DEFAULT_MODE) -> Path:
    """模式数据根：{user_dir}/{mode}/（服务器版按用户隔离；本地版 = USER_DIR/{mode}）。非法 mode 回退 story（审查约束）。"""
    m = mode if mode in MODES else DEFAULT_MODE
    base = _user_ctx_dir() or USER_DIR
    d = base / m
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
    """静态资源解析：当前用户目录优先（服务器版账号隔离），退回 bundled；表情包再查 stickers/。"""
    u = (_user_ctx_dir() or USER_DIR) / path
    if u.exists():
        return u
    b = BASE_DIR / path
    if b.exists():
        return b
    if path.startswith("assets/"):
        alt = (_user_ctx_dir() or USER_DIR) / "stickers" / Path(path).name
        if alt.exists():
            return alt
    return b


# ── 配置状态 ─────────────────────────────────────
def _load_config() -> dict:
    """加载配置。缺失字段用默认值。兼容旧 reply_* 字段自动映射到 polisher_*。
    A8：providers 多供应商结构 + 旧 api_key/api_base 自动迁移（幂等，迁移前备份）。"""
    cfg = {
        # 派生便捷字段：api_key/api_base = 当前激活供应商的值（不落盘，见 save_config）
        "api_key": "", "api_base": API_BASE,
        "active_provider": "deepseek",
        "providers": [_deepseek_preset()],
        # 服务器地址（A7 认证/同步服务器）：默认公网 8787；开发/自建可改
        "auth_server_url": AUTH_SERVER_DEFAULT,
        "analyzer_model": "deepseek-v4-flash",
        "organizer_model": "deepseek-v4-flash", "polisher_model": "deepseek-v4-flash",
        "retriever_model": "deepseek-v4-flash",
        "retriever_effort": "none", "analyzer_effort": "high",
        "polisher_effort": "high", "organizer_effort": "none",
        "retriever_temperature": 0.0, "polisher_temperature": 0.5,
        "proactive_enabled": True, "proactive_hard": 6, "proactive_soft": 0.35,
        "prob_reply_enabled": True, "prob_reply_value": 0.10,
        "hidden_reply_enabled": True,
    }
    data = None
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # 服务器地址（A7 起=认证/同步服务器；空默认公网）。仅 http(s)，防注入。
            _srv = str(data.get("auth_server_url", "") or "").strip().rstrip("/")
            if not _srv:
                # 旧字段迁移：0.8.0 的 server_url（远程部署用）语义并入 auth_server_url
                _srv = str(data.get("server_url", "") or "").strip().rstrip("/")
            cfg["auth_server_url"] = (_srv if _srv.startswith(("http://", "https://"))
                                      else AUTH_SERVER_DEFAULT)
            cfg["server_url"] = ""   # 旧字段兼容占位（保存时不再写）
            # 多供应商：新结构直接读；旧结构（顶层 api_key/api_base）自动迁移
            if "providers" in data:
                providers = normalize_providers(data.get("providers"))
                if providers:
                    cfg["providers"] = providers
                    active = str(data.get("active_provider", "") or "").strip()
                    if not any(p["id"] == active for p in providers):
                        active = providers[0]["id"]
                    cfg["active_provider"] = active
            else:
                mig = _migrate_legacy_providers(data)
                if mig:
                    cfg["providers"] = mig["providers"] or [_deepseek_preset()]
                    cfg["active_provider"] = mig["active_provider"]
            # 派生便捷字段：取本 dict 内的激活供应商（不可用模块级全局 config）
            _plist = cfg.get("providers") or []
            _p = next((pr for pr in _plist if pr["id"] == cfg.get("active_provider")),
                      _plist[0] if _plist else None)
            cfg["api_key"] = _p.get("api_key", "") if _p else ""
            cfg["api_base"] = _p.get("base_url", API_BASE) if _p else API_BASE
            for key in ("analyzer_model", "organizer_model", "polisher_model", "retriever_model"):
                cfg[key] = _clean_model(data.get(key, "deepseek-v4-flash"))
            _effort_defaults = {"retriever_effort": "none", "analyzer_effort": "high",
                                "polisher_effort": "high", "organizer_effort": "none"}
            for key in _effort_defaults:
                val = data.get(key, _effort_defaults[key])
                cfg[key] = val if val in VALID_EFFORTS else _effort_defaults[key]
            if "reply_model" in data and "polisher_model" not in data:
                rm = data.get("reply_model", "deepseek-v4-flash")
                cfg["polisher_model"] = _clean_model(rm)
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
            # 主动性配置（缺省：开启 + 每 6 轮 1 次判断机会 + 35% 触发概率）
            cfg["proactive_enabled"] = bool(data.get("proactive_enabled", True))
            try:
                ph = int(data.get("proactive_hard", 6))
                cfg["proactive_hard"] = max(1, min(10, ph))
            except (TypeError, ValueError):
                cfg["proactive_hard"] = 6
            try:
                ps = float(data.get("proactive_soft", 0.35))
                cfg["proactive_soft"] = max(0.0, min(1.0, ps))
            except (TypeError, ValueError):
                cfg["proactive_soft"] = 0.35
            # 概率式回复配置（缺省：开启 + 10% 概率；配合 10 分钟静默窗≈每 1-2 小时一次机会）
            cfg["prob_reply_enabled"] = bool(data.get("prob_reply_enabled", True))
            try:
                pv = float(data.get("prob_reply_value", 0.10))
                cfg["prob_reply_value"] = max(0.0, min(1.0, pv))
            except (TypeError, ValueError):
                cfg["prob_reply_value"] = 0.10
            # 隐藏式回复配置（缺省：跟随概率式开启；独立开关，关前台概率式不影响隐藏式）
            cfg["hidden_reply_enabled"] = bool(data.get("hidden_reply_enabled", True))
    except Exception:
        pass
    # 旧默认值迁移：把“从未改过的旧默认”平滑迁到新推荐值（改过任意一项则尊重用户）。
    # 仅改内存，下一次保存配置时落盘；每次启动判定一致、幂等。
    if isinstance(data, dict):
        try:
            old = (int(data.get("proactive_hard", -1)) == 4
                   and abs(float(data.get("proactive_soft", -1)) - 0.5) < 1e-9
                   and abs(float(data.get("prob_reply_value", -1)) - 0.3) < 1e-9)
        except (TypeError, ValueError):
            old = False
        if old:
            cfg["proactive_hard"] = 6
            cfg["proactive_soft"] = 0.35
            cfg["prob_reply_value"] = 0.10
    # 环境变量兜底只用于本地版：服务器版运营者 Key 走 FIREFLY_PROXY_KEY（QuotaClient），
    # 绝不能把 env 兜底 Key 写进全局 config 并通过 /config 暴露前缀
    if not cfg["api_key"] and not os.environ.get("FIREFLY_SERVER"):
        cfg["api_key"] = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return cfg


# 模块级共享状态：routes 直接改 dict 字段后调 save_config()
config = _load_config()


def _active_provider() -> dict:
    """当前激活供应商（配置里的权威 record；无则返回深色塞默认）。"""
    providers = config.get("providers") or []
    if not providers:
        return _deepseek_preset()
    active = config.get("active_provider", "")
    for p in providers:
        if p["id"] == active:
            return p
    return providers[0]


def active_provider() -> dict:
    """对外接口：当前激活供应商（含派生字段 api_key/base_url）。"""
    return _active_provider()


def provider_by_id(pid: str) -> dict | None:
    for p in (config.get("providers") or []):
        if p["id"] == pid:
            return p
    return None


def set_active_provider(pid: str) -> bool:
    """切换激活供应商（必须存在于列表，审查约束）。返回是否成功。"""
    if provider_by_id(pid) is None:
        return False
    config["active_provider"] = pid
    _sync_derived()
    return True


def upsert_provider(p: dict) -> dict | None:
    """新增/替换供应商（净化后入库）。清理后的记录不存在时返回 None。"""
    c = _clean_provider(p)
    if c is None:
        return None
    providers = config.setdefault("providers", [])
    for i, old in enumerate(providers):
        if old["id"] == c["id"]:
            providers[i] = c
            break
    else:
        providers.append(c)
    if not config.get("active_provider") or provider_by_id(config["active_provider"]) is None:
        config["active_provider"] = c["id"]
    _sync_derived()
    return c


def remove_provider(pid: str) -> bool:
    """删除供应商（至少保留一个；删除激活项则激活第一个）。"""
    providers = config.get("providers") or []
    if not any(p["id"] == pid for p in providers) or len(providers) <= 1:
        return False
    config["providers"] = [p for p in providers if p["id"] != pid]
    if config.get("active_provider") == pid:
        config["active_provider"] = config["providers"][0]["id"]
    _sync_derived()
    return True


def _sync_derived():
    """派生便捷字段 api_key/api_base = 激活供应商的值（不落盘）。"""
    p = _active_provider()
    config["api_key"] = p.get("api_key", "")
    config["api_base"] = p.get("base_url", API_BASE)


def _on_caps_probe(changed: dict):
    """能力探测回调（api_client 遇 unknown param 剥离重试成功后调用）：
    回写激活供应商 caps 并落盘（本地版单用户；服务器版 relay 由前端处理，服务端不回写）。"""
    try:
        p = _active_provider()
        p.setdefault("caps", {}).update(changed)
        save_config()
    except Exception:
        pass


def save_config() -> None:
    # 只落盘 providers 结构（containing api_key）、active_provider 与其它设置；
    # 不写顶层 api_key/api_base（旧字段迁移后废除）
    CONFIG_FILE.write_text(
        json.dumps({
            "providers": config.get("providers") or [_deepseek_preset()],
            "active_provider": config.get("active_provider", "deepseek"),
            "auth_server_url": config.get("auth_server_url", AUTH_SERVER_DEFAULT),
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
            "proactive_enabled": bool(config.get("proactive_enabled", True)),
            "proactive_hard": max(1, min(10, int(config.get("proactive_hard", 6)))),
            "proactive_soft": max(0.0, min(1.0, float(config.get("proactive_soft", 0.35)))),
            "prob_reply_enabled": bool(config.get("prob_reply_enabled", True)),
            "prob_reply_value": max(0.0, min(1.0, float(config.get("prob_reply_value", 0.10)))),
            "hidden_reply_enabled": bool(config.get("hidden_reply_enabled", True)),
        }, ensure_ascii=False),
        encoding="utf-8")


def get_api_key() -> str:
    """当前生效的 API Key：用户上下文（服务器版，用户自己的 Key）→ 激活供应商 → 环境变量兜底。"""
    ctx = _user_ctx.get()
    if ctx and ctx.get("api_key"):
        return ctx["api_key"]
    return config.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "").strip()


def get_client():
    """获取当前 API 客户端。
    服务器版三种模式：
    - relay（默认）：用户自带 Key → RelayClient（APP 代发，服务器不持有 Key）
    - proxy（托管）：用户不提供 Key，服务器用运营者 Key 直发（OpenCode Go 端点）
    direct（本地版）：原逻辑，Key 必须在服务器可用。"""
    ctx = _user_ctx.get()
    # 托管模式：服务器用运营者 Key 直发（用户无 Key，走 direct 全链路：服务器知识库注入真实资产）
    if ctx and ctx.get("proxy"):
        key = os.environ.get("FIREFLY_PROXY_KEY", "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            return None
        from modules.api_client import QuotaClient
        # 无论是否注册了配额钩子，托管模式一律走 QuotaClient：它会在每次 create 时
        # 强制 model=mimo-v2.5，用户/全局配置不可能把运营者 Key 切到其它模型。
        return QuotaClient(api_key=key, base_url=GO_BASE,
                           quota_fn=_proxy_quota_checker, timeout=120.0,
                           counter_fn=_proxy_quota_counter, fail_fn=_proxy_quota_failer)
    mode = config.get("api_mode", "relay" if ctx else "direct")
    if mode == "relay":
        from modules.api_client import RelayClient
        base = (ctx.get("api_base") if ctx else None) or config.get("api_base", API_BASE)
        if not _valid_http_base(base):
            base = API_BASE
        return RelayClient(user_key=user_scope_key() or "local", api_base=base)
    key = get_api_key()
    if not key:
        return None
    from modules.api_client import _CompatClient
    base = (ctx.get("api_base") if ctx else None) or config.get("api_base", API_BASE)
    if not _valid_http_base(base):
        base = API_BASE
    # caps 随激活供应商（能力位分支：extra_body/多模态等按供应商开关）
    caps = _active_provider().get("caps") or {}
    return _CompatClient(api_key=key, base_url=base, timeout=30.0,
                         caps=caps, on_caps_change=_on_caps_probe)
