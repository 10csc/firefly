# -*- coding: utf-8 -*-
"""聊天操作端点（阶段 2.8 自 api/chat.py 拆出，纯移动无行为变化）

职责：rest（休息整理）/ undo / clear-history / wake-status / open-mode / proactive-status / time。
对话主链（合并窗口 + 流水线 + chat/hint/flush + stage/history）留在 api/chat.py；
本模块经 api/chat re-export，api/router 与 routes.py 兼容层、既有测试的取用路径不变。
"""

import json
import logging
import time

from modules import app_config as cfg
from modules import sync_state
from modules.context_manager import ContextManager
from routes_auth import _auth_server_base
from routes_common import (_body_mode, _body_mode_ex, _is_server, _query_mode, _read_json,
                           get_session)

logger = logging.getLogger(__name__)


def rest(h):
    client = cfg.get_client()
    if not client:
        h._json({"ok": False, "error": "未设置 API Key"})
        return
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        from modules.memory_manager import MemoryManager
        from modules.conversation_store import load_all_context, count_user_turns, conv_file
        try:
            _root = conv_file(mode).parent.parent
            _st0 = sync_state.load(_root)
            _txn = sync_state.begin_txn(_root, "rest", {"base_rev": int(_st0.get("history_rev") or 0)})
        except sync_state.SyncRecoveryPending as e:
            h._json({"ok": False, "code": "SYNC_RECOVERY_PENDING",
                     "error": str(e)}, 503)
            return
        except Exception:
            _root, _txn = None, None
        mm = MemoryManager(client, cfg.MODEL, mode=mode)
        # ★ 2026-09-18 修复：这里的两个入参过去都取自**内存里的会话上下文**
        #   （get_full() / turn_count），而它被 hydrate_context 的窗口截断过。
        #   rest 的切片游标 last_integrated_turn 是**磁盘全量**轮号，两者口径不一致：
        #   长对话重启一次后 → 游标写入窗口长度 → verify_index 判定正常不修
        #   → 下次切片为空 → 永久"无新对话，跳过"，记忆再也不更新且零报错。
        #   现在两个口径都以**盘上全量**为准。
        #   注意必须用 load_all_context（role 形状）而不是 load_all（who 形状）——
        #   见 load_all_context 的 docstring。
        full_history = load_all_context(mode)
        from modules.auto_rest import keep_turns as _keep_turns
        result = mm.rest(full_history, count_user_turns(mode),
                         today=_resolve_today(), keep_turns=_keep_turns())
        # 休息成功后也更新手账
        # （原文进历史存档这件事已在 MemoryManager.rest 内部做掉——手动与自动
        #   走同一条链，不会再产生两套长期记忆）
        if result.success:
            mm.update_journal(full_history[-100:])
            from modules.llm_base import reload_journal
            reload_journal(mode)
            # 立即刷新当前会话的 memory_head：新头部随下一条消息生效，
            # 不再等进程重启 / 30 会话淘汰（真 bug 修复）
            session["memory_head"] = mm.load_head()
            try:
                root = conv_file(mode).parent.parent
                st = sync_state.load(root)
                sync_state.record_rest(root, int(st.get("history_rev") or 0))
                if _txn is not None:
                    sync_state.commit_txn(root, _txn)
                else:
                    sync_state.append_op(root, "rest", {"base_rev": int(st.get("history_rev") or 0)})
            except Exception as e:
                logger.warning("整理：写同步操作失败: %s", e)
        elif _txn is not None and _root:
            sync_state.abort_txn(_root)
    # skipped：无新对话可整理（LLM 未运行，added/resolved 均为 0——前端显示"没有新内容"而非"记忆已更新"）
    skipped = bool(result.error) and result.success
    h._json({"ok": result.success, "added": len(result.added_entries),
             "resolved": len(result.resolved_entries), "error": result.error,
             "skipped": skipped,
             # 0.8.1：文件级结果（弹窗文案用真实变化，LLM 的 added/resolved 数组可能为空
             # 但头部/手账仍更新了——只报"新增0条"会误导用户）
             "head_changed": bool(result.new_head.strip()),
             "journal_updated": True})


