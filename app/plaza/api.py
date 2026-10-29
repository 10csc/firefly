# -*- coding: utf-8 -*-
"""广场 HTTP 端点（共创平台 M1）

两条路径（前端**只调相对路径**，两种模式同一套代码）：
- **服务器模式**（`_is_server()`）：本地实现，直接读 `plaza/store.py`；
  这些路径不在 `server/app.py` 的公开白名单里 → **自动落进登录闸门**（`:427-430`），
  满足"广场完全登录可见"的产品决策。
- **本地模式**：把请求**代理**到 `_auth_server_base()`（认证服务器），
  带 `auth_store` 里持有的 token —— 与 A7 的 `/auth/*` 代理同一先例
  （`app/routes_auth.py:30-84`），因此没有 CORS / `file://` 问题。

端点（M1 只读；M2 追加 draft/review/publish/report）：
```
GET /plaza/api/list     列表：分类/标签/关键词/官方/排序/分页
GET /plaza/api/card     详情
GET /plaza/api/asset    封面/头像（长缓存 + ETag/304）
GET /plaza/api/download 下载 card.zip（计入下载数；字节配额见 04 文档）
```

体积纪律（3Mbps 带宽）：列表响应**不含卡正文**；图片长缓存；zip 走低优先通道（M4）。
"""

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import quote

from plaza import card_format as cf
from plaza import store as st
from plaza.card_payload import (
    _ASSET_MIME,
    _IMAGE_KEYS,
    _blob_data_url,
    _card_zip_from_payload,
    _data_url_rel,
    _derive_auto_id,
    _derive_patch_files,
    _derive_patch_images,
)
from plaza.http_transport import (
    _LOCAL_PROXY_TIMEOUT,
    _auth_base,
    _current_user,
    _err,
    _is_admin_user,
    _is_server,
    _local_token,
    _plaza_side,
    _plaza_side_post,
    _proxy,
    _proxy_post,
    _q,
    _send_bytes,
)

logger = logging.getLogger(__name__)

# 超时（秒）：列表/详情要快；下载在 3Mbps 上 1.5MB ≈ 4s，留足重传余量
_T_LIST = 12
_T_DOWNLOAD = 60


# ── 端点 ────────────────────────────────────────────
def plaza_list(h) -> None:
    """GET /plaza/api/list —— 列表（不含卡正文）。"""
    if not _is_server():
        _proxy(h, "list", timeout=_T_LIST)
        return
    q = _q(h)
    data = st.list_cards(
        category=q.get("category") or None,
        tag=q.get("tag") or None,
        q=q.get("q") or None,
        official=(q.get("official") in ("1", "true", "yes")) if q.get("official") else None,
        sort=q.get("sort") or "new",
        page=q.get("page") or 1,
        size=q.get("size") or 20,
    )
    data["ok"] = True
    h._json(data)


def plaza_detail(h) -> None:
    """GET /plaza/api/card?id= —— 详情。"""
    if not _is_server():
        _proxy(h, "card", timeout=_T_LIST)
        return
    cid = (_q(h).get("id") or "").strip()
    if not st.valid_card_id(cid):
        return _err(h, 400, "卡片 id 非法")
    card = st.get_card(cid)
    if not card:
        return _err(h, 404, "卡片不存在或未公开")
    h._json({"ok": True, "card": card})


def plaza_asset(h) -> None:
    """GET /plaza/api/asset?id=&slot=thumb|avatar|display|sticker-1..8|cover —— 长缓存 + ETag/304。

    槽位白名单与 `card_format.IMAGE_SPECS` 同一份（单一真相源）；`cover` 是老槽位，
    老客户端仍在请求它，所以继续服务。
    """
    if not _is_server():
        _proxy(h, "asset")
        return
    q = _q(h)
    cid = (q.get("id") or "").strip()
    slot = (q.get("slot") or "cover").strip()
    if not st.valid_card_id(cid) or slot not in cf.IMAGE_SPECS:
        return _err(h, 400, "参数非法")
    name = st._find_asset(cid, slot)
    if not name:
        return _err(h, 404, "图片不存在")
    fp = st.assets_dir(cid) / name
    try:
        blob = fp.read_bytes()
    except OSError:
        return _err(h, 404, "图片读取失败")
    etag = '"%s"' % hashlib.sha256(blob).hexdigest()[:32]
    if (h.headers.get("If-None-Match") or "").strip() == etag:
        h.send_response(304)
        h.send_header("ETag", etag)
        h.send_header("Cache-Control", "public, max-age=86400")
        h.end_headers()
        return
    _send_bytes(h, blob, _ASSET_MIME.get(fp.suffix.lower(), "application/octet-stream"),
                cache="public, max-age=86400", extra={"ETag": etag})


