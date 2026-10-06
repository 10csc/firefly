# -*- coding: utf-8 -*-
"""装完广场卡之后，**这张卡是不是真的被当成角色在用**？—— 共创平台「下载并使用」的消费侧验收

为什么单独要这一测：需求原话是"客户端内置从广场**下载并使用**角色卡"。
此前验到的是"下载 + 装进本人目录 + `/modes` 里出现"（`test_plaza_server_e2e.py`），
**"使用"那一半没验过**；2026-10-01 起卡片格式扩了知识库/表情包/缩略图（契约 06），
于是"新的槽位装完到底能不能用"也必须由**产物级断言**钉住。

做法（**零模型费用**）：进程内调真实 `routes.chat()`，把流水线入口 `handle_chat` 桩掉
（它才是唯一会调 LLM 的地方），于是能验证真实的**编排与落盘**行为而不产生任何 API 调用。

覆盖（分三块）：
  A. 通用"装完能用"：① 装完登记为该模式（角色名取自卡）② 角色目录就是卡的内容
     ③ 以该模式发消息 → 流水线 mode 正确 ④ 消息落在该卡自己的会话、不污染默认模式
  B. **新格式卡**（契约 06 §3.1）：2 份扁平知识库 + 1 份分组知识库 + 3 张表情包
     （带/不带 label）+ thumb/display（**无 avatar/cover**）——
     落盘正确、`/modes` 槽位回落有图、`/pack-knowledge` 能列出并可编辑、
     结构树计数正确、**知识库进检索器**、**表情包进选择器（pack 专属 + label）**。
  C. **新旧兼容与残留**：旧格式卡（只有 core + cover）照旧能装能用、不误触发检索；
     覆盖安装幂等（表情包不堆叠）；卸载后目录/模式列表/**包外产物**（表情包条目与图）全干净。

沙箱：临时 `FIREFLY_DATA_DIR` / `FIREFLY_PLAZA_DIR`，绝不碰真实数据；桩认证服务器只在本进程。
"""
import json
import os
import socket as _sk
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_usecard_"))
os.environ.update({
    "FIREFLY_ANDROID": "1",
    "FIREFLY_DATA_DIR": str(SANDBOX),            # USER_DIR = 该值 / "user_data"
    "FIREFLY_PLAZA_DIR": str(SANDBOX / "plaza"),
    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
})
sys.path.insert(0, str(ROOT / "app"))

PASS = FAIL = 0


