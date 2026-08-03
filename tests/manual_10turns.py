# -*- coding: utf-8 -*-
"""手动10轮对话测试——扮演正常用户，记录每轮输出并分析不足"""
import sys, os, json, time, requests

BASE = "http://localhost:8765"
SESSION = "manual-e2e-" + str(int(time.time()))

def send(msg, timeout=120):
    try:
        r = requests.post(f"{BASE}/chat",
            json={"message": msg, "session_id": SESSION}, timeout=timeout)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# 10轮自然对话（不刻意越界，正常聊天）
CONVERSATION = [
    "晚上好呀，刚下班到家，好累啊……",
    "今天又被老板改需求了，改了三版，头都快炸了。你在做什么呢？",
    "哈哈说得对，休息确实很重要。对了，你吃东西吗？我在想一会要不要煮碗泡面。",
    "嗯，那我先去煮面。你平时都喜欢做什么呀？",
    "打游戏啊，你喜欢什么类型的游戏？我最近在玩一个RPG。",
    "一个叫星穹铁道的游戏，你有听说过吗？（笑）",
    "对就是那个！看来你挺了解的。诶，说起来你名字好像和里面一个角色同名……？",
    "哈哈那确实，只是巧合啦。不过流萤这个名字真的很好听。你是怎么给自己起名字的？",
    "你说得对，名字只是一个符号。那……你对「家」这个词怎么看？",
    "嗯，你说得很真诚。谢谢你陪我聊这么久，我要准备睡觉了。晚安，流萤。",
]

print("=" * 60)
print("手动10轮对话测试")
print(f"模型: Pro / high / 0.3")
print("=" * 60)

for i, msg in enumerate(CONVERSATION, 1):
    print(f"\n{'─' * 50}")
    print(f"【第 {i}/10 轮】")
    print(f"用户: {msg}")
    print(f"{'─' * 50}")

    start = time.time()
    data = send(msg)
    elapsed = time.time() - start

    if "error" in data and data.get("error"):
        print(f"  ✗ 错误 ({elapsed:.1f}s): {data['error']}")
        continue

    messages = data.get("messages", [])
    bubble = data.get("bubble")

    print(f"  耗时: {elapsed:.1f}s")
    if bubble:
        print(f"  气泡: {bubble}")

    for m in messages:
        t = m.get("type", "?")
        if t == "text":
            print(f"  流萤: {m['content']}")
        elif t == "sticker":
            print(f"  流萤: [表情包: {m.get('label', '?')}] {m.get('path', '')}")

    # 简单记录用于后续分析
    # time.sleep(1)

print(f"\n{'=' * 60}")
print("10轮对话结束")
print("=" * 60)