def plaza_download(h) -> None:
    """GET /plaza/api/download?id= —— 下载 card.zip。"""
    if not _is_server():
        _proxy(h, "download", timeout=_T_DOWNLOAD)
        return
    cid = (_q(h).get("id") or "").strip()
    if not st.valid_card_id(cid):
        return _err(h, 400, "卡片 id 非法")
    if not st.get_card(cid):
        return _err(h, 404, "卡片不存在或未公开")
    fp = st.card_zip_path(cid)
    try:
        blob = fp.read_bytes()
    except OSError:
        return _err(h, 404, "卡片文件读取失败")
    st.bump_download(cid)
    # 卡是用户内容 → 不缓存；文件名用卡 id（前端另存为更友好的名字）
    _send_bytes(h, blob, "application/zip", cache="no-store", extra={
        "Content-Disposition": f'attachment; filename="{quote(cid)}.card.zip"'})


def plaza_installed(h) -> None:
    """GET /plaza/api/installed —— 本端已安装的广场卡 + 配额用量（列表页打"已安装"标记用）。

    **不走代理**：装在哪份数据根，就由哪份后端回答（本地模式回答本机，服务器模式回答该账号）。
    """
    if not _is_server():
        # 本地模式：已装列表看本机清单；广场侧的"是否存在"由 list/detail 负责
        pass
    from plaza import install as pl
    u = pl.plaza_usage()
    root = pl._user_root()
    items = []
    for pid in u["ids"]:
        try:
            p = json.loads((root / pid / "character" / "preset.json").read_text(encoding="utf-8"))
        except Exception:
            p = {}
        items.append({"id": pid, "name": p.get("name") or pid,
                      "char_name": p.get("char_name") or ""})
    _admin = _is_admin_user(h)          # 一次解析（_bearer_user 别重复调）
    h._json({"ok": True, "items": items, "used_cards": u["count"], "used_bytes": u["bytes"],
             # 管理员不受安装配额限制 ⇒ 上限给 **null**（"不限"），由前端显示文案；
             # 普通账号照旧给数字（QUOTA_MAX_CARDS / QUOTA_MAX_BYTES）。
             "max_cards": None if _admin else pl.QUOTA_MAX_CARDS,
             "max_bytes": None if _admin else pl.QUOTA_MAX_BYTES,
             "server_mode": _is_server()})


def plaza_install(h) -> None:
    """POST /plaza/api/install {id, replace?} —— 装到**本端**数据根（不代理）。"""
    from routes import _read_json
    from plaza import install as pl
    body = _read_json(h) or {}
    cid = str(body.get("id") or "").strip()
    if not st.valid_card_id(cid):
        return _err(h, 400, "卡片 id 非法")
    ok, why, info = pl.install_card(cid, replace=bool(body.get("replace")),
                                    is_admin=_is_admin_user(h))
    if not ok:
        return _err(h, 401 if "请先登录" in why else 400, why)
    h._json({"ok": True, "card": info})


def plaza_uninstall(h) -> None:
    """POST /plaza/api/uninstall {id} —— 卸载本端安装的广场卡（不代理）。"""
    from routes import _read_json
    from plaza import install as pl
    body = _read_json(h) or {}
    cid = str(body.get("id") or "").strip()
    if not st.valid_card_id(cid):
        return _err(h, 400, "卡片 id 非法")
    ok, why = pl.uninstall_card(cid)
    if not ok:
        return _err(h, 400, why)
    h._json({"ok": True, "id": cid})


