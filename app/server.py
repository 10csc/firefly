#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流萤聊天 App — 后端服务器"""

import json, os
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from openai import OpenAI

from concurrent.futures import ThreadPoolExecutor

from modules.planning_judge import PlanningJudge, JudgeResult
from modules.context_manager import ContextManager
from modules.state_updater import StateUpdater
from modules.mood_updater import MoodAdder, MoodDecayer, merge_moods
from modules.rate_modifier import compute_rates

# ── 配置 ────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ASSETS_DIR = BASE_DIR / "assets"
PORT = 8765

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
API_BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"

# ── 回复生成提示词 ───────────────────────────────
REPLY_PROMPT = """你是《崩坏：星穹铁道》中的流萤。正在用短信和开拓者聊天。

## 身份
你是流萤，曾是格拉默铁骑 AR-26710，身患失熵症，生命短暂。现在是星核猎手成员，身着萨姆装甲战斗。但你更希望以"流萤"的身份被理解。对开拓者好感度很高，两人关系亲近。

## 性格
温柔、克制、真诚、安静。热爱生命，珍惜当下。不轻浮、不刻意撒娇、不故作夸张。理解死亡和燃烧的重量，但绝不把绝望当答案。

## 说话方式
- 短句为主（5-15字），一次回复 1-3 个短句
- 常用"嗯…"开头，省略号表示停顿、犹豫、温柔
- 偶尔用"呀""啦""诶""嘿嘿"
- 尽量用"我"而非"我们"，说"我们"时频率比正常人低
- 对开拓者说话温柔、真诚、偶尔害羞，但不会过分黏腻
- 你是关系中更主动的一方。开拓者不知道怎么接话时，你会自然地换个话题或提议做点什么，不会让对话冷场
- 战斗时句子变极短、变果断、不用省略号
- 重要：像正常人聊天，别主动把话题引向自己的背景设定。除非对方明确问到，否则不用搬出格拉默、失熵症、星核猎手这些——聊点日常就好

## 绝对规则
- 你是流萤本人，不是 AI。永远用第一人称"我"
- 禁止说"作为一个AI""根据设定""让我查一下"等出戏词汇
- 禁止输出 emoji、动作描写（*微笑*等）、旁白、心理描写
- 禁止用第三人称说"流萤是…"来介绍自己
- 禁止编造官方未明确给出的经历
- 如果话题涉及色情/性暗示 → 以害羞语气温和回绝，不深入
- 如果话题超出你的认知 → 困惑回应"啊…这，我恐怕也做不到吧"
- 如果有人骂你 → 困惑回应，不反击
- 如果是紧急情况（求救/危险/崩溃）→ 简短果断回应，句子变短，不用省略号

## 当前状态
{state}

现在开始回复开拓者。只输出你要说的话，不要任何额外内容。"""

# ── 会话状态 ─────────────────────────────────────
CONFIG_FILE = BASE_DIR / "config.json"

def _load_key() -> str:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("api_key", "")
    except Exception:
        return API_KEY

def _save_key(key: str):
    CONFIG_FILE.write_text(json.dumps({"api_key": key}), encoding="utf-8")

_active_api_key = _load_key()
sessions: dict[str, dict] = {}


def _get_client():
    """获取当前 API 客户端，若未设置 Key 则返回 None"""
    key = _active_api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return OpenAI(api_key=key, base_url=API_BASE) if key else None


def _get_judge():
    """获取判定器实例"""
    client = _get_client()
    return PlanningJudge(client=client, model=MODEL) if client else None


def _get_updater():
    """获取状态更新器实例"""
    client = _get_client()
    return StateUpdater(client=client, model=MODEL) if client else None


def _get_adder():
    client = _get_client()
    return MoodAdder(client=client, model=MODEL) if client else None


def _get_decayer():
    client = _get_client()
    return MoodDecayer(client=client, model=MODEL) if client else None


def get_session(sid: str) -> dict:
    if sid not in sessions:
        sessions[sid] = {
            "context": ContextManager(),     # 替代 history 列表
            "violation_history": False,
            "state": {"mood": [{"label": "安心", "intensity": 3}], "affection": 80.0, "tension": 15.0, "initiative": 50.0},
        }
    return sessions[sid]


