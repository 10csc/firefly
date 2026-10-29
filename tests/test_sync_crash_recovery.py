# -*- coding: utf-8 -*-
"""hot5 故障注入：本地事务、服务器 pending 提交、损坏文件与原子写。

这套测试不依赖 UI 或旧同步测试。每个场景都构造真实文件状态，并模拟进程在
关键提交点退出；恢复后必须满足“副作用完成、op 可重试、重试幂等、绝不半提交”。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from modules import storage
from modules import sync_server_state as srv
from modules import sync_state

PASS = 0
FAIL = 0


def check(name: str, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  V", name)
    else:
        FAIL += 1
        print("  X", name, detail)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def read_favorites(root: Path) -> list[dict]:
    fp = root / "data" / "favorites.json"
    if not fp.exists():
        return []
    value = json.loads(fp.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else []


def run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="firefly_hot5_crash_"))
    os.environ["FIREFLY_DATA_DIR"] = str(tmp)
    os.environ["FIREFLY_ANDROID"] = "1"

    print("=== A. 本地 PREPARED：retract / clear 必须补完副作用 ===")
    root = tmp / "local-retract" / "story"
    conv = root / "data" / "conversation.jsonl"
    write_jsonl(conv, [
        {"seq": 1, "mid": "keep", "epoch": 0, "content": "保留"},
        {"seq": 2, "mid": "drop-a", "epoch": 0, "content": "撤回 A"},
        {"seq": 3, "mid": "drop-b", "epoch": 0, "content": "撤回 B"},
    ])
    sync_state.begin_txn(root, "retract", {"mids": ["drop-a", "drop-b"]})
    sync_state.recover_txn(root)
    mids = [x.get("mid") for x in read_jsonl(conv)]
    st = sync_state.load(root)
    outbox = sync_state.load_outbox(root)
    check("A1 retract 重放删除指定 mid", mids == ["keep"], repr(mids))
    check("A2 retract 墓碑已落盘",
          {"drop-a", "drop-b"} <= set(st.get("tombstones") or []))
    check("A3 retract 事务转为唯一 outbox",
          len(outbox) == 1 and outbox[0].get("type") == "retract" and
          not sync_state.txn_path(root).exists(), repr(outbox))
    sync_state.recover_txn(root)
    check("A4 重复恢复不重复入队", len(sync_state.load_outbox(root)) == 1)

    root2 = tmp / "local-clear" / "story"
    (root2 / "data" / "archive").mkdir(parents=True)
    (root2 / "journal").mkdir(parents=True)
    write_jsonl(root2 / "data" / "conversation.jsonl",
                [{"seq": 1, "mid": "x", "content": "旧消息"}])
    (root2 / "data" / "memory.md").write_text("旧记忆\n", encoding="utf-8")
    (root2 / "data" / ".memory_index").write_text("{}", encoding="utf-8")
    (root2 / "data" / "pipeline.jsonl").write_text("{}\n", encoding="utf-8")
    (root2 / "journal" / "手账.md").write_text("旧手账\n", encoding="utf-8")
    (root2 / "data" / "archive" / "old.md").write_text("旧归档\n", encoding="utf-8")
    sync_state.begin_txn(root2, "clear", {"reason": "crash-test"})
    sync_state.recover_txn(root2)
    st2 = sync_state.load(root2)
    check("A5 clear 重放清空对话",
          (root2 / "data" / "conversation.jsonl").read_text("utf-8") == "")
    check("A6 clear 重放删除记忆/手账/流水/归档",
          not (root2 / "data" / "memory.md").exists()
          and not (root2 / "data" / ".memory_index").exists()
          and not (root2 / "data" / "pipeline.jsonl").exists()
          and not (root2 / "journal" / "手账.md").exists()
          and not (root2 / "data" / "archive").exists())
    check("A7 clear 代际前进且 outbox 唯一",
          st2.get("history_epoch") == 1
          and len(sync_state.load_outbox(root2)) == 1
          and sync_state.load_outbox(root2)[0].get("type") == "clear")

    print("=== B. 本地 PREPARED：收藏增删必须能重放 ===")
    root3 = tmp / "local-favorite" / "story"
    (root3 / "data").mkdir(parents=True)
    item = {
        "id": "fav-crash", "source": "mid:crash-mid", "seq": 9,
        "who": "user", "type": "text", "content": "崩溃前收藏",
    }
    sync_state.begin_txn(root3, "favorite", {"id": item["id"], "item": item})
    sync_state.recover_txn(root3)
    favs = read_favorites(root3)
    check("B1 PREPARED 收藏补齐 favorites.json",
          [x.get("id") for x in favs] == ["fav-crash"], repr(favs))
    check("B2 收藏事务提交为 favorite op",
          len(sync_state.load_outbox(root3)) == 1
          and sync_state.load_outbox(root3)[0].get("type") == "favorite")
    check("B3 重收藏会撤销本地墓碑",
          "fav-crash" not in (sync_state.load(root3).get("favorite_tombstones") or []))

    sync_state.add_favorite_tombstone(root3, "stale")
    sync_state.begin_txn(root3, "unfavorite", {"id": "fav-crash"})
    sync_state.recover_txn(root3)
    out3 = sync_state.load_outbox(root3)
    st3 = sync_state.load(root3)
    check("B4 PREPARED 取消收藏重放物理删除", read_favorites(root3) == [])
    check("B5 取消收藏重放写墓碑",
          "fav-crash" in (st3.get("favorite_tombstones") or []))
    check("B6 两个已提交 op 都保留且同 id 去重",
          len(out3) == 2 and {x.get("type") for x in out3} == {"favorite", "unfavorite"},
          repr(out3))

    print("=== C. PREPARED rest 不伪造成功；COMMITTED 可在 outbox 前恢复 ===")
    root4 = tmp / "local-rest" / "story"
    (root4 / "data").mkdir(parents=True)
    sync_state.begin_txn(root4, "rest", {"base_rev": 3})
    sync_state.recover_txn(root4)
    check("C1 PREPARED rest 被丢弃且不产生 op",
          sync_state.load_outbox(root4) == []
          and sync_state.load(root4).get("memory_rev") == 0
          and not sync_state.txn_path(root4).exists())

    root5 = tmp / "local-committed" / "story"
    (root5 / "data").mkdir(parents=True)
    txn = sync_state.begin_txn(root5, "clear", {"reason": "committed"})
    committed = dict(txn)
    committed["state"] = "COMMITTED"
    committed["op"] = {
        "op_id": txn["op_id"], "type": txn["type"],
        "base_rev": txn["base_rev"], "payload": txn["payload"],
    }
    sync_state.txn_path(root5).write_text(
        json.dumps(committed, ensure_ascii=False), encoding="utf-8")
    with patch.object(sync_state, "_append_outbox_unique", return_value=False):
        sync_state.recover_txn(root5)
    check("C2 outbox 写失败时保留 COMMITTED 事务",
          sync_state.txn_path(root5).exists() and sync_state.load_outbox(root5) == [])
    sync_state.recover_txn(root5)
    check("C3 下次启动补写 outbox 后清事务",
          not sync_state.txn_path(root5).exists()
          and len(sync_state.load_outbox(root5)) == 1)

    print("=== D. 损坏事务/outbox 与原子写失败 ===")
    root6 = tmp / "local-corrupt" / "story"
    (root6 / "data").mkdir(parents=True)
    sync_state.txn_path(root6).write_text("{broken", encoding="utf-8")
    sync_state.recover_txn(root6)
    check("D1 损坏事务隔离为 .bad，不阻塞启动",
          not sync_state.txn_path(root6).exists()
          and sync_state.txn_path(root6).with_suffix(".bad").exists())
    sync_state.outbox_path(root6).write_text(
        '{"op_id":"ok","type":"favorite","payload":{"id":"x"}}\n'
        '{broken}\n[]\n'
        '{"op_id":"ok2","type":"unfavorite","payload":{"id":"y"}}\n',
        encoding="utf-8",
    )
    check("D2 损坏 outbox 行被跳过、有效行保留",
          [x.get("op_id") for x in sync_state.load_outbox(root6)] == ["ok", "ok2"])
    sync_state.clear_outbox(root6, ["ok"])
    check("D3 部分确认只删除目标 op",
          [x.get("op_id") for x in sync_state.load_outbox(root6)] == ["ok2"])

    fp = root6 / "data" / "atomic.md"
    fp.write_bytes(b"OLD\n")
    real_write_text = Path.write_text

    def fail_temp(self, *args, **kwargs):
        if ".tmp" in self.name:
            raise OSError("injected atomic write failure")
        return real_write_text(self, *args, **kwargs)

    with patch.object(Path, "write_text", fail_temp):
        ok = storage.atomic_write_text(fp, "NEW\n")
    check("D4 原子写失败返回 false 且旧内容不变",
          ok is False and fp.read_bytes() == b"OLD\n")
    check("D5 文本落盘固定为 UTF-8/LF",
          storage.atomic_write_text(fp, "A\nB\n") and fp.read_bytes() == b"A\nB\n")

    print("=== E. 服务器 pending two-phase：失败后 load 自愈 ===")
    sroot = tmp / "server-retract" / "story"
    (sroot / "data").mkdir(parents=True)
    write_jsonl(sroot / "data" / "conversation.jsonl", [
        {"seq": 1, "mid": "s-keep", "epoch": 0},
        {"seq": 2, "mid": "s-drop", "epoch": 0},
    ])
    with patch.object(srv, "_apply_side_effect", return_value=False):
        failed = srv.apply_ops(
            sroot, [{"op_id": "s-retract", "type": "retract",
                     "base_rev": 0, "payload": {"mids": ["s-drop"]}}])
    check("E1 数据副作用失败时返回明确 rejected",
          not failed["ok"] and failed["rejected"][0]["code"] == "SYNC_FILE_COMMIT_FAILED",
          repr(failed))
    raw_pending = json.loads(srv.state_path(sroot).read_text(encoding="utf-8"))
    check("E2 失败现场保留 pending_op",
          bool(raw_pending.get("pending_op")))
    recovered = srv.load(sroot)
    check("E3 下次 load 自动完成撤回副作用",
          [x.get("mid") for x in read_jsonl(sroot / "data" / "conversation.jsonl")]
          == ["s-keep"]
          and "s-drop" in recovered.get("tombstones", [])
          and recovered.get("pending_op") is None)

    sroot2 = tmp / "server-clear" / "story"
    (sroot2 / "data").mkdir(parents=True)
    write_jsonl(sroot2 / "data" / "conversation.jsonl",
                [{"seq": 1, "mid": "old", "content": "旧"}])
    (sroot2 / "data" / "memory.md").write_text("旧记忆\n", encoding="utf-8")
    with patch.object(srv, "_apply_side_effect", return_value=False):
        srv.apply_ops(sroot2, [{"op_id": "s-clear", "type": "clear", "base_rev": 0}])
    cleared = srv.load(sroot2)
    check("E4 服务器 clear 崩溃恢复清文件并推进 epoch",
          (sroot2 / "data" / "conversation.jsonl").read_text("utf-8") == ""
          and not (sroot2 / "data" / "memory.md").exists()
          and cleared.get("history_epoch") == 1
          and cleared.get("pending_op") is None)

    print("=== F. 服务器幂等、顺序 rev 与状态写失败 ===")
    sroot3 = tmp / "server-idem" / "story"
    (sroot3 / "data").mkdir(parents=True)
    op = {"op_id": "same-op", "type": "favorite",
          "payload": {"id": "fav-one"}}
    first = srv.apply_ops(sroot3, [op])
    second = srv.apply_ops(sroot3, [op])
    check("F1 响应丢失后重试不重复推进 rev",
          first["state"]["rev"] == 1 and second["state"]["rev"] == 1
          and second["skipped"][0].get("duplicate") is True)
    seq = srv.apply_ops(sroot3, [
        {"op_id": "seq-clear", "type": "clear", "base_rev": 0},
        {"op_id": "seq-retract", "type": "retract", "base_rev": 1,
         "payload": {"mids": ["m9"]}},
    ])
    check("F2 同一批 op 使用逐条最新 base_rev 顺序应用",
          seq["ok"] and seq["state"]["rev"] == 3
          and "m9" in seq["state"]["tombstones"], repr(seq))
    stale = srv.apply_ops(sroot3, [{
        "op_id": "stale-retract", "type": "retract", "base_rev": 0,
        "payload": {"mids": ["never"]},
    }])
    check("F3 stale base_rev 被拒且不推进 rev",
          not stale["ok"]
          and stale["rejected"][0]["code"] == "SYNC_OP_STALE_BASE_REV"
          and stale["state"]["rev"] == 3)

    sroot4 = tmp / "server-save-fail" / "story"
    (sroot4 / "data").mkdir(parents=True)
    with patch.object(srv, "save", return_value=False):
        failed_save = srv.apply_ops(sroot4, [{
            "op_id": "no-state", "type": "favorite", "payload": {"id": "f"}
        }])
    check("F4 状态写失败不执行数据副作用",
          not failed_save["ok"]
          and failed_save["rejected"][0]["code"] == "SYNC_STATE_WRITE_FAILED"
          and srv.load(sroot4)["rev"] == 0
          and not (sroot4 / "data" / "favorites.json").exists())

    print("=== G. 恢复持续失败：不得覆盖本地 txn / 不得接收服务器新 op ===")
    lroot = tmp / "local-retry-retract" / "story"
    write_jsonl(lroot / "data" / "conversation.jsonl", [
        {"seq": 1, "mid": "retry-keep", "epoch": 0},
        {"seq": 2, "mid": "retry-drop", "epoch": 0},
    ])
    original_txn = sync_state.begin_txn(
        lroot, "retract", {"mids": ["retry-drop"]})
    with patch.object(sync_state, "_remove_mids_from_conversation", return_value=False):
        first_recover = sync_state.recover_txn(lroot)
        before_begin = json.loads(
            sync_state.txn_path(lroot).read_text(encoding="utf-8"))
        begin_blocked = False
        try:
            sync_state.begin_txn(lroot, "clear")
        except sync_state.SyncRecoveryPending:
            begin_blocked = True
        after_begin = json.loads(
            sync_state.txn_path(lroot).read_text(encoding="utf-8"))
    check("G1 本地副作用持续失败时 recover 返回 false 且保留 txn",
          first_recover is False and sync_state.txn_path(lroot).exists())
    check("G2 新事务被拒绝且原 op_id 未被覆盖",
          begin_blocked
          and before_begin.get("op_id") == original_txn.get("op_id")
          and after_begin.get("op_id") == original_txn.get("op_id"))
    check("G3 故障解除后同一事务可恢复并进入 outbox",
          sync_state.recover_txn(lroot) is True
          and not sync_state.txn_path(lroot).exists()
          and [x.get("mid") for x in read_jsonl(
              lroot / "data" / "conversation.jsonl")] == ["retry-keep"]
          and sync_state.load_outbox(lroot)[0].get("op_id") == original_txn.get("op_id"))

    clear_root = tmp / "local-retry-clear" / "story"
    write_jsonl(clear_root / "data" / "conversation.jsonl",
                [{"seq": 1, "mid": "still-here"}])
    sync_state.begin_txn(clear_root, "clear")
    with patch.object(sync_state, "_clear_local_history_files", return_value=False):
        check("G4 clear 副作用失败保留 PREPARED",
              sync_state.recover_txn(clear_root) is False
              and sync_state.txn_path(clear_root).exists())
    check("G5 clear 重试后完成且不产生半提交",
          sync_state.recover_txn(clear_root) is True
          and sync_state.load(clear_root).get("history_epoch") == 1
          and sync_state.load_outbox(clear_root)[0].get("type") == "clear")

    fav_root = tmp / "local-retry-favorite" / "story"
    (fav_root / "data").mkdir(parents=True)
    fav = {"id": "fav-retry", "source": "mid:fav-retry",
           "who": "user", "type": "text", "content": "重试收藏"}
    sync_state.begin_txn(fav_root, "favorite", {"id": fav["id"], "item": fav})
    with patch.object(sync_state, "_restore_favorite_item", return_value=False):
        check("G6 favorite 副作用失败保留 PREPARED",
              sync_state.recover_txn(fav_root) is False
              and sync_state.txn_path(fav_root).exists())
    check("G7 favorite 重试补齐文件与 op",
          sync_state.recover_txn(fav_root) is True
          and [x.get("id") for x in read_favorites(fav_root)] == ["fav-retry"]
          and sync_state.load_outbox(fav_root)[0].get("type") == "favorite")
    sync_state.begin_txn(fav_root, "unfavorite", {"id": "fav-retry"})
    with patch.object(sync_state, "_remove_favorite_item", return_value=False):
        check("G8 unfavorite 副作用失败保留 PREPARED",
              sync_state.recover_txn(fav_root) is False
              and sync_state.txn_path(fav_root).exists())
    check("G9 unfavorite 重试删除并补墓碑",
          sync_state.recover_txn(fav_root) is True
          and read_favorites(fav_root) == []
          and "fav-retry" in (sync_state.load(fav_root).get("favorite_tombstones") or []))

    sretry = tmp / "server-retry-pending" / "story"
    write_jsonl(sretry / "data" / "conversation.jsonl", [
        {"seq": 1, "mid": "server-keep"},
        {"seq": 2, "mid": "server-drop"},
    ])
    first_op = {"op_id": "server-retry-1", "type": "retract",
                "base_rev": 0, "payload": {"mids": ["server-drop"]}}
    next_op = {"op_id": "server-retry-2", "type": "favorite",
               "payload": {"id": "fav-after-recovery"}}
    with patch.object(srv, "_apply_side_effect", return_value=False):
        srv.apply_ops(sretry, [first_op])
        blocked_server = srv.apply_ops(sretry, [next_op])
        pending_on_disk = json.loads(
            srv.state_path(sretry).read_text(encoding="utf-8"))
    check("G10 pending 恢复失败时拒绝新 op，不覆盖 pending",
          blocked_server.get("code") == "SYNC_RECOVERY_PENDING"
          and pending_on_disk.get("pending_op", {}).get("op", {}).get("op_id")
          == "server-retry-1")
    recovered_server = srv.load(sretry)
    after_recovery = srv.apply_ops(sretry, [next_op])
    check("G11 pending 恢复后可继续应用新 op",
          recovered_server.get("pending_op") is None
          and recovered_server.get("rev") == 1
          and after_recovery.get("ok")
          and after_recovery.get("state", {}).get("rev") == 2)

    sfinal = tmp / "server-final-save-pending" / "story"
    (sfinal / "data").mkdir(parents=True)
    real_server_save = srv.save

    def fail_commit_point(root, state):
        if state.get("pending_op") is None:
            return False
        return real_server_save(root, state)

    with patch.object(srv, "save", side_effect=fail_commit_point):
        srv.apply_ops(sfinal, [{
            "op_id": "final-save-1", "type": "favorite",
            "payload": {"id": "fav-final"},
        }])
        blocked_final = srv.apply_ops(sfinal, [{
            "op_id": "final-save-2", "type": "favorite",
            "payload": {"id": "fav-next"},
        }])
    check("G12 最终状态写失败也保持 pending 并拒绝新 op",
          blocked_final.get("code") == "SYNC_RECOVERY_PENDING"
          and json.loads(srv.state_path(sfinal).read_text(encoding="utf-8"))
          .get("pending_op", {}).get("op", {}).get("op_id") == "final-save-1")
    final_recovered = srv.load(sfinal)
    check("G13 最终状态写恢复只提交一次 rev",
          final_recovered.get("pending_op") is None
          and final_recovered.get("rev") == 1
          and len(final_recovered.get("ops") or []) == 1)

    print("=== H. 响应错包与远端状态落盘故障：先验后写 ===")
    from api import data_sync
    from modules import app_config as cfg

    response_root_parent = tmp / "response-guard"
    token = cfg.set_user_context(user_dir=response_root_parent)
    try:
        root = response_root_parent / "story"
        conv = root / "data" / "conversation.jsonl"
        write_jsonl(conv, [{"seq": 1, "mid": "keep-state", "epoch": 0}])
        before_conv = conv.read_bytes()

        def state_mismatch(path, payload=None, binary=False):
            return {"ok": True, "mode": "haruno",
                    "state": {"history_epoch": 9, "history_rev": 9}}

        err = data_sync._sync_one_mode_body(state_mismatch, "story", {})
        check("H1 state 错包在应用前失败，local bytes 不变",
              "haruno" in err and conv.read_bytes() == before_conv
              and sync_state.current_epoch(root) == 0, err)

        sync_state.append_op(root, "clear", {"reason": "response-guard"})
        before_outbox = sync_state.outbox_path(root).read_bytes()

        def ops_mismatch(path, payload=None, binary=False):
            if path.startswith("/sync/state"):
                return {"ok": True, "mode": "story",
                        "state": {"history_epoch": 0, "history_rev": 0}}
            return {"ok": True, "mode": "haruno",
                    "state": {"history_epoch": 8, "history_rev": 8}}

        err = data_sync._sync_one_mode_body(ops_mismatch, "story", {})
        check("H2 ops 错包在清 outbox/应用 state 前失败",
              "haruno" in err
              and sync_state.outbox_path(root).read_bytes() == before_outbox
              and sync_state.current_epoch(root) == 0
              and conv.read_bytes() == before_conv, err)

        sync_state.outbox_path(root).unlink()

        def manifest_mismatch(path, payload=None, binary=False):
            if path.startswith("/sync/state"):
                return {"ok": True, "mode": "story",
                        "state": {"history_epoch": 0, "history_rev": 0}}
            return {"ok": True, "mode": "haruno", "files": {}, "state_rev": 0}

        err = data_sync._sync_one_mode_body(manifest_mismatch, "story", {})
        check("H3 manifest 错包在文件计划前失败",
              "haruno" in err and conv.read_bytes() == before_conv, err)
    finally:
        cfg.reset_user_context(token)

    remote_root = tmp / "remote-apply" / "story"
    write_jsonl(remote_root / "data" / "conversation.jsonl",
                [{"seq": 1, "mid": "remote-old"}])
    with patch.object(sync_state, "_clear_local_history_files", return_value=False):
        clear_error = ""
        try:
            sync_state.apply_remote_state(remote_root, {
                "history_epoch": 1, "history_rev": 1, "memory_rev": 0,
            })
        except OSError as exc:
            clear_error = str(exc)
    check("H4 远端新 epoch 清理失败时不推进本地 epoch",
          bool(clear_error) and sync_state.current_epoch(remote_root) == 0,
          clear_error)

    save_root = tmp / "remote-save" / "story"
    write_jsonl(save_root / "data" / "conversation.jsonl",
                [{"seq": 1, "mid": "save-keep"}])
    with patch.object(sync_state, "save", return_value=False):
        save_error = ""
        try:
            sync_state.apply_remote_state(save_root, {
                "history_epoch": 0, "history_rev": 2, "memory_rev": 1,
                "tombstones": ["gone"],
            })
        except OSError as exc:
            save_error = str(exc)
    check("H5 远端 state 保存失败不返回已合并状态",
          bool(save_error) and sync_state.load(save_root)["tombstones"] == [],
          save_error)

    archive_root = tmp / "server-archive-fail" / "story"
    write_jsonl(archive_root / "data" / "conversation.jsonl",
                [{"seq": 1, "mid": "archive-old"}])
    (archive_root / "data" / "archive").mkdir(parents=True)
    (archive_root / "data" / "archive" / "old.md").write_text("OLD\n", encoding="utf-8")
    with patch("shutil.rmtree", side_effect=OSError("injected archive failure")):
        archive_failed = srv.apply_ops(archive_root, [{
            "op_id": "archive-clear", "type": "clear", "base_rev": 0,
        }])
    check("H6 服务器归档删除失败不返回成功且保留 pending",
          not archive_failed.get("ok")
          and archive_failed.get("rejected", [{}])[0].get("code")
          == "SYNC_FILE_COMMIT_FAILED"
          and bool(json.loads(srv.state_path(archive_root).read_text("utf-8"))
                   .get("pending_op")))
    archive_recovered = srv.load(archive_root)
    check("H7 归档故障解除后 pending 恢复且只推进一次",
          archive_recovered.get("pending_op") is None
          and archive_recovered.get("history_epoch") == 1
          and not (archive_root / "data" / "archive").exists())


def main() -> int:
    global FAIL
    try:
        run()
    except Exception:
        FAIL += 1
        print("  X 未捕获异常")
        traceback.print_exc()

    print(f"\n=== hot5 崩溃恢复: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
