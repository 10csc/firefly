#!/usr/bin/env python3
"""真机 WebView CDP 调试工具：执行 JS 读取/控制页面状态。
用法: python cdp.py '<js 表达式>'   （表达式求值结果 JSON 打印）
"""
import json
import sys
import urllib.request
import websocket  # websocket-client

WS = "ws://127.0.0.1:9222/devtools/page/355CC9F90C3A65BEAFEF895BF8AAA45F"

def _call(ws, method, params=None, mid=1):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.send if False else ws.recv())
        if msg.get("id") == mid:
            return msg

def evaluate(expr):
    ws = websocket.create_connection(WS, timeout=15,
                                     header=["Origin: devtools://devtools"])
    try:
        r = _call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"__js_error__": res["exceptionDetails"].get("text", ""),
                    "detail": str(res["exceptionDetails"].get("exception", {}).get("description", ""))[:300]}
        return res.get("result", {}).get("value")
    finally:
        ws.close()

if __name__ == "__main__":
    expr = sys.argv[1] if len(sys.argv) > 1 else "document.title"
    val = evaluate(expr)
    print(json.dumps(val, ensure_ascii=False, indent=1) if not isinstance(val, str) else val)
