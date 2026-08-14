# -*- coding: utf-8 -*-
"""服务器版端到端验证脚本 — 模拟服务器版 APP 的完整行为链（含资产本地化与占位符填充）

链路：登录（Bearer token）→ /assets/index+raw 拉资产本地化
      → 带 X-API-Key/X-API-Base 发 /chat → 服务器流水线 relay 入队
      → 本脚本（扮演 APP）轮询 /relay/pending → **占位符填充（本地资产）**
      → 用用户 Key 直连 api_base 代发 → POST /relay/result 回传
      → 服务器唤醒流水线 → 返回回复

用法：python tools/server_e2e.py [消息文本]
依赖：requests；API Key 从 ~/.local/share/opencode/auth.json 读取（不落源码）
"""
import json
import sys
import threading
import time
from pathlib import Path

import requests

SERVER = "http://101.200.14.126:8787"
GO_BASE = "https://opencode.ai/zen/go/v1"
EMAIL, PASSWORD = "fireflytest@qq.com", "Test12345"
MODE = "story"
MAX_TURNS = 12   # relay 最多代发轮数（流水线 4 步 LLM，多轮消息合并时翻倍）

PLACEHOLDERS = ("__CORE__", "__IDENTITY__", "__SMS_SAMPLES__", "__KNOWLEDGE__")


def load_go_key() -> str:
    fp = Path.home() / ".local/share/opencode/auth.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        key = data.get("opencode-go", {}).get("key", "")
        if key.startswith("sk-"):
            return key
    except Exception:
        pass
    print("  [ERROR] 未找到 opencode-go Key（~/.local/share/opencode/auth.json）")
    sys.exit(1)


def sync_assets(headers: dict) -> dict:
    """模拟 APP 的 initAssets：清单指纹 → 下载资产（story 模式）。"""
    assets = {}
    r = requests.get(f"{SERVER}/assets/index?mode={MODE}", headers=headers, timeout=60)
    r.raise_for_status()
    idx = r.json()
    for name, info in (("knowledge", idx.get("knowledge", {})),
                       ("core", idx.get("character", {}).get("core", {})),
                       ("identity", idx.get("character", {}).get("identity", {})),
                       ("sms_samples", idx.get("character", {}).get("sms_samples", {}))):
        if (info.get("version") or "0") == "0":
            assets[name] = ""
            continue
        r = requests.get(f"{SERVER}/assets/raw?name={name}&mode={MODE}",
                         headers=headers, timeout=60)
        r.raise_for_status()
        assets[name] = r.json().get("content", "")
    return assets


def fill_placeholders(payload: dict, assets: dict) -> None:
    """模拟 APP 的 fillPlaceholders：把占位符替换为本地资产文本。"""
    for m in payload.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, str):
            for ph in PLACEHOLDERS:
                c = c.replace(ph, assets.get(ph[2:-2], ""))
            m["content"] = c


def main():
    use_proxy = "--proxy" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    message = args[0] if args else "你好呀，流萤，我是新用户"
    key = load_go_key()
    print(f"=== 服务器版 E2E（OpenCode Go 端点 + 资产本地化 + 占位符填充"
          f"{' + 服务器中转' if use_proxy else ''}）===")
    print(f"  服务器: {SERVER}  api_base: {GO_BASE}  mode: {MODE}")

    # ── 1. 登录 ──
    r = requests.post(f"{SERVER}/auth/login",
                      json={"email": EMAIL, "password": PASSWORD, "device": "e2e-script"},
                      timeout=30)
    d = r.json()
    if not d.get("ok"):
        print(f"  [FAIL] 登录失败: {d}")
        sys.exit(1)
    token = d["token"]
    print(f"  ✓ 登录成功（token 前 12 位: {token[:12]}...）")

    headers = {"Authorization": f"Bearer {token}",
               "X-API-Key": key, "X-API-Base": GO_BASE}

    # ── 2. 资产本地化（APP 端行为）──
    assets = sync_assets(headers)
    print(f"  ✓ 资产本地化: knowledge={len(assets['knowledge'])}字符 "
          f"core={len(assets['core'])} identity={len(assets['identity'])} "
          f"samples={len(assets['sms_samples'])}")
    if not assets["core"]:
        print("  [FAIL] core 资产为空（资产端点 mode 化或服务器数据问题）")
        sys.exit(1)

    # ── 3. 模拟 APP 的 relay 轮询线程 ──
    done = threading.Event()
    relay_log = []
    placeholder_leak = []

    def app_relay_loop():
        for _ in range(MAX_TURNS * 4):
            if done.is_set():
                return
            try:
                r = requests.post(f"{SERVER}/relay/pending", headers=headers, timeout=30)
                item = r.json()
                if not item.get("pending"):
                    time.sleep(0.5)
                    continue
                call_id, payload, api_base = item["call_id"], item["payload"], item["api_base"]
                fill_placeholders(payload, assets)
                # 泄漏检测：填充后仍含占位符 = 填充失败（角色设定未生效）
                if any(ph in json.dumps(payload, ensure_ascii=False) for ph in PLACEHOLDERS):
                    placeholder_leak.append(f"填充失败({call_id[:4]})")
                if use_proxy:
                    # 中转降级路径：服务器用 X-API-Key 头代发（模拟浏览器 CORS 失败后）
                    resp = requests.post(f"{SERVER}/relay/proxy", headers=headers,
                                         json={"call_id": call_id, "payload": payload},
                                         timeout=120)
                    pd = resp.json()
                    if not pd.get("ok") or not pd.get("response"):
                        raise RuntimeError(f"中转失败: {pd.get('error')}")
                    relay_log.append((call_id[:4], resp.status_code, "proxy"))
                    continue   # 服务器已回传，无需 /relay/result
                resp = requests.post(f"{api_base}/chat/completions",
                                     headers={"Authorization": f"Bearer {key}"},
                                     json=payload, timeout=120)
                relay_log.append((call_id[:4], resp.status_code))
                requests.post(f"{SERVER}/relay/result", headers=headers,
                              json={"call_id": call_id, "response": resp.json()},
                              timeout=30)
            except Exception as e:
                relay_log.append(("ERR", str(e)[:60]))
                time.sleep(1)

    t = threading.Thread(target=app_relay_loop, daemon=True)
    t.start()

    # ── 4. 发消息 ──
    t0 = time.time()
    try:
        r = requests.post(f"{SERVER}/chat", headers=headers,
                          json={"session_id": "e2e-go", "message": message},
                          timeout=180)
    finally:
        done.set()
    elapsed = time.time() - t0

    if r.status_code != 200:
        print(f"  [FAIL] /chat HTTP {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    body = r.json()
    texts = [m.get("content", "") for m in body.get("messages", []) if m.get("type") == "text"]
    if not texts:
        print(f"  [FAIL] 无文本回复: {json.dumps(body, ensure_ascii=False)[:300]}")
        sys.exit(1)
    print(f"  ✓ relay 代发 {len(relay_log)} 次: {relay_log}")
    if placeholder_leak:
        print(f"  [FAIL] 占位符泄漏: {placeholder_leak}")
        sys.exit(1)
    print(f"  ✓ 占位符全部填充（无 __ 残留）")
    print(f"  ✓ 总耗时 {elapsed:.1f}s")
    print(f"  ✓ 流萤回复:")
    for t_ in texts:
        print(f"    {t_}")


if __name__ == "__main__":
    main()
