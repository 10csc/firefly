# -*- coding: utf-8 -*-
"""收藏删除路由的错误边界回归。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


class _Handler:
    def __init__(self):
        self.response = None

    def _json(self, payload, status=200):
        self.response = {"status": status, "payload": payload}


def _delete_with_failure(exc):
    from modules import favorite_store
    import routes_assets

    handler = _Handler()
    with patch.object(routes_assets, "_read_json",
                      return_value={"mode": "story", "id": "fav_1"}), \
         patch.object(favorite_store, "delete_favorite", side_effect=exc):
        routes_assets.delete_favorite_route(handler)
    return handler.response


def test_favorite_error_is_returned_as_domain_failure():
    from modules.favorite_store import FavoriteError

    response = _delete_with_failure(FavoriteError("收藏删除写入失败"))

    assert response == {
        "status": 200,
        "payload": {"ok": False, "error": "收藏删除写入失败"},
    }


def test_unexpected_delete_error_uses_generic_message():
    response = _delete_with_failure(OSError("disk unavailable"))

    assert response == {
        "status": 200,
        "payload": {"ok": False, "error": "收藏删除失败，请重试"},
    }
