# -*- coding: utf-8 -*-
"""角色卡 HTTP payload 与 card.zip 之间的转换。

本模块不承载 HTTP 路由，只负责把前端 payload、data URL、文件/图片 patch
转换成 `card_format.build_card_zip()` 可消费的对象。
"""

import json
import time
from pathlib import Path

from plaza import card_format as cf
from plaza import store as st

_ASSET_MIME = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_IMAGE_KEYS = ("avatar", "thumb", "display", "cover")


def _data_url_rel(data_url: str, slot: str) -> tuple:
    """图片 data URL → `(character/ 相对路径, 字节)`。扩展名按魔数判定。"""
    blob = cf.decode_data_url(data_url)
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif blob[:2] == b"\xff\xd8":
        ext = ".jpg"
    elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        ext = ".webp"
    else:
        raise cf.CardError(
            cf.E_BAD_IMAGE,
            f"{slot} 不是受支持的图片格式（只允许 png/jpg/webp）",
        )
    return f"assets/{slot}{ext}", blob


def _blob_data_url(rel: str, blob: bytes) -> str:
    """卡内图片字节 → data URL（草稿回读给前端继续编辑用）。"""
    import base64 as _b64

    mime = _ASSET_MIME.get(Path(rel).suffix.lower(), "application/octet-stream")
    return "data:%s;base64,%s" % (mime, _b64.b64encode(blob).decode())


def _card_zip_from_payload(
    card: dict,
    *,
    require_thumb: bool | None = None,
    is_admin: bool = False,
) -> bytes:
    """把制卡 payload 组装成 card.zip（preset.json 由后端生成）。"""
    c = card if isinstance(card, dict) else {}
    for key in ("id", "name", "char_name", "user_name", "desc"):
        if not str(c.get(key) or "").strip():
            raise cf.CardError(cf.E_BAD_MANIFEST, f"缺少必填字段：{key}")

    manifest = {"id": c.get("id")}
    manifest.update({k: c.get(k) for k in cf.CARD_METADATA_FIELDS})
    files: dict = {}

    # 1) 老字段（core/identity/sms_samples）——继续接受，等价映射到 files[]
    for rel, key in (
        ("core.md", "core"),
        ("identity.md", "identity"),
        ("sms_samples.md", "sms_samples"),
    ):
        if str(c.get(key) or "").strip():
            files[rel] = str(c[key]).encode("utf-8")

    # 2) files[]：自由文本（V1）。同一路径以 files[] 为准。
    raw_files = c.get("files")
    if raw_files is not None and not isinstance(raw_files, list):
        raise cf.CardError(cf.E_BAD_MANIFEST, "files 必须是数组")
    for item in raw_files or []:
        if not isinstance(item, dict):
            raise cf.CardError(cf.E_BAD_MANIFEST, "files[] 元素必须是 {path, text} 对象")
        path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if not path:
            raise cf.CardError(cf.E_BAD_PATH, "files[] 里有条目缺 path")
        if item.get("text") is None:
            raise cf.CardError(cf.E_BAD_MANIFEST, f"files[] 条目缺 text：{path}")
        if path == "preset.json":
            raise cf.CardError(
                cf.E_UNKNOWN_FILE,
                "preset.json 由服务器生成，不接受用户提交（安全边界）",
            )
        if path == "opening.json":
            raise cf.CardError(
                cf.E_UNKNOWN_FILE,
                "opening.json 请走 payload 的 opening 字段（结构化开场白）",
            )
        if path == "stickers.json":
            raise cf.CardError(
                cf.E_UNKNOWN_FILE,
                "stickers.json 由 images.stickers[].label 生成，请不要直接提交",
            )
        cf.classify(path)
        files[path] = str(item["text"]).encode("utf-8")

    # 3) opening（结构化开场白；老格式允许直接给字符串）
    opening = c.get("opening")
    if opening:
        if isinstance(opening, dict):
            op = {
                "narrations": list(opening.get("narrations") or []),
                "first_messages": [
                    str(x)
                    for x in (opening.get("first_messages") or [])
                    if str(x).strip()
                ],
            }
        else:
            op = {"narrations": [], "first_messages": [str(opening)]}
        if op["first_messages"] or op["narrations"]:
            files["opening.json"] = json.dumps(op, ensure_ascii=False).encode("utf-8")

    # 4) images：V1 images{} + 老平铺 cover/avatar（同一槽位以 images 为准）
    imgs = c.get("images")
    if imgs is not None and not isinstance(imgs, dict):
        raise cf.CardError(cf.E_BAD_MANIFEST, "images 必须是对象")
    imgs = imgs or {}
    unknown = sorted(k for k in imgs if k not in _IMAGE_KEYS + ("stickers",))
    if unknown:
        raise cf.CardError(
            cf.E_UNKNOWN_FILE,
            f"images 里有未知槽位：{'、'.join(unknown)}"
            f"（可用：{'、'.join(_IMAGE_KEYS)}、stickers）",
        )
    for slot in _IMAGE_KEYS:
        data_url = str(imgs.get(slot) or c.get(slot) or "")
        if not data_url:
            continue
        rel, blob = _data_url_rel(data_url, slot)
        files[rel] = blob
    stickers = imgs.get("stickers")
    if stickers is not None and not isinstance(stickers, list):
        raise cf.CardError(cf.E_BAD_MANIFEST, "images.stickers 必须是数组")
    labels: dict = {}
    idx = 0
    for s in stickers or []:
        if isinstance(s, dict):
            data_url, label = str(s.get("data") or ""), s.get("label")
        else:
            data_url, label = str(s or ""), None
        if not data_url:
            continue
        idx += 1
        if idx > len(cf.STICKER_SLOTS):
            raise cf.CardError(
                cf.E_TOO_BIG,
                f"表情包最多 {len(cf.STICKER_SLOTS)} 张（收到第 {idx} 张）",
            )
        slot = cf.STICKER_SLOTS[idx - 1]
        rel, blob = _data_url_rel(data_url, slot)
        files[rel] = blob
        lab = str(label or "").strip()
        if lab:
            if len(lab) > cf.MAX_STICKER_LABEL:
                raise cf.CardError(
                    cf.E_BAD_MANIFEST,
                    f"表情包标签超过 {cf.MAX_STICKER_LABEL} 字：{lab}",
                )
            labels[Path(rel).name] = lab
    if labels:
        files["stickers.json"] = json.dumps(labels, ensure_ascii=False).encode("utf-8")

    files["preset.json"] = json.dumps(
        cf.build_preset(manifest), ensure_ascii=False
    ).encode("utf-8")

    if require_thumb is None:
        require_thumb = raw_files is not None or "images" in c
    return cf.build_card_zip(
        manifest, files, require_thumb=bool(require_thumb), is_admin=is_admin
    )


