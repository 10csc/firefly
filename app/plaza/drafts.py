# -*- coding: utf-8 -*-
"""制卡草稿与发布（共创平台 M2）

## 数据根
```
plaza/drafts/{owner_hash}/{draft_id}.zip    草稿卡体（复用 card.zip 格式与全部校验）
plaza/drafts/{owner_hash}/{draft_id}.json   草稿元数据（作者/状态/审核结论/时间/体积）
```

## 为什么草稿体也用 card.zip
一份格式胜过两份：体积上限、白名单、哈希、图片校验全部复用 `card_format`，
不需要为"草稿"再写一套规则（两套规则必然漂移，见 `docs/错误总结.md` #10）。

## 发布的两道闸（**这是本模块存在的理由**）
1. **必须有通过的审核结论**：草稿里 `review.verdict == pass`；官方（admin）免审但留痕。
2. **审核结论必须对应这份卡体**：草稿记录了审核时的 `digest`，发布时重新计算并比对。
   否则"审的是 A、发的是 B"（TOCTOU）——这是审核机制最容易被绕过的口子。

## 作者的展示名
默认用邮箱打码（`ab***@qq.com` → `ab***`），允许作者在表单里自填（≤40 字、去链接）。
**绝不把邮箱原文写进广场元数据**（`meta.author` 只有 `uid_hash` + 展示名）。
"""

import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path

from modules import app_config as cfg
from modules.storage import atomic_write_bytes, atomic_write_json

from plaza import card_format as cf
from plaza import store as st

logger = logging.getLogger(__name__)

DRAFT_MAX = 50                       # 每人最多保留的草稿数（★2026-10-02：10 → 50；**管理员不限**）
# 定位：草稿份数是**存储安全阀**，不是内容限制（用户实测"最多保留 10 份"太紧）。管理员
# （`role == admin`）在 `save_draft(is_admin=True)` 时完全豁免 —— 他们的草稿是运营/调试用途。
DRAFT_STATUSES = ("draft", "reviewed", "rejected", "published")
_DISPLAY_RE = re.compile(r"^[\w\u4e00-\u9fff·．.\-]{1,40}$")

DRAFT_BAD_ID = "PLAZA_DRAFT_BAD_ID"
DRAFT_NOT_FOUND = "PLAZA_DRAFT_NOT_FOUND"
DRAFT_LIMIT = "PLAZA_DRAFT_LIMIT"
DRAFT_NOT_REVIEWED = "PLAZA_DRAFT_NOT_REVIEWED"
DRAFT_STALE_REVIEW = "PLAZA_DRAFT_STALE_REVIEW"
DRAFT_PUBLISH_FAILED = "PLAZA_PUBLISH_FAILED"


class DraftError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── 身份 ────────────────────────────────────────────
def owner_hash(user: dict | None) -> str:
    """用户 → 稳定的匿名作者标识（不可逆；广场元数据里只存它，不存邮箱/uid）。"""
    if not user:
        return ""
    uid = user.get("id")
    if uid is None:
        return ""
    return hashlib.sha256(f"firefly-plaza-uid:{uid}".encode("utf-8")).hexdigest()[:32]


def default_display(user: dict | None) -> str:
    """默认展示名 = 邮箱打码（不含完整邮箱，避免广场泄露联系方式）。"""
    email = str((user or {}).get("email") or "")
    if "@" not in email:
        return "匿名作者"
    local, _, domain = email.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}***@{domain}"


def clean_display(raw, user: dict | None) -> str:
    s = str(raw or "").strip()
    if not s:
        return default_display(user)
    if not _DISPLAY_RE.fullmatch(s):
        # 含链接/表情/超长 → 退回默认，不报错（作者体验优先，安全兜底）
        return default_display(user)
    return s


# ── 路径 ────────────────────────────────────────────
def drafts_root() -> Path:
    return st.plaza_root() / "drafts"


def owner_dir(ohash: str) -> Path:
    return drafts_root() / (ohash or "_anon")


def draft_zip(ohash: str, did: str) -> Path:
    return owner_dir(ohash) / f"{did}.zip"