def get_time(h):
    """GET /time：服务器时钟（YYYY-MM-DD + 时间戳）。
    记忆整理等需要"今天"的场合用它做权威时钟（本地版经后端转发到认证服务器；
    _resolve_today：服务器优先、不可达时本地时钟兜底）。"""
    h._json({"ok": True, "date": time.strftime("%Y-%m-%d"),
             "ts": int(time.time())})


def _resolve_today() -> str:
    """rest 等场景的"今天"：服务器时间优先，本地设备时钟兜底。
    - 服务器版：进程就在业务服务器上，取本机时钟即服务器时间（无网络往返）；
    - 本地版：请求认证服务器 /time（0.8s 超时），成功用服务器日期，失败用设备时钟
      （离线宽限内 rest 仍可用）。"""
    if _is_server():
        return time.strftime("%Y-%m-%d")
    import urllib.request
    try:
        req = urllib.request.Request(_auth_server_base() + "/time")
        with urllib.request.urlopen(req, timeout=0.8) as r:
            data = json.loads(r.read().decode("utf-8"))
        d = str(data.get("date", "") or "").strip()
        import re as _re
        if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            return d
    except Exception:
        pass
    return time.strftime("%Y-%m-%d")


def undo(h):
    body = _read_json(h)
    mode = _body_mode(body)
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        result = session["context"].pop_last_turn()
        from modules.conversation_store import remove_last_turn, conv_file
        try:
            _root = conv_file(mode).parent.parent
            _txn = sync_state.begin_txn(_root, "retract")
        except sync_state.SyncRecoveryPending as e:
            h._json({"ok": False, "code": "SYNC_RECOVERY_PENDING",
                     "error": str(e)}, 503)
            return
        except Exception:
            _root, _txn = None, None
        removed, removed_mids = remove_last_turn(mode=mode, return_mids=True)
        if removed:
            try:
                root = conv_file(mode).parent.parent
                sync_state.add_tombstones(root, removed_mids)
                if _txn is not None:
                    _txn["payload"] = {"mids": removed_mids}
                    sync_state.commit_txn(root, _txn)
                else:
                    sync_state.append_op(root, "retract", {"mids": removed_mids})
            except Exception as e:
                logger.warning("撤回：写墓碑失败: %s", e)
        elif _txn is not None and _root:
            sync_state.abort_txn(_root)
        # 语音随撤回同步清理（docs/工具/tts.md §6）：以"文件里还剩的最大 seq"为准，
        # 不依赖 remove_last_turn 的返回值口径 —— 对 seq 间隙、连续撤回都成立。
        try:
            import voice
            n = voice.purge_after_undo(mode)
            if n:
                logger.info("撤回：同时删除语音 %d 个（mode=%s）", n, mode)
        except Exception as e:
            logger.warning("撤回时删除语音失败（不影响主流程）: %s", e)
        # 撤回后回退 .memory_index：若整合游标 > 当前轮数（说明被撤回轮次
        # 已被记过数），必须压低游标，否则后续 rest 按 turn 号切片会把新的
        # 对话全部误判为"已整理过"而永远跳过（真 bug 修复）
        # 阶段 3.6：memory_manager.verify_index() 已在 wake/rest 入口兜底自愈；
        # 这里保留即时回写只是为了让"撤回后立刻休息"这一类路径不依赖兜底时机。
        try:
            from modules.memory_manager import _index_file
            from modules.conversation_store import count_user_turns
            fp = _index_file(mode)
            if fp.exists():
                idx = json.loads(fp.read_text(encoding="utf-8"))
                last_turn = int(idx.get("last_integrated_turn", 0))
                # 以文件为准：重启后内存 context 只回灌近期轮次，
                # session["context"].turn_count 小于真实进度会把游标误压低；
                # 注意必须用用户轮数（与游标同口径），不可用消息总行数
                cur_turn = count_user_turns(mode=mode)
                if last_turn > cur_turn:
                    idx["last_integrated_turn"] = cur_turn
                    fp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("撤回后记忆游标回退失败: %s", e)
    # 以文件为准：重启后内存 context 为空但文件仍有历史，文件删成功就算成功
    if removed > 0 or result is not None:
        h._json({"ok": True, "removed_turn": 1, "files_removed": removed})
    else:
        h._json({"ok": False, "error": "没有可撤回的轮次"})