def _derive_auto_id(base_id: str, ohash: str) -> str:
    """没给新 id 时自动派生一个：`<base>-copy`、`-copy2`…。"""
    from plaza import drafts as dr

    for i in range(1, 21):
        cid = f"{base_id}-copy" if i == 1 else f"{base_id}-copy{i}"
        if len(cid) <= 64 and dr.load_draft(ohash, cid)[1] is None:
            return cid
    return f"{base_id}-copy-{int(time.time())}"[:64]


def _derive_patch_files(card: dict, files: dict, remove: dict) -> tuple[list, list]:
    """把文本 patch（新增/替换）与删除应用进 `files`（就地改）。"""
    uploaded, removed = [], []
    raw_files = card.get("files")
    if raw_files is not None and not isinstance(raw_files, list):
        raise cf.CardError(cf.E_BAD_MANIFEST, "files 必须是数组")
    for item in raw_files or []:
        if not isinstance(item, dict):
            raise cf.CardError(cf.E_BAD_MANIFEST, "files[] 元素必须是 {path, text} 对象")
        path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if not path:
            raise cf.CardError(cf.E_BAD_PATH, "files[] 里有条目缺 path")
        if item.get("text") is None:
            raise cf.CardError(cf.E_BAD_MANIFEST, f"files[] 条目缺 text：{path}")
        if path in ("preset.json", "opening.json", "stickers.json"):
            raise cf.CardError(
                cf.E_UNKNOWN_FILE,
                f"{path} 由服务器生成，不能作为 patch 上传（opening 走 opening 字段）",
            )
        cf.classify(path)
        files[path] = str(item["text"]).encode("utf-8")
        uploaded.append(path)
    raw_del = remove.get("files")
    if raw_del is not None and not isinstance(raw_del, list):
        raise cf.CardError(cf.E_BAD_MANIFEST, "remove.files 必须是数组")
    for rel in raw_del or []:
        rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
        if not rel:
            continue
        if rel == "preset.json":
            raise cf.CardError(cf.E_UNKNOWN_FILE, "preset.json 不能删（每张卡都必须有）")
        if rel not in files:
            raise cf.CardError(cf.E_UNKNOWN_FILE, f"要删的文件不在基础卡里：{rel}")
        files.pop(rel, None)
        removed.append(rel)
    return uploaded, removed