def plaza_derive(h) -> None:
    """POST /plaza/api/derive —— 以**已发布的广场卡**为模板，**服务端复制**成自己的新草稿，
    只上传 `patch` 里变化的文件（未改的文本/图片不用重传 ⇒ 省流量）。

    入参（契约 02）：
      · `base_id`（必须）：广场上**已发布**的卡；不存在/未发布 ⇒ 404 人话；
      · `card`（可选，**只给变化的部分**）：`id`（新卡 id，缺省自动 `<base>-copy`）、
        元数据字段（name/desc/category/tags/…，缺省沿用 base）、`files:[{path,text}]`（新增/替换文本）、
        `images:{thumb|avatar|display|cover: dataURL, stickers:[{data,label}]}`（**只放被替换的图**）；
      · `remove`（可选）：`{files:[路径], images:[槽位名或文件名]}`；
      · `display`（可选）：作者展示名。
    产物 = **当前用户的新草稿**（新 id；原卡与原作者完全不受影响），随后走既有审核/发布闸门。
    **隐私红线**：只复制卡体（`character/**` 白名单文件）——绝不碰 base 作者的账号数据/记忆/聊天。
    """
    if _plaza_side_post(h, "derive"):
        return
    from routes import _read_json
    from plaza import card_format as cf
    from plaza import drafts as dr
    body = _read_json(h) or {}
    user = _current_user(h)
    if not user:
        return _err(h, 401, "请先登录再改编角色卡")
    base_id = str(body.get("base_id") or "").strip()
    if not st.valid_card_id(base_id):
        return _err(h, 400, "基础卡 id 非法")
    # ① 只认**已发布**的公开卡（pending/archived/rejected/不存在 ⇒ 404 人话；这也挡住了越权改编）
    if st.get_card(base_id) is None:
        return _err(h, 404, f"基础卡不存在或未发布（只能改编广场上已发布的卡）：{base_id}")
    try:
        base = cf.parse_card_zip(st.card_zip_path(base_id).read_bytes())
    except (cf.CardError, OSError) as e:
        logger.warning("基础卡读取失败：%s", getattr(e, "code", type(e).__name__))
        return _err(h, 500, "基础卡读取失败，请稍后重试；仍失败请到「反馈」附诊断包")

    card = body.get("card") if isinstance(body.get("card"), dict) else {}
    remove = body.get("remove") if isinstance(body.get("remove"), dict) else {}
    is_admin = _is_admin_user(h)
    ohash = dr.owner_hash(user)
    # ← 服务端复制（0 上传）。`preset.json` **不复制**：它是"唯一权威清单"，里面写着旧 id，
    #   必须按新卡重新生成（否则 preset 解析器按 id 不一致直接拒）。
    files = {k: bytes(v) for k, v in (base.get("files") or {}).items() if k != "preset.json"}
    copied = {"files": len(files), "images": sum(1 for k in files if k.startswith("assets/"))}
    try:
        up_f, rm_f = _derive_patch_files(card, files, remove)
        up_i, rm_i = _derive_patch_images(card, remove, files)
        # ② 元数据：base 的**卡字段**打底，patch 里出现的才覆盖；id 换新（其余运行期字段一律不带过来）
        manifest = {}
        for k in cf.CARD_METADATA_FIELDS:
            if base["manifest"].get(k) is not None:
                manifest[k] = base["manifest"][k]
        for k in cf.CARD_METADATA_FIELDS:
            if k in card and card.get(k) is not None:
                manifest[k] = card.get(k)
        new_id = str(card.get("id") or "").strip() or _derive_auto_id(base_id, ohash)
        if not st.valid_card_id(new_id):
            return _err(h, 400, "新卡 id 非法（只允许字母/数字/下划线/连字符）")
        manifest["id"] = new_id
        if card.get("opening"):                     # opening 走结构化字段（与制卡页同口径）
            op = card["opening"]
            op = ({"narrations": list(op.get("narrations") or []),
                   "first_messages": [str(x) for x in (op.get("first_messages") or []) if str(x).strip()]}
                  if isinstance(op, dict) else {"narrations": [], "first_messages": [str(op)]})
            if op["first_messages"] or op["narrations"]:
                files["opening.json"] = json.dumps(op, ensure_ascii=False).encode("utf-8")
                up_f.append("opening.json")
        # preset.json 由后端按新卡重新生成，与制卡入口共用同一字段投影。
        files["preset.json"] = json.dumps(
            cf.build_preset(manifest), ensure_ascii=False
        ).encode("utf-8")
        require_thumb = bool(card.get("files") or "images" in card or cf.thumb_name(files))
        data = cf.build_card_zip(manifest, files, require_thumb=require_thumb, is_admin=is_admin)
        meta = dr.save_draft(ohash, data, display=dr.clean_display(body.get("display"), user),
                             is_admin=is_admin, derived_from=base_id)
    except cf.CardError as e:
        return _err(h, 400, f"改编校验未通过：{e.message}；请按提示修改后重试")
    except dr.DraftError as e:
        return _err(h, 400, e.message)
    h._json({"ok": True, "derived_from": base_id,
             "draft": {"id": meta["id"], "status": meta["status"], "updated_at": meta["updated_at"],
                       "size_bytes": meta["size_bytes"]},
             "copied": copied,                       # 服务端复制来的（没上传）
             "uploaded": {"files": up_f, "images": up_i},
             "removed": {"files": rm_f, "images": rm_i}})