def clear_history(h):
    body = _read_json(h)
    mode, fell_back = _body_mode_ex(body)
    if fell_back:
        h._json({"ok": False, "code": "MODE_NOT_FOUND",
                 "error": "当前角色包不存在，已拒绝清理历史"}, 400)
        return
    session = get_session(body.get("session_id", "default"), mode)
    with session["lock"]:
        try:
            from modules.conversation_store import conv_file
            root = conv_file(mode).parent.parent
        except Exception as e:
            logger.exception("清理历史：定位模式目录失败 mode=%s", mode)
            h._json({"ok": False, "code": "SYNC_CLEAR_PREPARE_FAILED",
                     "error": f"清理事务准备失败：{e}"}, 500)
            return
        # 与完整同步回合共用模式锁；会话锁只保护内存上下文，不能隔离
        # clear 的文件/epoch/outbox 与 sync_now 的状态/上传/拉取窗口。
        with sync_state.mode_lock(root):
            try:
                resume_clear = False
                if sync_state.txn_path(root).exists():
                    try:
                        pending = json.loads(
                            sync_state.txn_path(root).read_text(encoding="utf-8"))
                        resume_clear = isinstance(pending, dict) and pending.get("type") == "clear"
                    except Exception:
                        resume_clear = False
                    if not sync_state.recover_txn(root):
                        h._json({"ok": False, "code": "SYNC_CLEAR_PARTIAL",
                                 "error": "历史清理尚未完整提交，请稍后重试；现场已保留"},
                                503)
                        return
                if not resume_clear:
                    sync_state.begin_txn(root, "clear", {"reason": "user_clear_history"})
            except sync_state.SyncRecoveryPending as e:
                h._json({"ok": False, "code": "SYNC_RECOVERY_PENDING",
                         "error": str(e)}, 503)
                return
            except Exception as e:
                logger.exception("清理历史：事务准备失败 mode=%s", mode)
                h._json({"ok": False, "code": "SYNC_CLEAR_PREPARE_FAILED",
                         "error": f"清理事务准备失败：{e}"}, 500)
                return

            # 数据文件、epoch/rev 与 outbox 都走同一恢复事务。保存或提交失败时保留
            # PREPARED/COMMITTED 现场，下一次入口先恢复，避免“文件已清、状态未落盘”假成功。
            if not resume_clear and not sync_state.recover_txn(root):
                h._json({"ok": False, "code": "SYNC_CLEAR_PARTIAL",
                         "error": "历史清理尚未完整提交，请稍后重试；现场已保留"},
                        503)
                return

            session["context"] = ContextManager()
            # 手账进的是回复器的 system 稳定层（进程内缓存），必须同步失效，
            # 否则清完历史后流萤还在"记得"手账里的旧约定。
            try:
                from modules.llm_base import reload_journal
                reload_journal(mode)
            except Exception as e:
                logger.warning("清理历史：重载手账失败: %s", e)
            # 记忆头部同理：本会话缓存的 memory_head 要立刻换成"无运行时记忆"的基线，
            # 不然当前这个会话到进程结束前都还在用已删掉的旧摘要。
            try:
                from modules.memory_manager import wake as memory_wake
                session["memory_head"] = memory_wake(cfg.get_client(), cfg.MODEL, mode)
            except Exception as e:
                session["memory_head"] = ""
                logger.warning("清理历史：重载记忆头失败（置空）: %s", e)
            # 会话聊天产生的数据全部随历史清理（配置与角色卡资产除外，见上）：
            # 文件已经由事务处理；这里只复位进程内信号量并清零忽视计数。
            try:
                from modules.proactive import _active_set, reset_states
                _active_set(mode, 1)
                # C-6.1（2026-09-13）：这里原来写的是 _IGNORED.pop(mode) / _HIDDEN.pop(mode)，
                # 而这些表的键是 (mode, 用户作用域) 元组 → 按字符串 pop 从来删不掉任何东西：
                # 清了历史，降档惩罚与隐藏式冷却仍在（"清空后角色依然不主动开口"）。
                reset_states(mode)
            except Exception:
                pass
            # 语音、pipeline 与 proactive_log 已由 CLEAR 事务的
            # `_clear_local_history_files` 一并处理；事务提交后不再做可失败的文件副作用。
        h._json({"ok": True})


