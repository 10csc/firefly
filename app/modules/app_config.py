# -*- coding: utf-8 -*-
"""应用配置与路径 — 全局唯一的 user_data 公式来源 + 配置读写点

server 拆分产物：路径引导、目录创建、默认文件拷贝、运行时配置状态。
其他模块（llm_base/sticker_picker/routes）统一从这里取 USER_DIR，
避免各自复制 frozen 判断公式导致路径分裂。
"""

import json, os, re, sys, time, logging
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


# ── 包级配置（主动消息等随角色走的设置；2026-09-08 主动消息按包化）──
# 存 user_data/{mode}/data/proactive.json；读取顺序：包级 → 全局 config → 默认。
# 全局 config 的 proactive_* 保留作"默认值"（新包/未配置包继承）。
_PACK_CFG_NAME = "proactive.json"


def pack_cfg(mode: str, key: str, default=None):
    """包级配置读取：user_data/{mode}/data/proactive.json → 全局 config → default。"""
    fp = mode_data_dir(mode) / _PACK_CFG_NAME
    try:
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get(key) not in (None, ""):
                return data[key]
    except Exception:
        pass
    return config.get(key, default)


def set_pack_cfg(mode: str, updates: dict) -> None:
    """写包级配置（合并 updates 后落盘）。"""
    fp = mode_data_dir(mode) / _PACK_CFG_NAME
    data = {}
    try:
        if fp.exists():
            old = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                data = old
    except Exception:
        data = {}
    data.update(updates)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── 按用户配置覆盖（A2：/set-config 服务器版只写覆盖，不污染全站默认）──
_user_overlay: contextvars.ContextVar = contextvars.ContextVar("firefly_user_overlay", default=None)


def set_user_overlay(d: dict | None) -> None:
    """注入当前请求的用户配置覆盖（server_app 每请求从 user_data/{uid}/settings.json 读）。"""
    _user_overlay.set(d if isinstance(d, dict) else {})


def get_user_overlay() -> dict:
    return _user_overlay.get() or {}


def eff_cfg(key: str, default=None):
    """生效配置取值：用户覆盖（服务器版）→ 全局 config → default。
    本地版无覆盖时行为与读 config 完全一致。"""
    ov = _user_overlay.get() or {}
    if key in ov and ov[key] not in (None, ""):
        return ov[key]
    return config.get(key, default)


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

PORT = 8765
# ── 版本号（唯一权威源）──────────────────────────
# 发版铁律：改这里必须同步改 4 处：
#   1. 本文件 APP_VERSION
#   2. app/static/js/update.js 的 CURRENT_VERSION（0.9.0 起 app.js 已拆分为 js/ 模块）
#   3. android/app/build.gradle.kts 的 versionName
#   4. package/firefly.iss 的 AppVersion
# 用 tools/check_version.py 一键校验四者一致；格式 x.y.z 纯数字点分，
# 禁止 -beta/-rc 后缀（Gitee 无 prerelease 概念，后缀会污染 releases/latest）。
APP_VERSION = "0.8.1"
API_BASE = "https://api.deepseek.com/v1"
# OpenCode Go 兼容端点（OpenAI 兼容 chat/completions，模型 ID 与 DeepSeek 一致）
GO_BASE = "https://opencode.ai/zen/go/v1"
MODEL = "deepseek-v4-flash-vision-exp"

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


_CONFIG_BAK_KEEP = 5   # 供应商迁移备份 config.json.bak-* 保留份数（防反复迁移刷爆磁盘）


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
        # 保留策略：只留最新 _CONFIG_BAK_KEEP 份（文件名时间戳序，按名排序删旧）；
        # 失败静默——清理失败不影响迁移主流程
        try:
            _baks = sorted(CONFIG_FILE.parent.glob(CONFIG_FILE.name + ".bak-*"))
            for _old in _baks[:max(len(_baks) - _CONFIG_BAK_KEEP, 0)]:
                _old.unlink(missing_ok=True)
        except OSError:
            pass
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

