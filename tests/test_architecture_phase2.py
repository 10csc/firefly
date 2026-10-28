# -*- coding: utf-8 -*-
"""Phase 2 独立回归：死链导入、配置/包元数据作用域、资产指纹。

不访问网络或真实用户目录。pack-assist 使用假 LLM 客户端，但经过真实文本读取链；
多用户测试在两个线程内分别设置 contextvars，并用屏障强制交错读取。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
for _p in (ROOT, APP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  V", name)
    else:
        FAIL += 1
        print("  X", name, detail)


class _FakeClient:
    def __init__(self, captured: list):
        self.captured = captured
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.captured.append(kwargs)
        reply = json.dumps({
            "reply": "已生成提案",
            "changes": [{"op": "append", "new": "新增内容", "reason": "测试"}],
        }, ensure_ascii=False)
        message = SimpleNamespace(content=reply, reasoning_content="")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _assist_once(mode: str, file: str) -> tuple[list, dict]:
    from modules.pack_assist import assist_turn
    captured: list = []
    result = assist_turn(
        _FakeClient(captured), mode, file, "请补充事实", [],
        model="deepseek-flash", effort="none",
    )
    return captured, result


def _check_pack_assist_chain(tmp: Path) -> None:
    from core import config as cfg
    from core import userctx

    root = tmp / "assist-user"
    token = userctx.set_user_context(user_dir=root)
    try:
        knowledge = root / "story" / "character" / "knowledge" / "probe.md"
        knowledge.parent.mkdir(parents=True)
        knowledge.write_text("# 知识探针\\n知识库原始内容-7f31", encoding="utf-8")
        memory = root / "story" / "character" / "memory" / "default.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("## 核心记忆\\n出厂记忆原始内容-9a42", encoding="utf-8")

        calls, result = _assist_once("story", "knowledge/probe.md")
        user_prompt = calls[0]["messages"][1]["content"] if calls else ""
        check("知识库辅助不再触发 ImportError", bool(calls) and result.get("reply") == "已生成提案")
        check("知识库辅助读取真实用户副本", "知识库原始内容-7f31" in user_prompt)

        calls, result = _assist_once("story", "memory/default.md")
        user_prompt = calls[0]["messages"][1]["content"] if calls else ""
        check("出厂记忆辅助不再触发 ImportError", bool(calls) and result.get("reply") == "已生成提案")
        check("出厂记忆辅助读取真实用户副本", "出厂记忆原始内容-9a42" in user_prompt)
    finally:
        userctx.reset_user_context(token)


def _check_config_fallback(tmp: Path) -> None:
    from core import config as cfg
    from core import userctx

    root = tmp / "config-user"
    token = userctx.set_user_context(user_dir=root)
    overlay_token = userctx._user_overlay.set({})
    try:
        cfg._user_overlay.set({"proactive_hard": 7})
        check("pack_cfg 回落读取用户 overlay", cfg.pack_cfg("story", "proactive_hard") == 7,
              f"got={cfg.pack_cfg('story', 'proactive_hard')!r}")

        pack_file = root / "story" / "data" / "proactive.json"
        pack_file.parent.mkdir(parents=True)
        pack_file.write_text(json.dumps({"proactive_hard": 9}), encoding="utf-8")
        check("包级配置仍优先于用户 overlay", cfg.pack_cfg("story", "proactive_hard") == 9,
              f"got={cfg.pack_cfg('story', 'proactive_hard')!r}")
    finally:
        userctx._user_overlay.reset(overlay_token)
        userctx.reset_user_context(token)


def _check_multiuser_concurrency(tmp: Path) -> None:
    from core import config as cfg
    from core import presets
    from core import userctx
    from modules import llm_retriever
    from modules.organizer import Organizer, OrganizerInput

    shared_mode = "phase2-shared"
    roots = [tmp / "user-a", tmp / "user-b"]
    kb_dirs = [tmp / "kb-a", tmp / "kb-b"]
    expected = [
        {"presentation": "sticker", "knowledge_dirs": [str(kb_dirs[0])], "marker": "KB-FROM-A"},
        {"presentation": "narration", "knowledge_dirs": [str(kb_dirs[1])], "marker": "KB-FROM-B"},
    ]
    for i, d in enumerate(kb_dirs):
        d.mkdir(parents=True)
        (d / "only.md").write_text(f"# {expected[i]['marker']}\\n", encoding="utf-8")

    barrier = threading.Barrier(2)
    barrier_local = threading.local()
    result: dict[str, dict] = {}
    errors: list[str] = []

    def fake_user_packs():
        if not getattr(barrier_local, "synced", False):
            barrier_local.synced = True
            barrier.wait(timeout=5)
        idx = 0 if userctx.user_scope_key() == str(roots[0]) else 1
        return {
            shared_mode: {
                "id": shared_mode,
                "name": "并发探针",
                "char_name": "角色",
                "user_name": "用户",
                "presentation": expected[idx]["presentation"],
                "knowledge_dirs": expected[idx]["knowledge_dirs"],
            }
        }

    def worker(idx: int) -> None:
        token = userctx.set_user_context(user_dir=roots[idx])
        overlay_token = userctx._user_overlay.set({"proactive_hard": 3 + idx})
        try:
            org = Organizer(None, mode=shared_mode)
            org._organize_sticker = lambda _inp: SimpleNamespace(branch="sticker")
            org._organize_narration = lambda _inp: SimpleNamespace(branch="narration")
            out = org.organize(OrganizerInput(
                user_input="并发测试", reply_texts=["回复"], mode=shared_mode))
            llm_retriever.clear_knowledge_cache()
            knowledge = llm_retriever._load_knowledge(shared_mode)
            result[str(idx)] = {
                "branch": out.branch,
                "knowledge": knowledge,
                "pack_cfg": cfg.pack_cfg(shared_mode, "proactive_hard"),
            }
        except Exception as exc:
            errors.append(f"{idx}: {type(exc).__name__}: {exc}")
        finally:
            userctx._user_overlay.reset(overlay_token)
            userctx.reset_user_context(token)

    with patch.object(presets, "user_packs", side_effect=fake_user_packs):
        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    check("多用户并发任务均正常结束", not errors and all(not t.is_alive() for t in threads), str(errors))
    check("组织器按各用户包选择演出分支",
          result.get("0", {}).get("branch") == "sticker"
          and result.get("1", {}).get("branch") == "narration",
          str({k: v.get("branch") for k, v in result.items()}))
    check("检索器只读取各用户声明的知识目录",
          "KB-FROM-A" in result.get("0", {}).get("knowledge", "")
          and "KB-FROM-B" not in result.get("0", {}).get("knowledge", "")
          and "KB-FROM-B" in result.get("1", {}).get("knowledge", "")
          and "KB-FROM-A" not in result.get("1", {}).get("knowledge", ""),
          str({k: v.get("knowledge", "") for k, v in result.items()}))
    check("多用户 proactive overlay 不串值",
          result.get("0", {}).get("pack_cfg") == 3
          and result.get("1", {}).get("pack_cfg") == 4,
          str({k: v.get("pack_cfg") for k, v in result.items()}))


def _check_asset_md5() -> None:
    from routes_assets import _ASSET_MD5_CACHE, _asset_md5

    prefix = "x" * 64
    _ASSET_MD5_CACHE.clear()
    first = _asset_md5(prefix + "-tail-a")
    second = _asset_md5(prefix + "-tail-b")
    check("资产版本指纹覆盖完整文本", first != second, f"{first} == {second}")


def _routes_pack_imports(path: Path) -> list[str]:
    """返回文件中导入 routes_pack 的语句位置（运行时与延迟导入都算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "routes_pack":
            found.append(f"{path.name}:{node.lineno}: from routes_pack")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "routes_pack":
                    found.append(f"{path.name}:{node.lineno}: import routes_pack")
    return found


