# -*- coding: utf-8 -*-
"""诊断包导出（本地导出，**不经服务器**）—— 用户报 bug 时把这一份文件发过来即可。

设计（用户 2026-09-19 拍板："走本地导出文件/压缩包，要能导向 QQ"）：
  · **只在本地版提供**：服务器版一律 403（与 /requests /pipeline 同理 —— 那些是全站缓冲，
    且"用户把诊断包传到服务器"会引入一条全新的上传链路与合规问题，明确不做）。
  · **导出到用户自己的设备**：`Content-Disposition: attachment` 触发浏览器/WebView 下载，
    文件落在本机；之后由用户通过 QQ 发出去。服务端**不接触**这份数据。
  · **脱敏是硬要求**（见 docs/未完成事项清单.md P1-2 的验收标准）：
      - API Key 只留"是否已设置 + 长度"，**任何形态的 key 都不进包**；
      - 对话/记忆/手账/存档**正文一律不进包**，只给"轮数 / 字节数 / 是否有"；
      - `pipeline` 只保留"形状"（阶段名、耗时、输入输出**长度**），输入输出正文丢弃
        —— 这是最容易被忽略的一处：pipeline 原本存的就是各阶段完整输入输出（含角色与
        用户的对话原文）。
  · 包内附 `README.txt` 写清"这份包里有什么、没有什么"，用户敢发、我收得明白。

为什么单独一个模块而不是塞进 api/debug.py：导出是个"打包 + 脱敏"的独立职责，
和"读端点"不是一回事；而且它有一套**只增不减的脱敏白名单**，放一起容易被后来人顺手加字段。
"""
from __future__ import annotations

import io
import json
import os
import platform
import sys
import time
import zipfile

from modules import app_config as cfg


def _redact_config() -> dict:
    """配置脱敏：Key 只留"已设置 + 长度"，其余原样（模型/温度/服务器地址等排障要用的留着）。

    为什么保留 api_base：排障时"用户是不是指向了自定义供应商"是高频问题；
    地址本身不是秘密。
    """
    from core import config as C
    d = dict(C.config or {})
    providers = []
    for p in (d.get("providers") or []):
        if not isinstance(p, dict):
            continue
        k = str(p.get("api_key") or "")
        providers.append({**{kk: vv for kk, vv in p.items() if kk != "api_key"},
                          "api_key_set": bool(k),
                          "api_key_len": len(k)})
    out = {k: v for k, v in d.items() if k not in ("api_key", "providers")}
    k = str(d.get("api_key") or "")
    out["api_key_set"] = bool(k)
    out["api_key_len"] = len(k)
    out["providers"] = providers
    return out


def _pipeline_shape() -> list:
    """pipeline 只留形状：阶段名 / 长度 / 是否存在。**正文一律丢弃**。"""
    try:
        from orchestrator import get_pipeline_log
        log = get_pipeline_log(20, mode=cfg.DEFAULT_MODE)
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]
    out = []
    for e in log or []:
        if not isinstance(e, dict):
            continue
        row = {"time": e.get("time"), "mode": e.get("mode"),
               "user_input_len": len(str(e.get("user_input") or "")),
               "messages": len(e.get("messages") or [])}
        for stage in ("retriever", "analyzer", "polisher", "organizer"):
            v = e.get(stage)
            if v is None:
                row[stage] = None
            elif isinstance(v, dict):
                row[stage] = {k: (len(str(x)) if isinstance(x, (str, list, dict)) else x)
                              for k, x in v.items()}
            else:
                row[stage] = len(str(v))
        out.append(row)
    return out


def _conversation_stats() -> dict:
    """每个包：轮数 / 手账与记忆"有没有 / 多少字节"。**不含正文**。"""
    out = {}
    for m in cfg.MODES:
        root = cfg.mode_root(m)
        item = {}
        for name, rel in (("conversation", "data/conversation.jsonl"),
                          ("user_memory", "data/memory.md"),
                          ("journal", "journal/手账.md"),
                          ("pipeline_log", "data/pipeline.jsonl")):
            fp = root / rel
            try:
                item[name] = {"exists": fp.is_file(),
                              "bytes": fp.stat().st_size if fp.is_file() else 0}
            except OSError:
                item[name] = {"exists": False, "bytes": 0}
        try:
            from modules.conversation_store import count_user_turns
            item["user_turns"] = count_user_turns(mode=m)
        except Exception:
            item["user_turns"] = None
        try:
            from modules.memory_archive import archive_months
            item["archive_months"] = archive_months(m)
        except Exception:
            item["archive_months"] = []
        out[m] = item
    return out


