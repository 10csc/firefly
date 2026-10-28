# -*- coding: utf-8 -*-
"""Phase 3 独立回归：角色卡字段投影、payload/改编共用构造、模块 re-export。"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import ExitStack
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


def _card_payload() -> dict:
    return {
        "id": "probe-card",
        "name": "探针卡",
        "char_name": "流萤",
        "user_name": "开拓者",
        "presentation": "narration",
        "desc": "字段投影探针",
        "tagline": "保持安静",
        "category": "剧情",
        "tags": ["测试", "重构"],
        "core": "核心设定",
    }


def _check_shared_field_projection() -> None:
    from plaza import card_format as cf
    from plaza import card_payload

    manifest = {
        "id": "probe-card",
        "name": "探针卡",
        "char_name": "流萤",
        "user_name": "开拓者",
        "presentation": "",
        "desc": "字段投影探针",
        "tagline": None,
        "category": "剧情",
        "tags": ["测试"],
    }
    preset = cf.build_preset(manifest)
    check(
        "preset 只投影运行时字段",
        preset == {
            "id": "probe-card",
            "name": "探针卡",
            "char_name": "流萤",
            "user_name": "开拓者",
            "presentation": "sticker",
            "desc": "字段投影探针",
            "tagline": "",
            "schema": cf.PRESET_SCHEMA,
        },
        repr(preset),
    )
    check("category/tags 不泄漏到 preset", "category" not in preset and "tags" not in preset)

    captured = {}

    def fake_build(manifest_arg, files_arg, **kwargs):
        captured["manifest"] = manifest_arg
        captured["files"] = files_arg
        captured["kwargs"] = kwargs
        return b"PK-test"

    with patch.object(card_payload.cf, "build_card_zip", side_effect=fake_build):
        card_payload._card_zip_from_payload(_card_payload())
    got = json.loads(captured["files"]["preset.json"].decode("utf-8"))
    expected = {
        "id": "probe-card",
        "name": "探针卡",
        "char_name": "流萤",
        "user_name": "开拓者",
        "presentation": "narration",
        "desc": "字段投影探针",
        "tagline": "保持安静",
        "schema": 1,
    }
    check("制卡 payload 使用共享 preset 构造", got == expected, repr(got))
    check(
        "共享元数据字段表覆盖创建入口",
        all(k in captured["manifest"] for k in cf.CARD_METADATA_FIELDS),
        repr(captured["manifest"]),
    )


def _check_derive_uses_same_definition(tmp: Path) -> None:
    from plaza import api, drafts as dr, store as st
    import routes

    base = {
        "manifest": {
            "id": "base-card",
            "name": "基础卡",
            "char_name": "流萤",
            "user_name": "开拓者",
            "presentation": "narration",
            "desc": "基础描述",
            "tagline": "基础标语",
            "category": "治愈",
            "tags": ["基础"],
        },
        "files": {
            "core.md": b"base core",
            "preset.json": b'{"id":"base-card"}',
        },
    }
    zip_path = tmp / "base.card.zip"
    zip_path.write_bytes(b"PK-base")
    captured = {}

    def fake_build(manifest_arg, files_arg, **kwargs):
        captured["manifest"] = manifest_arg
        captured["files"] = dict(files_arg)
        return b"PK-derived"

    class H:
        def __init__(self):
            self.result = None

        def _json(self, payload, status=200):
            self.result = (status, payload)

    h = H()
    with ExitStack() as stack:
        stack.enter_context(patch.object(api, "_plaza_side_post", return_value=False))
        stack.enter_context(patch.object(routes, "_read_json", return_value={
            "base_id": "base-card",
            "card": {"id": "copied-card", "name": "改编卡", "presentation": ""},
        }))
        stack.enter_context(patch.object(api, "_current_user", return_value={"id": 7}))
        stack.enter_context(patch.object(api, "_is_admin_user", return_value=False))
        stack.enter_context(patch.object(st, "get_card", return_value={"id": "base-card"}))
        stack.enter_context(patch.object(st, "card_zip_path", return_value=zip_path))
        stack.enter_context(patch.object(api.cf, "parse_card_zip", return_value=base))
        stack.enter_context(patch.object(api.cf, "build_card_zip", side_effect=fake_build))
        stack.enter_context(patch.object(api.cf, "thumb_name", return_value=None))
        stack.enter_context(patch.object(dr, "owner_hash", return_value="owner-7"))
        stack.enter_context(patch.object(dr, "clean_display", return_value="作者"))
        stack.enter_context(patch.object(dr, "save_draft", return_value={
            "id": "copied-card",
            "status": "draft",
            "updated_at": 1,
            "size_bytes": 9,
        }))
        api.plaza_derive(h)

    got = json.loads(captured["files"]["preset.json"].decode("utf-8"))
    expected = {
        "id": "copied-card",
        "name": "改编卡",
        "char_name": "流萤",
        "user_name": "开拓者",
        "presentation": "sticker",
        "desc": "基础描述",
        "tagline": "基础标语",
        "schema": 1,
    }
    check("改编入口使用共享 preset 构造", got == expected, repr(got))
    check(
        "改编可覆盖的元数据字段来自共享表",
        captured["manifest"].get("name") == "改编卡"
        and captured["manifest"].get("category") == "治愈",
        repr(captured["manifest"]),
    )
    check("改编响应保留既有形状", h.result and h.result[1].get("ok") is True, repr(h.result))


def _check_compatibility_surface() -> None:
    from plaza import api, card_payload, http_transport

    check(
        "api 保留 card payload re-export",
        api._card_zip_from_payload is card_payload._card_zip_from_payload
        and api._derive_patch_files is card_payload._derive_patch_files,
    )
    check(
        "api 保留 transport re-export",
        api._plaza_side_post is http_transport._plaza_side_post
        and api._send_bytes is http_transport._send_bytes,
    )
    check(
        "HTTP 路由表仍在 api 模块",
        "/plaza/api/derive" in api.POST_ROUTES
        and api.POST_ROUTES["/plaza/api/derive"] is api.plaza_derive,
    )


def main() -> int:
    print("=== A. 共享角色卡字段投影 ===")
    _check_shared_field_projection()
    print("=== B. plaza_derive 使用同一 preset 构造 ===")
    with tempfile.TemporaryDirectory(prefix="firefly-phase3-") as td:
        _check_derive_uses_same_definition(Path(td))
    print("=== C. re-export 与路由兼容 ===")
    _check_compatibility_surface()
    print(f"\n=== plaza refactor: PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