# ── 模式（预设包注册表）─────────────────────────────
# 一个预设包 = assets/character/{id}/ 一个目录（含 preset.json 清单），
# 打包三样东西：角色（人格/口吻）、剧本（知识库/核查口径）、演出形态（presentation）。
# 每个模式独立数据根：USER_DIR/{mode}/，其下 character/ data/ journal/ 各一份。
# story = 剧情模式（流萤·主线）；haruno = 春日手信（匹诺康尼黄金时刻·普通学生旅行AU）。
_PRESET_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# 预设包格式版本（R-05，2026-09-10）：当前版本。preset.json 可选声明 "schema"，
# 缺失视为 1；高于本值时只告警并按已知规则解析（前向兼容，不拒绝加载）。
PRESET_SCHEMA = 1


def _parse_preset(fp: Path, expect_id: str) -> dict | None:
    """解析单个 preset.json 并校验（id 须等于目录名）。非法返回 None（告警不阻塞）。"""
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("预设包 %s 的 preset.json 解析失败，跳过: %s", expect_id, e)
        return None
    pid = str(data.get("id", "")).strip()
    if pid != expect_id or not _PRESET_ID_RE.fullmatch(pid):
        logger.warning("预设包 %s 的 id 非法或与目录名不一致，跳过", expect_id)
        return None
    name = str(data.get("name", "")).strip()
    cname = str(data.get("char_name", "")).strip()
    uname = str(data.get("user_name", "")).strip()
    if not name or not cname or not uname:
        logger.warning("预设包 %s 缺必填字段（name/char_name/user_name），跳过", pid)
        return None
    presentation = str(data.get("presentation", "")).strip()
    if presentation not in ("sticker", "narration", "none"):
        logger.warning("预设包 %s 的 presentation 非法（%r），回退 sticker", pid, presentation)
        presentation = "sticker"
    # 知识库目录（可选）：包显式声明的仓库相对路径清单；不声明则用包内 knowledge/（存在才挂）
    kd = data.get("knowledge_dirs")
    knowledge_dirs = [str(x).strip().strip("/") for x in kd
                      if isinstance(x, str) and str(x).strip()] if isinstance(kd, list) else None
    # 包格式版本（R-05，2026-09-10）：**只读前向兼容**——缺失视为当前版本 1；
    # 高于已知版本只告警、仍按已知规则解析（宽进），避免"新客户端建的包在老版本打不开"。
    # 注意：该字段必须同时写进两处写盘点（pack_forge.forge_finish、routes_pack.create_pack），
    # 否则会退化成 `bans` 那样的死字段（写了没人读 / 读了没人写）。
    try:
        schema = int(data.get("schema", PRESET_SCHEMA))
    except (TypeError, ValueError):
        logger.warning("预设包 %s 的 schema 非整数，按 %d 处理", pid, PRESET_SCHEMA)
        schema = PRESET_SCHEMA
    if schema > PRESET_SCHEMA:
        logger.warning("预设包 %s 声明 schema=%d 高于本版支持的 %d，按已知规则解析（建议升级客户端）",
                       pid, schema, PRESET_SCHEMA)
    return {"id": pid, "name": name, "char_name": cname,
            "user_name": uname, "presentation": presentation,
            "desc": str(data.get("desc", "") or "").strip(),
            "tagline": str(data.get("tagline", "") or "").strip(),
            "knowledge_dirs": knowledge_dirs, "schema": schema}


