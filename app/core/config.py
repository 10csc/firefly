# -*- coding: utf-8 -*-
"""配置读写与供应商 — config.json 的加载/保存 + 供应商 CRUD + 客户端构造（任务 2.1 自 app_config 拆出）

`config` 是运行期唯一的全局配置字典（原地更新，引用稳定）；`get_client()` 依上下文
分派 relay / proxy / direct 三种客户端。
"""

import json
import os
import logging

from core import paths as _paths
from core import userctx as _userctx
from core.paths import AUTH_SERVER_DEFAULT, mode_data_dir
from core.userctx import _user_ctx, _user_overlay, user_scope_key

logger = logging.getLogger(__name__)


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


# ── 版本号（唯一权威源）──────────────────────────
# 发版铁律：改这里必须同步改 4 处：
#   1. 本文件 APP_VERSION
#   2. app/static/js/update.js 的 CURRENT_VERSION（0.9.0 起 app.js 已拆分为 js/ 模块）
#   3. android/app/build.gradle.kts 的 versionName
#   4. package/firefly.iss 的 AppVersion
# 用 tools/check_version.py 一键校验四者一致；格式 x.y.z 纯数字点分，
# 禁止 -beta/-rc 后缀（Gitee 无 prerelease 概念，后缀会污染 releases/latest）。
APP_VERSION = "0.9.0"
API_BASE = "https://api.deepseek.com/v1"
# OpenCode Go 兼容端点（OpenAI 兼容 chat/completions，模型 ID 与 DeepSeek 一致）
GO_BASE = "https://opencode.ai/zen/go/v1"
MODEL = "deepseek-flash"

# ── 供应商（A8 多供应商；2026-08-21）────────────────
# 结构：providers=[{id,name,base_url,api_key,models[],caps{}}] + active_provider=id
# caps 能力位（DeepSeek 私有能力按供应商分支）：
#   thinking=支持 extra_body thinking/reasoning_effort；reasoning=解析 reasoning_content；
#   vision=支持 image_url 多模态；prompt_cache=回传缓存命中统计；models_endpoint=GET /models
# 内置建议清单（v1 时代 VALID_MODELS/端点白名单的替代物：建议 + 用户自由输入，不做硬白名单）
SUGGESTED_PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-flash", "deepseek-v4-pro"],
     "caps": {"thinking": True, "reasoning": True, "vision": True,
              "prompt_cache": True, "models_endpoint": True}},
    {"id": "opencode-go", "name": "OpenCode Go", "base_url": "https://opencode.ai/zen/go/v1",
     "models": ["mimo-v2.5", "mimo-v2.5-free"],
     "caps": {"thinking": False, "reasoning": False, "vision": False,
              "prompt_cache": False, "models_endpoint": False}},
]
# 模型名：官方英文名（UI 下拉建议用 deepseek 官方清单；不再用「快速/更强」中文档位）
SUGGESTED_MODELS = ["deepseek-flash", "deepseek-v4-pro"]
VALID_EFFORTS = ("none", "low", "high", "max")

# 旧模型名归一化（2026-09-15）：历史默认 deepseek-v4-flash(-vision-exp) 不在官方 /models 列表
# （官方实测：deepseek-flash / deepseek-v4-pro），老配置加载时幂等映射到官方名；用户自改的非旧名不动。
_MODEL_RENAME = {"deepseek-v4-flash": "deepseek-flash",
                 "deepseek-v4-flash-vision-exp": "deepseek-flash"}

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
        "analyzer_model": "deepseek-flash",
        "organizer_model": "deepseek-flash", "polisher_model": "deepseek-flash",
        "retriever_model": "deepseek-flash",
        "retriever_effort": "none", "analyzer_effort": "high",
        "polisher_effort": "high", "organizer_effort": "none",
        "retriever_temperature": 0.0, "polisher_temperature": 0.5,
        "proactive_enabled": False, "proactive_hard": 6, "proactive_soft": 0.35,
        "prob_reply_enabled": True, "prob_reply_value": 0.10,
        "hidden_reply_enabled": True,
    }
    data = None
    try:
        data = json.loads(_paths.CONFIG_FILE.read_text(encoding="utf-8"))
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
                from core.migrations import _migrate_legacy_providers   # 延迟导入：migrations 依赖本模块的供应商常量（单向循环）
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
                raw_model = data.get(key, "deepseek-flash")
                cfg[key] = _clean_model(_MODEL_RENAME.get(raw_model, raw_model))
            _effort_defaults = {"retriever_effort": "none", "analyzer_effort": "high",
                                "polisher_effort": "high", "organizer_effort": "none"}
            for key in _effort_defaults:
                val = data.get(key, _effort_defaults[key])
                cfg[key] = val if val in VALID_EFFORTS else _effort_defaults[key]
            if "reply_model" in data and "polisher_model" not in data:
                rm = data.get("reply_model", "deepseek-flash")
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
    except Exception as e:
        # B1（审计 2026-09-15）：损坏（而非缺失）的 config.json 不再静默吞——先告警
        # 并备份留证再回退默认值。原逻辑下损坏现场会被下一次 save_config 覆盖，
        # 用户只看到"要重新填 Key"，没有任何线索可查。
        if _paths.CONFIG_FILE.exists():
            logger.warning("config.json 解析失败（回退默认值）: %s", e)
            _backup_corrupt_config()
    if data is not None and not isinstance(data, dict):
        # B1（审计 2026-09-15）：JSON 合法但不是对象（list/str/数字）——同样静默
        # 回退默认值，同样备份留证
        logger.warning("config.json 内容不是 JSON 对象（回退默认值）")
        _backup_corrupt_config()
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


