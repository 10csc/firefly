# -*- coding: utf-8 -*-
"""配置与供应商路由（routes.py 拆分产物，纯重构无行为变化）：
set_key / set_config / check_key / get_config / get_models / get_balance。"""

import json
import logging
import os
import threading
from urllib.parse import urlparse, parse_qs

from modules import app_config as cfg

from routes_common import _read_json, _is_server

logger = logging.getLogger(__name__)

# 用户设置覆盖的读-改-写串行化（C-6.12，2026-09-13）
# 服务器版每请求一个线程（ThreadingHTTPServer），/set-config 是
# 「读 settings.json → 改 → 整份写回」，同账号的两个并发请求会互相覆盖：
# 后写的整份替换，前一个刚改的字段静默消失（前端 400ms 防抖 + 多设备同时改都能触发）。
# 锁**按 uid 分**而不是全局：不同账号之间没有共享数据，不该给无关用户排队。
_OVERLAY_LOCKS: dict[int, threading.Lock] = {}
_OVERLAY_LOCKS_GUARD = threading.Lock()


def _overlay_lock(uid: int) -> threading.Lock:
    """取该用户的覆盖文件锁（懒建；键数量 = 活跃账号数，量级很小，不做过期回收）。"""
    with _OVERLAY_LOCKS_GUARD:
        lk = _OVERLAY_LOCKS.get(uid)
        if lk is None:
            lk = threading.Lock()
            _OVERLAY_LOCKS[uid] = lk
        return lk


def _load_overlay_file(uid: int) -> dict:
    """从磁盘读该用户的 settings.json（不存在/损坏 → 空覆盖，与请求入口同一口径）。

    为什么必须在锁内**重新读盘**：请求入口（server_app._setup_user_context）已经把
    overlay 装进了本请求的 contextvar，那是**请求开始时**的快照。若拿它当读-改-写基准，
    并发下后到的请求会用自己的旧快照整份覆盖回去，锁就白加了（丢更新）。
    只有重读磁盘才能拿到"上一个请求刚写进去的结果"。"""
    try:
        fp = cfg.USER_DIR / str(uid) / "settings.json"
        if fp.exists():
            ov = json.loads(fp.read_text(encoding="utf-8"))
            return ov if isinstance(ov, dict) else {}
    except Exception as e:
        logger.warning("读取用户设置覆盖失败（按空覆盖继续）: %s", e)
    return {}


def set_key(h):
    # 服务器版：Key 只存用户浏览器（X-API-Key 头），禁写服务器全局配置（防落盘 + 全局串 Key）
    if _is_server():
        h._json({"ok": False, "error": "服务器版请在设置面板填写 Key（仅存本机浏览器）"}, 403)
        return
    body = _read_json(h)
    # A8：Key 写入激活供应商（providers 结构，不再写顶层字段）
    _p = cfg.active_provider()
    _p["api_key"] = (body.get("api_key") or "").strip()
    cfg._sync_derived()
    cfg.save_config()
    h._json({"ok": bool(cfg.config["api_key"])})