def _check_pack_dependency_boundary() -> None:
    guarded = [
        APP / "api" / "pack_paths.py",
        APP / "core" / "pack_structure.py",
    ]
    reverse = [hit for path in guarded for hit in _routes_pack_imports(path)]
    check("pack_paths/core 不再反向导入 routes_pack", not reverse, repr(reverse))

    from api import pack_paths

    mode = "dependency-probe"
    user_fp, bundled_fp = pack_paths._paths_for(mode, "knowledge/probe.md")
    check(
        "知识库路径由 pack_paths 直接解析",
        user_fp == pack_paths.kb_user_root(mode) / "probe.md"
        and bundled_fp == pack_paths.kb_bundled_root(mode) / "probe.md",
        f"user={user_fp} bundled={bundled_fp}",
    )


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="firefly_phase2_") as td:
        tmp = Path(td)
        print("=== A. 知识库/出厂记忆 AI 辅助真实读取链 ===")
        _check_pack_assist_chain(tmp)
        print("=== B. pack_cfg 用户 overlay 回落 ===")
        _check_config_fallback(tmp)
        print("=== C. 多用户配置与包元数据并发隔离 ===")
        _check_multiuser_concurrency(tmp)
        print("=== D. 资产版本缓存键 ===")
        _check_asset_md5()
        print("=== E. pack 路径依赖方向 ===")
        _check_pack_dependency_boundary()


def main() -> int:
    try:
        run()
    except Exception:
        global FAIL
        FAIL += 1
        print("  X 未捕获异常")
        traceback.print_exc()
    print(f"\n=== architecture phase2: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
