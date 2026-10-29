# -*- coding: utf-8 -*-
"""hot5 同步真实 HTTP 端到端验收（全新口径，不依赖旧同步测试）。

覆盖：双向首同步、同 seq 异 mid、收藏增删、图片 blob、文档 CAS、状态 rev、
导入覆盖、超过 200 文件分批、多 mode、异常 JSON/trace_id、路径穿越和幂等重试。
"""
import json
import os
import sys
import time
import traceback
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_real_harness import SyncHarness, jsonl_records

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  V", name)
    else:
        FAIL += 1
        print("  X", name, detail)


def write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                    encoding="utf-8")


def tree_snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def run():
    h = SyncHarness()
    try:
        print("=== A. 空数据 / 首轮上传 / 重复同步幂等 ===")
        h.reset()
        a = h.mode_root("a", clean=True)
        conv_a = [
            {"seq": 1, "mid": "a-user-1", "epoch": 0, "who": "user", "type": "text",
             "content": "你好", "time": "2026-10-27 10:00:00"},
            {"seq": 2, "mid": "a-firefly-1", "epoch": 0, "who": "firefly", "type": "text",
             "content": "晚上好", "time": "2026-10-27 10:00:01"},
        ]
        write_jsonl(a / "data" / "conversation.jsonl", conv_a)
        (a / "data" / "memory.md").write_text("A 的记忆\n", encoding="utf-8")
        (a / "data" / "favorites.json").write_text(json.dumps([
            {"id": "fav-a", "source": "mid:a-user-1", "seq": 1,
             "who": "user", "type": "text", "content": "你好",
             "time": "2026-10-27 10:00:00"}
        ], ensure_ascii=False), encoding="utf-8")
        image = b"\x89PNG\r\n\x1a\nhot5-image"
        (a / "images" / "img_hot5.png").write_bytes(image)

        status, _, first = h.sync_now("a", "story")
        check("A1 首轮 /sync/now 200/ok", status == 200 and first.get("ok"), str(first))
        sr = h.server_mode_root()
        check("A2 对话已真实 multipart 上传",
              [x.get("mid") for x in jsonl_records(sr / "data" / "conversation.jsonl")]
              == ["a-user-1", "a-firefly-1"])
        check("A3 收藏已落服务器", "fav-a" in (sr / "data" / "favorites.json").read_text("utf-8"))
        check("A4 图片 blob 字节一致",
              (sr / "images" / "img_hot5.png").read_bytes() == image)
        status2, _, second = h.sync_now("a", "story")
        check("A5 第二轮仍成功且不重复",
              status2 == 200 and second.get("ok")
              and len(jsonl_records(sr / "data" / "conversation.jsonl")) == 2)

        print("=== B. 服务器独有 / 同 seq 不同 mid 双向收敛 ===")
        h.reset()
        a = h.mode_root("a", clean=True)
        write_jsonl(a / "data" / "conversation.jsonl", [
            {"seq": 1, "mid": "common", "epoch": 0, "who": "user", "type": "text",
             "content": "共同", "time": "2026-10-27 11:00:00"},
            {"seq": 2, "mid": "device-a", "epoch": 0, "who": "user", "type": "text",
             "content": "A 端同 seq", "time": "2026-10-27 11:00:01"},
        ])
        sr = h.server_mode_root()
        write_jsonl(sr / "data" / "conversation.jsonl", [
            {"seq": 1, "mid": "common", "epoch": 0, "who": "user", "type": "text",
             "content": "共同", "time": "2026-10-27 11:00:00"},
            {"seq": 2, "mid": "device-b", "epoch": 0, "who": "user", "type": "text",
             "content": "B 端同 seq", "time": "2026-10-27 11:00:02"},
        ])
        (sr / "data" / "memory.md").write_text("服务器先有\n", encoding="utf-8")
        (sr / "images" / "remote.png").write_bytes(b"REMOTE-IMAGE")
        future = time.time() + 60
        os.utime(sr / "data" / "memory.md", (future, future))
        status, _, result = h.sync_now("a", "story")
        check("B1 双向同步成功", status == 200 and result.get("ok"), str(result))
        mids = [x.get("mid") for x in jsonl_records(a / "data" / "conversation.jsonl")]
        check("B2 两端同 seq 不同 mid 都保留",
              sorted(mids) == ["common", "device-a", "device-b"], repr(mids))
        check("B3 服务器独有文档拉回",
              (a / "data" / "memory.md").read_text("utf-8") == "服务器先有\n")
        check("B4 服务器独有图片拉回",
              (a / "images" / "remote.png").read_bytes() == b"REMOTE-IMAGE")

        print("=== C. 收藏 add/delete op + 墓碑不复活 ===")
        h.reset()
        a = h.mode_root("a", clean=True)
        token = h.cfg.set_user_context(user_dir=h.device_roots["a"])
        try:
            from modules import favorite_store
            fav = favorite_store.add_favorite("story", {
                "mid": "fav-mid-1", "seq": 7, "who": "user", "type": "text",
                "content": "值得收藏",
            })
        finally:
            h.cfg.reset_user_context(token)
        status, _, result = h.sync_now("a", "story")
        check("C1 收藏新增 op 同步成功", status == 200 and result.get("ok"), str(result))
        check("C2 服务器收藏存在",
              fav["id"] in (h.server_mode_root() / "data" / "favorites.json").read_text("utf-8"))

        token = h.cfg.set_user_context(user_dir=h.device_roots["a"])
        try:
            deleted = favorite_store.delete_favorite("story", fav["id"])
        finally:
            h.cfg.reset_user_context(token)
        check("C3 本地删除收藏", deleted)
        status, _, result = h.sync_now("a", "story")
        check("C4 收藏删除 op 同步成功", status == 200 and result.get("ok"), str(result))
        st = h.state()[2].get("state") or {}
        check("C5 服务器收藏墓碑已落盘", fav["id"] in (st.get("favorite_tombstones") or []))
        check("C6 服务器收藏文件已物理过滤",
              json.loads((h.server_mode_root() / "data" / "favorites.json").read_text("utf-8")) == [])

        b = h.mode_root("b", clean=True)
        b_old = [{"id": fav["id"], "source": "mid:fav-mid-1", "seq": 7,
                  "who": "user", "type": "text", "content": "值得收藏"}]
        (b / "data" / "favorites.json").write_text(json.dumps(b_old, ensure_ascii=False),
                                                   encoding="utf-8")
        status, _, result = h.sync_now("b", "story")
        check("C7 另一设备旧收藏不复活",
              status == 200 and result.get("ok")
              and json.loads((b / "data" / "favorites.json").read_text("utf-8")) == [],
              str(result))

        print("=== D. 文档 base_sha CAS + state_rev 快照一致性 ===")
        h.reset()
        a = h.mode_root("a", clean=True)
        sr = h.server_mode_root()
        server_doc = sr / "data" / "memory.md"
        server_doc.write_text("BASE\n", encoding="utf-8")
        _, _, manifest = h.manifest()
        base_sha = manifest["files"]["data/memory.md"]["sha256"]
        base_rev = manifest["state_rev"]
        server_doc.write_text("SERVER-NEWER\n", encoding="utf-8")
        status, _, conflict = h.upload(
            "story", {"data/memory.md": "LOCAL-STALE\n"},
            base_sha={"data/memory.md": base_sha}, state_rev=base_rev)
        check("D1 旧 base_sha 被拒且服务器内容不变",
              status == 200 and conflict.get("conflicts") == 1
              and server_doc.read_text("utf-8") == "SERVER-NEWER\n", str(conflict))
        current_sha = manifest["files"]["data/memory.md"]["sha256"]
        # 重新取当前 sha 后写入应成功。
        _, _, current = h.manifest()
        current_sha = current["files"]["data/memory.md"]["sha256"]
        status, _, ok = h.upload(
            "story", {"data/memory.md": "LOCAL-CAS-OK\n"},
            base_sha={"data/memory.md": current_sha}, state_rev=current["state_rev"])
        check("D2 当前 base_sha CAS 成功",
              status == 200 and "data/memory.md" in ok.get("applied", []), str(ok))
        status, _, op = h.ops("story", [{
            "op_id": "rev-change", "type": "favorite",
            "payload": {"id": "fav-rev"}}])
        check("D3 状态 rev 因 op 推进", status == 200 and op.get("state", {}).get("rev") == 1)
        status, _, body = h.export("story", ["data/memory.md"], state_rev=base_rev)
        parsed = json.loads(body.decode("utf-8")) if body[:1] == b"{" else {}
        check("D4 旧 state_rev 拉取被 409 拒绝",
              status == 409 and parsed.get("code") == "SYNC_STATE_CHANGED", str(parsed))
        status, _, stale_import = h.upload(
            "story", {"data/memory.md": "SHOULD-NOT-WRITE\n"},
            base_sha={"data/memory.md": current_sha}, state_rev=base_rev)
        check("D5 manifest 后状态变化时 import 拒绝",
              status == 409 and stale_import.get("code") == "SYNC_STATE_CHANGED")

        print("=== E. 超过 200 文件分批、多 mode、路径穿越 ===")
        h.reset()
        a = h.mode_root("a", clean=True)
        for i in range(205):
            (a / "data" / f"doc-{i:03d}.md").write_text(f"doc {i}\n", encoding="utf-8")
        status, _, result = h.sync_now("a", "story")
        check("E1 205 文件上传成功", status == 200 and result.get("ok"), str(result))
        b = h.mode_root("b", clean=True)
        status, _, result = h.sync_now("b", "story")
        check("E2 客户端按 200 一批完成拉取",
              status == 200 and result.get("ok")
              and len(list((b / "data").glob("doc-*.md"))) == 205, str(result))

        h.reset()
        a = h.mode_root("a", clean=True)
        (a / "data" / "memory.md").write_text("story\n", encoding="utf-8")
        har = a.parent / "haruno"
        (har / "data").mkdir(parents=True)
        (har / "data" / "memory.md").write_text("haruno\n", encoding="utf-8")
        status, _, result = h.sync_now("a", "all")
        check("E3 all 模式逐包同步成功", status == 200 and result.get("ok"), str(result))
        check("E4 两个 mode 落各自目录",
              (h.server_root / "story" / "data" / "memory.md").read_text("utf-8") == "story\n"
              and (h.server_root / "haruno" / "data" / "memory.md").read_text("utf-8") == "haruno\n")

        status, _, traversal = h.upload("story", {"../escape.md": "x"})
        check("E5 multipart 越界路径被拒且不落盘",
              status == 200 and not (h.server_root / "escape.md").exists(), str(traversal))

        print("=== F. 异常兜底 JSON + trace_id，不伪装成网络错误 ===")
        from modules import sync_engine
        real_scan = sync_engine.scan_manifest
        with patch.object(sync_engine, "scan_manifest",
                          side_effect=RuntimeError("hot5 forced failure")):
            status, headers, err = h.manifest()
        check("F1 后端异常返回 500 JSON",
              status == 500 and err.get("code") == "SYNC_INTERNAL_ERROR", str(err))
        check("F2 异常响应带 trace_id",
              bool(err.get("trace_id")) and len(err.get("trace_id")) >= 8)
        check("F3 异常响应 Content-Type 为 JSON",
              "application/json" in headers.get("Content-Type", ""))
        with patch.object(sync_engine, "scan_manifest", real_scan):
            status, _, recovered = h.manifest()
        check("F4 异常恢复后端点仍可用", status == 200 and recovered.get("ok"))

        print("=== G. 导出 zip 与清单哈希一致 ===")
        h.reset()
        a = h.mode_root("a", clean=True)
        payload = b"ZIP-PAYLOAD-\x00\x01"
        (a / "images" / "zip.png").write_bytes(payload)
        h.sync_now("a", "story")
        status, headers, zip_raw = h.export("story", ["images/zip.png"],
                                            state_rev=h.state()[2]["state_rev"])
        with zipfile.ZipFile(BytesIO(zip_raw)) as zf:
            same = zf.read("images/zip.png") == payload
        check("G1 导出是真 zip 且字节一致", status == 200 and same)
        check("G2 导出响应绑定 state_rev",
              headers.get("X-Firefly-State-Rev") == str(h.state()[2]["state_rev"]))

        print("=== H. unknown mode 在所有同步入口零副作用 ===")
        h.reset()
        a = h.mode_root("a", clean=True)
        unknown_mode = "ghost-mode-zz"
        unknown_root = a.parent / unknown_mode
        (unknown_root / "data").mkdir(parents=True)
        (unknown_root / "data" / "conversation.jsonl").write_text(
            json.dumps({"seq": 1, "mid": "ghost-keep", "epoch": 0}) + "\n",
            encoding="utf-8",
        )
        story = h.server_mode_root("story")
        (story / "data" / "story.md").write_text("STORY-KEEP\n", encoding="utf-8")
        before_server = tree_snapshot(h.server_root)
        before_local = tree_snapshot(unknown_root)

        status, _, rejected = h.state(unknown_mode)
        check("H1 state 显式拒绝不可寻址 mode",
              status == 400 and rejected.get("code") == "SYNC_MODE_NOT_ADDRESSABLE",
              str(rejected))
        status, _, rejected = h.manifest(unknown_mode)
        check("H2 manifest 显式拒绝且不返回 story 清单",
              status == 400 and rejected.get("code") == "SYNC_MODE_NOT_ADDRESSABLE",
              str(rejected))
        status, _, rejected = h.ops(unknown_mode, [{
            "op_id": "unknown-clear", "type": "clear", "base_rev": 0,
        }])
        check("H3 ops 在应用 clear 前拒绝",
              status == 400 and rejected.get("code") == "SYNC_MODE_NOT_ADDRESSABLE",
              str(rejected))
        status, _, export_raw = h.export(unknown_mode, ["data/story.md"])
        rejected = json.loads(export_raw.decode("utf-8"))
        check("H4 export 拒绝且不返回 story zip",
              status == 400 and rejected.get("code") == "SYNC_MODE_NOT_ADDRESSABLE",
              str(rejected))
        status, _, rejected = h.upload(unknown_mode, {"data/leak.md": "LEAK\n"})
        check("H5 import 拒绝且不写 story",
              status == 400 and rejected.get("code") == "SYNC_MODE_NOT_ADDRESSABLE",
              str(rejected))
        status, _, rejected = h.sync_now("a", unknown_mode)
        check("H6 本地 sync_now 不把未知包改成 story",
              status == 400 and rejected.get("code") == "SYNC_MODE_NOT_ADDRESSABLE",
              str(rejected))
        status, _, story_state = h.state("story")
        check("H7 story rev 未被未知 mode 推进",
              status == 200 and story_state.get("state_rev") == 0, str(story_state))
        check("H8 服务端与本地未知 mode 字节不变",
              tree_snapshot(h.server_root) == before_server
              and tree_snapshot(unknown_root) == before_local)

        print("=== I. /clear-history 真实 HTTP：失败不假成功，重试幂等 ===")
        part = h.mode_root("clear", clean=True)
        from modules import sync_state

        def seed_clear_files():
            write_jsonl(part / "data" / "conversation.jsonl", [
                {"seq": 1, "mid": "clear-user", "epoch": 0, "content": "旧对话"},
            ])
            (part / "data" / "memory.md").write_text("旧记忆\n", encoding="utf-8")
            (part / "data" / ".memory_index").write_text("{}", encoding="utf-8")
            (part / "journal" / "手账.md").write_text("旧手账\n", encoding="utf-8")
            (part / "data" / "voice").mkdir(parents=True, exist_ok=True)
            (part / "data" / "voice" / "v1.wav").write_bytes(b"RIFF-old-voice")
            (part / "data" / "voice" / "model.bin").write_bytes(b"voice-model")
            sync_state.outbox_path(part).unlink(missing_ok=True)
            sync_state.state_path(part).unlink(missing_ok=True)
            sync_state.txn_path(part).unlink(missing_ok=True)

        seed_clear_files()
        before_unknown_clear = tree_snapshot(part)
        status, _, raw = h.local_post("clear", "/clear-history", {
            "mode": "ghost-mode-zz", "session_id": "clear-unknown",
        })
        unknown_clear = json.loads(raw.decode("utf-8"))
        check("I0 未知 mode 在 clear 前拒绝且零写入",
              status == 400 and not unknown_clear.get("ok")
              and unknown_clear.get("code") == "MODE_NOT_FOUND"
              and tree_snapshot(part) == before_unknown_clear,
              str(unknown_clear))
        with patch.object(sync_state, "_clear_local_history_files", return_value=False):
            status, _, raw = h.local_post("clear", "/clear-history", {
                "mode": "story", "session_id": "clear-fail",
            })
        failed = json.loads(raw.decode("utf-8"))
        check("I1 文件副作用失败返回 503 且不 ok:true",
              status == 503 and not failed.get("ok")
              and failed.get("code") == "SYNC_CLEAR_PARTIAL", str(failed))
        check("I2 失败时保留 PREPARED、不推进 epoch、不写 outbox",
              sync_state.txn_path(part).exists()
              and sync_state.current_epoch(part) == 0
              and sync_state.load_outbox(part) == [])
        status, _, raw = h.local_post("clear", "/clear-history", {
            "mode": "story", "session_id": "clear-fail",
        })
        retried = json.loads(raw.decode("utf-8"))
        outbox = sync_state.load_outbox(part)
        check("I3 重试恢复同一 clear，不重复推进或重复入队",
              status == 200 and retried.get("ok")
              and sync_state.current_epoch(part) == 1
              and len(outbox) == 1 and outbox[0].get("type") == "clear",
              repr(outbox))
        check("I4 成功后对话/记忆/手账/语音已清",
              (part / "data" / "conversation.jsonl").read_text("utf-8") == ""
              and not (part / "data" / "memory.md").exists()
              and not (part / "journal" / "手账.md").exists()
              and not (part / "data" / "voice" / "v1.wav").exists())
        check("I4b 语音目录内非 store 管理文件保留",
              (part / "data" / "voice" / "model.bin").read_bytes() == b"voice-model")

        seed_clear_files()
        real_save = sync_state.save
        with patch.object(sync_state, "save", return_value=False):
            status, _, raw = h.local_post("clear", "/clear-history", {
                "mode": "story", "session_id": "clear-save-fail",
            })
        save_failed = json.loads(raw.decode("utf-8"))
        check("I5 epoch/rev 落盘失败仍不返回成功",
              status == 503 and not save_failed.get("ok")
              and sync_state.txn_path(part).exists()
              and sync_state.current_epoch(part) == 0, str(save_failed))
        with patch.object(sync_state, "save", side_effect=real_save):
            status, _, raw = h.local_post("clear", "/clear-history", {
                "mode": "story", "session_id": "clear-save-fail",
            })
        recovered = json.loads(raw.decode("utf-8"))
        check("I6 保存故障解除后同一事务恢复且只提交一次",
              status == 200 and recovered.get("ok")
              and sync_state.current_epoch(part) == 1
              and len(sync_state.load_outbox(part)) == 1
              and not sync_state.txn_path(part).exists(), str(recovered))

    finally:
        h.close()


def main() -> int:
    global FAIL
    try:
        run()
    except Exception:
        FAIL += 1
        print("  X 未捕获异常")
        traceback.print_exc()

    print(f"\n=== hot5 HTTP E2E: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