def set_config(h):
    # 局部绑定：call-time 从 routes 取，保持 patch("routes._read_json") 拦截面不变
    from routes import _read_json
    body = _read_json(h)
    is_proxy = (h.headers.get("X-API-Mode", "") or "").strip().lower() == "proxy"
    if _is_server():
        # A2：服务器版 per-user 覆盖——只写 user_data/{uid}/settings.json（全站默认不被污染）
        _USER_KEYS = ("analyzer_model", "organizer_model", "polisher_model", "retriever_model",
                      "retriever_effort", "analyzer_effort", "polisher_effort", "organizer_effort",
                      "retriever_temperature", "polisher_temperature",
                      "proactive_enabled", "proactive_hard", "proactive_soft",
                      "prob_reply_enabled", "prob_reply_value", "hidden_reply_enabled")
        _uid = cfg.user_dir_id()
        # C-6.12：整个「读盘 → 改 → 写盘」持该用户的锁，基准取锁内重读的结果
        with _overlay_lock(_uid):
            overlay = _load_overlay_file(_uid)
            for key in _USER_KEYS:
                if key not in body:
                    continue
                if key.endswith("_model"):
                    overlay[key] = "mimo-v2.5" if is_proxy else cfg._clean_model(body[key], "deepseek-v4-flash-vision-exp")
                elif key.endswith("_temperature"):
                    try:
                        overlay[key] = max(0.0, min(2.0, float(body[key])))
                    except (TypeError, ValueError):
                        pass
                elif key.endswith("_effort"):
                    if body[key] in cfg.VALID_EFFORTS:
                        overlay[key] = body[key]
                elif key.startswith("proactive_hard"):
                    try:
                        overlay[key] = max(1, min(10, int(body[key])))
                    except (TypeError, ValueError):
                        pass
                elif key in ("proactive_soft", "prob_reply_value"):
                    try:
                        overlay[key] = max(0.0, min(1.0, float(body[key])))
                    except (TypeError, ValueError):
                        pass
                else:
                    overlay[key] = bool(body[key])
            try:
                from modules.storage import atomic_write_json
                ov_path = cfg.USER_DIR / str(_uid) / "settings.json"
                atomic_write_json(ov_path, overlay)
                cfg.set_user_overlay(overlay)
            except Exception as e:
                logger.warning("用户设置覆盖保存失败: %s", e)
        h._json({
            "ok": True,
            "active_provider": cfg.config.get("active_provider", "deepseek"),
            "analyzer_model": overlay.get("analyzer_model", cfg.config["analyzer_model"]),
            "organizer_model": overlay.get("organizer_model", cfg.config["organizer_model"]),
            "polisher_model": overlay.get("polisher_model", cfg.config["polisher_model"]),
            "retriever_model": overlay.get("retriever_model", cfg.config["retriever_model"]),
            "retriever_effort": overlay.get("retriever_effort", cfg.config["retriever_effort"]),
            "analyzer_effort": overlay.get("analyzer_effort", cfg.config["analyzer_effort"]),
            "polisher_effort": overlay.get("polisher_effort", cfg.config["polisher_effort"]),
            "organizer_effort": overlay.get("organizer_effort", cfg.config["organizer_effort"]),
            "retriever_temperature": overlay.get("retriever_temperature", cfg.config["retriever_temperature"]),
            "polisher_temperature": overlay.get("polisher_temperature", cfg.config["polisher_temperature"]),
            "proactive_enabled": bool(overlay.get("proactive_enabled", cfg.config.get("proactive_enabled", True))),
            "proactive_hard": overlay.get("proactive_hard", cfg.config.get("proactive_hard", 6)),
            "proactive_soft": overlay.get("proactive_soft", cfg.config.get("proactive_soft", 0.35)),
            "prob_reply_enabled": bool(overlay.get("prob_reply_enabled", cfg.config.get("prob_reply_enabled", True))),
            "prob_reply_value": overlay.get("prob_reply_value", cfg.config.get("prob_reply_value", 0.10)),
            "hidden_reply_enabled": bool(overlay.get("hidden_reply_enabled", cfg.config.get("hidden_reply_enabled", True))),
        })
        return
    # ── 本地版（单用户写全站配置，行为同 0.8.0）──
    # 服务器版：剥离 api_key 字段（Key 不落服务器全局配置；模型/主动性等全局参数照常）
    new_key = "" if _is_server() else (body.get("api_key") or "").strip()
    # A8：模型名自由输入（官方英文名，无白名单）；仅服务器托管（proxy）模式锁 mimo-v2.5
    for key in ("analyzer_model", "organizer_model", "polisher_model", "retriever_model"):
        val = body.get(key, cfg.config[key])
        if _is_server() and is_proxy:
            cfg.config[key] = "mimo-v2.5"
        else:
            cfg.config[key] = cfg._clean_model(val, cfg.config[key])
    # 供应商管理（本地版：整表替换 + 激活切换；服务器版供应商配置在浏览器 localStorage，不走这里）
    if not _is_server():
        if "providers" in body:
            validated = cfg.normalize_providers(body.get("providers"))
            # 空 Key 视为「保留原 Key」（前端不回传全量 Key，只能看到 has_key 状态）
            for p in validated:
                if not p["api_key"]:
                    old = cfg.provider_by_id(p["id"])
                    if old:
                        p["api_key"] = old.get("api_key", "")
            if validated:
                cfg.config["providers"] = validated
        if "active_provider" in body:
            cfg.set_active_provider(str(body.get("active_provider") or "").strip())
        # 兼容旧接口：api_base → 当前激活供应商 base_url；api_key → 激活供应商 Key
        if "api_base" in body:
            _base = str(body.get("api_base") or "").strip().rstrip("/")
            _p = cfg.active_provider()
            if cfg._valid_http_base(_base):
                _p["base_url"] = _base
                cfg._sync_derived()
    for key in ("retriever_effort", "analyzer_effort", "polisher_effort", "organizer_effort"):
        val = body.get(key, cfg.config[key])
        if val in cfg.VALID_EFFORTS:
            cfg.config[key] = val
    try:
        t = float(body.get("retriever_temperature", cfg.config["retriever_temperature"]))
        cfg.config["retriever_temperature"] = max(0.0, min(2.0, t))
    except (TypeError, ValueError):
        pass
    try:
        t = float(body.get("polisher_temperature", cfg.config["polisher_temperature"]))
        cfg.config["polisher_temperature"] = max(0.0, min(2.0, t))
    except (TypeError, ValueError):
        pass
    eff = body.get("polisher_effort", cfg.config["polisher_effort"])
    if eff in cfg.VALID_EFFORTS:
        cfg.config["polisher_effort"] = eff
    # 主动性插件配置（v2：轮次硬约束 + 概率软约束，替代 v1 时间制）
    if "proactive_enabled" in body:
        cfg.config["proactive_enabled"] = bool(body.get("proactive_enabled"))
    if "proactive_hard" in body:
        try:
            ph = int(body.get("proactive_hard", 6))
            cfg.config["proactive_hard"] = max(1, min(10, ph))
        except (TypeError, ValueError):
            pass
    if "proactive_soft" in body:
        try:
            ps = float(body.get("proactive_soft", 0.35))
            cfg.config["proactive_soft"] = max(0.0, min(1.0, ps))
        except (TypeError, ValueError):
            pass
    # 概率式回复配置
    if "prob_reply_enabled" in body:
        cfg.config["prob_reply_enabled"] = bool(body.get("prob_reply_enabled"))
    if "prob_reply_value" in body:
        try:
            pv = float(body.get("prob_reply_value", 0.10))
            cfg.config["prob_reply_value"] = max(0.0, min(1.0, pv))
        except (TypeError, ValueError):
            pass
    # 隐藏式回复配置（独立开关，关前台概率式不影响隐藏式）
    if "hidden_reply_enabled" in body:
        cfg.config["hidden_reply_enabled"] = bool(body.get("hidden_reply_enabled"))
    if new_key:
        # 本地版：Key 写入激活供应商（providers 结构）
        _p = cfg.active_provider()
        _p["api_key"] = new_key
        cfg._sync_derived()
    cfg.save_config()
    h._json({
        "ok": bool(cfg.config["api_key"]),
        "active_provider": cfg.config.get("active_provider", "deepseek"),
        "api_base": cfg.config.get("api_base", cfg.API_BASE),
        "analyzer_model": cfg.eff_cfg("analyzer_model"),
        "organizer_model": cfg.eff_cfg("organizer_model"),
        "polisher_model": cfg.eff_cfg("polisher_model"),
        "retriever_model": cfg.eff_cfg("retriever_model"),
        "retriever_effort": cfg.eff_cfg("retriever_effort"),
        "analyzer_effort": cfg.eff_cfg("analyzer_effort"),
        "polisher_effort": cfg.eff_cfg("polisher_effort"),
        "organizer_effort": cfg.eff_cfg("organizer_effort"),
        "retriever_temperature": cfg.eff_cfg("retriever_temperature"),
        "polisher_temperature": cfg.eff_cfg("polisher_temperature"),
        "proactive_enabled": bool(cfg.eff_cfg("proactive_enabled", True)),
        "proactive_hard": cfg.eff_cfg("proactive_hard", 6),
        "proactive_soft": cfg.eff_cfg("proactive_soft", 0.35),
        "prob_reply_enabled": bool(cfg.eff_cfg("prob_reply_enabled", True)),
        "prob_reply_value": cfg.eff_cfg("prob_reply_value", 0.10),
        "hidden_reply_enabled": bool(cfg.eff_cfg("hidden_reply_enabled", True)),
    })