def _backup_corrupt_config() -> None:
    """损坏的 config.json 备份留证（B1，审计 2026-09-15）：
    拷贝为 config.json.corrupt-<时间戳>，失败仅告警（不阻塞启动）。"""
    try:
        fp = _paths.CONFIG_FILE
        if not fp.exists():
            return
        import shutil
        from modules.storage import date_stamp
        bak = fp.with_name(f"{fp.name}.corrupt-{date_stamp()}")
        shutil.copy2(fp, bak)
        logger.warning("已备份损坏配置: %s", bak.name)
    except OSError as e:
        logger.warning("损坏配置备份失败: %s", e)


def _preserve_key_from_disk() -> list:
    """★ 写入期不变量（2026-09-20）：**不许用"空 Key"覆盖磁盘上已有的非空 Key**。

    真机事故：用户手机里的 API Key 被清空，且**没有任何提示**（`/config` 里
    `has_key` 变 false，登录态却仍在 ⇒ 不是数据目录被清，而是配置被回写时 Key 丢了）。
    会写配置的路径至少四条，任何一条漏判都会造成这种静默数据丢失；因此在**唯一落盘口**
    统一设防，将来新增端点也自动安全。

    做三件事：
      1) 内存里某个 provider 的 Key 为空、而磁盘上同 id 是非空 → 把磁盘的救回来；
      2) 内存的 providers 列表**整表替换**时把"磁盘上有 Key 但已不在列表里"的那一项补回；
      3) 救回后重新派生顶层字段（`_sync_derived`），否则 `/config` 仍显示 has_key=false。

    返回被救回的 provider id 列表（供日志与测试断言）。
    """
    import json as _json
    import logging as _logging
    saved = []
    try:
        fp = _paths.CONFIG_FILE
        if not fp.exists():
            return saved
        disk = _json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(disk, dict):
            return saved
        disk_providers = [d for d in (disk.get("providers") or []) if isinstance(d, dict)]
        by_id = {str(d.get("id")): d for d in disk_providers}
        mem = config.get("providers") or []
        # (1) 同 id 救回
        for it in mem:
            if not isinstance(it, dict):
                continue
            if not (it.get("api_key") or "").strip():
                old = by_id.get(str(it.get("id")))
                old_key = (old or {}).get("api_key") or ""
                if old_key.strip():
                    it["api_key"] = old_key
                    saved.append(str(it.get("id")))
        # (2) 列表被整表替换：磁盘上"有 Key 但已不在内存列表里"的项补回
        mem_ids = {str(i.get("id")) for i in mem if isinstance(i, dict)}
        for d in disk_providers:
            if str(d.get("id")) not in mem_ids and (d.get("api_key") or "").strip():
                mem.append(d)
                saved.append(str(d.get("id")) + "(补回)")
        if not mem and disk_providers:
            config["providers"] = disk_providers
            saved.append("*全部补回")
        if saved:
            try:
                _sync_derived()
            except Exception:
                pass
            # 只记"发生了 Key 救回"，**绝不记录 Key 本身**
            caller = "?"
            try:
                import sys as _sys
                fr = _sys._getframe(2)          # 0=本函数 1=save_config 2=真正的调用方
                caller = f"{fr.f_code.co_filename.split(chr(92))[-1].split('/')[-1]}:" \
                         f"{fr.f_lineno}:{fr.f_code.co_name}"
            except Exception:
                pass
            _logging.getLogger(__name__).warning(
                "保存配置时拦下 Key 清空（已从磁盘救回 %s）；调用方 %s", saved, caller)
    except Exception as e:      # 不变量本身失败也不能阻塞保存
        try:
            import logging as _l
            _l.getLogger(__name__).warning("Key 保留检查失败（按原样继续）: %s", e)
        except Exception:
            pass
    return saved


def save_config() -> None:
    # ★ 先跑写入期不变量：空 Key 不许覆盖磁盘上已有的非空 Key（见 _preserve_key_from_disk）
    _preserve_key_from_disk()
    # 只落盘 providers 结构（containing api_key）、active_provider 与其它设置；
    # 不写顶层 api_key/api_base（旧字段迁移后废除）
    # B1（审计 2026-09-15）：裸 write_text → 原子写。写盘中断留下截断 JSON，下次
    # 启动解析失败静默回退默认值——供应商/Key/模型全部消失且无任何提示
    # （同目录 packs.json 早已用 atomic_write_json，config 漏了）。
    # 失败仅告警不抛（storage 约定），磁盘上保持旧值。
    from modules.storage import atomic_write_json
    atomic_write_json(_paths.CONFIG_FILE, {
            "providers": config.get("providers") or [_deepseek_preset()],
            "active_provider": config.get("active_provider", "deepseek"),
            "auth_server_url": config.get("auth_server_url", AUTH_SERVER_DEFAULT),
            "analyzer_model": config.get("analyzer_model", "deepseek-flash"),
            "organizer_model": config.get("organizer_model", "deepseek-flash"),
            "polisher_model": config.get("polisher_model", "deepseek-flash"),
            "retriever_model": config.get("retriever_model", "deepseek-flash"),
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
        })


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
                           quota_fn=_userctx._proxy_quota_checker, timeout=120.0,
                           counter_fn=_userctx._proxy_quota_counter, fail_fn=_userctx._proxy_quota_failer)
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
