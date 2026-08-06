# -*- coding: utf-8 -*-
"""API 路由 — 每个端点一个函数，POST_ROUTES / GET_ROUTES 分发

server 拆分产物：server.py 只留 HTTP 骨架（分发/响应工具/启动），
业务路由全部在这里。路由函数签名统一为 fn(h)，h 为 handler 实例，
通过 h._json(...) / h._serve_file(...) 回写响应。
"""

import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from modules import app_config as cfg
from modules.context_manager import ContextManager
from modules.multipart import parse_multipart
from orchestrator import handle_chat

logger = logging.getLogger(__name__)


# ── 会话状态 ─────────────────────────────────────
sessions: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_MAX = 30   # 会话上限：防任意 session_id 无限撑内存（超过删除最早创建的）


def get_session(sid: str) -> dict:
    # ThreadingHTTPServer 下并发首访同一 sid 会重复创建并互相覆盖 context，必须加锁
    with _SESSIONS_LOCK:
        if sid not in sessions:
            # 首次创建会话：加载记忆头部（无记忆/中断/异常都降级为空串，不阻塞会话）
            from modules.memory_manager import wake as memory_wake
            from modules.conversation_store import hydrate_context
            client = cfg.get_client()
            memory_head = memory_wake(client, cfg.MODEL) if client else ""
            ctx = ContextManager()
            try:
                n = hydrate_context(ctx)
                if n:
                    logger.info("会话 %s 回灌 %d 轮历史", sid, n)
            except Exception as e:
                logger.warning("历史回灌失败（空上下文启动）: %s", e)
            sessions[sid] = {
                "context": ctx,
                "memory_head": memory_head,
                # 会话级锁：chat/rest/undo/clear-history 串行化，防并发读写竞态
                "lock": threading.Lock(),
            }
            # 超限清理（dict 保持插入序 = 创建序，删最早的一个）
            while len(sessions) > _SESSION_MAX:
                oldest = next(k for k in sessions if k != sid)
                del sessions[oldest]
        return sessions[sid]


# JSON 请求体上限：防异常大 body 吃内存（本地单用户，1MB 足够）
_MAX_BODY = 1_048_576


def _read_json(h) -> dict:
    length = int(h.headers.get("Content-Length", 0))
    if length <= 0:
        return {}
    if length > _MAX_BODY:
        return {}
    try:
        body = json.loads(h.rfile.read(length))
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


# ══ POST 路由 ═══════════════════════════════════

def set_key(h):
    body = _read_json(h)
    cfg.config["api_key"] = (body.get("api_key") or "").strip()
    cfg.save_config()
    h._json({"ok": bool(cfg.config["api_key"])})


def set_config(h):
    body = _read_json(h)
    new_key = (body.get("api_key") or "").strip()
    for key in ("analyzer_model", "organizer_model", "polisher_model", "retriever_model"):
        val = body.get(key, cfg.config[key])
        if val in cfg.VALID_MODELS:
            cfg.config[key] = val
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
    try:
        t = max(0.0, min(2.0, float(body.get("polisher_temperature", cfg.config["polisher_temperature"]))))
        cfg.config["polisher_temperature"] = t
    except (TypeError, ValueError):
        pass
    if new_key:
        cfg.config["api_key"] = new_key
    cfg.save_config()
    h._json({
        "ok": bool(cfg.config["api_key"]),
        "analyzer_model": cfg.config["analyzer_model"],
        "organizer_model": cfg.config["organizer_model"],
        "polisher_model": cfg.config["polisher_model"],
        "retriever_model": cfg.config["retriever_model"],
        "retriever_effort": cfg.config["retriever_effort"],
        "analyzer_effort": cfg.config["analyzer_effort"],
        "polisher_effort": cfg.config["polisher_effort"],
        "organizer_effort": cfg.config["organizer_effort"],
        "retriever_temperature": cfg.config["retriever_temperature"],
        "polisher_temperature": cfg.config["polisher_temperature"],
    })


def save_journal(h):
    body = _read_json(h)
    content = body.get("content", "")
    # 路径与 load_journal 同源，避免两处各写一遍公式再次分裂
    from modules.llm_base import JOURNAL_FILE, reload_journal
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_FILE.write_text(content, encoding="utf-8")
    reload_journal()
    h._json({"ok": True})


def check_key(h):
    h._json({"has_key": bool(cfg.get_client())})