def _discover_presets(base: Path | None = None) -> dict:
    """扫描发现预设包：bundled（assets/character/{id}/preset.json）+ 本地版追加用户自建区
    （USER_DIR/{id}/character/preset.json；服务器版跳过——用户区属各账号，自定义整包不入全局注册表）。
    内置包优先（用户区同 id 跳过）；一个都没发现回退两内置包硬编码兜底。base 参数供测试注入。"""
    presets = {}
    roots = [base or (BASE_DIR / "assets" / "character")]
    for root in roots:
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            fp = d / "preset.json"
            if not fp.exists():
                continue
            p = _parse_preset(fp, d.name)
            if p:
                presets[p["id"]] = p
    # 用户自建区（仅本地版；测试注入 base 时跳过）
    if base is None and not os.environ.get("FIREFLY_SERVER"):
        try:
            user_dirs = sorted(p for p in USER_DIR.iterdir() if p.is_dir())
        except OSError:
            user_dirs = []
        for d in user_dirs:
            fp = d / "character" / "preset.json"
            if not fp.exists():
                continue
            p = _parse_preset(fp, d.name)
            if not p:
                continue
            if p["id"] in presets:
                logger.warning("用户自建包 %s 与内置包同 id，跳过（内置优先）", p["id"])
                continue
            p["custom"] = True
            presets[p["id"]] = p
    if not presets:
        logger.error("预设包发现为空，回退内置 story/haruno 兜底")
        presets["story"] = {"id": "story", "name": "剧情模式", "char_name": "流萤",
                            "user_name": "开拓者", "presentation": "sticker",
                            "desc": "她的故事，与你共同推进",
                            "tagline": "会找到的，属于我的梦...",
                            "knowledge_dirs": ["knowledge", "database/dialogues_compiled"]}
        presets["haruno"] = {"id": "haruno", "name": "春日手信", "char_name": "流萤",
                             "user_name": "开拓者", "presentation": "narration",
                             "desc": "流萤想象的普通学生生活",
                             "tagline": "会找到的，属于我的梦...",
                             "knowledge_dirs": None}
    return presets


PRESETS = _discover_presets()
DEFAULT_MODE = "story" if "story" in PRESETS else next(iter(PRESETS), "story")
# 模式元组（兼容既有几百处 `mode in cfg.MODES` 校验）：默认模式在前，其余按 id 排序
MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))


def reload_presets() -> None:
    """重新扫描预设包（新建/删除自定义包后调用）。
    PRESETS 原地清空重建（dict 引用共享，各模块运行时再取）；MODES 重新赋值——
    消费方须用 cfg.MODES 运行时访问（from-import 的值拷贝会陈旧）。"""
    global PRESETS, MODES
    fresh = _discover_presets()
    PRESETS.clear()
    PRESETS.update(fresh)
    MODES = tuple([DEFAULT_MODE] + sorted(k for k in PRESETS if k != DEFAULT_MODE))


def char_name(mode: str = DEFAULT_MODE) -> str:
    """当前模式角色名（预设包声明；非法 mode 回退默认包，兜底"流萤"）。"""
    p = PRESETS.get(mode) or PRESETS.get(DEFAULT_MODE) or {}
    return p.get("char_name") or "流萤"