def get_wake_status(h):
    from modules.memory_manager import _index_file, _memory_file
    mode = _query_mode(h)
    interrupted = _index_file(mode).exists() and not _memory_file(mode).exists()
    h._json({"interrupted": interrupted, "has_memory": _memory_file(mode).exists()})


def open_mode(h):
    """模式开场演出：包内存在 opening.json 且首次进入时返回自动首条消息（旁白+角色的话）。

    幂等保护：会话已有历史时不再重复开场（重进不重演）。
    """
    body = _read_json(h)
    mode = _body_mode(body)
    from modules.llm_base import resolve_character_file
    if resolve_character_file("opening.json", mode).exists():
        from modules.conversation_store import get_total_count
        if get_total_count(mode=mode) == 0:
            from orchestrator import preset_opening
            msgs = preset_opening(mode)
            h._json({"messages": msgs, "opened": True})
            return
    h._json({"messages": [], "opened": False})


def proactive_status(h):
    """主动性检查入口：REPLY 预占用 → 主动式/概率式串联判断 → 生成 → 写盘。

    前端轮询调用（空闲时）；每次调用都是独立判断，门控不通过则零成本返回
    {"messages": []}。生成的主动消息直接写盘，返回 messages 供前端即时渲染
    （与 /chat 返回格式一致）。

    信号量：REPLY 非阻塞预占用（忙碌则放弃）；ACTIVE 在 proactive 模块内
    管理（主动式/概率式互斥 + 用户回应复位 + 超时恢复）。
    """
    client = cfg.get_client()
    if not client or cfg.relay_needs_key():
        # 服务器版 relay 模式用户未带 Key：零成本返回，避免轮询线程空等 120s relay 超时
        h._json({"messages": []}); return
    body = _read_json(h)
    mode = _body_mode(body)
    session_id = body.get("session_id", "default")

    from modules.proactive import check_and_generate, reply_try_lock, reply_unlock
    if not reply_try_lock(mode):
        h._json({"messages": []}); return   # 回复通道忙（响应式生成中/其他主动生成中）
    try:
        session = get_session(session_id, mode)
        with session["lock"]:
            result = check_and_generate(
                session, client, mode=mode,
                enabled=bool(cfg.pack_cfg(mode, "proactive_enabled", True)),
                hard=cfg.pack_cfg(mode, "proactive_hard", 6),
                soft=cfg.pack_cfg(mode, "proactive_soft", 0.35),
                prob_enabled=bool(cfg.pack_cfg(mode, "prob_reply_enabled", True)),
                prob_value=cfg.pack_cfg(mode, "prob_reply_value", 0.10),
                polisher_model=cfg.eff_cfg("polisher_model"),
                polisher_effort=cfg.eff_cfg("polisher_effort"),
                polisher_temperature=cfg.eff_cfg("polisher_temperature"),
                organizer_model=cfg.eff_cfg("organizer_model"),
                organizer_effort=cfg.eff_cfg("organizer_effort"),
                memory_head=session.get("memory_head", ""),
            )
    finally:
        reply_unlock(mode)
    if not result.messages or result.discarded:
        h._json({"messages": [], "reason": result.reason_type})
        return
    h._json({"messages": result.messages, "proactive": True})
