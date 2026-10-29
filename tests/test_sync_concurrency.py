# -*- coding: utf-8 -*-
"""hot5 压力与并发验收：真实 HTTP、真实 multipart、真实落盘。

覆盖 32 并发 op、重复请求幂等、同设备并发 sync、8 设备多轮收敛、
多 mode 隔离、非法路径 batch、malformed/超大上传，以及 outbox 并发读改写。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_real_harness import AUTH_FIXTURE, SyncHarness, jsonl_records

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
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


def parallel(fn, args: list, workers: int | None = None):
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers or len(args)) as ex:
        futures = [ex.submit(fn, arg) for arg in args]
        return [f.result() for f in futures]


def parse_json(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def run() -> None:
    h = SyncHarness()
    try:
        print("=== A. 32 并发 op：rev 唯一、无丢单、重复批幂等 ===")
        h.reset()
        barrier = threading.Barrier(32)

        def send_op(i: int):
            barrier.wait(timeout=10)
            return h.ops("story", [{
                "op_id": f"parallel-fav-{i}",
                "type": "favorite",
                "payload": {"id": f"fav-{i}"},
            }])

        started = time.perf_counter()
        results = parallel(send_op, list(range(32)))
        elapsed = time.perf_counter() - started
        bodies = [x[2] for x in results]
        applied = [item for body in bodies for item in body.get("applied", [])]
        revs = sorted(int(x.get("rev") or 0) for x in applied)
        state = h.state()[2]["state"]
        check("A1 32 个并发请求全部 HTTP 200/ok",
              all(x[0] == 200 and x[2].get("ok") for x in results),
              repr([(x[0], x[2].get("code")) for x in results if not x[2].get("ok")]))
        check("A2 32 个 op 恰好应用一次", len(applied) == 32, str(len(applied)))
        check("A3 rev 唯一且连续 1..32", revs == list(range(1, 33)), repr(revs))
        check("A4 权威状态最终 rev=32",
              state.get("rev") == 32 and len(state.get("ops") or []) == 32)
        print(f"    （32 并发耗时 {elapsed:.3f}s）")

        barrier2 = threading.Barrier(32)

        def resend_op(i: int):
            barrier2.wait(timeout=10)
            return h.ops("story", [{
                "op_id": f"parallel-fav-{i}",
                "type": "favorite",
                "payload": {"id": f"fav-{i}"},
            }])

        duplicate = parallel(resend_op, list(range(32)))
        state2 = h.state()[2]["state"]
        check("A5 响应丢失后的整批重试全部命中幂等",
              all(x[0] == 200 and x[2].get("ok")
                  and len(x[2].get("skipped") or []) == 1
                  for x in duplicate))
        check("A6 重试不推进 rev、不重复账本",
              state2.get("rev") == 32 and len(state2.get("ops") or []) == 32)

        print("=== B. 同收藏并发增删：最终墓碑唯一，无状态复活 ===")
        h.reset()
        barrier3 = threading.Barrier(24)

        def fav(i: int):
            barrier3.wait(timeout=10)
            return h.ops("story", [{
                "op_id": f"same-fav-{i}", "type": "favorite",
                "payload": {"id": "same-target"},
            }])

        fav_results = parallel(fav, list(range(24)))
        barrier4 = threading.Barrier(24)

        def unfav(i: int):
            barrier4.wait(timeout=10)
            return h.ops("story", [{
                "op_id": f"same-unfav-{i}", "type": "unfavorite",
                "payload": {"id": "same-target"},
            }])

        unfav_results = parallel(unfav, list(range(24)))
        st = h.state()[2]["state"]
        check("B1 24 并发收藏 + 24 并发取消全部成功",
              all(x[0] == 200 and x[2].get("ok")
                  for x in fav_results + unfav_results))
        check("B2 最终状态 rev=48",
              st.get("rev") == 48 and len(st.get("ops") or []) == 48)
        check("B3 最终收藏墓碑恰好一个",
              (st.get("favorite_tombstones") or []).count("same-target") == 1)

        print("=== C. 8 设备 × 12 消息 × 图片：两轮真实同步收敛 ===")
        h.reset()
        devices = [f"stress-{i}" for i in range(8)]
        for i, dev in enumerate(devices):
            mode = h.mode_root(dev, clean=True)
            rows = [{
                "seq": j + 1, "mid": f"{dev}-mid-{j}", "epoch": 0,
                "who": "user" if j % 2 == 0 else "firefly",
                "type": "text", "content": f"{dev}-消息-{j}",
                "time": f"2026-10-27 12:00:{j:02d}",
            } for j in range(12)]
            write_jsonl(mode / "data" / "conversation.jsonl", rows)
            (mode / "images" / f"{dev}.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + (f"image-{dev}".encode() * 4096))

        started = time.perf_counter()
        first = parallel(lambda dev: h.sync_now(dev, "story"), devices, workers=8)
        elapsed1 = time.perf_counter() - started
        server_rows = jsonl_records(h.server_mode_root() / "data" / "conversation.jsonl")
        server_mids = [x.get("mid") for x in server_rows]
        check("C1 8 设备首轮并发同步全部成功",
              all(x[0] == 200 and x[2].get("ok") for x in first),
              repr([x[2] for x in first if not x[2].get("ok")]))
        check("C2 服务器恰好收到 96 条且 mid 唯一",
              len(server_rows) == 96 and len(set(server_mids)) == 96,
              f"{len(server_rows)}/{len(set(server_mids))}")
        check("C3 8 张图片全部落服务器",
              len(list((h.server_mode_root() / "images").glob("*.png"))) == 8)
        print(f"    （8 设备首轮耗时 {elapsed1:.3f}s）")

        started = time.perf_counter()
        second = parallel(lambda dev: h.sync_now(dev, "story"), devices, workers=8)
        elapsed2 = time.perf_counter() - started
        local_counts = [
            len(jsonl_records(h.mode_root(dev, "story") / "data" / "conversation.jsonl"))
            for dev in devices
        ]
        local_images = [
            len(list((h.mode_root(dev, "story") / "images").glob("*.png")))
            for dev in devices
        ]
        check("C4 8 设备第二轮并发同步全部成功",
              all(x[0] == 200 and x[2].get("ok") for x in second),
              repr([x[2] for x in second if not x[2].get("ok")]))
        check("C5 每台设备收敛到 96 条且无重复",
              local_counts == [96] * 8, repr(local_counts))
        check("C6 每台设备收敛到 8 张图片",
              local_images == [8] * 8, repr(local_images))
        print(f"    （8 设备收敛轮耗时 {elapsed2:.3f}s）")

        print("=== D. 同设备 8 并发 /sync/now：本地回合锁防叠写 ===")
        h.reset()
        mode = h.mode_root("same-device", clean=True)
        rows = [{
            "seq": i + 1, "mid": f"same-{i}", "epoch": 0,
            "who": "user", "type": "text", "content": f"消息 {i}",
        } for i in range(100)]
        write_jsonl(mode / "data" / "conversation.jsonl", rows)
        same = parallel(
            lambda _i: h.sync_now("same-device", "story"), list(range(8)), workers=8)
        server_same = jsonl_records(
            h.server_mode_root() / "data" / "conversation.jsonl")
        check("D1 同设备 8 个同步回合全部成功",
              all(x[0] == 200 and x[2].get("ok") for x in same),
              repr([x[2] for x in same if not x[2].get("ok")]))
        check("D2 100 条消息未重复/未截断",
              len(server_same) == 100
              and len({x.get("mid") for x in server_same}) == 100)

        print("=== E. story/haruno 并发文件上传隔离 ===")
        h.reset()
        barrier5 = threading.Barrier(16)

        def upload_shard(i: int):
            mode_name = "story" if i % 2 == 0 else "haruno"
            barrier5.wait(timeout=10)
            return h.upload(
                mode_name,
                {f"data/shard-{i}.md": f"mode={mode_name};id={i}\n"},
                state_rev=0,
            )

        shards = parallel(upload_shard, list(range(16)))
        story_files = sorted((h.server_mode_root("story") / "data").glob("shard-*.md"))
        haruno_files = sorted((h.server_mode_root("haruno") / "data").glob("shard-*.md"))
        check("E1 16 个并发跨 mode 上传全部成功",
              all(x[0] == 200 and x[2].get("ok") for x in shards),
              repr([x[2] for x in shards if not x[2].get("ok")]))
        check("E2 story/haruno 各 8 个文件且内容不串目录",
              len(story_files) == 8 and len(haruno_files) == 8
              and all("mode=story" in x.read_text("utf-8") for x in story_files)
              and all("mode=haruno" in x.read_text("utf-8") for x in haruno_files))
        check("E3 两个 mode 的 rev 状态独立",
              h.state("story")[2]["state_rev"] == 0
              and h.state("haruno")[2]["state_rev"] == 0)

        print("=== F. 恶意/损坏请求：不落盘、不假成功、不会断连 ===")
        status, headers, body = h.server_call(
            "POST", "/sync/import", b"x",
            {"Content-Type": "multipart/form-data; boundary=BAD",
             "X-Firefly-Sync-Protocol": "2"})
        malformed = parse_json(body)
        check("F1 malformed multipart 返回 400 JSON",
              status == 400 and malformed.get("code") == "SYNC_MULTIPART_INVALID",
              f"{status} {malformed}")
        status, headers, body = h.server_call(
            "POST", "/sync/import",
            json.dumps({"mode": "story"}).encode("utf-8"),
            {"Content-Type": "application/json"})
        wrong_type = parse_json(body)
        check("F2 非 multipart 导入被明确拒绝",
              status == 400 and wrong_type.get("code") == "SYNC_MULTIPART_INVALID")

        sock = socket.create_connection(("127.0.0.1", h.auth_port), timeout=5)
        sock.settimeout(5)
        request = (
            "POST /sync/import HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{h.auth_port}\r\n"
            f"Authorization: Bearer {AUTH_FIXTURE}\r\n"
            "Content-Type: multipart/form-data; boundary=BIG\r\n"
            f"Content-Length: {60 * 1024 * 1024 + 1}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        chunks = []
        while True:
            piece = sock.recv(4096)
            if not piece:
                break
            chunks.append(piece)
        sock.close()
        raw_response = b"".join(chunks)
        response_body = raw_response.split(b"\r\n\r\n", 1)[-1]
        oversized = parse_json(response_body)
        check("F3 超过 60MB 上限在读取正文前返回 413",
              raw_response.startswith(b"HTTP/1.1 413")
              and oversized.get("code") == "SYNC_IMPORT_TOO_LARGE",
              raw_response[:200].decode("utf-8", "replace"))

        illegal = h.upload("story", {
            "../escape-hot5.md": "escape",
            "images/sub/nested.png": b"NESTED",
            "data/.server_sync_state.json": "{}",
            "data/.sync_outbox.jsonl": "{}\n",
            "images/safe.png": b"\x89PNG\r\n\x1a\nsafe",
        })
        check("F4 非法/内部路径被逐项拒绝，只写合法图片",
              illegal[0] == 200
              and illegal[2].get("applied") == ["images/safe.png"]
              and (h.server_mode_root() / "images" / "safe.png").exists()
              and not (h.server_root / "escape-hot5.md").exists()
              and not (h.server_mode_root() / "images" / "sub").exists())

        huge_image = b"\x89PNG\r\n\x1a\n" + (b"Z" * (2 * 1024 * 1024))
        big = h.upload("story", {"images/big.png": huge_image}, state_rev=0)
        check("F5 2MB 图片真实 multipart 上传字节一致",
              big[0] == 200 and big[2].get("ok")
              and (h.server_mode_root() / "images" / "big.png").read_bytes()
              == huge_image)

        print("=== G. 本地 outbox 并发 append/clear 不丢新 op ===")
        h.reset()
        local_root = h.mode_root("outbox", clean=True)
        base_ids = []
        ids_lock = threading.Lock()

        def append_initial(i: int):
            op = sync_state.append_op(local_root, "favorite", {"id": f"b-{i}"})
            with ids_lock:
                base_ids.append(op["op_id"])

        parallel(append_initial, list(range(64)), workers=16)
        check("G1 64 个并发 append 全部落盘",
              len(sync_state.load_outbox(local_root)) == 64)

        keep_ids = list(base_ids[32:])
        clear_ids = list(base_ids[:32])
        new_ids = []
        barrier6 = threading.Barrier(64)

        def churn(i: int):
            barrier6.wait(timeout=10)
            if i < 32:
                sync_state.clear_outbox(local_root, [clear_ids[i]])
            else:
                op = sync_state.append_op(
                    local_root, "favorite", {"id": f"n-{i}"})
                with ids_lock:
                    new_ids.append(op["op_id"])

        parallel(churn, list(range(64)), workers=64)
        remaining = {x["op_id"] for x in sync_state.load_outbox(local_root)}
        check("G2 并发确认不覆盖同时新追加的 op",
              set(new_ids) <= remaining and len(remaining) == 64, str(len(remaining)))
        check("G3 已确认的旧 op 全部移除、未确认旧 op 保留",
              not (set(clear_ids) & remaining)
              and set(keep_ids) <= remaining)
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

    print(f"\n=== hot5 并发压力: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