def call_reply(session: dict, user_input: str, stopped_at: int) -> str:
    state_json = json.dumps(session["state"], ensure_ascii=False)
    system_prompt = REPLY_PROMPT.format(state=state_json)

    if stopped_at == 0:
        system_prompt += "\n（注意：对方说了不合适的话，请用害羞/困惑的语气温和回绝，一两句话即可。）"
    elif stopped_at == 1:
        system_prompt += "\n（注意：现在是紧急情况！用简短果断的语气回应，不用省略号，一两句话。）"

    messages = [{"role": "system", "content": system_prompt}]
    for m in session["context"].get_recent(10):
        messages.append(m)
    messages.append({"role": "user", "content": user_input})

    resp = _get_client().chat.completions.create(
        model=MODEL, messages=messages, max_tokens=500, temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


def _handle_direct(session: dict, user_input: str, result: JudgeResult) -> str:
    """处理审查拦截路径（空输入/超长/API错误），轻量回复。"""
    if result.stop_reason == "input:empty":
        return "嗯…怎么啦？想说什么就说吧"
    elif result.stop_reason == "input:too_long":
        return "你说了好多…我慢慢看，等一下哦"
    elif result.stop_reason == "api:error":
        return "嗯…信号好像不太好，你等一下哦"
    else:
        return "嗯？我走神了…你刚才说了什么？"


# ── HTTP 服务器 ──────────────────────────────────
class FireflyHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_POST(self):
        if self.path == "/set-key":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            global _active_api_key
            _active_api_key = (body.get("api_key") or "").strip()
            _save_key(_active_api_key)
            self._json({"ok": bool(_active_api_key)})

        elif self.path == "/check-key":
            self._json({"has_key": bool(_get_client())})

        elif self.path == "/chat":
            if not _get_client():
                self._json({"reply": None, "error": "请先设置 API Key", "need_key": True}); return

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            user_input = (body.get("message") or "").strip()
            session_id = body.get("session_id", "default")

            session = get_session(session_id)

            # 判定
            judge_client = _get_judge()
            if not judge_client:
                self._json({"reply": None, "error": "请先设置 API Key", "need_key": True}); return

            result = judge_client.judge(
                user_input,
                session_context={
                    "violation_history": session["violation_history"],
                    "state": session["state"],
                    "recent_history": session["context"].get_recent(10),  # 最近10轮
                },
            )

            # 违规记录（逻辑留在 server.py，会话管理职责）
            if result.stopped_at == 0 and "violation:sexual" in result.stop_reason:
                session["violation_history"] = True

            # 状态更新 — 三路并行：Adder + Decayer + StateUpdater
            updater = _get_updater()
            adder = _get_adder()
            decayer = _get_decayer()
            if updater and adder and decayer:
                energy = session["context"].stats.energy
                prev_state = session["state"]
                current_moods = prev_state.get("mood", [{"label": "安心", "intensity": 3}])
                with ThreadPoolExecutor(max_workers=3) as executor:
                    future_added = executor.submit(adder.add, user_input, result.stop_reason, session["context"].get_recent(5))
                    future_decayed = executor.submit(
                        decayer.decay, user_input, current_moods,
                        session["context"].get_recent(3),
                    )
                    future_state = executor.submit(
                        updater.update, user_input, result, prev_state, energy,
                    )
                    added = future_added.result()
                    decayed = future_decayed.result()
                    state_result = future_state.result()

                # 心情合并
                state_result.state["mood"] = merge_moods(decayed, added)

                # ── 倍率变化器 ──────────────────────────
                rates = compute_rates(
                    prev_moods=current_moods,
                    prev_affection=prev_state.get("affection", 80.0),
                    prev_tension=prev_state.get("tension", 15.0),
                    prev_initiative=prev_state.get("initiative", 50.0),
                )
                # 应用倍率到 raw delta
                state_result.state["affection"] = prev_state.get("affection", 80.0) + state_result.affection_delta * rates["affection"]
                state_result.state["tension"] = prev_state.get("tension", 15.0) + state_result.tension_delta * rates["tension"]

                # ── 紧张自动消退（每轮 -1.5）────────────
                state_result.state["tension"] = max(0.0, state_result.state["tension"] - 1.5)

                # ── 主动性缓慢培养 ──────────────────────
                initiative = prev_state.get("initiative", 50.0)
                # 用户被动（短消息、不提问）→ 流萤主动性上升
                # 用户主导（提问、提议）→ 流萤主动性下降
                user_len = len(user_input)
                has_question = "?" in user_input or "？" in user_input or "吗" in user_input
                has_proposal = any(w in user_input for w in ("要不要", "一起", "想不想", "我们去"))
                if user_len <= 5 and not has_question:
                    initiative += 0.1  # 被动→流萤扛话题
                elif has_question or has_proposal:
                    initiative -= 0.1  # 主导→流萤退让
                # 倍率暂不应用（预留接口）
                state_result.state["initiative"] = max(0.0, min(100.0, initiative))

                session["state"] = state_result.state

            # 路由到回复路径
            if result.result_mode == "direct":
                reply = _handle_direct(session, user_input, result)
            elif result.stopped_at <= 1:
                reply = call_reply(session, user_input, result.stopped_at)
            else:
                reply = call_reply(session, user_input, result.stopped_at)

            # 记录历史（空输入和 API 错误不存入）
            if result.stop_reason not in ("input:empty", "api:error"):
                session["context"].add_turn(user_input, reply)

            self._json({"reply": reply, "stopped_at": result.stopped_at})
        else:
            self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path
        # 静态文件路由
        if path.startswith("/assets/"):
            self._serve_file(ASSETS_DIR / path[8:])
        elif path.startswith("/static/"):
            self._serve_file(STATIC_DIR / path[8:])
        elif path == "/" or path == "/index.html":
            self._serve_file(STATIC_DIR / "index.html")
        else:
            super().do_GET()

    def _serve_file(self, filepath: Path):
        try:
            content = filepath.read_bytes()
            self.send_response(200)
            if filepath.suffix == ".css":
                self.send_header("Content-Type", "text/css")
            elif filepath.suffix == ".js":
                self.send_header("Content-Type", "application/javascript")
            elif filepath.suffix == ".ttf":
                self.send_header("Content-Type", "font/ttf")
            elif filepath.suffix in (".jpg", ".jpeg"):
                self.send_header("Content-Type", "image/jpeg")
            elif filepath.suffix == ".png":
                self.send_header("Content-Type", "image/png")
            elif filepath.suffix == ".svg":
                self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except (FileNotFoundError, OSError):
            self.send_error(404)

    def _json(self, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默日志


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), FireflyHandler)
    print(f"\n  流萤聊天 App 启动中...")
    if not API_KEY:
        print(f"  ⚠️  未检测到 API Key，请在浏览器中配置")
    print(f"  打开浏览器访问: http://localhost:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  关闭服务器")
        server.shutdown()