def check_key(h):
    # 服务器版：has_key 反映当前用户请求头的 Key（relay 模式 get_client 恒非 None，
    # 不能用它判断）；本地版：等价于原 bool(get_client())
    h._json({"has_key": cfg.user_has_key()})


def _platform_tag() -> str:
    """当前运行平台：pc（本地版 Windows，可退出）/ android（本地版安卓）/ server（服务器版）。"""
    if os.environ.get("FIREFLY_ANDROID"):
        return "android"
    if os.environ.get("FIREFLY_SERVER"):
        return "server"
    return "pc"


def get_config(h):
    key = cfg.active_provider().get("api_key", "") or cfg.config.get("api_key", "")
    # 服务器版不返回 key_prefix：全局/env 兜底 Key 的前缀也不能向登录用户暴露
    key_prefix = "" if _is_server() else (key[:12] + "..." if key else "")
    # 供应商列表：Key 只回 has_key + 前缀（不回全量），服务器版供应商配置在浏览器（localStorage）
    providers = []
    for p in (cfg.config.get("providers") or []):
        pk = p.get("api_key", "")
        providers.append({
            "id": p["id"], "name": p.get("name", p["id"]),
            "base_url": p.get("base_url", ""),
            "models": p.get("models", []),
            "caps": p.get("caps", {}),
            "has_key": bool(pk) if not _is_server() else False,
            "key_prefix": (pk[:12] + "...") if (pk and not _is_server()) else "",
        })
    h._json({
        "platform": _platform_tag(),
        "has_key": bool(cfg.get_api_key()),
        "key_prefix": key_prefix,
        "active_provider": cfg.config.get("active_provider", "deepseek"),
        "providers": providers,
        "suggested_providers": cfg.SUGGESTED_PROVIDERS,
        "api_base": cfg.config.get("api_base", cfg.API_BASE),
        "api_bases": [cfg.API_BASE, cfg.GO_BASE],
        "analyzer_model": cfg.eff_cfg("analyzer_model"),
        "organizer_model": cfg.eff_cfg("organizer_model"),
        "polisher_model": cfg.eff_cfg("polisher_model"),
        "retriever_model": cfg.eff_cfg("retriever_model"),
        "retriever_effort": cfg.eff_cfg("retriever_effort"),
        "analyzer_effort": cfg.eff_cfg("analyzer_effort"),
        "polisher_effort": cfg.eff_cfg("polisher_effort"),
        "organizer_effort": cfg.eff_cfg("organizer_effort"),
        "retriever_temperature": cfg.eff_cfg("retriever_temperature"),
        "polisher_temperature": cfg.eff_cfg("polisher_temperature"),
        "proactive_enabled": bool(cfg.eff_cfg("proactive_enabled", True)),
        "proactive_hard": cfg.eff_cfg("proactive_hard", 6),
        "proactive_soft": cfg.eff_cfg("proactive_soft", 0.35),
        "prob_reply_enabled": bool(cfg.eff_cfg("prob_reply_enabled", True)),
        "prob_reply_value": cfg.eff_cfg("prob_reply_value", 0.10),
        "hidden_reply_enabled": bool(cfg.eff_cfg("hidden_reply_enabled", True)),
        "suggested_models": list(cfg.SUGGESTED_MODELS),
        "valid_models": list(cfg.SUGGESTED_MODELS),
        "valid_efforts": list(cfg.VALID_EFFORTS),
    })