def draft_meta(ohash: str, did: str) -> Path:
    return owner_dir(ohash) / f"{did}.json"


def _read_meta(ohash: str, did: str) -> dict:
    try:
        data = json.loads(draft_meta(ohash, did).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("草稿元数据读取失败 %s/%s：%s", ohash, did, e)
        return {}


def list_drafts(ohash: str) -> list:
    """本人的草稿列表（按更新时间倒序）。"""
    d = owner_dir(ohash)
    if not d.is_dir():
        return []
    out = []
    for fp in d.glob("*.json"):
        did = fp.stem
        meta = _read_meta(ohash, did)
        if not meta:
            continue
        out.append({"id": did, "name": meta.get("name", ""), "status": meta.get("status", "draft"),
                    "updated_at": int(meta.get("updated_at") or 0),
                    "review_verdict": (meta.get("review") or {}).get("verdict", ""),
                    "size_bytes": int(meta.get("size_bytes") or 0)})
    out.sort(key=lambda x: -x["updated_at"])
    return out


# ── 写 ──────────────────────────────────────────────
def save_draft(ohash: str, card_zip: bytes, *, display: str = "",
               status: str = "draft", review: dict | None = None,
               is_admin: bool = False, derived_from: str = "") -> dict:
    """保存（或覆盖）草稿。返回草稿元数据。

    `is_admin=True`：管理员 ⇒ 文字总量不限（与发布同口径，2026-10-01 松绑；否则
    "平台流程走草稿"会导致管理员豁免拿不到）。
    `derived_from`：改编来源的广场卡 id（`/plaza/api/derive` 写；**只记录**：发布时随
    `extra_meta` 落到广场 meta，**不进任何客户端投影**）。
    """
    parsed = cf.parse_card_zip(card_zip, is_admin=is_admin)
    did = parsed["manifest"]["id"]
    d = owner_dir(ohash)
    d.mkdir(parents=True, exist_ok=True)
    existing = _read_meta(ohash, did)
    if not existing and not is_admin:      # 管理员不限草稿份数（存储安全阀对他们没意义）
        others = [p for p in d.glob("*.json") if p.stem != did]
        if len(others) >= DRAFT_MAX:
            raise DraftError(DRAFT_LIMIT,
                             f"草稿最多保留 {DRAFT_MAX} 份，请先删除不再需要的")
    meta = {
        "id": did,
        "name": parsed["manifest"]["name"],
        "char_name": parsed["manifest"]["char_name"],
        "category": parsed["manifest"]["category"],
        "display": display or existing.get("display") or "",
        "status": status if status in DRAFT_STATUSES else "draft",
        "created_at": int(existing.get("created_at") or time.time()),
        "updated_at": int(time.time()),
        "size_bytes": len(card_zip),
    }
    if derived_from:
        meta["derived_from"] = str(derived_from)[:64]
    elif existing.get("derived_from"):
        meta["derived_from"] = existing["derived_from"]      # 覆盖保存时保留来源（别再问客户端要一次）
    if review is not None:
        # 只留结论，不留任何调用凭据（review 里本来就没有）
        meta["review"] = {k: review.get(k) for k in
                          ("verdict", "risk", "reasons", "categories", "model",
                           "usage", "signals", "reviewed_at", "digest",
                           # ★2026-10-02：审核**覆盖范围**一起留档 ⇒ 草稿回读/平台 UI 才能如实写
                           # "按前 N 字符抽检（本次是否截断）"，不许让人以为全文都过了审
                           "reviewed_chars", "truncated", "files_included", "files_omitted",
                           # ★2026-10-02：**失败性质**也要留档 ⇒ UI 区分"系统错误"与"审核未通过"
                           "error_kind", "raw_excerpt", "finish_reason", "attempts", "retried")}
    elif existing.get("review"):
        meta["review"] = existing["review"]
    if not atomic_write_bytes(draft_zip(ohash, did), card_zip):
        raise DraftError(DRAFT_PUBLISH_FAILED, "草稿保存失败（磁盘写入错误）")
    if not atomic_write_json(draft_meta(ohash, did), meta):
        raise DraftError(DRAFT_PUBLISH_FAILED, "草稿元数据保存失败")
    return meta


def load_draft(ohash: str, did: str) -> tuple:
    """返回 (元数据, 卡体字节)；不存在返回 ({}, b"")。"""
    zp = draft_zip(ohash, did)
    if not st.valid_card_id(did) or not zp.is_file():
        return {}, b""
    return _read_meta(ohash, did), zp.read_bytes()


def delete_draft(ohash: str, did: str) -> bool:
    if not st.valid_card_id(did):
        return False
    ok = False
    for fp in (draft_zip(ohash, did), draft_meta(ohash, did)):
        try:
            if fp.is_file():
                fp.unlink()
                ok = True
        except OSError:
            pass
    return ok


# ── 发布 ────────────────────────────────────────────
def publish_draft(ohash: str, did: str, *, user: dict | None = None,
                  replace: bool = False, official: bool | None = None) -> dict:
    """把草稿发布到广场。返回广场卡片投影。

    闸门：① 草稿存在；② 审核 verdict==pass（admin 免审但留痕）；③ 审核 digest 与当前卡体一致。
    `official`：`None` = 按身份定（**管理员默认官方**，与既有行为一致）；显式 True 需要调用方
    已做过管理员校验（API 层 `_check_official`，普通用户传 ⇒ 403）。
    """
    meta, blob = load_draft(ohash, did)
    if not blob:
        raise DraftError(DRAFT_NOT_FOUND, "草稿不存在")
    is_admin = str((user or {}).get("role") or "") == "admin"
    parsed = cf.parse_card_zip(blob, is_admin=is_admin)      # 管理员：文字总量不限
    digest = parsed["digest"]
    review = meta.get("review") or {}
    if not is_admin:
        if review.get("verdict") != "pass":
            raise DraftError(DRAFT_NOT_REVIEWED,
                             "这张卡还没有通过审核（请先提交审核）" if not review
                             else f"审核结论为「{review.get('verdict')}」，不能发布")
        if str(review.get("digest") or "") != digest:
            raise DraftError(DRAFT_STALE_REVIEW, "卡片在审核后又被改动过，请重新提交审核")
    display = clean_display(meta.get("display"), user)
    _official = is_admin if official is None else bool(official)
    # 改编来源**只记录**到广场 meta（`_public_view`/`_detail_view` 按白名单取字段 ⇒ 不会外泄）
    _extra = {"derived_from": meta.get("derived_from")} if meta.get("derived_from") else None
    try:
        card = st.publish_card(parsed["manifest"], parsed["files"],
                               status="published", uid_hash=ohash, display=display,
                               official=_official, replace=replace, is_admin=is_admin,
                               extra_meta=_extra)
    except st.StoreError as e:
        raise DraftError(DRAFT_PUBLISH_FAILED, e.message)
    st.append_audit(did, {"action": "publish", "by": ohash, "official": _official,
                          "review": {k: review.get(k) for k in ("verdict", "risk", "model")},
                          "bypass_review": bool(is_admin and review.get("verdict") != "pass")})
    # 发布成功后草稿转为 published 态（保留卡体，便于作者回看/再次提交改版）
    meta["status"] = "published"
    meta["updated_at"] = int(time.time())
    atomic_write_json(draft_meta(ohash, did), meta)
    return card


def unpublish(ohash: str, did: str, *, user: dict | None = None) -> bool:
    """作者下架自己的卡（置 archived，不物理删除——审计与历史仍可查）。"""
    meta = st.get_card(did, statuses=None)
    if not meta:
        return False
    is_admin = str((user or {}).get("role") or "") == "admin"
    if not is_admin and meta.get("author", {}).get("uid_hash") != ohash:
        return False
    ok = st.set_status(did, "archived")
    if ok:
        st.append_audit(did, {"action": "unpublish", "by": ohash, "admin": is_admin})
    return ok


def purge_owner(ohash: str) -> None:
    """整账号清理（管理台用）。"""
    shutil.rmtree(owner_dir(ohash), ignore_errors=True)