def _derive_patch_images(card: dict, remove: dict, files: dict) -> tuple[list, list]:
    """把图片 patch（只替换出现的槽位）与删除应用进 `files`（就地改）。"""
    uploaded, removed = [], []
    imgs = card.get("images")
    if imgs is not None and not isinstance(imgs, dict):
        raise cf.CardError(cf.E_BAD_MANIFEST, "images 必须是对象")
    imgs = imgs or {}
    unknown = sorted(k for k in imgs if k not in _IMAGE_KEYS + ("stickers",))
    if unknown:
        raise cf.CardError(
            cf.E_UNKNOWN_FILE,
            f"images 里有未知槽位：{'、'.join(unknown)}"
            f"（可用：{'、'.join(_IMAGE_KEYS)}、stickers）",
        )
    for slot in _IMAGE_KEYS:
        url = str(imgs.get(slot) or "")
        if not url:
            continue
        rel, blob = _data_url_rel(url, slot)
        for old in [k for k in files if k.startswith("assets/") and Path(k).stem == slot]:
            files.pop(old, None)
        files[rel] = blob
        uploaded.append(rel)
    stickers = imgs.get("stickers")
    if stickers is not None:
        if not isinstance(stickers, list):
            raise cf.CardError(cf.E_BAD_MANIFEST, "images.stickers 必须是数组")
        labels = st._sticker_labels_from_files(files)
        for old in [k for k in files if k.startswith("assets/sticker-")]:
            files.pop(old, None)
        files.pop("stickers.json", None)
        idx = 0
        for s in stickers:
            data_url = str(s.get("data") or "") if isinstance(s, dict) else str(s or "")
            label = s.get("label") if isinstance(s, dict) else None
            if not data_url:
                continue
            idx += 1
            if idx > len(cf.STICKER_SLOTS):
                raise cf.CardError(
                    cf.E_TOO_BIG,
                    f"表情包最多 {len(cf.STICKER_SLOTS)} 张（收到第 {idx} 张）",
                )
            slot = cf.STICKER_SLOTS[idx - 1]
            rel, blob = _data_url_rel(data_url, slot)
            files[rel] = blob
            uploaded.append(rel)
            lab = str(label or "").strip()
            if lab and len(lab) > cf.MAX_STICKER_LABEL:
                raise cf.CardError(
                    cf.E_BAD_MANIFEST,
                    f"表情包标签超过 {cf.MAX_STICKER_LABEL} 字：{lab}",
                )
            if lab:
                labels[Path(rel).name] = lab
        labels = {k: v for k, v in labels.items() if f"assets/{k}" in files}
        if labels:
            files["stickers.json"] = json.dumps(labels, ensure_ascii=False).encode("utf-8")
    raw_del = remove.get("images")
    if raw_del is not None and not isinstance(raw_del, list):
        raise cf.CardError(cf.E_BAD_MANIFEST, "remove.images 必须是数组")
    for item in raw_del or []:
        key = str(item or "").strip().replace("\\", "/")
        if not key:
            continue
        stem = Path(key).stem if key.startswith("assets/") else key
        hits = [k for k in files if k.startswith("assets/") and Path(k).stem == stem]
        if not hits:
            raise cf.CardError(cf.E_UNKNOWN_FILE, f"要删的图片不在基础卡里：{key}")
        for k in hits:
            files.pop(k, None)
            removed.append(k)
    labels = {
        k: v
        for k, v in st._sticker_labels_from_files(files).items()
        if f"assets/{k}" in files
    }
    if labels:
        files["stickers.json"] = json.dumps(labels, ensure_ascii=False).encode("utf-8")
    elif "stickers.json" in files and not any(
        k.startswith("assets/sticker-") for k in files
    ):
        files.pop("stickers.json", None)
    return uploaded, removed
