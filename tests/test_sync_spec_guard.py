# -*- coding: utf-8 -*-
"""hot5 同步规范守卫：未来改代码时必须继续满足的可执行不变量。

不是“搜索某个函数名存在”就算通过：关键项同时有 AST 结构守卫和行为样例。
如果以后重构把异常兜底、CAS、幂等键、墓碑、LF 字节稳定性或 mode 锁删掉，
本文件会直接变红。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tests"))

from modules import storage
from modules import sync_engine
from modules import sync_server_state as srv
from modules import sync_state
from sync_real_harness import SyncHarness

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


def parse(path: Path) -> tuple[str, ast.Module]:
    text = path.read_text(encoding="utf-8")
    return text, ast.parse(text)


def func(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return next(
        (n for n in tree.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name),
        None,
    )


def decorator_names(node: ast.FunctionDef) -> set[str]:
    out = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            out.add(dec.id)
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                out.add(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                out.add(dec.func.attr)
    return out


def contains_call(node: ast.AST, attr: str) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name) and child.func.id == attr:
                return True
            if isinstance(child.func, ast.Attribute) and child.func.attr == attr:
                return True
    return False


def contains_with_context(node: ast.AST, name: str) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.With):
            continue
        for item in child.items:
            expr = item.context_expr
            if isinstance(expr, ast.Name) and expr.id == name:
                return True
            if isinstance(expr, ast.Attribute) and expr.attr == name:
                return True
            if isinstance(expr, ast.Call):
                fn = expr.func
                if isinstance(fn, ast.Name) and fn.id == name:
                    return True
                if isinstance(fn, ast.Attribute) and fn.attr == name:
                    return True
    return False


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
        encoding="utf-8",
    )


def run() -> None:
    print("=== A. AST：协议入口、锁、异常兜底与危险同名覆盖 ===")
    data_text, data_tree = parse(ROOT / "app" / "api" / "data_sync.py")
    guarded = {"sync_manifest", "sync_state", "sync_ops", "sync_import",
               "sync_export", "sync_now"}
    bad_guard = []
    for name in guarded:
        node = func(data_tree, name)
        if node is None or "_sync_guard" not in decorator_names(node):
            bad_guard.append(name)
    check("A1 所有同步 HTTP 入口都有异常兜底装饰器", not bad_guard, repr(bad_guard))

    top_imported_modules = set()
    for node in data_tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "modules":
            for alias in node.names:
                if alias.name == "sync_state":
                    top_imported_modules.add(alias.asname or alias.name)
    check("A2 本地同步状态模块必须用别名导入，禁止被 handler 同名覆盖",
          "sync_store" in top_imported_modules
          and "sync_state" not in top_imported_modules,
          repr(top_imported_modules))

    import_node = func(data_tree, "sync_import")
    export_node = func(data_tree, "sync_export")
    now_node = func(data_tree, "sync_now")
    one_mode = func(data_tree, "_sync_one_mode")
    check("A3 import 读取前校验 multipart 长度与上限",
          import_node is not None
          and "_IMPORT_MAX_BYTES" in ast.get_source_segment(data_text, import_node)
          and "Content-Length" in ast.get_source_segment(data_text, import_node))
    check("A4 manifest/export/import 均在同一 mode 锁内",
          all(contains_call(func(data_tree, name), "mode_lock")
              for name in ("sync_manifest", "sync_import", "sync_export")))
    check("A5 本地完整同步回合持有 mode 锁",
          one_mode is not None
          and contains_call(one_mode, "mode_lock")
          and contains_with_context(one_mode, "mode_lock"))
    chat_text, chat_tree = parse(ROOT / "app" / "api" / "chat_ops.py")
    clear_node = func(chat_tree, "clear_history")
    check("A5b /clear-history 与同步回合共享 mode 锁",
          clear_node is not None
          and contains_with_context(clear_node, "mode_lock"),
          ast.get_source_segment(chat_text, clear_node) or "")
    one_body = func(data_tree, "_sync_one_mode_body")
    check("A6 客户端导出按批次循环，不静默截断",
          one_body is not None
          and "_SYNC_EXPORT_BATCH" in data_text
          and "range(0, len(need), _SYNC_EXPORT_BATCH)"
          in (ast.get_source_segment(data_text, one_body) or ""))

    state_text, state_tree = parse(ROOT / "app" / "modules" / "sync_state.py")
    clear_outbox = func(state_tree, "clear_outbox")
    recover_txn = func(state_tree, "recover_txn")
    merge_remote = func(state_tree, "merge_server_state")
    check("A7 outbox 确认与追加共享互斥锁",
          clear_outbox is not None and contains_with_context(clear_outbox, "_lock"))
    check("A8 本地事务恢复覆盖损坏/COMMITTED/PREPARED",
          recover_txn is not None
          and all(x in ast.get_source_segment(state_text, recover_txn)
                  for x in ("COMMITTED", "PREPARED", "favorite", "unfavorite", "rest")))
    check("A9 远端收藏墓碑必须合并到本地",
          merge_remote is not None
          and "favorite_tombstones"
          in ast.get_source_segment(state_text, merge_remote))

    server_text, server_tree = parse(ROOT / "app" / "modules" / "sync_server_state.py")
    apply_ops = func(server_tree, "apply_ops")
    check("A10 服务器 op 应用持锁并使用 pending 提交点",
          apply_ops is not None
          and contains_with_context(apply_ops, "mode_lock")
          and "pending_op" in ast.get_source_segment(server_text, apply_ops))
    check("A11 stale base_rev 有专用错误码",
          "SYNC_OP_STALE_BASE_REV" in server_text)

    begin_txn = func(state_tree, "begin_txn")
    recover_src = ast.get_source_segment(state_text, recover_txn) or ""
    begin_src = ast.get_source_segment(state_text, begin_txn) or ""
    check("A12 本地恢复失败必须返回失败态并阻止新 txn 覆盖旧现场",
          "return False" in recover_src
          and "SyncRecoveryPending" in begin_src
          and "if not recover_txn(root)" in begin_src)
    one_body_src = ast.get_source_segment(data_text, one_body) or ""
    check("A13 完整同步回合遇到未恢复 txn 必须拒绝上传与拉取",
          "if not recovered" in one_body_src
          and "暂缓上传" in one_body_src)
    apply_src = ast.get_source_segment(server_text, apply_ops) or ""
    check("A14 服务器 pending 未恢复时整批拒绝新 op",
          "SYNC_RECOVERY_PENDING" in apply_src
          and "st.get(\"pending_op\")" in apply_src)

    storage_text, storage_tree = parse(ROOT / "app" / "modules" / "storage.py")
    write_text_fn = func(storage_tree, "atomic_write_text")
    check("A15 原子文本写固定 LF，防止 Windows 反复制造同步差异",
          write_text_fn is not None
          and 'newline="\\n"' in ast.get_source_segment(storage_text, write_text_fn))

    sync_js = (ROOT / "app" / "static" / "js" / "sync.js").read_text(encoding="utf-8")
    check("A16 前端按 r.ok 判成功，HTTP 失败不伪装完成",
          "if (r.ok && d.ok)" in sync_js
          and "r.status" in sync_js
          and "d.trace_id" in sync_js)

    print("=== B. 行为：消息身份、确定性合并、收藏与状态只进不退 ===")
    old_a = '{"seq":7,"who":"user","content":"A 端旧格式"}\n'
    old_b = '{"seq":7,"who":"user","content":"B 端旧格式"}\n'
    old_merged, old_cf = sync_engine.merge_jsonl(old_a, old_b)
    old_rows = [json.loads(x) for x in old_merged.splitlines()]
    check("B1 旧格式同 seq 异内容按 legacy mid 保留两条，不误判冲突",
          len(old_rows) == 2 and old_cf == [])

    new_a = '{"seq":8,"mid":"same-mid","content":"新版"}\n'
    new_b = '{"seq":8,"mid":"same-mid","content":"另一版"}\n'
    _, new_cf = sync_engine.merge_jsonl(new_a, new_b)
    check("B2 同 mid 异内容仍进入冲突保护", len(new_cf) == 1)

    ab, _ = sync_engine.merge_jsonl(old_a, old_b)
    ba, _ = sync_engine.merge_jsonl(old_b, old_a)
    check("B3 双端交换合并结果字节稳定",
          hashlib.sha256(ab.encode()).digest() == hashlib.sha256(ba.encode()).digest())

    tmp = Path(tempfile.mkdtemp(prefix="firefly_hot5_spec_"))
    root = tmp / "story"
    (root / "data").mkdir(parents=True)
    sync_state.add_favorite_tombstone(root, "dead-fav")
    fav_text = json.dumps([
        {"id": "dead-fav", "seq": 1, "content": "旧"},
        {"id": "live-fav", "seq": 2, "content": "新"},
    ], ensure_ascii=False)
    filtered, _ = sync_engine.merge_favorites(fav_text, fav_text, sync_state.load(root))
    check("B4 收藏墓碑在双向合并两侧都生效",
          [x["id"] for x in json.loads(filtered)] == ["live-fav"])
    sync_state.apply_remote_state(root, {
        "history_epoch": 2, "history_rev": 9, "memory_rev": 4,
        "tombstones": ["m1"], "favorite_tombstones": ["dead-fav", "dead-2"],
    })
    sync_state.apply_remote_state(root, {
        "history_epoch": 1, "history_rev": 1, "memory_rev": 1,
        "tombstones": [], "favorite_tombstones": [],
    })
    state = sync_state.load(root)
    check("B5 远端状态只进不退，旧响应不能清墓碑",
          state["history_epoch"] == 2 and state["history_rev"] == 9
          and state["memory_rev"] == 4
          and {"m1", "dead-fav", "dead-2"}
          <= set(state["tombstones"]) | set(state["favorite_tombstones"]))

    print("=== C. 行为：服务器 CAS、op 幂等、导出拒绝静默截断 ===")
    sroot = tmp / "server"
    (sroot / "data").mkdir(parents=True)
    first = srv.apply_ops(sroot, [{
        "op_id": "guard-clear", "type": "clear", "base_rev": 0,
    }])
    stale = srv.apply_ops(sroot, [{
        "op_id": "guard-stale", "type": "retract", "base_rev": 0,
        "payload": {"mids": ["x"]},
    }])
    retry = srv.apply_ops(sroot, [{
        "op_id": "guard-clear", "type": "clear", "base_rev": 0,
    }])
    check("C1 clear 推进权威 rev 与 epoch",
          first["ok"] and first["state"]["rev"] == 1
          and first["state"]["history_epoch"] == 1)
    check("C2 stale op 被拒且状态不变",
          not stale["ok"] and stale["state"]["rev"] == 1
          and stale["rejected"][0]["code"] == "SYNC_OP_STALE_BASE_REV")
    check("C3 已应用 op 重试只返回 duplicate",
          retry["ok"] and retry["state"]["rev"] == 1
          and retry["skipped"][0].get("duplicate"))

    h = SyncHarness()
    try:
        h.reset()
        status, headers, raw = h.export("story", [f"data/f-{i}.md" for i in range(201)])
        body = json.loads(raw.decode("utf-8")) if raw[:1] == b"{" else {}
        check("C4 服务器单批 >200 文件明确 413，不返回残缺 zip",
              status == 413
              and body.get("code") == "SYNC_EXPORT_TOO_MANY_FILES")
        status, headers, manifest = h.manifest()
        check("C5 manifest 绑定 state_rev 并回显 mode",
              status == 200 and manifest.get("mode") == "story"
              and isinstance(manifest.get("state_rev"), int))
        status, headers, raw = h.export("story", [], state_rev="not-int")
        malformed = json.loads(raw.decode("utf-8")) if raw[:1] == b"{" else {}
        check("C6 非整数 state_rev 明确 400，不悄悄忽略 CAS",
              status == 400 and malformed.get("code") == "SYNC_STATE_REV_INVALID")
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

    print(f"\n=== hot5 规范守卫: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