def plaza_review(h) -> None:
    """POST /plaza/api/review {sk, card, display?} —— 用**用户自己的 Key** 审核，审完即弃。

    - 必须跑在**广场侧**（服务器）：否则本地模式用户能自己伪造"审核通过"再发布；
    - `sk` 只在本函数栈里使用：不落盘、不进日志、不进审计、不回显；
    - 审核结论与卡体摘要一起存进草稿，发布时比对（防"审 A 发 B"）。
    """
    if _plaza_side_post(h, "review"):
        return
    from routes import _read_json
    from plaza import card_format as cf
    from plaza import drafts as dr
    from plaza import review as rv
    body = _read_json(h) or {}
    sk = str(body.get("sk") or "").strip()
    ok, why = rv.validate_sk_format(sk)
    if not ok:
        return _err(h, 400, why)
    user = _current_user(h)
    ohash = dr.owner_hash(user)
    try:
        data = _card_zip_from_payload(body.get("card") or {}, is_admin=_is_admin_user(h))
        result = rv.review_card_data(data, sk)
        display = dr.clean_display(body.get("display"), user)
        # ★2026-10-02：**系统错误不能伪装成"审核未通过"**。
        #   · pass          ⇒ reviewed
        #   · reject        ⇒ rejected（真的被审掉了）
        #   · 解析/调用失败 ⇒ 保持 draft（带 `error_kind`），UI 如实显示"系统/模型问题"
        #   · manual（模型拿不准 = 业务结论）⇒ rejected（保持既有语义：等人工复核）
        if result.get("verdict") == "pass":
            _status = "reviewed"
        elif result.get("error_kind"):
            _status = "draft"
        else:
            _status = "rejected"
        try:
            dr.save_draft(ohash, data, display=display, status=_status,
                          review=result, is_admin=_is_admin_user(h))
        except dr.DraftError as e:
            logger.warning("审核结论落草稿失败：%s", e.code)
            if e.code == dr.DRAFT_PUBLISH_FAILED:
                return _err(h, 500, "审核结果保存失败，请稍后重试",
                            error_kind="storage_error", code=e.code)
            if e.code == dr.DRAFT_LIMIT:
                return _err(h, 400, e.message, error_kind="draft_limit", code=e.code)
            return _err(h, 400, e.message, code=e.code)
    except cf.CardError as e:
        return _err(h, 400, f"卡片校验未通过：{e.message}；请按提示修改后重试")
    except rv.ReviewError as e:
        # 系统/Key 问题：带 error_kind 回传 ⇒ 前端显示"系统问题"，不是"审核未通过"
        return _err(h, 400, e.message, error_kind=getattr(e, "kind", "api_error"))
    finally:
        # 尽力而为：断开引用（Python 不保证内存擦除，本模块保证的是零持久化/零记录）
        sk = None
        body.pop("sk", None)
    h._json({"ok": True, "review": result})