def chat(h):
    client = cfg.get_client()
    if not client:
        h._json({"reply": None, "error": "请先设置 API Key", "need_key": True})
        return

    body = _read_json(h)
    session_id = body.get("session_id", "default")
    hint = (body.get("hint") or "").strip()

    # 即时写盘：用户消息一发就记。
    # 前端分条发送（messages 数组）→ 分条写盘（刷新后显示多条），
    # LLM 侧用合并文本（\n 连接，保持一轮处理）。
    # 表情包消息：{"type":"sticker","label":...} → 写盘 sticker 类型 + LLM 提示。
    from modules.conversation_store import append_message as _append_msg
    msgs = body.get("messages")
    llm_parts = []
    if isinstance(msgs, list):
        for m in msgs:
            # 统一消息对象类型：{"type":"text","content":...} / {"type":"sticker","label":...}
            if isinstance(m, dict) and m.get("type") == "text" and m.get("content"):
                text = str(m["content"]).strip()
                if text:
                    _append_msg("user", {"type": "text", "content": text})
                    llm_parts.append(text)
            elif isinstance(m, dict) and m.get("type") == "sticker" and m.get("label"):
                label = m["label"]
                path = m.get("path") or m.get("file") or ""
                if not path:
                    try:
                        from tools.sticker_picker import pick_sticker_by_label
                        entry = pick_sticker_by_label(label)
                        path = entry.file if entry else ""
                    except Exception:
                        path = ""
                if path:
                    _append_msg("user", {"type": "sticker", "label": label, "path": path})
                llm_parts.append(f"[表情包：{label}]")
            elif isinstance(m, str) and m.strip():
                # 兼容旧格式（纯字符串）
                _append_msg("user", {"type": "text", "content": m.strip()})
                llm_parts.append(m.strip())
    if llm_parts:
        user_input = "\n".join(llm_parts)
    else:
        user_input = (body.get("message") or "").strip()
        if user_input:
            _append_msg("user", {"type": "text", "content": user_input})

    session = get_session(session_id)
    # 会话级锁：同会话操作串行（chat 耗时长，防 undo/rest 并发读写 ctx）
    with session["lock"]:
        result = handle_chat(
            user_input, session, client,
            analyzer_model=cfg.config["analyzer_model"],
            organizer_model=cfg.config["organizer_model"],
            polisher_model=cfg.config["polisher_model"],
            retriever_model=cfg.config["retriever_model"],
            retriever_effort=cfg.config["retriever_effort"],
            analyzer_effort=cfg.config["analyzer_effort"],
            polisher_effort=cfg.config["polisher_effort"],
            organizer_effort=cfg.config["organizer_effort"],
            retriever_temperature=cfg.config["retriever_temperature"],
            polisher_temperature=cfg.config["polisher_temperature"],
            memory_head=session.get("memory_head", ""),
            hint=hint,
        )
    # 即时写盘：流萤回复每条立刻记，并把 time 回传给前端
    enriched = []
    for m in result.messages:
        record = {"type": m.get("type")}
        if m.get("type") == "text":
            record["content"] = m.get("content", "")
            seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]})
        elif m.get("type") == "sticker":
            record["path"] = m.get("path", "")
            record["label"] = m.get("label", "")
            seq, t = _append_msg("firefly", {"type": "sticker", "path": record["path"], "label": record["label"]})
        else:
            record["content"] = str(m)
            seq, t = _append_msg("firefly", {"type": "text", "content": record["content"]})
        record["time"] = t
        enriched.append(record)
    h._json({"messages": enriched})


def rest(h):
    client = cfg.get_client()
    if not client:
        h._json({"ok": False, "error": "未设置 API Key"})
        return
    body = _read_json(h)
    session = get_session(body.get("session_id", "default"))
    with session["lock"]:
        from modules.memory_manager import MemoryManager
        mm = MemoryManager(client, cfg.MODEL)
        full_history = session["context"].get_full()
        result = mm.rest(full_history, session["context"].turn_count)
        # 休息成功后也更新手账
        if result.success:
            mm.update_journal(full_history[-100:])
            from modules.llm_base import reload_journal
            reload_journal()
    h._json({"ok": result.success, "added": len(result.added_entries),
             "resolved": len(result.resolved_entries), "error": result.error})


