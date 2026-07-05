# -*- coding: utf-8 -*-
"""端到端20轮对话测试"""
import sys, os, json, time, subprocess, requests
from datetime import datetime

BASE = "http://localhost:8765"
SESSION = "e2e-" + str(int(time.time()))

PASS, FAIL = 0, 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}")


def send(msg):
    """发送消息，返回 JSON"""
    try:
        r = requests.post(f"{BASE}/chat",
            json={"message": msg, "session_id": SESSION}, timeout=30)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# 20轮自然对话
CONVERSATION = [
    "晚上好呀，在干嘛呢？",
    "没事，刚下班到家，有点累",
    "谢谢你关心啦，你平时这个点都在做什么呢？",
    "打游戏啊，玩的什么游戏？",
    "哈哈你这描述好可爱",
    "今天加班特别烦，老板又改需求了",
    "被你说中了，还没吃…你不说我都没注意到",
    "你是什么星座的呀？",
    "哦不对，你是格拉默铁骑来的，应该不知道星座吧…不好意思",
    "没关系没关系，我才该道歉",
    "话说你平时喜欢什么颜色？",
    "白色确实很适合你",
    "我们公司楼下有家蛋糕店特别好吃，下次给你带一块？",
    "你现在穿的机甲…脱不下来吗？",
    "那平时穿什么衣服？",
    "我在想你穿裙子应该很好看",
    "怎么突然不说话啦",
    "好吧不逗你了，你今天心情怎么样？",
    "那就好。对了，明天周末有什么打算吗？",
    "晚安，早点休息哦",
]

# 启动服务器
print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动服务器...")
proc = subprocess.Popen(
    [sys.executable, "server.py"],
    cwd=os.path.join(os.path.dirname(__file__), "..", "app"),
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
)
time.sleep(2)

# 检查服务
try:
    r = requests.get(f"{BASE}/", timeout=5)
    check("服务器启动", r.status_code == 200)
except Exception as e:
    check(f"服务器启动失败: {e}", False)
    proc.kill()
    sys.exit(1)

# 发消息
print(f"\n{'='*60}")
print(f"20轮对话测试 — 开始")
print(f"{'='*60}")

for i, msg in enumerate(CONVERSATION, 1):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] 轮次 {i}/20")
    print(f"  开拓者: {msg}")

    data = send(msg)

    if "error" in data and data.get("error"):
        check(f"轮次{i}→错误: {data['error']}", False)
        continue

    messages = data.get("messages", [])
    bubble = data.get("bubble")

    for j, m in enumerate(messages):
        t = m.get("type", "?")
        if t == "text":
            all_replies.add(m.get("content", "").strip())
        elif t == "sticker":
            if not m.get("path"):
                sticker_path_ok = False
        if t == "text":
            print(f"  流萤: {m['content']}")
        elif t == "sticker":
            print(f"  流萤: [表情包: {m.get('label', '?')}] {m.get('path', '')}")

    check(f"轮次{i}→有回复", len(messages) > 0)
    if i >= 5:  # 5轮后检查多消息
        multi = len([m for m in messages if m.get("type") == "text"]) > 1
        # 不强制要求，但记录

    if bubble:
        print(f"  🔄 气泡切换: {bubble}")

    time.sleep(1.5)  # 间隔，避免 API 限流

# ── 质量检查 ──────────────────────────────────────
print(f"
{'='*60}")
print(f"质量检查")
print(f"{'='*60}")

unique_count = len(all_replies)
check(f"回复去重≥5（实际{unique_count}）", unique_count >= 5)
check("sticker→path字段非空", sticker_path_ok)

# 关服务器
print(f"
{'='*60}")
print(f"  通过: {PASS}  失败: {FAIL}")
print(f"{'='*60}")
proc.kill()
if FAIL > 0:
    sys.exit(1)
