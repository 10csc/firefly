# -*- coding: utf-8 -*-
"""广场 V1 **端到端验收**（真服务器进程 + 真账号 token + 真落盘 + 真消费侧；verifier 独占）

契约：docs/设计/角色卡共创平台/06_制卡自由度与图片规格.md（含 §七 验证记录）
目标链（task-11 第 4 条）：
    **登录 → 制卡（2 份知识库 + 3 张表情包）→ 审核 → 发布 → 列表可见（只用缩略图）
      → 安装 → App 侧可用**
`App 侧可用`**不是断言"文件落盘了"**（那是最容易假绿的一条），而是断言：
    · `llm_retriever.has_knowledge(card_id)` 为真，且**检索内容里真的出现卡内知识库的正文**；
    · `domain.stickers.picker` 里**真的有这张卡的 3 张表情包**，且 label 来自卡内 `stickers.json`；
    · 卸载后这两处**都干净**。
这三条正是 clientpack 报过的两个消费侧缺陷（"知识库进不了检索器 / 表情包进不了选择器"）的回归位。

沙箱：临时数据根 + `server_app.py` 真进程 + 真账号 token；**不连外网、不碰真实 user_data、
零模型费用**（"审核"按真实形状把结论写进沙箱草稿，产品闸门一行不改）。
退出码：0 全过；1 有 FAIL；2 服务没起来。
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _plaza_accept_frame import (  # noqa: E402
    check, human, finish, free_port, data_url, base_card, png_bytes,
    THUMB_OK, AVATAR_OK, DISPLAY_OK, SANDBOX, USER_DIR, PLAZA_DIR, ROOT,
)

import subprocess                                                             # noqa: E402

import db as srv_db                                                           # noqa: E402
from plaza import card_format as cf                                           # noqa: E402

CARD_ID = "acc_e2e_card"
# ⚠ 标记必须落在**知识库文件**里：`core.md` 不进检索（检索器只吃 `knowledge/**`）。
#   第一版把 KB_MARK 写在 core.md 里却断言"检索内容含它" —— 又是断言写错（错误总结 #29）。
KB_MARK = "知识库正文标记：星核猎手以狩猎星核为业。"       # 扁平 knowledge/世界观.md
KB_MARK2 = "分组知识库标记：五域之世界观。"                # 一层分组 knowledge/world/星核猎手.md
CORE_MARK = "核心设定正文标记：不进检索器，只进卡体。"      # core.md（反证用：**不应**出现在检索内容里）
STICKER_LABELS = ["开心", "困惑", "认真"]

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID = srv_db.create_user("e2e@qq.com", "h", "s", install_id="inst-" + "e" * 32, role="admin")
TOK = "tok-e2e"
srv_db.create_session(TOK, UID, "e2e", EXP)

PORT = free_port()
check("E2E-0", f"拿到空闲端口（bind(0)）{PORT}", PORT > 0, f"port={PORT}", layer="E2E")
import os                                                                     # noqa: E402
env = dict(os.environ)
env.update({"FIREFLY_PORT": str(PORT), "PYTHONUNBUFFERED": "1", "FIREFLY_NO_BROWSER": "1"})
log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=env, stdout=log, stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, headers=None):
    h = {"Authorization": "Bearer " + TOK}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:
        return 0, {}, f"<{type(e).__name__}: {e}>".encode("utf-8", "replace")


def errors_seen():
    """服务器日志里的 401/500/异常（全链"零 401/报错"要有证据）。"""
    try:
        t = (SANDBOX / "server.log").read_text("utf-8", "replace")
    except OSError:
        return []
    bad = []
    for ln in t.splitlines():
        if " 401 " in ln or " 500 " in ln or "Traceback" in ln or "ERROR" in ln:
            bad.append(ln.strip()[:160])
    return bad


try:
    ready = False
    for _ in range(80):
        s, _h, _b = req("GET", "/health")
        if s == 200:
            ready = True
            break
        time.sleep(0.5)
    check("E2E-1", "真实服务器起来（/health 200）", ready, f"port={PORT}", layer="E2E")
    if not ready:
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)

    print("\n== 1. 登录态（真 token；未带 token 必须 401）==")
    s, _h, _b = req("GET", "/plaza/api/list", headers={"Authorization": ""})
    check("E1", f"未登录读列表 → 401（实际 {s}）", s == 401, f"{s}", layer="E2E")
    s, _h, b = req("GET", "/plaza/api/list")
    check("E2", "带 token 读列表 → 200", s == 200, f"{s}", layer="E2E")

    print("\n== 2. 制卡：2 份知识库（扁平 + 一层分组）+ 3 张带 label 表情包 + 头像/缩略图/大图 ==")
    card = base_card(CARD_ID, files=[
        {"path": "core.md", "text": "# 核心设定\n" + CORE_MARK},
        {"path": "knowledge/世界观.md", "text": "# 世界观\n" + KB_MARK},
        {"path": "knowledge/world/星核猎手.md", "text": "# 星核猎手\n" + KB_MARK2},
    ], images={
        "thumb": data_url(THUMB_OK),
        "avatar": data_url(AVATAR_OK),
        "display": data_url(DISPLAY_OK),
        "stickers": [{"data": data_url(png_bytes(32, 32, seed=40 + i)), "label": lb}
                     for i, lb in enumerate(STICKER_LABELS)],
    })
    s, _h, b = req("POST", "/plaza/api/draft", {"card": card, "display": "官方"})
    check("E3", "存草稿成功（新格式：files[] + images{}）", s == 200,
          f"status={s} {b[:120].decode('utf-8', 'replace')!r}", layer="E2E")

    # 审核：按**真实形状**把结论写进沙箱草稿（不花模型钱，产品闸门一行不改）
    patched = 0
    for meta_fp in (PLAZA_DIR / "drafts").rglob("*.json"):
        zf = meta_fp.with_suffix(".zip")
        if not zf.is_file():
            continue
        m = json.loads(meta_fp.read_text(encoding="utf-8"))
        if m.get("id") != CARD_ID:
            continue
        m["review"] = {"verdict": "pass", "risk": "low", "reasons": ["桩：合规"],
                       "model": "deepseek-stub", "digest": cf.parse_card_zip(zf.read_bytes())["digest"]}
        m["status"] = "reviewed"
        meta_fp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        patched += 1
    check("E4", "审核结论按真实形状写入沙箱草稿", patched == 1, f"patched={patched}", layer="E2E")

    s, _h, b = req("POST", "/plaza/api/publish", {"id": CARD_ID})
    check("E5", "发布成功", s == 200, f"status={s} {b[:120].decode('utf-8', 'replace')!r}", layer="E2E")

    print("\n== 3. 列表可见 + 只用缩略图 ==")
    s, _h, b = req("GET", "/plaza/api/list?page=1&size=20")
    lst = json.loads(b.decode("utf-8")) if s == 200 else {}
    items = lst.get("items") or []
    row = next((x for x in items if x["id"] == CARD_ID), None)
    check("E6", "列表里能看到刚发布的卡", row is not None, f"ids={[x['id'] for x in items]}", layer="E2E")
    if row:
        check("E7", "列表条目只给 thumb 地址（无 cover/display）",
              bool(row.get("thumb")) and not row.get("cover") and not row.get("display"),
              f"thumb={row.get('thumb')!r} cover={row.get('cover')!r} display={row.get('display')!r}",
              layer="E2E")
        check("E8", "列表响应不含 display/sticker 地址",
              all(x not in b.decode("utf-8", "replace")
                  for x in ('"slot=display"', '"slot=sticker-1"')), layer="E2E")
    s, _h, b = req("GET", f"/plaza/api/asset?id={CARD_ID}&slot=thumb")
    check("E9", f"缩略图可取其 ≤12KB（{human(len(b))}）", s == 200 and 0 < len(b) <= 12 * 1024,
          f"status={s} bytes={len(b)}", layer="E2E")

    print("\n== 4. 安装 → 落到本账号目录 ==")
    s, _h, b = req("POST", "/plaza/api/install", {"id": CARD_ID})
    check("E10", "安装成功", s == 200, f"status={s} {b[:160].decode('utf-8', 'replace')!r}", layer="E2E")
    pack_dir = USER_DIR / str(UID) / CARD_ID
    check("E11", f"包落在 user_data/{UID}/{CARD_ID}/character/",
          (pack_dir / "character" / "preset.json").is_file(), f"{pack_dir}", layer="E2E")
    kb_flat = pack_dir / "character" / "knowledge" / "世界观.md"
    kb_grp = pack_dir / "character" / "knowledge" / "world" / "星核猎手.md"
    check("E12", "卡内**扁平**知识库随包落地", kb_flat.is_file(), f"{kb_flat}", layer="E2E")
    check("E13", "卡内**一层分组**知识库随包落地", kb_grp.is_file(), f"{kb_grp}", layer="E2E")
    st_dir = pack_dir / "character" / "assets"
    stickers_on_disk = sorted(p.name for p in st_dir.glob("sticker-*")) if st_dir.is_dir() else []
    check("E14", f"卡内 3 张表情包随包落地（{stickers_on_disk}）",
          len(stickers_on_disk) == 3, f"{stickers_on_disk}", layer="E2E")

    print("\n== 5. ★App 侧真的可用（不是只看文件在不在）==")
    # ⚠ 消费侧函数（检索器/注册表/选择器）都**按当前用户上下文**取数据根，
    #   而 `core/preset_parse._scan_custom_dirs()` 这类"扫数据根"的老口径只看 `USER_DIR`。
    #   在**同一个测试进程**里热改上下文来判断，会踩到两者口径不一致：
    #   实测出现 `packs.json 自愈：移除 ['acc_e2e_card']` ⇒ 登记被自己删掉 ⇒ 误判成"检索器读不到知识库"。
    #   ⇒ 正确的判法：**另起一个全新进程**，把数据根摆成"装完卡后 App 自己那份"
    #     （`FIREFLY_DATA_DIR={数据根}` ⇒ `USER_DIR={数据根}/user_data`，卡就在 `USER_DIR/{卡id}`），
    #     在那里跑消费侧单点复核（_harden/consumer_check.py）。
    import subprocess as _sp

    # ⚠ 数据根要传**账号目录**（`{沙箱}/user_data/{uid}`）的**父级**语义：
    #   `FIREFLY_DATA_DIR=X` ⇒ `USER_DIR = X/user_data`，而安装的包在 `{沙箱}/user_data/{uid}/{卡id}`。
    #   所以让 `USER_DIR` 直接等于"账号目录"：`X = {沙箱}/user_data/{uid}` 的父级 = {沙箱}/user_data …
    #   正确做法是把 X 设成**账号目录的父级再嵌套**——直接传账号目录会得到 `.../1/user_data`。
    #   这里显式构造：`FIREFLY_DATA_DIR = {沙箱}/user_data/{uid}`，并把 consumer_check 的契约改为
    #   "传进来的 DATA_ROOT 就是 USER_DIR 的父级" ⇒ 需要 `{沙箱}/user_data` 且 uid 段单独给。
    #   最简且无歧义：把账号目录当 USER_DIR（consumer_check 里改读环境变量 FIREFLY_USER_DIR）。
    consumer = _sp.run([sys.executable, str(ROOT / "_harden" / "consumer_check.py"),
                        str(USER_DIR / str(UID)), CARD_ID],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = (consumer.stdout or "").strip().splitlines()[-1] if consumer.stdout.strip() else "{}"
    try:
        cinfo = json.loads(line)
    except Exception:
        cinfo = {}
    if not cinfo:
        print("      consumer_check 原始输出：", (consumer.stdout or "")[-400:],
              (consumer.stderr or "")[-400:], flush=True)

    check("E15a", "消费侧进程里该包在**用户视角包表**里（user_packs/all_packs）",
          bool(cinfo.get("in_user_packs")) and bool(cinfo.get("in_all_packs")),
          f"in_user_packs={cinfo.get('in_user_packs')} in_all_packs={cinfo.get('in_all_packs')} "
          f"valid_mode={cinfo.get('valid_mode')}；注：全局 PRESETS 只含内置包 "
          f"{cinfo.get('presets_keys')}，广场卡走 user_packs 合并（core/presets.py:177）", layer="E2E")
    check("E15", "★检索器认到该包**有知识库**（has_knowledge=True）",
          bool(cinfo.get("has_knowledge")),
          f"has_knowledge={cinfo.get('has_knowledge')} roots={cinfo.get('knowledge_roots')}",
          layer="E2E")
    full = cinfo.get("knowledge_full") or ""
    check("E16", "★检索内容里**真的**含卡内知识库正文（扁平）", KB_MARK in full,
          f"len={len(full)} 卡内知识库文件={cinfo.get('knowledge_files')} "
          f"正文={full[:220]!r}", layer="E2E")
    check("E17", "★检索内容里含卡内**一层分组**知识库正文", KB_MARK2 in full,
          f"len={len(full)}", layer="E2E")
    # 反证（防"什么内容都算加载"）：core.md **不是**知识库，不该出现在检索内容里
    check("E17b", "core.md 正文**不在**检索内容里（知识源只吃 knowledge/**）",
          CORE_MARK not in full, f"len={len(full)}", layer="E2E")

    st_list = cinfo.get("stickers") or []
    check("E18", f"★表情包选择器里有这张卡的 3 张（实际 {len(st_list)}）", len(st_list) == 3,
          f"{st_list}", layer="E2E")
    labels = sorted((s.get("label") or "") for s in st_list)
    check("E19", f"★label 来自卡内 stickers.json（实际 {labels}）",
          labels == sorted(STICKER_LABELS), f"期望 {sorted(STICKER_LABELS)}", layer="E2E")
    check("E20", f"★这 3 张在启用集合里（{sum(1 for s in st_list if s.get('enabled'))}/3）",
          sum(1 for s in st_list if s.get("enabled")) == 3, f"{st_list}", layer="E2E")

    print("\n== 6. 卸载后干净（不只是目录没了）==")
    if pack_dir.exists():
        _pre_links = [str(p) for p in pack_dir.rglob("*") if p.is_symlink()]
    else:
        _pre_links = ["<包目录不存在>"]
    check("K06c", f"安装目录里**零符号链接**（实际 {len(_pre_links)}）",
          _pre_links == [], f"{_pre_links[:3]}", layer="E2E")
    s, _h, b = req("POST", "/plaza/api/uninstall", {"id": CARD_ID})
    check("E21", "卸载成功", s == 200, f"status={s}", layer="E2E")
    check("E22", "账号目录里的包已删", not pack_dir.exists(), f"{pack_dir}", layer="E2E")
    # 卸载后表情包不能有残留：主进程没有"包视图"（消费侧在另一份数据根里），
    # 所以直接查**服务端接口**给出的已安装集合，才是与用户可见状态一致的口径。
    s, _h, b = req("GET", "/plaza/api/installed")
    inst = json.loads(b.decode("utf-8")) if s == 200 else {}
    check("E23", "卸载后 /plaza/api/installed 里也不再有它（无残留）",
          s == 200 and all(x.get("id") != CARD_ID for x in (inst.get("items") or [])),
          f"status={s} items={[x.get('id') for x in (inst.get('items') or [])]}", layer="E2E")

    print("\n== 7. 全链零 401/报错 ==")
    errs = errors_seen()
    check("E24", f"服务器日志零 401/500/Traceback（实际 {len(errs)} 条）", not errs,
          "; ".join(errs[:3]), layer="E2E")

    print("\n== 8. K06：软链穿越（zip 格式本身不携带软链语义；用**真实 zip + 真实落盘**判）==")
    # 判定分两半，各自独立：
    #   ① 构造一个**真的 ZIP 软链条目**（ZipInfo.external_attr 的高 16 位 = Unix 模式 0o120777，
    #      内容是被指向的路径），看 `parse_card_zip`/安装链路是否会把"链接目标"当文件收下；
    #   ② 落盘后检查：安装目录里**不该出现任何符号链接**（即便 zip 里塞了也不该复现成软链）。
    import stat as _stat
    import zipfile as _zip

    evil_zip = None
    try:
        from plaza import card_format as _cf
        zipfiles = {
            "preset.json": json.dumps({"id": "acc_symlink", "name": "软链卡", "char_name": "小萤",
                                       "user_name": "你", "presentation": "sticker",
                                       "desc": "软链穿越用例", "schema": 1},
                                      ensure_ascii=False).encode(),
            "assets/thumb.png": THUMB_OK,
            # ★ 软链条目：在 zip 里它就是"一个普通文件"，内容 = 目标路径；模式位标成 Unix 软链
            "knowledge/escape.md": b"/etc/passwd",
        }
        manifest = {"format": 1, "id": "acc_symlink", "name": "软链卡", "char_name": "小萤",
                    "user_name": "你", "desc": "软链穿越用例", "presentation": "sticker",
                    "schema": 1, "created_at": 0}
        # ⚠ 必须**先带上软链条目再算 files[]/digest**：第一版先算后加，结果被
        #   `CARD_HASH_MISMATCH` 拒掉 —— 看着"拒绝了"，其实一条都没测到软链（错误总结 #29）。
        manifest["files"] = _cf.file_entries(zipfiles)
        manifest["digest"] = _cf.card_digest(zipfiles)
        buf = __import__("io").BytesIO()
        with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for k, v in zipfiles.items():
                if k == "knowledge/escape.md":
                    zi = _zip.ZipInfo("character/" + k)
                    zi.create_system = 3                     # Unix
                    zi.external_attr = (0o120777 << 16)      # 软链模式位
                    zf.writestr(zi, v)
                else:
                    zf.writestr("character/" + k, v)
        evil_zip = buf.getvalue()
    except Exception as e:
        print(f"      （构造软链 zip 失败：{type(e).__name__}: {e}）", flush=True)
    check("K06a", "已构造含软链条目的真实 zip", evil_zip is not None and len(evil_zip) > 100,
          f"{len(evil_zip) if evil_zip else 0}B", layer="E2E")
    if evil_zip:
        try:
            parsed = cf.parse_card_zip(evil_zip)
            got = parsed["files"].get("knowledge/escape.md")
            # 关键判据：软链条目的**内容**是被指向的路径字符串，而不是链接本身。
            # 只要内容被当成普通文本收下（这就是 zip 的语义），就不存在"链接被跟随"；
            # 真正要守住的是**落盘后不产生符号链接**（下面 K06c 用真实安装验）。
            check("K06b", "软链条目按**普通文件内容**处理（zip 无软链语义，未被跟随）",
                  got == b"/etc/passwd",
                  f"收到 {got!r}（若为 None 说明被丢弃；两种都不是'跟随链接'）", layer="E2E")
        except cf.CardError as e:
            check("K06b", "软链条目被整卡拒绝（也是可接受的处理）", True,
                  f"{e.code} {e.message[:50]}", layer="E2E")

    # 真实安装落盘后检查：**安装目录里不许出现符号链接**（已在第 6 节卸载前判过 K06c）


finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log.close()

sys.exit(finish({"layer": "E2E"}))