def add_sticker_route(h):
    # multipart/form-data 解析：保存图片到 user_data/stickers/，写入 registry.json
    from tools.sticker_picker import add_sticker, StickerAddError
    try:
        fields, files = parse_multipart(h)
        category = fields.get("category", "")
        label = fields.get("label", "")
        file_info = files.get("file")
        if not file_info:
            h._json({"ok": False, "error": "缺少图片文件"}); return
        if category not in ("可爱", "帅气"):
            h._json({"ok": False, "error": "分类必须为 可爱/帅气"}); return
        if not label:
            h._json({"ok": False, "error": "缺少含义描述"}); return

        # 保存图片：用时间戳前缀避免重名
        original = file_info["filename"]
        ext = Path(original).suffix or ".png"
        safe_name = f"user_{int(time.time())}_{Path(original).stem}{ext}"
        save_dir = cfg.USER_DIR / "stickers"
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / safe_name).write_bytes(file_info["data"])

        # 写入注册表
        entry = add_sticker(f"stickers/{safe_name}", category, label)
        h._json({"ok": True, "sticker_id": entry.id, "label": entry.label})
    except StickerAddError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"上传失败: {e}"})


def sticker_update(h):
    from tools.sticker_picker import update_sticker, StickerUpdateError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    new_label = (body.get("label") or "").strip() or None
    new_category = (body.get("category") or "").strip() or None
    try:
        entry = update_sticker(sid, new_label=new_label, new_category=new_category)
        h._json({"ok": True, "id": entry.id, "label": entry.label, "category": entry.category})
    except StickerUpdateError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"修改失败: {e}"})


def sticker_delete(h):
    from tools.sticker_picker import delete_sticker, StickerDeleteError
    body = _read_json(h)
    sid = (body.get("id") or "").strip()
    try:
        delete_sticker(sid)
        h._json({"ok": True, "id": sid})
    except StickerDeleteError as e:
        h._json({"ok": False, "error": str(e)})
    except Exception as e:
        h._json({"ok": False, "error": f"删除失败: {e}"})


def character_file_update(h):
    body = _read_json(h)
    filename = (body.get("filename") or "").strip()
    content = (body.get("content") or "")
    # 白名单：仅允许用户维护的补充设定（核心设定 core/identity/sms_samples 隐藏且不可经 API 修改）
    allowed = {"用户设定.md"}
    if filename not in allowed:
        h._json({"ok": False, "error": f"不允许的文件: {filename}"}); return
    if not content:
        h._json({"ok": False, "error": "内容不能为空"}); return
    try:
        filepath = cfg.USER_DIR / "character" / filename
        filepath.write_text(content, encoding="utf-8")
        # 清除各模块的角色设定缓存
        from modules.llm_base import clear_cache
        clear_cache()
        from modules.polisher import clear_samples_cache
        clear_samples_cache()
        h._json({"ok": True, "filename": filename})
    except Exception as e:
        h._json({"ok": False, "error": f"保存失败: {e}"})


def undo(h):
    body = _read_json(h)
    session = get_session(body.get("session_id", "default"))
    with session["lock"]:
        result = session["context"].pop_last_turn()
        from modules.conversation_store import remove_last_turn
        removed = remove_last_turn()
    # 以文件为准：重启后内存 context 为空但文件仍有历史，文件删成功就算成功
    if removed > 0 or result is not None:
        h._json({"ok": True, "removed_turn": 1, "files_removed": removed})
    else:
        h._json({"ok": False, "error": "没有可撤回的轮次"})


def clear_history(h):
    body = _read_json(h)
    session = get_session(body.get("session_id", "default"))
    with session["lock"]:
        session["context"] = ContextManager()
        # 清空持久化文件
        from modules.conversation_store import _CONV_FILE
        try:
            if _CONV_FILE.exists():
                _CONV_FILE.write_text("", encoding="utf-8")
        except Exception:
            pass
        # 记忆整理进度必须同步归零：turn_count 已归零，旧 index 会让下次
        # 休息时把新对话全部误判为"已整理过"而跳过
        try:
            from modules.memory_manager import _INDEX_FILE
            if _INDEX_FILE.exists():
                _INDEX_FILE.write_text(
                    json.dumps({"last_integrated_turn": 0}, ensure_ascii=False),
                    encoding="utf-8")
        except Exception:
            pass
    h._json({"ok": True})


# ══ GET 路由 ════════════════════════════════════

def get_config(h):
    key = cfg.config.get("api_key", "")
    h._json({
        "has_key": bool(cfg.get_api_key()),
        "key_prefix": key[:12] + "..." if key else "",
        "analyzer_model": cfg.config["analyzer_model"],
        "organizer_model": cfg.config["organizer_model"],
        "polisher_model": cfg.config["polisher_model"],
        "retriever_model": cfg.config["retriever_model"],
        "retriever_effort": cfg.config["retriever_effort"],
        "analyzer_effort": cfg.config["analyzer_effort"],
        "polisher_effort": cfg.config["polisher_effort"],
        "organizer_effort": cfg.config["organizer_effort"],
        "retriever_temperature": cfg.config["retriever_temperature"],
        "polisher_temperature": cfg.config["polisher_temperature"],
        "valid_models": list(cfg.VALID_MODELS),
        "valid_efforts": list(cfg.VALID_EFFORTS),
    })