def get_modes(h):
    """GET /modes：预设包清单（前端模式卡片/名称/封面/头像的数据源，角色预设化）。
    每包返回 id/name/presentation/avatar/cover/has_opening；资产 URL 存在才给（空串=无）。"""
    from modules.llm_base import resolve_character_file

    def _pack_asset_url(mode: str, fname: str) -> str:
        # 按槽位名 glob（任意图片扩展名）：用户副本优先，退回 bundled 包目录
        slot = fname.split(".")[0]
        for base in (cfg.mode_character_dir(mode) / "assets",
                     cfg.bundled_character_dir(mode) / "assets"):
            if base.is_dir():
                for fp in sorted(base.glob(f"{slot}.*")):
                    if fp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                        return f"/assets/character/{mode}/assets/{fp.name}"
        return ""

    items = []
    for mode in cfg.MODES:
        p = cfg.PRESETS.get(mode) or {}
        items.append({
            "id": mode,
            "name": p.get("name") or mode,
            "presentation": p.get("presentation", "sticker"),
            "char_name": p.get("char_name") or "",
            "custom": bool(p.get("custom")),
            "desc": p.get("desc") or "",
            "tagline": p.get("tagline") or "",
            "avatar": _pack_asset_url(mode, "avatar.png"),
            "cover": _pack_asset_url(mode, "cover.png"),
            "has_opening": resolve_character_file("opening.json", mode).exists(),
        })
    h._json({"modes": items, "default": cfg.DEFAULT_MODE})