def _summary() -> dict:
    d = {}
    d["app_version"] = cfg.APP_VERSION
    d["mode_default"] = cfg.DEFAULT_MODE
    d["modes"] = list(cfg.MODES)
    d["platform"] = {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine(), "python": sys.version.split()[0],
                     "frozen": bool(getattr(sys, "frozen", False)),
                     "android": bool(os.environ.get("FIREFLY_ANDROID")),
                     "server": bool(os.environ.get("FIREFLY_SERVER"))}
    d["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    for name, fn in (("hotupdate", "/hotupdate/status"), ("notice", "/notice/status")):
        pass
    try:
        from hotupdate import status as hu_status
        s = hu_status()
        d["hotupdate"] = {k: s.get(k) for k in
                          ("base_version", "applied_serial", "applied_hash", "pending_serial",
                           "boot_fail_count", "overlay_files", "enabled", "verify_backend",
                           "verify_ok", "last_error", "rolled_back_reason")}
    except Exception as e:
        d["hotupdate"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        import notice as NT
        s = NT.status()
        d["notice"] = {k: s.get(k) for k in ("serial", "entries", "images", "last_check",
                                             "last_error", "verify_backend")}
    except Exception as e:
        d["notice"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from modules.auto_rest import status as rest_status
        d["memory"] = rest_status(cfg.DEFAULT_MODE)
    except Exception as e:
        d["memory"] = {"error": f"{type(e).__name__}: {e}"}
    return d


def _metrics() -> dict:
    try:
        from modules import metrics as M
        snap = getattr(M, "snapshot", None)
        if callable(snap):
            return snap()
    except Exception:
        pass
    return {"note": "本版本没有可用的 metrics.snapshot()"}


def _requests() -> list:
    """请求记录：本身就是元数据（model/token/费用），不含正文，原样带上。"""
    try:
        from modules.llm_base import get_request_log
        return get_request_log(200) or []
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]


README = """Firefly 诊断包（本地导出）
================================

这份文件是「Firefly」在你本机生成的诊断包，用来定位问题。
它**没有上传到任何服务器** —— 是你点「导出」时直接下载到你设备上的。

怎么用
------
把它发到 QQ 群 1097936258，或直接发给开发者，并顺手写一句：
  · 你点了什么 / 期望什么 / 实际出现什么
  · 大概什么时间出现的（包里有时间戳，对上就行）

包里有什么
----------
summary.json            版本 / 平台 / 热更状态 / 公告状态 / 记忆窗口（**无正文**）
config.redacted.json    配置（API Key 只留"是否已设置 + 长度"）
requests.json           每次模型调用的 模块/模型/token/费用（**无正文**）
pipeline.shape.json     四个阶段的名字、耗时、输入输出的**长度**（**正文已丢弃**）
conversation-stats.json 每个角色包：对话轮数、手账/记忆/存档的**字节数**（**无正文**）
metrics.json            进程内指标
env.txt                 运行环境（Python 版本 / 是否冻结版 / 是否安卓）

包里**没有**什么（这点很重要）
------------------------------
· 没有你的 API Key（只有"已设置，长度 51"这种信息）
· 没有你与角色的任何对话正文、记忆、手账、存档原文
· 没有图片、表情包文件

如果你不放心，可以先把 zip 解开看一眼（就是几个 json 文本文件）。
"""


def build_diagnostics_zip() -> bytes:
    """组装诊断包（纯内存，不落盘）。返回 zip 字节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def add(name: str, obj) -> None:
            z.writestr(name, json.dumps(obj, ensure_ascii=False, indent=1))
        add("summary.json", _summary())
        add("config.redacted.json", _redact_config())
        add("requests.json", _requests())
        add("pipeline.shape.json", _pipeline_shape())
        add("conversation-stats.json", _conversation_stats())
        add("metrics.json", _metrics())
        z.writestr("env.txt",
                   f"python {sys.version}\nfrozen={bool(getattr(sys, 'frozen', False))}\n"
                   f"android={bool(os.environ.get('FIREFLY_ANDROID'))}\n"
                   f"platform={platform.platform()}\n")
        z.writestr("README.txt", README)
    return buf.getvalue()


def export_diagnostics(h):
    """GET /export-diagnostics → 诊断包 zip（本地导出，不经服务器）。"""
    if os.environ.get("FIREFLY_SERVER"):
        h._json({"ok": False,
                 "error": "服务器版不提供诊断包导出（诊断包只在你自己的设备上生成）"}, 403)
        return
    try:
        data = build_diagnostics_zip()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("诊断包生成失败")
        h._json({"ok": False, "error": f"诊断包生成失败：{type(e).__name__}: {e}"}, 500)
        return
    fname = f"firefly-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f'attachment; filename="{fname}"')
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)
