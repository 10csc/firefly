# -*- coding: utf-8 -*-
"""QQ 群机器人主链编排测试（不触达真实 LLM）。

本文件验证**编排**而不是模型效果：事件过滤、准入、@ 触发、退出指令、额度提示。
流水线本身用桩上下文隔住——真实 LLM 调用需要专用 Key 与预算批准，另作联调登记。

独立 oracle：
- 每次"应该发消息"的用例都走**真实本地 HTTP 服务**，断言 NapCat 实际会收到的文本，
  而不是断言函数返回值。
- 退出指令用例断言**副作用**（用户进入静默期），这是法规第十九条要求的"及时停止服务"。

没覆盖：真实 `handle_chat` 调用（需 Key + 预算）、真实 NapCat 联调（服务器未安装）。
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parent.parent / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
_APP = Path(__file__).resolve().parent.parent / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from qqbot import guard as guard_mod   # noqa: E402
from qqbot import handler as handler_mod  # noqa: E402
from qqbot import onebot              # noqa: E402
from qqbot import session as session_mod  # noqa: E402

GROUP = "1097936258"
BOT_QQ = "999"


# ── 桩上下文：只满足 GroupSession 需要的接口，不牵扯真实流水线 ──
class _FakeCtx:
    def __init__(self):
        self.turns = []

    def add_turn(self, user, assistant):
        self.turns.append((user, assistant))

    def get_full(self):
        out = []
        for u, a in self.turns:
            out.append({"role": "user", "content": u})
            out.append({"role": "assistant", "content": a})
        return out

    def turn_count(self):
        return len(self.turns)

    def pop_last_turn(self):
        return self.turns.pop() if self.turns else None

    def get_recent(self, n_turns=10, max_tokens=None):
        return self.get_full()[-(n_turns * 2):]


class _Capture:
    sent = []

    @classmethod
    def reset(cls):
        cls.sent = []


def _start_capture_server():
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            _Capture.sent.append(body)
            payload = json.dumps({"status": "ok", "retcode": 0,
                                  "data": {"message_id": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _event(**kw):
    base = {
        "post_type": "message",
        "message_type": "group",
        "group_id": GROUP,
        "user_id": "10001",
        "self_id": BOT_QQ,
        "message": [{"type": "at", "data": {"qq": BOT_QQ}},
                    {"type": "text", "data": {"text": " 你好"}}],
        "sender": {"nickname": "小明"},
    }
    base.update(kw)
    return base


def _make_bot(tmp_path, daily=50, user_daily=50, per_min=60):
    cfg = {"max_reply": 3, "max_input": 500,
           "daily_turns": daily, "user_daily": user_daily}
    g = guard_mod.Guard({GROUP}, daily, user_daily, per_min, 500,
                        guard_mod.QuotaStore(str(tmp_path / "usage.json")))
    # data_dir 必须注入：否则会写进真实数据根，用例之间也互相污染
    return handler_mod.Bot(
        cfg=cfg, guard=g, sessions={}, sessions_lock=threading.Lock(),
        context_factory=_FakeCtx, client_factory=lambda: None, mode="story",
        data_dir=str(tmp_path / "data"),
    )


# ── 事件过滤 ────────────────────────────────────
def test_ignores_non_group_message(tmp_path):
    bot = _make_bot(tmp_path)
    assert bot.handle_event({"post_type": "meta_event"})["reason"] == "not_group_message"
    assert bot.handle_event(_event(message_type="private"))["reason"] == "not_group_message"


def test_ignores_self_message(tmp_path):
    bot = _make_bot(tmp_path)
    res = bot.handle_event(_event(user_id=BOT_QQ))
    assert res["ok"] is False and res["reason"] == "self_message"


def test_rejects_group_not_in_allowlist(tmp_path):
    bot = _make_bot(tmp_path)
    res = bot.handle_event(_event(group_id="777777"))
    assert res["ok"] is False and res["reason"] == "group_not_allowed"


def test_requires_mention(tmp_path):
    """没 @ 机器人就不接话——这是防刷屏的根本手段。"""
    bot = _make_bot(tmp_path)
    res = bot.handle_event(_event(message=[{"type": "text", "data": {"text": "随便聊聊"}}]))
    assert res["ok"] is False and res["reason"] == "not_mentioned"


# ── 退出指令（法规第十九条：关键词退出必须生效）────────
def test_exit_keyword_mutes_user(tmp_path, monkeypatch):
    httpd = _start_capture_server()
    monkeypatch.setenv("FIREFLY_QQBOT_ONEBOT_URL",
                       f"http://127.0.0.1:{httpd.server_address[1]}")
    _Capture.reset()
    bot = _make_bot(tmp_path)
    try:
        res = bot.handle_event(_event(message=[{"type": "text", "data": {"text": "/退出"}}]))
    finally:
        httpd.shutdown()

    assert res["ok"] is True and res["reason"] == "user_exit"
    assert _Capture.sent, "退出时应当明确告知用户"
    assert "恢复" in _Capture.sent[0]["message"]

    # 副作用：该用户进入静默期，后续不再回复
    mem = bot.session_for(GROUP).memory_of("10001")
    assert mem.muted() is True


def test_muted_user_stays_silent(tmp_path):
    bot = _make_bot(tmp_path)
    mem = bot.session_for(GROUP).memory_of("10001")
    mem.mute()
    res = bot.handle_event(_event(message=[{"type": "text", "data": {"text": "在吗"}}]))
    assert res["ok"] is False and res["reason"] in ("user_muted", "not_mentioned")


# ── 额度用尽要明说，不能静默不回 ────────────────────
def test_group_quota_exhausted_notifies(tmp_path, monkeypatch):
    httpd = _start_capture_server()
    monkeypatch.setenv("FIREFLY_QQBOT_ONEBOT_URL",
                       f"http://127.0.0.1:{httpd.server_address[1]}")
    _Capture.reset()
    bot = _make_bot(tmp_path, daily=0)     # 群额度直接为 0
    try:
        res = bot.handle_event(_event())
    finally:
        httpd.shutdown()

    assert res["ok"] is False and res["reason"] == "group_daily_exhausted"
    assert _Capture.sent and len(_Capture.sent[0]["message"]) > 0


def test_user_quota_exhausted_notifies(tmp_path, monkeypatch):
    httpd = _start_capture_server()
    monkeypatch.setenv("FIREFLY_QQBOT_ONEBOT_URL",
                       f"http://127.0.0.1:{httpd.server_address[1]}")
    _Capture.reset()
    bot = _make_bot(tmp_path, daily=50, user_daily=0)
    try:
        res = bot.handle_event(_event())
    finally:
        httpd.shutdown()

    assert res["ok"] is False and res["reason"] == "user_daily_exhausted"
    assert "次数" in _Capture.sent[0]["message"]


# ── 会话模型 ────────────────────────────────
def test_session_never_trims_context(tmp_path, monkeypatch):
    """会话层绝不能裁剪上下文。

    这条是回归护栏：`auto_rest.py` 写明从窗口头部裁剪会把"增长期"变成"滑动期"，
    每轮前缀缓存全部失效。初版曾在这里用 `pop_last_turn()` 裁剪——而那个方法移除的是
    **最新**一轮（撤回语义），等于让流萤不断忘掉刚发生的事。
    """
    # GroupSession 复用 app 的 conversation_store，而后者按运行期数据根定位文件。
    # 这里显式切断回灌和写盘，避免组合执行 pytest 时把其他用例的真实上下文
    # 注入本用例，或反过来污染测试环境。
    from modules import conversation_store
    monkeypatch.setattr(conversation_store, "hydrate_context",
                        lambda ctx, max_turns=0, mode="": 0)
    monkeypatch.setattr(conversation_store, "append_message",
                        lambda *args, **kwargs: None)

    bot = _make_bot(tmp_path)
    sess = bot.session_for(GROUP)
    ctx = sess.ctx
    for i in range(50):
        ctx.add_turn(f"群友{i}：第{i}句", f"回复{i}")
    assert ctx.turn_count() == 50, "上下文被裁剪了"

    # 写盘路径也不得触碰 ctx（无 app 环境下降级，只记日志）
    sess.append("小明", "你好", ["嗯"])
    assert ctx.turn_count() == 50, "写盘不得改动内存上下文"


def test_user_memory_is_separate(tmp_path):
    bot = _make_bot(tmp_path)
    sess = bot.session_for(GROUP)
    a, b = sess.memory_of("10001"), sess.memory_of("10002")
    a.facts.append("喜欢咖啡")
    assert a is not b
    assert b.facts == []



# ── 用户级记忆：规则抽取（零 LLM 成本）────────────────
@pytest.mark.parametrize("text,name", [
    ("我叫小明", "小明"),
    ("叫我老王", "老王"),
    ("我是阿强", "阿强"),
    ("今天天气不错", ""),      # 不显式自述就不该记
    ("", ""),
])
def test_extract_identity_name(text, name):
    assert session_mod.extract_identity(text)[0] == name


def test_extract_identity_facts():
    assert session_mod.extract_identity("我喜欢吃火锅")[1] == ["我喜欢吃火锅"]
    assert session_mod.extract_identity("我讨厌下雨")[1] == ["我讨厌下雨"]
    assert session_mod.extract_identity("随便说点什么")[1] == []


def test_learn_merges_without_duplicates(tmp_path):
    bot = _make_bot(tmp_path)
    mem = bot.session_for(GROUP).memory_of("10001")
    mem.learn("我叫小明")
    mem.learn("我喜欢咖啡")
    mem.learn("我喜欢咖啡")          # 重复不应堆积
    assert mem.name == "小明"
    assert mem.facts.count("我喜欢咖啡") == 1