def plaza_draft_list(h) -> None:
    """GET /plaza/api/drafts —— 本人的草稿列表。"""
    if _plaza_side(h, "drafts"):
        return
    from plaza import drafts as dr
    ohash = dr.owner_hash(_current_user(h))
    h._json({"ok": True, "items": dr.list_drafts(ohash)})


def plaza_draft_get(h) -> None:
    """GET /plaza/api/draft?id= —— 取回草稿内容（供表单继续编辑）。

    返回 `draft.card` **两种形状都有**：
      · 新形状（06 §3.2 对称）：`files:[{path,text}]`、`images:{avatar,thumb,display,stickers:[{data,label}]}`、
        `opening:{narrations,first_messages}` + manifest 元数据字段；
      · 老形状（老前端兜底）：`core`/`identity`/`sms_samples`/`cover`/`avatar`。
    `preset.json` / `opening.json` / `stickers.json` **不进 files[]**：前者由后端生成，
    后两者走结构化的 `opening` 与 `images.stickers[].label`。
    """
    if _plaza_side(h, "draft"):
        return
    from plaza import drafts as dr
    ohash = dr.owner_hash(_current_user(h))
    did = (_q(h).get("id") or "").strip()
    meta, blob = dr.load_draft(ohash, did)
    if not blob:
        return _err(h, 404, "草稿不存在")
    try:
        parsed = cf.parse_card_zip(blob)
    except cf.CardError as e:
        return _err(h, 400, f"草稿已损坏（{e.code}）")
    card = dict(parsed["manifest"])
    files_out = []
    images: dict = {}
    stickers = []
    labels: dict = {}
    if "stickers.json" in parsed["files"]:
        try:
            raw_labels = json.loads(parsed["files"]["stickers.json"].decode("utf-8"))
            if isinstance(raw_labels, dict):
                labels = {str(k): str(v) for k, v in raw_labels.items()}
        except Exception:
            labels = {}
    for rel in sorted(parsed["files"]):
        blob2 = parsed["files"][rel]
        if rel.startswith("assets/"):
            slot = Path(rel).stem
            url = _blob_data_url(rel, blob2)
            if slot in cf.STICKER_SLOTS:
                stickers.append((int(slot.split("-")[1]), Path(rel).name, url))
            else:
                images[slot] = url
            continue
        if rel in ("preset.json", "opening.json", "stickers.json"):
            continue
        files_out.append({"path": rel, "text": blob2.decode("utf-8", "replace")})
    if stickers:
        # 与 §3.2 请求形状对称：`[{"data": …, "label": "…"}]`（label 为空串 = 没填标签）
        images["stickers"] = [{"data": u, "label": labels.get(n, "")} for _i, n, u in sorted(stickers)]
    card["files"] = files_out
    card["images"] = images
    # —— 老形状（老前端兜底）——
    for rel, key in (("core.md", "core"), ("identity.md", "identity"),
                     ("sms_samples.md", "sms_samples")):
        if rel in parsed["files"]:
            card[key] = parsed["files"][rel].decode("utf-8", "replace")
    if "opening.json" in parsed["files"]:
        try:
            card["opening"] = json.loads(parsed["files"]["opening.json"].decode("utf-8"))
        except Exception:
            card["opening"] = ""
    for slot in ("cover", "avatar", "thumb", "display"):
        if slot in images:
            card[slot] = images[slot]
    h._json({"ok": True, "draft": {"meta": meta, "card": card}})


def _official_requested(body: dict) -> bool | None:
    """payload 里对官方标记的**显式**要求：`True` / `False` / `None`（没提）三态。

    为什么必须三态（2026-10-01 收口）：前端已有官方勾选框，会发 `official: true/false`
    —— 只判"有没有 true"的话，管理员勾掉也照样是官方，勾选框就成了装饰 ✗。
    `official` 允许出现在顶层或 `body.card` 里（前端两种写法都用过）。
    """
    if not isinstance(body, dict):
        return None
    for src in (body, body.get("card")):
        if isinstance(src, dict) and isinstance(src.get("official"), bool):
            return src["official"]
    return None