def user_name(mode: str = DEFAULT_MODE) -> str:
    """当前模式用户称呼（同 char_name）。"""
    p = PRESETS.get(mode) or PRESETS.get(DEFAULT_MODE) or {}
    return p.get("user_name") or "开拓者"


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
    开发：app/assets/character/；安卓：backend/app/assets/character/；frozen：_internal/assets/character/"""
    return BASE_DIR / "assets" / "character" / mode


# ── 老数据迁移（一次性，幂等）──────────────────────
# 目录级隔离前：user_data/character/ data/ story/手账.md 平铺。
# 迁移到 story 模式：user_data/story/{character,data,journal}/。
# 用 move（同盘 rename 原子），迁移后旧位置不再读写，避免新旧双份分裂。
# 顺序铁律：迁移必须先于默认拷贝——否则默认文件占位导致用户数据迁移被跳过丢失。
#
# 2026-09-10 修复（三项）：
#   ① **不再在 import 期自动执行**。原来顶层直接调用，而服务器版入口 server_app.py
#      也 import 本模块 → 服务器每次启动都会对**共享** user_data 根执行移动式迁移。
#      现改为显式调用：本地版入口 app/server.py 的 main() 调 run_legacy_migration()
#      （服务器版永不调用；运维如需迁移自建包，需手工调用并在迁移前先备份）。
#   ② 迁移前对涉及源打一份 user_data/.legacy_pre_move.zip 备份（失败即中止迁移）。
#   ③ move 失败不再静默：逐条 logger.warning + 汇总，返回统计供调用方展示。
_LEGACY_PRE_MOVE_ZIP = "legacy_pre_move.zip"


def _legacy_targets():
    """迁移映射：(源, 目标路径字符串) 列表（含旧手账位置）。

    注意：这里**只做纯路径计算**，不能调用 mode_root/mode_character_dir 等派生函数——
    它们内部有 mkdir 副作用，会让"无待迁移内容即零副作用"的承诺失效
    （2026-09-10 自测发现：原写法在无旧布局时也会凭空建出 user_data/story/）。"""
    return [
        (USER_DIR / "character", USER_DIR / "story" / "character"),
        (USER_DIR / "data", USER_DIR / "story" / "data"),
        (USER_DIR / "story" / "手账.md", USER_DIR / "story" / "journal" / "手账.md"),
    ]


def _backup_legacy_sources() -> Path:
    """迁移前把涉及源打包到 user_data/{_LEGACY_PRE_MOVE_ZIP}（失败抛异常，由调用方中止迁移）。"""
    import zipfile
    out = USER_DIR / _LEGACY_PRE_MOVE_ZIP
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, _dst in _legacy_targets():
            if not src.exists():
                continue
            if src.is_file():
                zf.write(src, src.name)
                n += 1
            else:
                for fp in sorted(src.rglob("*")):
                    if fp.is_file():
                        zf.write(fp, f"{src.name}/{fp.relative_to(src).as_posix()}")
                        n += 1
    if n == 0:
        try:
            out.unlink()
        except OSError:
            pass
        return out
    logger.warning("旧布局迁移：已备份 %d 个待移动文件 → %s", n, out)
    return out


def run_legacy_migration() -> dict:
    """显式执行旧布局迁移（幂等）。返回统计 dict。

    仅由本地版入口 app/server.py 的 main() 调用——**服务器版不得调用**
    （服务器 model 下 user_data 根是多账号共享的，迁移会产生跨账号归属歧义）。
    无待迁移内容时立即返回且不产生任何副作用（含不写备份）。

    2026-09-10（R-04）：目标已存在时不再一律跳过，改为**内容感知**——
      内容摘要相同 → 该源是 bundled 的陈旧副本，**丢弃**（否则它会永久遮蔽新版内置内容，
      实测线上 user_data/story/character/ 就留了这样 3 个文件，其中 2 个与 bundled 逐字相同）；
      内容不同    → 可能是用户数据，**保留原处**并告警（宁可留冗余，不误删）。"""
    import shutil as _sh
    moved, failed, skipped, discarded = 0, 0, 0, 0
    pending = [t for t in _legacy_targets() if t[0].exists()]
    if not pending:
        return {"pending": 0, "moved": 0, "failed": 0, "skipped": 0,
                "discarded": 0, "aborted": False}

    # ③ 迁移前先备份；备份失败即中止（不再"无备份地移动用户数据"）
    try:
        _backup_legacy_sources()
    except Exception as e:
        logger.error("旧布局迁移已中止：迁移前备份失败 %s", e)
        return {"pending": len(pending), "moved": 0, "failed": 0, "skipped": 0,
                "discarded": 0, "aborted": True, "error": str(e)}

    def _same_content(a: Path, b: Path) -> bool:
        try:
            return a.read_bytes().replace(b"\r\n", b"\n") == b.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            return False

    def _handle(src_file: Path, dst_file: Path) -> str:
        """返回 'moved' | 'discarded' | 'skipped' | 'failed'"""
        if dst_file.exists():
            if _same_content(src_file, dst_file):
                try:
                    src_file.unlink()
                    return "discarded"
                except OSError:
                    return "skipped"
            logger.warning("旧布局迁移：目标已存在且内容不同，保留源文件不删: %s", src_file)
            return "skipped"
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            _sh.move(str(src_file), str(dst_file))
            return "moved"
        except OSError as e:
            logger.warning("旧布局迁移失败（文件留在原位）: %s → %s: %s", src_file, dst_file, e)
            return "failed"

    for _src, _dst in pending:
        if _src.is_file():
            r = _handle(_src, _dst)
            moved += r == "moved"; discarded += r == "discarded"
            skipped += r == "skipped"; failed += r == "failed"
            continue
        for _f in _src.iterdir():
            if not _f.is_file():
                continue
            r = _handle(_f, _dst / _f.name)
            moved += r == "moved"; discarded += r == "discarded"
            skipped += r == "skipped"; failed += r == "failed"
    if moved or failed or skipped or discarded:
        logger.warning("旧布局迁移完成：moved=%d discarded=%d skipped=%d failed=%d",
                       moved, discarded, skipped, failed)
    return {"pending": len(pending), "moved": moved, "failed": failed,
            "skipped": skipped, "discarded": discarded, "aborted": False}


# ── 首次启动引导：建目录 + 拷贝默认文件 ──
# 顺序铁律：迁移必须先于默认拷贝——否则默认文件占位导致用户数据迁移被跳过丢失。
# 2026-09-10：整段收敛进 run_startup_init()，由入口在**迁移之后**调用；不再在 import 期
# 执行。原实现的问题：服务器版入口 server_app.py 也 import 本模块 → 每次启动都在
# **共享** user_data 根建目录并写入 config.json / story/character/*.md（实测线上
# user_data/story/character/ 下的 3 个孤文件即由此产生，且不属于任何账号）。
# 默认源路径：frozen 时数据在 _internal/，开发时在 app/；BASE_DIR 已指向对应位置
# 注意：这是**模块级快照**，绑定 import 时的 USER_DIR。运行时改 USER_DIR 的场景
# （测试注入、将来多根部署）必须用 _defaults_map() 重算，不要直接用这个字典。
_DEFAULTS = {
    USER_DIR / "config.json": ROOT / "config.json",
    mode_character_dir() / "core.md": bundled_character_dir() / "core.md",
    mode_character_dir() / "identity.md": bundled_character_dir() / "identity.md",
    mode_character_dir() / "sms_samples.md": bundled_character_dir() / "sms_samples.md",
}


def _defaults_map() -> dict:
    """按**当前** USER_DIR / bundled 路径重算默认文件映射（{目标: 源}）。

    mode_character_dir 会 mkdir，故放在函数内、仅由 run_startup_init 调用（那里本就要建目录）。
    2026-09-10：原实现直接用模块级 _DEFAULTS，在 USER_DIR 被运行时替换的场景下会指向旧路径
    （自测发现：首启拷贝落到了上一个 USER_DIR）。"""
    return {
        USER_DIR / "config.json": ROOT / "config.json",
        mode_character_dir() / "core.md": bundled_character_dir() / "core.md",
        mode_character_dir() / "identity.md": bundled_character_dir() / "identity.md",
        mode_character_dir() / "sms_samples.md": bundled_character_dir() / "sms_samples.md",
    }


def _cleanup_stale_defaults() -> int:
    """启动时清理"未被用户修改过的"设定副本，让 bundled 的后续更新能送达（R-04）。

    背景：首启把 bundled 的 core/identity/sms_samples 拷进 user_data/{mode}/character/，
    而 resolve_character_file 无条件优先用户副本 → 此后**内置内容更新永远到不了老用户**
    （已实测：改 bundled 后 load_slot 读到的仍是旧副本）。
    判定算法与 routes_pack.get_pack_files 的 customized 完全一致（strip 后逐字比较）：
      相同 → 该副本无信息量，删除即可让读取回落到 bundled（下次更新自然跟随）；
      不同 → 用户改过，**保留**（决定权在用户，UI 的"恢复默认"可删）。
    幂等：删完即无副本可比，重复启动零动作。返回删除数量。"""
    removed = 0
    try:
        for dst, src in _defaults_map().items():
            if not dst.exists() or not src.exists():
                continue
            try:
                if dst.read_text(encoding="utf-8").strip() == src.read_text(encoding="utf-8").strip():
                    dst.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception as e:
        logger.warning("陈旧副本清理失败（不影响启动）: %s", e)
    if removed:
        logger.warning("已清理 %d 个未修改的设定副本（让内置包更新生效）", removed)
    return removed


def run_startup_init() -> dict:
    """本地版首启引导：建目录 + 清理"未修改的"设定副本。**服务器版不得调用**。

    必须在 run_legacy_migration() **之后**调用（迁移未跑就处理副本会占位，
    导致旧数据迁移被跳过而丢失）。

    2026-09-10（R-04）：**不再预拷贝 bundled 设定到用户区**。原实现首启把
    core/identity/sms_samples 拷进 user_data/{mode}/character/，而 resolve_character_file
    无条件优先用户副本 → 此后内置内容更新永远到不了老用户（已实测）。
    读取链路本身就是「用户副本 → bundled」回落，文件不存在也能正常读到 bundled，
    而用户一旦在软件内编辑就由 character_file_update 写出真正的用户副本
    ——预拷贝纯属多余，还制造了"陈旧遮蔽"。
    因此这里只做两件事：建目录 + 清理"与 bundled 逐字相同"的历史副本
    （这些副本无信息量，删掉即可让内置更新自然跟随；用户改过的副本一律保留）。"""
    created = 0
    USER_DIR.mkdir(parents=True, exist_ok=True)
    for _sub in ("stickers",):
        p = USER_DIR / _sub
        if not p.exists():
            p.mkdir(exist_ok=True)
            created += 1
    cleaned = _cleanup_stale_defaults()
    return {"dirs_created": created, "files_copied": 0, "stale_removed": cleaned}


def resolve_asset(path: str) -> Path:
    """静态资源解析：当前用户目录优先（服务器版账号隔离），退回 bundled；表情包再查 stickers/。
    包资产（assets/character/{包}/assets/{文件}）：用户副本 {user}/{包}/character/assets/ 优先
    （软件内编辑头像/封面的产物），再退回 bundled 包目录。"""
    u = (_user_ctx_dir() or USER_DIR) / path
    if u.exists():
        return u
    parts = Path(path).parts
    if (len(parts) == 5 and parts[0] == "assets" and parts[1] == "character"
            and parts[3] == "assets" and parts[2] in MODES):
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
        "analyzer_model": "deepseek-v4-flash-vision-exp",
        "organizer_model": "deepseek-v4-flash-vision-exp", "polisher_model": "deepseek-v4-flash-vision-exp",
        "retriever_model": "deepseek-v4-flash-vision-exp",
        "retriever_effort": "none", "analyzer_effort": "high",
        "polisher_effort": "high", "organizer_effort": "none",
        "retriever_temperature": 0.0, "polisher_temperature": 0.5,
        "proactive_enabled": False, "proactive_hard": 6, "proactive_soft": 0.35,
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
                cfg[key] = _clean_model(data.get(key, "deepseek-v4-flash-vision-exp"))
            _effort_defaults = {"retriever_effort": "none", "analyzer_effort": "high",
                                "polisher_effort": "high", "organizer_effort": "none"}
            for key in _effort_defaults:
                val = data.get(key, _effort_defaults[key])
                cfg[key] = val if val in VALID_EFFORTS else _effort_defaults[key]
            if "reply_model" in data and "polisher_model" not in data:
                rm = data.get("reply_model", "deepseek-v4-flash-vision-exp")
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
            # 主动性配置（0.8.1 默认关闭：避免测试/安静场景被动打扰；用户可在设置中开启）
            cfg["proactive_enabled"] = bool(data.get("proactive_enabled", False))
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
            "analyzer_model": config.get("analyzer_model", "deepseek-v4-flash-vision-exp"),
            "organizer_model": config.get("organizer_model", "deepseek-v4-flash-vision-exp"),
            "polisher_model": config.get("polisher_model", "deepseek-v4-flash-vision-exp"),
            "retriever_model": config.get("retriever_model", "deepseek-v4-flash-vision-exp"),
            "retriever_effort": config.get("retriever_effort", "none"),
            "analyzer_effort": config.get("analyzer_effort", "high"),
            "polisher_effort": config.get("polisher_effort", "high"),
            "organizer_effort": config.get("organizer_effort", "none"),
            "retriever_temperature": config.get("retriever_temperature", 0.0),
            "polisher_temperature": config.get("polisher_temperature", 0.5),
            "proactive_enabled": bool(config.get("proactive_enabled", False)),
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
        # C-2（2026-09-13）：托管 Key **只认 FIREFLY_PROXY_KEY**。
        # 原来还回落 DEEPSEEK_API_KEY——那是运营者个人的按量计费主 Key，
        # 一旦 FIREFLY_PROXY_KEY 忘记配置，托管模式的消耗会静默打到主余额上
        # （账单口径与套餐额度彻底脱钩，且没有任何日志提示）。
        # 宁可托管模式不可用（返回 None → 前端提示需要 Key），也不静默烧主余额。
        key = os.environ.get("FIREFLY_PROXY_KEY", "").strip()
        if not key:
            logger.warning("托管模式已启用但未配置 FIREFLY_PROXY_KEY，拒绝回落主 Key（C-2）")
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