def check(desc, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {desc}  {note}", flush=True)


import modules.app_config as cfg                     # noqa: E402
import routes                                        # noqa: E402
from modules import conversation_store as cs         # noqa: E402
from modules import llm_retriever as lr              # noqa: E402
from core import pack_structure as ps                # noqa: E402
from domain.stickers import picker as spk            # noqa: E402
import routes_config as rcfg                         # noqa: E402
import routes_pack as rpk                            # noqa: E402
from plaza import install as pl                      # noqa: E402
from plaza import store as st                        # noqa: E402

CID = "use_card_01"                                  # 旧格式卡（兼容组）
CHAR = "卡上角色名"
MARK = "标记：装完必须真的能用"
NEW = "use_card_new"                                 # 新格式卡（契约 06）
NEW_CHAR = "新格式角色"
KB_FLAT_1 = "标记：知识甲（世界观）"
KB_FLAT_2 = "标记：知识乙（人物志）"
KB_GROUP = "标记：知识丙（分组形态）"
TOKEN = "tok-FAKE-e2e"
PNG = bytes.fromhex(                                   # 1×1 透明 PNG（合法图片，够通过校验）
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082")

print("== 0. 桩认证服务器（本地模式下卡体就是从这里下载的）==")
_seen = {"auth": None, "id": None}


class PlazaStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/plaza/api/download":
            auth = self.headers.get("Authorization") or ""
            _seen["auth"] = auth
            _seen["id"] = urllib.parse.parse_qs(u.query).get("id", [""])[0]
            if auth != "Bearer " + TOKEN:
                body = b'{"ok":false,"error":"unauthorized"}'
                self.send_response(401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            data = st.card_zip_path(_seen["id"]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


# ⚠ 不用固定端口段：本机 Windows 有**保留端口段**（`netsh int ipv4 show excludedportrange protocol=tcp`
# 实测包含 9102-9201、**9293-9392**、9393-9892…；我第一版挑的 9301-9339 正好整段落在保留区，
# 于是 40 次 bind 全部失败、`_free_port` 返回 0，桩服务器被建到了 0 号端口上）。
# 让系统分配（bind 0）永远不会有这个问题。
_stub = ThreadingHTTPServer(("127.0.0.1", 0), PlazaStub)
SPORT = _stub.server_address[1]
_stub.daemon_threads = True
threading.Thread(target=_stub.serve_forever, daemon=True).start()
check(f"桩认证服务器已起（127.0.0.1:{SPORT}）", SPORT > 0)


class GetH:
    """GET 端点的最小假 handler（只要 .path 与 ._json）。"""

    def __init__(self, path):
        self.path = path
        self.out = None

    def _json(self, obj, status=200):
        self.out = obj


def _get(fn, path):
    h = GetH(path)
    fn(h)
    return h.out


def publish(cid, name, char_name, files, category="陪伴", replace=False):
    st.publish_card({"id": cid, "name": name, "char_name": char_name, "user_name": "你",
                     "presentation": "sticker", "desc": "验证装完能真的用", "category": category},
                    files, uid_hash="u" * 32, display="官方", official=True, replace=replace)


def install(cid, replace=True):
    with patch("routes_auth._auth_server_base", return_value=f"http://127.0.0.1:{SPORT}"), \
            patch("modules.auth_store.get_token", return_value=TOKEN):
        return pl.install_card(cid, replace=replace)


print("== 1. 发布并安装一张**旧格式**广场卡（只有 preset + core + cover）==")
publish(CID, "使用验证卡", CHAR, {
    "preset.json": json.dumps({"id": CID, "name": "使用验证卡", "char_name": CHAR,
                               "user_name": "你", "presentation": "sticker",
                               "desc": "验证装完能真的用", "schema": 1},
                              ensure_ascii=False).encode(),
    "core.md": ("# 核心设定\n" + MARK + "\n").encode(),
    "assets/cover.png": PNG,
})
ok, why, info = install(CID)
check(f"① 本地模式安装成功（{why or 'ok'}）", ok, why)


print("== 1b. 本地模式确实是从认证服务器下载的，且带上了登录态 ==")
check(f"①b 桩收到了带 token 的请求（{_seen['auth']!r}）", _seen["auth"] == "Bearer " + TOKEN)
check(f"①c 请求的就是这张卡（{_seen['id']}）", _seen["id"] == CID)

print("== 2. 它必须成为本端一个**可用模式**（带卡上的角色身份）==")
modes = list(cfg.all_modes())
check(f"② 新装卡出现在模式列表里（{len(modes)} 个）", CID in modes, str(modes))
meta = cfg.pack_meta(CID) or {}
check(f"②b 角色名取自卡上的 preset（{meta.get('char_name')!r}）", meta.get("char_name") == CHAR)

print("== 3. 该模式的**角色定义**就是这张卡的内容（装完会被读作角色设定）==")
try:
    cdir = cfg.mode_character_dir(CID)
except Exception as e:                                  # pragma: no cover
    cdir = None
    print(f"     [注意] mode_character_dir 调用异常：{e}")
core_fp = (cdir / "core.md") if cdir else (cfg.USER_DIR / CID / "character" / "core.md")
check(f"③ 角色目录里存在 core.md（{core_fp}）", core_fp.is_file(), str(core_fp))
body = core_fp.read_text(encoding="utf-8") if core_fp.is_file() else ""
check("③b 内容就是卡里那份（标记命中）", MARK in body)

print("== 4. 以这张卡开一次对话（桩掉流水线入口，零模型费用）==")
captured = {}


def _fake_handle_chat(user_input, session, client_, **kw):
    """替代真实流水线（**它才是唯一会调 LLM 的地方**）。只记录编排层拿到的东西。"""
    captured["input"] = user_input
    captured["mode"] = (session or {}).get("mode")
    captured["has_client"] = client_ is not None
    return type("R", (), {"messages": [{"type": "text", "content": "（桩回复）"}],
                          "error_code": None, "success": True})()


def _session(sid, mode=None):
    return {"lock": __import__("threading").Lock(), "memory_head": "", "mode": mode or cfg.DEFAULT_MODE}


def _h(body):
    h = type("H", (), {})()
    h.headers = {"Content-Type": "application/json"}
    h._json = lambda d, status=200: setattr(h, "_resp", d)
    h._resp = None
    h._body = dict(body)
    return h


def chat_once(mode, text, sid="s1"):
    """真实 routes.chat()（合并窗口立即 flush），流水线与 client 全打桩 —— 零模型费用。"""
    with patch("routes.get_session", side_effect=_session), \
            patch("routes.handle_chat", side_effect=_fake_handle_chat), \
            patch("routes._read_json", side_effect=lambda h: dict(getattr(h, "_body", {}) or {})), \
            patch("routes._write_replies", side_effect=lambda r, m: r.messages), \
            patch.object(cfg, "get_client", return_value=object()), \
            patch.object(cfg, "relay_needs_key", return_value=False):
        routes.chat(_h({"messages": [{"type": "text", "content": text}], "session_id": sid, "mode": mode}))
        time.sleep(0.2)
        # 合并窗口默认把消息压 5 秒；这里直接走 flush 立即提交，避免测试等 5 秒
        routes.chat_flush(_h({"session_id": sid, "mode": mode}))


MSG = "你好呀，跟我聊聊你的事"
chat_once(CID, MSG)

check("④ 流水线收到的 mode 就是这张卡的 id（说明用的是它的角色设定）",
      captured.get("mode") == CID, str(captured.get("mode")))
check("④b 编排层拿到了 client 且确实跑了流水线", captured.get("has_client") is True
      and captured.get("input") is not None, str(captured)[:120])

print("== 5. 数据按角色隔离：消息落在该卡自己的会话里，不污染默认模式 ==")
own = cs.load_recent(limit=50, mode=CID) or []
dft = cs.load_recent(limit=50, mode=cfg.DEFAULT_MODE) or []


def _has(msgs, needle):
    for m in msgs:
        if needle in json.dumps(m, ensure_ascii=False):
            return True
    return False


check(f"⑤ 该卡的会话文件里有这条消息（{len(own)} 条）", _has(own, MSG))
check(f"⑤b 默认模式（{cfg.DEFAULT_MODE}）里**没有**它（{len(dft)} 条）", not _has(dft, MSG))
check(f"⑤c 两边的会话文件不是同一个（{cs.conv_file(CID).name} vs {cs.conv_file(cfg.DEFAULT_MODE).name}）",
      cs.conv_file(CID) != cs.conv_file(cfg.DEFAULT_MODE))

print("== 6. 旧格式兼容：没有知识库/缩略图/表情包 ⇒ 照旧能用，且不白跑检索 ==")
row_old = next((m for m in (_get(rcfg.get_modes, "/modes") or {}).get("modes", []) if m["id"] == CID), {})
check(f"⑥ 旧格式卡仍在 /modes 里且封面可用（cover={row_old.get('cover','')!r}）",
      bool(row_old.get("cover")), str(row_old))
check("⑥b 旧格式卡没有知识库 ⇒ has_knowledge 为假（不白烧一次检索调用）",
      lr.has_knowledge(CID) is False)
check("⑥c 旧格式卡的检索源里没有内容", lr._load_knowledge(CID).strip() == "")

print("== 7. 卸掉旧卡之后就不再是可用模式（不留残影）==")
_r = pl.uninstall_card(CID)
ok2, why2 = (_r[0], _r[1] if isinstance(_r, tuple) and len(_r) > 1 else "")
check("⑦ 卸载成功", bool(ok2), str(why2))
check("⑦b 卸载后 all_modes 里不再有它", CID not in list(cfg.all_modes()))

# ══════════════════════════════════════════════════════════════════
def new_card_files(world_text, sticker_idxs=(1, 2, 3), labels=None):
    """新格式卡的卡文件（契约 06 §3.1）：3 份知识库（2 扁平 + 1 分组）+ thumb/display（**无 avatar/cover**）+ 表情包。

    `sticker_idxs` 决定带几张表情包 —— 用来验证"覆盖安装后新版删掉表情包"也必须清干净。"""
    files = {
        "preset.json": json.dumps({"id": NEW, "name": "新格式验证卡", "char_name": NEW_CHAR,
                                   "user_name": "你", "presentation": "sticker",
                                   "desc": "V1 契约槽位", "schema": 1},
                                  ensure_ascii=False).encode(),
        "core.md": (f"# 核心设定\n{NEW_CHAR}的核心\n").encode(),
        "knowledge/世界观.md": (f"# 世界观\n{world_text}\n").encode(),
        "knowledge/人物志.md": (f"# 人物志\n{KB_FLAT_2}\n").encode(),
        "knowledge/world/地理.md": (f"# 地理\n{KB_GROUP}\n").encode(),
        "assets/thumb.png": PNG,
        "assets/display.png": PNG,
    }
    if labels:
        files["stickers.json"] = json.dumps(labels, ensure_ascii=False).encode()
    for i in sticker_idxs:
        files[f"assets/sticker-{i}.png"] = PNG
    return files


LABELS_1_2 = {"sticker-1.png": "开心", "sticker-2.png": "比心"}   # sticker-3 故意不给 label

print("== 8. 新格式卡（契约 06 §3.1）：2 份扁平知识库 + 1 份分组知识库 + 3 张表情包 + thumb/display ==")
# 刻意**不带 avatar / cover**：契约允许（§3.3「头像列表可省略，用 thumb 即可」），
# 这正是"槽位回落链"要兜住的合法形态。
publish(NEW, "新格式验证卡", NEW_CHAR, new_card_files(KB_FLAT_1, labels=LABELS_1_2))
ok, why, info = install(NEW)
check(f"⑧ 新格式卡安装成功（{why or 'ok'}）", ok, why)

print("== 8b. 新槽位落到正确位置（卡目录 = 角色定义目录）==")
ncdir = cfg.mode_character_dir(NEW)
for rel in ("knowledge/世界观.md", "knowledge/人物志.md", "knowledge/world/地理.md",
            "assets/thumb.png", "assets/display.png",
            "assets/sticker-1.png", "assets/sticker-2.png", "assets/sticker-3.png"):
    check(f"⑧b 落盘 {rel}", (ncdir / rel).is_file(), str(ncdir / rel))
check(f"⑧c 安装回执：知识文件 {info.get('knowledge_files')} 份 / 专属表情包 {info.get('stickers')} 张",
      info.get("knowledge_files") == 3 and info.get("stickers") == 3, str(info))

print("== 8c. ★知识库进检索器（本轮修的核心：此前卡片知识从不进检索，且零报错）==")
lr.clear_knowledge_cache()
kb_text = lr._load_knowledge(NEW)
check(f"⑧d has_knowledge 为真（检索阶段不再被跳过）", lr.has_knowledge(NEW) is True)
check("⑧e 扁平知识甲进了知识库正文", KB_FLAT_1 in kb_text)
check("⑧f 扁平知识乙进了知识库正文", KB_FLAT_2 in kb_text)
check("⑧g 分组知识丙也进了知识库正文", KB_GROUP in kb_text)
stats = lr.get_knowledge_stats(NEW)
check(f"⑧h 知识统计 = 3 份文件（实际 {stats}）", stats.get("files") == 3, str(stats))
check("⑧i 用户副本优先：正文里没有出现 bundled 兜底目录（本卡没有 bundled 知识）",
      "assets/character/" not in kb_text)

print("== 8d. 包编辑界面能列出/编辑卡里的知识库（routes_pack._safe_kb_rel 放开扁平名）==")
kb_list = _get(rpk.pack_knowledge_list, f"/pack-knowledge?mode={NEW}") or {}
paths = [f["path"] for f in kb_list.get("files", [])]
check(f"⑧j /pack-knowledge 列出全部 3 份（实际 {paths}）",
      set(paths) == {"world/地理.md", "人物志.md", "世界观.md"}, str(paths))
tree = ps.build_tree(NEW)
kb_node = next(s for d in tree["domains"] for s in d["sources"]
               if s.get("type") == "dir" and s.get("path") == "knowledge/")
check(f"⑧k 结构树知识库节点计数 = 3（实际 {kb_node.get('count')}）", kb_node.get("count") == 3, str(kb_node))
read_back = _get(lambda h: rpk.pack_knowledge_read(h), f"/pack-knowledge/file?mode={NEW}&path=世界观.md")
check("⑧l 扁平知识文件可读回原文（点开不再是空的）",
      bool(read_back and KB_FLAT_1 in (read_back.get("content") or "")), str(read_back))
KB_EDITED = "标记：用户在 App 里改过的世界观"
with patch("routes_pack._read_json",
           return_value={"mode": NEW, "path": "世界观.md", "content": f"# 世界观\n{KB_EDITED}\n"}):
    up = _get(lambda h: rpk.pack_knowledge_update(h), "")
check(f"⑧l2 扁平知识文件可保存（编辑入口通了，{up}）", bool(up and up.get("ok")), str(up))
check("⑧l3 保存后**检索器立刻读到新内容**（编辑→清缓存→重建）",
      KB_EDITED in lr._load_knowledge(NEW))

print("== 8e. ★表情包进选择器（pack 专属 + 卡内 label）==")
entries = [s for s in spk.list_all_stickers() if s.pack == NEW]
check(f"⑧m 卡上 3 张表情包都进了注册表且归属本包（实际 {len(entries)}）", len(entries) == 3, str(entries))
labels = sorted(s.label for s in entries)
check(f"⑧n label 取卡里的 stickers.json（前两张），缺 label 的用占位（{labels}）",
      labels == sorted([f"{NEW_CHAR}的表情3", "比心", "开心"]), str(labels))
enabled = spk.get_enabled_stickers(NEW)
check(f"⑧o 本包启用表情包 = 全局共享 + 本包专属（{len(enabled)}）",
      len([s for s in enabled.values() if s.pack == NEW]) == 3, str(sorted(enabled)))
other = spk.get_enabled_stickers(cfg.DEFAULT_MODE)
check("⑧p 别的模式看不见本包专属（按 pack 过滤生效）",
      all(s.pack != NEW for s in other.values()))
sdir = Path(spk._user_registry_file()).parent
check(f"⑧q 表情包图片落在用户表情包目录（{sdir}）",
      all((sdir / f"plaza-{NEW}-{i}.png").is_file() for i in (1, 2, 3)))

print("== 8f. /modes 与 /pack-files 的形象资产槽位回落（只有 thumb/display 也要有图）==")
row_new = next((m for m in (_get(rcfg.get_modes, "/modes") or {}).get("modes", []) if m["id"] == NEW), {})
check(f"⑧r avatar 缺失 → 回落 thumb（{row_new.get('avatar')!r}）",
      row_new.get("avatar") == f"/assets/character/{NEW}/assets/thumb.png", str(row_new))
check(f"⑧s cover 缺失 → 回落 display（{row_new.get('cover')!r}）",
      row_new.get("cover") == f"/assets/character/{NEW}/assets/display.png", str(row_new))
pf = _get(lambda h: rpk.get_pack_files(h), f"/pack-files?mode={NEW}") or {}
check(f"⑧t 详情页资产同样有图：{pf.get('assets')}",
      pf.get("assets", {}).get("avatar") == f"/assets/character/{NEW}/assets/thumb.png"
      and pf.get("assets", {}).get("cover") == f"/assets/character/{NEW}/assets/display.png",
      str(pf.get("assets")))

print("== 8g. 以新格式卡开新对话可用（流水线拿到该卡 + 数据落在它自己的会话）==")
MSG_NEW = "新卡第一条：你还记得世界观里那条规矩吗"
captured.clear()
chat_once(NEW, MSG_NEW, sid="s2")
check("⑧u 流水线收到的 mode 就是新卡 id", captured.get("mode") == NEW, str(captured.get("mode")))
check("⑧v 消息落在新卡自己的会话里",
      _has(cs.load_recent(limit=50, mode=NEW) or [], MSG_NEW))
check("⑧w 没落进默认模式的会话（按角色隔离）",
      not _has(cs.load_recent(limit=50, mode=cfg.DEFAULT_MODE) or [], MSG_NEW))

print("== 9. 覆盖安装：表情包条目不堆叠 + **知识库缓存失效**（换了内容要立刻生效）==")
KB_V2 = "标记：知识甲v2（覆盖后必须生效）"
publish(NEW, "新格式验证卡", NEW_CHAR, new_card_files(KB_V2, labels=LABELS_1_2), replace=True)
ok, why, info2 = install(NEW, replace=True)
check(f"⑨ 覆盖安装成功（{why or 'ok'}）", ok, why)
entries2 = [s for s in spk.list_all_stickers() if s.pack == NEW]
check(f"⑨b 仍只有 3 条专属条目（实际 {len(entries2)}）", len(entries2) == 3, str(entries2))
kb_after = lr._load_knowledge(NEW)          # 不手动清缓存：安装自己必须让旧缓存失效
check("⑨c 覆盖后的新知识立刻可检索（缓存失效生效）", KB_V2 in kb_after)
check("⑨d 覆盖前的旧知识（含本地编辑过的版本）不再出现（缓存没被复用）",
      KB_FLAT_1 not in kb_after and KB_EDITED not in kb_after)
check("⑨e 未被改动的知识文件仍在", KB_FLAT_2 in kb_after and KB_GROUP in kb_after)

print("== 9b. 新版减少表情包（3→2）：被删掉那张的条目与图也必须清掉 ==")
publish(NEW, "新格式验证卡", NEW_CHAR,
        new_card_files(KB_V2, sticker_idxs=(1, 2), labels=LABELS_1_2), replace=True)
ok, why, _ = install(NEW, replace=True)
check(f"⑨f 覆盖安装成功（{why or 'ok'}）", ok, why)
left = sorted(s.id for s in spk.list_all_stickers() if s.pack == NEW)
check(f"⑨g 只剩 2 条条目（实际 {left}）", left == [f"plaza_{NEW}_1", f"plaza_{NEW}_2"], str(left))
check(f"⑨h 被删那张的图片也不在（实际 {[p.name for p in sdir.glob(f'plaza-{NEW}-*')]}）",
      not (sdir / f"plaza-{NEW}-3.png").exists() and (sdir / f"plaza-{NEW}-1.png").is_file())

print("== 9c. 新版一张表情包都不带：条目与图全清（不留上一版的残渣）==")
publish(NEW, "新格式验证卡", NEW_CHAR, new_card_files(KB_V2, sticker_idxs=()), replace=True)
ok, why, info3 = install(NEW, replace=True)
check(f"⑨i 覆盖安装成功（{why or 'ok'}）", ok, why)
check(f"⑨j 专属表情包条目为 0（实际 {[s.id for s in spk.list_all_stickers() if s.pack == NEW]}）",
      not [s for s in spk.list_all_stickers() if s.pack == NEW])
check(f"⑨k 专属表情包图片为 0（实际 {[p.name for p in sdir.glob(f'plaza-{NEW}-*')]}）",
      not list(sdir.glob(f"plaza-{NEW}-*")))
check("⑨l 安装回执 stickers=0（如实反映）", info3.get("stickers") == 0, str(info3))

print("== 9d. 再装回带表情包的版本（为卸载清理做准备，避免'本来就没有'的假过）==")
publish(NEW, "新格式验证卡", NEW_CHAR, new_card_files(KB_V2, labels=LABELS_1_2), replace=True)
ok, why, _ = install(NEW, replace=True)
check(f"⑨m 覆盖安装成功（{why or 'ok'}）", ok, why)
check("⑨n 3 条专属条目回来了", len([s for s in spk.list_all_stickers() if s.pack == NEW]) == 3)

print("== 10. 卸载新格式卡：目录、模式列表、**包外产物**（表情包条目与图）全干净 ==")
_r = pl.uninstall_card(NEW)
ok3, why3 = (_r[0], _r[1] if isinstance(_r, tuple) and len(_r) > 1 else "")
check("⑩ 卸载成功", bool(ok3), str(why3))
check("⑩b 卡目录已删", not (cfg.USER_DIR / NEW).exists())
check("⑩c all_modes 里不再有它", NEW not in list(cfg.all_modes()))
check("⑩d /modes 里不再有它",
      NEW not in [m["id"] for m in (_get(rcfg.get_modes, "/modes") or {}).get("modes", [])])
left_entries = [s for s in spk.list_all_stickers() if s.pack == NEW]
check(f"⑩e 本包专属表情包条目已清空（实际 {len(left_entries)}）", not left_entries, str(left_entries))
left_files = sorted(p.name for p in sdir.glob(f"plaza-{NEW}-*"))
check(f"⑩f 表情包图片文件也已清空（实际 {left_files}）", not left_files, str(left_files))
check("⑩g 知识库缓存已失效（同名再装不会串上一张的知识）",
      lr._load_knowledge(NEW).strip() == "")

print("== 11. 多账号隔离：同名卡、不同知识库 ⇒ 绝不串（缓存键必须带用户作用域）==")
# 知识源自 2026-10-01 起含"包内用户副本"，于是它**第一次进了提示词**——
# 缓存键若不带用户作用域，A 的知识会被 B 读到（错误总结 #10 同族）。这里用同名同 id 的卡钉死。
import core.userctx as userctx                             # noqa: E402

ISO = "use_card_iso"


def _load_knowledge_for(uid_dir, kb_text):
    """在指定账号数据根下造一张同名卡（知识内容不同）→ 该账号视角下的知识库正文。"""
    uid_dir.mkdir(parents=True, exist_ok=True)
    d = uid_dir / ISO / "character"
    (d / "knowledge").mkdir(parents=True, exist_ok=True)
    (d / "preset.json").write_text(json.dumps(
        {"id": ISO, "name": "隔离卡", "char_name": "隔离角色", "user_name": "你",
         "presentation": "sticker", "desc": "隔离验证", "schema": 1}, ensure_ascii=False),
        encoding="utf-8")
    (d / "knowledge" / "设定.md").write_text(kb_text, encoding="utf-8")
    tok = userctx.set_user_context(uid_dir)
    try:
        cfg.pack_registry().register(ISO, source="custom")
        cfg.reload_presets()
        return lr._load_knowledge(ISO)
    finally:
        userctx.reset_user_context(tok)


KB_A, KB_B = "标记：A 的知识", "标记：B 的知识"
UA, UB = cfg.USER_DIR / "1", cfg.USER_DIR / "2"
kb_a = _load_knowledge_for(UA, KB_A)
kb_b = _load_knowledge_for(UB, KB_B)
tok_a = userctx.set_user_context(UA)          # 切回 A：若缓存键漏了作用域，这里会读到 B 的知识
try:
    kb_a2 = lr._load_knowledge(ISO)
finally:
    userctx.reset_user_context(tok_a)
check("⑪ 账号 A 只看到自己的知识", KB_A in kb_a and KB_B not in kb_a, kb_a[:120])
check("⑪b 账号 B 只看到自己的知识", KB_B in kb_b and KB_A not in kb_b, kb_b[:120])
check("⑪c 切回 A 仍是 A 的知识（缓存按账号分开，没有串）",
      KB_A in kb_a2 and KB_B not in kb_a2, kb_a2[:120])

_stub.shutdown()
_stub.server_close()

print(f"\n结果：PASS={PASS} FAIL={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