def get_metrics(h):
    from modules.metrics import collect
    h._json(collect())


def get_balance(h):
    # 查询 DeepSeek 账户余额（key 来源与 get_client 一致，兼容环境变量）
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{cfg.API_BASE.replace('/v1','')}/user/balance",
            headers={"Authorization": f"Bearer {cfg.get_api_key()}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            h._json(json.loads(r.read()))
    except Exception as e:
        h._json({"error": str(e)})


def get_requests(h):
    from modules.llm_base import get_request_log
    log = get_request_log(200)
    h._json({"requests": log, "count": len(log)})


def get_pipeline(h):
    # 每轮对话各阶段的输入/输出/思考过程（调试答非所问用）
    from orchestrator import get_pipeline_log
    log = get_pipeline_log(20)
    h._json({"pipeline": log, "count": len(log)})


def get_history(h):
    # 分页加载历史：?limit=150&before_seq=N
    from modules.conversation_store import load_recent, get_total_count, get_min_seq
    qs = parse_qs(urlparse(h.path).query)
    try:
        limit = int(qs.get("limit", ["150"])[0])
    except Exception:
        limit = 150
    before_seq_raw = qs.get("before_seq", [None])[0]
    before_seq = int(before_seq_raw) if before_seq_raw else None
    msgs = load_recent(limit=limit, before_seq=before_seq)
    total = get_total_count()
    # has_more：当前页最小 seq > 全局最小 seq 时还有更早历史
    cur_min = int(msgs[0]["seq"]) if msgs else 0
    has_more = cur_min > get_min_seq() if total > 0 else False
    h._json({"messages": msgs, "total": total, "has_more": has_more})


def get_wake_status(h):
    from modules.memory_manager import _INDEX_FILE, _MEMORY_FILE
    interrupted = _INDEX_FILE.exists() and not _MEMORY_FILE.exists()
    h._json({"interrupted": interrupted, "has_memory": _MEMORY_FILE.exists()})


def get_stickers(h):
    # 返回全量表情包列表（供前端管理表展示）
    from tools.sticker_picker import list_all_stickers, _STICKERS_DEFAULT
    h._json({
        "stickers": [
            {"id": s.id, "file": s.file, "category": s.category,
             "label": s.label, "is_default": s.id in _STICKERS_DEFAULT}
            for s in list_all_stickers()
        ],
    })


def get_character_files(h):
    from modules.llm_base import resolve_character_file
    files = []
    # 核心设定已隐藏，仅暴露用户可维护的补充设定文件
    for fname in ("用户设定.md",):
        fp = resolve_character_file(fname)
        if fp.exists():
            files.append({"name": fname, "content": fp.read_text(encoding="utf-8")})
    h._json({"files": files})


def get_user_memory(h):
    # 用户记忆 = 跨会话记忆文件（休息时自动整理），展示为可编辑
    from modules.memory_manager import _MEMORY_FILE
    content = _MEMORY_FILE.read_text(encoding="utf-8") if _MEMORY_FILE.exists() else ""
    h._json({"content": content})


def save_user_memory(h):
    from modules.memory_manager import _MEMORY_FILE
    body = _read_json(h)
    content = (body.get("content") or "")
    try:
        _MEMORY_FILE.write_text(content, encoding="utf-8")
        h._json({"ok": True})
    except Exception as e:
        h._json({"ok": False, "error": f"保存失败: {e}"})


def get_journal(h):
    from modules.llm_base import load_journal
    h._json({"content": load_journal()})


# ── 分发表 ───────────────────────────────────────
POST_ROUTES = {
    "/set-key": set_key,
    "/set-config": set_config,
    "/save-journal": save_journal,
    "/save-user-memory": save_user_memory,
    "/check-key": check_key,
    "/chat": chat,
    "/rest": rest,
    "/add-sticker": add_sticker_route,
    "/sticker-update": sticker_update,
    "/sticker-delete": sticker_delete,
    "/character-file-update": character_file_update,
    "/undo": undo,
    "/clear-history": clear_history,
}

GET_ROUTES = {
    "/check-key": check_key,
    "/config": get_config,
    "/metrics": get_metrics,
    "/balance": get_balance,
    "/requests": get_requests,
    "/pipeline": get_pipeline,
    "/history": get_history,
    "/wake-status": get_wake_status,
    "/stickers": get_stickers,
    "/character-files": get_character_files,
    "/user-memory": get_user_memory,
    "/journal": get_journal,
}