def _check_official(h, body: dict) -> tuple[bool, bool]:
    """返回 `(放行?, 是否官方)`。

    口径（2026-10-01）：
      · `official: true` 且**非管理员** ⇒ 403 并记日志（现状保留，`_err` 已发响应）；
      · 管理员**显式 `false`** ⇒ 发非官方卡（勾选框真的有用）；
      · **没传** ⇒ 沿用既有语义：管理员默认 True、普通用户 False。
    """
    want = _official_requested(body)
    is_admin = _is_admin_user(h)
    if want is True and not is_admin:
        logger.warning("非管理员请求官方标记被拒：user=%s", (_current_user(h) or {}).get("email"))
        _err(h, 403, "只有管理员能设为官方卡；普通作者请去掉「官方」标记后重试")
        return False, False
    official = is_admin if want is None else bool(want and is_admin)
    return True, official


def plaza_draft_save(h) -> None:
    """POST /plaza/api/draft {card, display?} —— 存草稿（不审核、不发布）。"""
    if _plaza_side_post(h, "draft"):
        return
    from routes import _read_json
    from plaza import card_format as cf
    from plaza import drafts as dr
    body = _read_json(h) or {}
    user = _current_user(h)
    is_admin = _is_admin_user(h)
    try:
        data = _card_zip_from_payload(body.get("card") or {}, is_admin=is_admin)
        meta = dr.save_draft(dr.owner_hash(user), data,
                             display=dr.clean_display(body.get("display"), user),
                             is_admin=is_admin)
    except cf.CardError as e:
        return _err(h, 400, f"卡片校验未通过：{e.message}；请按提示修改后重试")
    except dr.DraftError as e:
        return _err(h, 400, e.message)
    h._json({"ok": True, "draft": {"id": meta["id"], "status": meta["status"],
                                   "updated_at": meta["updated_at"]}})


def plaza_draft_delete(h) -> None:
    """POST /plaza/api/draft/delete {id} —— 删除自己的草稿。"""
    if _plaza_side_post(h, "draft/delete"):
        return
    from routes import _read_json
    from plaza import drafts as dr
    body = _read_json(h) or {}
    did = str(body.get("id") or "").strip()
    if not st.valid_card_id(did):
        return _err(h, 400, "草稿 id 非法")
    ok = dr.delete_draft(dr.owner_hash(_current_user(h)), did)
    if not ok:
        return _err(h, 404, "草稿不存在")
    h._json({"ok": True, "id": did})


def plaza_publish(h) -> None:
    """POST /plaza/api/publish {id, replace?, official?} —— 发布已通过审核的草稿。

    `official: true` **只有管理员能传**（2026-10-01 用户要求：管理员要能发官方卡）；
    普通用户传 ⇒ 403（不是静默忽略）。管理员发布的卡默认带官方标记（与既有草稿路径一致）。
    """
    if _plaza_side_post(h, "publish"):
        return
    from routes import _read_json
    from plaza import drafts as dr
    body = _read_json(h) or {}
    did = str(body.get("id") or "").strip()
    if not st.valid_card_id(did):
        return _err(h, 400, "卡片 id 非法")
    _ok_official, official = _check_official(h, body)
    if not _ok_official:
        return
    user = _current_user(h)
    try:
        card = dr.publish_draft(dr.owner_hash(user), did, user=user, official=official,
                                replace=bool(body.get("replace")))
    except dr.DraftError as e:
        return _err(h, 400, e.message)
    h._json({"ok": True, "card": card})


def plaza_unpublish(h) -> None:
    """POST /plaza/api/unpublish {id} —— 作者下架自己的卡（或管理员下架任意卡）。"""
    if _plaza_side_post(h, "unpublish"):
        return
    from routes import _read_json
    from plaza import drafts as dr
    body = _read_json(h) or {}
    did = str(body.get("id") or "").strip()
    if not st.valid_card_id(did):
        return _err(h, 400, "卡片 id 非法")
    if not dr.unpublish(dr.owner_hash(_current_user(h)), did, user=_current_user(h)):
        return _err(h, 403, "无权下架这张卡（只有作者本人或管理员可以）")
    h._json({"ok": True, "id": did})