def get_models(h):
    """GET /models?provider=<id>：用该供应商 Key 请求其 /models 取官方模型清单（A8）。
    本地版后端带 Key 转发；服务器版 relay 由前端直连（Key 在浏览器），本端点不起作用。"""
    if _is_server():
        h._json({"error": "服务器版模型清单由浏览器直连供应商获取"}, 403); return
    qs = parse_qs(urlparse(h.path).query)
    pid = (qs.get("provider", [""])[0] or "").strip()
    p = cfg.provider_by_id(pid)
    if p is None:
        h._json({"error": "供应商不存在"}, 404); return
    if not p.get("api_key"):
        h._json({"error": "请先填写该供应商的 API Key"}, 400); return
    import urllib.request
    try:
        req = urllib.request.Request(
            p["base_url"].rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {p['api_key']}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        ids = [str(m.get("id", "")) for m in (data.get("data") or []) if isinstance(m, dict)]
        ids = [i for i in ids if i]
        h._json({"ok": True, "provider": pid, "models": ids})
    except Exception as e:
        h._json({"error": f"获取模型列表失败: {e}"})


def get_balance(h):
    # 查询 DeepSeek 账户余额（仅 DeepSeek 官方端点与激活供应商 Key；其它供应商不支持）
    import urllib.request
    api_base = cfg.config.get("api_base", cfg.API_BASE)
    if not api_base.startswith("https://api.deepseek.com"):
        h._json({"error": "仅 DeepSeek 官方支持余额查询", "supported": False}); return
    try:
        req = urllib.request.Request(
            f"{cfg.API_BASE.replace('/v1', '')}/user/balance",
            headers={"Authorization": f"Bearer {cfg.get_api_key()}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            h._json(json.loads(r.read()))
    except Exception as e:
        h._json({"error": str(e)})