def plaza_report(h) -> None:
    """POST /plaza/api/report {id, reason} —— 举报一张卡（登录可见的前提下的必要治理面）。

    同一举报人对同一张卡**只算一次**；达到阈值自动转待复核（从公开列表消失），**不是删除**。
    """
    if _plaza_side_post(h, "report"):
        return
    from routes import _read_json
    from plaza import drafts as dr
    from plaza import store as st2
    body = _read_json(h) or {}
    cid = str(body.get("id") or "").strip()
    if not st2.valid_card_id(cid):
        return _err(h, 400, "卡片 id 非法")
    if not st2.get_card(cid):
        return _err(h, 404, "卡片不存在或未公开")
    out = st2.add_report(cid, str(body.get("reason") or ""), dr.owner_hash(_current_user(h)))
    if out is None:
        return _err(h, 404, "卡片不存在")
    h._json({"ok": True, "id": cid})


def _require_admin(h) -> bool:
    """管理员校验（服务器侧）。本地模式已在 `_plaza_side*` 里代理出去，不会走到这。"""
    if not _is_admin_user(h):
        _err(h, 403, "需要管理员权限")
        return False
    return True


def plaza_admin_list(h) -> None:
    """GET /plaza/api/admin/list?status=&page=&size= —— 管理台列表（含未公开状态与举报数）。"""
    if _plaza_side(h, "admin/list"):
        return
    if not _require_admin(h):
        return
    q = _q(h)
    raw = (q.get("status") or "").strip()
    statuses = None if raw in ("", "all") else tuple(s for s in raw.split(",") if s)
    h._json(st.admin_list(statuses=statuses, page=q.get("page") or 1, size=q.get("size") or 20))


def plaza_admin_status(h) -> None:
    """POST /plaza/api/admin/status {id, status} —— 管理员下架/恢复/驳回一张卡。"""
    if _plaza_side_post(h, "admin/status"):
        return
    if not _require_admin(h):
        return
    from routes import _read_json
    from plaza import drafts as dr
    body = _read_json(h) or {}
    cid = str(body.get("id") or "").strip()
    status = str(body.get("status") or "").strip()
    if not st.valid_card_id(cid):
        return _err(h, 400, "卡片 id 非法")
    if status not in st.STATUSES:
        return _err(h, 400, f"状态必须是 {'/'.join(st.STATUSES)} 之一")
    if not st.set_status(cid, status):
        return _err(h, 404, "卡片不存在")
    st.append_audit(cid, {"action": "admin-status", "status": status,
                          "by": dr.owner_hash(_current_user(h))})
    h._json({"ok": True, "id": cid, "status": status})


# 路由键（app/api/router.py 引用；新增即改 tests/test_routes_oracle.py）
# 广场侧（本地模式代理）：list/card/asset/download/drafts/draft/review/publish/unpublish
# 本端侧（两种模式都本地执行）：installed/install/uninstall
GET_ROUTES = {
    "/plaza/api/list": plaza_list,
    "/plaza/api/card": plaza_detail,
    "/plaza/api/asset": plaza_asset,
    "/plaza/api/download": plaza_download,
    "/plaza/api/installed": plaza_installed,
    "/plaza/api/drafts": plaza_draft_list,
    "/plaza/api/draft": plaza_draft_get,
    "/plaza/api/admin/list": plaza_admin_list,
}

POST_ROUTES = {
    "/plaza/api/install": plaza_install,
    "/plaza/api/uninstall": plaza_uninstall,
    "/plaza/api/review": plaza_review,
    "/plaza/api/derive": plaza_derive,
    "/plaza/api/draft": plaza_draft_save,
    "/plaza/api/draft/delete": plaza_draft_delete,
    "/plaza/api/publish": plaza_publish,
    "/plaza/api/unpublish": plaza_unpublish,
    "/plaza/api/report": plaza_report,
    "/plaza/api/admin/status": plaza_admin_status,
}
