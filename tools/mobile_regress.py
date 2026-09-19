# -*- coding: utf-8 -*-
"""移动端回归：PC 外壳改动后，<1100px 必须**一点没变**。

手机被拿走了（2026-09-19），所以用无头 Chrome 模拟 390×844 / 768×1024 两档，
断言的是**结构不变量**（比重像素更抗噪，也更能说明"外壳没漏出来"）：

  1. 三个外壳元素（#pc-shell / #pc-main-head / #pc-statusbar）在窄屏 computed display 必须是 none；
  2. 各视图仍是**铺满视口**的 fixed 层（rect ≈ 0,0,W,H）——PC 的 absolute 化没漏到窄屏；
  3. 没有横向滚动（scrollWidth <= innerWidth + 1）；
  4. 首页/聊天/设置三个视图都能正常显示（宽度 > 0），且聊天视图不需要登录也能被 showChat 打开
     （窄屏仍是"登录前置"逻辑，这里用打桩 /auth/state 验证布局，不验证业务门）。
"""
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".tmp_test" / "ui" / "mobile"
OUT.mkdir(parents=True, exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9334
PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


prof = ROOT / ".tmp_test" / "chrome_prof_m"
proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
                         "--window-size=390,844", "--no-first-run", "--disable-gpu",
                         "--hide-scrollbars", "--remote-allow-origins=*",
                         f"--user-data-dir={prof}", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    tabs = None
    for _ in range(40):
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json/list", timeout=10).read())
            break
        except Exception:
            time.sleep(0.5)
    tab = [t for t in tabs if t.get("type") == "page"][0]
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30,
                                     header=["Origin: devtools://devtools"])
    mid = 0

    def call(m, p=None):
        global mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": m, "params": p or {}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == mid:
                return r

    def js(e):
        r = call("Runtime.evaluate", {"expression": e, "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return "JSERR: " + str(res["exceptionDetails"].get("exception", {})
                                   .get("description", ""))[:200]
        return res.get("result", {}).get("value")

    def shot(name):
        time.sleep(1.0)
        r = call("Page.captureScreenshot", {"format": "png"})
        d = r.get("result", {}).get("data", "")
        if d:
            (OUT / f"{name}.png").write_bytes(base64.b64decode(d))

    call("Page.enable"); call("Runtime.enable")

    def load(w, h, mobile):
        call("Emulation.setDeviceMetricsOverride",
             {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": mobile})
        call("Page.navigate", {"url": "http://127.0.0.1:8765/"})
        time.sleep(3.0)
        js("localStorage.setItem('firefly_guide_v2_done','1');"
           "localStorage.setItem('firefly_deep_guide_v2_done','1');"
           "localStorage.setItem('firefly_guide_v1_done','1');"
           "localStorage.setItem('firefly_deep_guide_v1_done','1')")
        call("Page.navigate", {"url": "http://127.0.0.1:8765/"})
        time.sleep(3.0)
        js("(function(){const o=window.fetch;window.fetch=function(u,opt){"
           "if(String(u).indexOf('/auth/state')>=0)return Promise.resolve({ok:true,status:200,"
           "json:()=>Promise.resolve({logged_in:true,offline_ok:true})});return o(u,opt);};})()")

    SHELL = ("['pc-statusbar','pc-shell','pc-main-head','pc-chat-side','pc-rail','pc-list'].map(function(id){"
             "var e=document.getElementById(id);return e?getComputedStyle(e).display:'absent';})")

    for (W, H, label, mob) in ((390, 844, "手机 390×844", True), (768, 1024, "平板 768×1024", True)):
        print(f"\n=== {label} ===")
        load(W, H, mob)
        disp = js(SHELL)
        # 第二版 PC 适配层只保留底部状态栏；其余自造外壳**已删除**（不存在 = 'absent' 也算合格）。
        check("窄屏：状态栏 display:none，且没有残留的自造外壳",
              disp[0] == "none" and all(d in ("none", "absent") for d in disp[1:]), str(disp))
        check("窄屏没有左栏/第二列占位（不该出现任何 pc-* 外壳）",
              all(d == "absent" for d in disp[1:]), str(disp[1:]))
        check("无横向滚动", js("document.documentElement.scrollWidth <= innerWidth + 1"),
              f"scrollW={js('document.documentElement.scrollWidth')} vw={js('innerWidth')}")
        r = js("JSON.stringify(document.getElementById('home-view').getBoundingClientRect())")
        rect = json.loads(r) if isinstance(r, str) and r.startswith("{") else {}
        # 390 窄屏 = 满幅；720~1100 过渡档 = 居中 640 单栏（style.css 既有设计，不是本次改动）
        if W < 720:
            check("首页满幅铺满视口（fixed 全屏，没被 PC 定位漏进来）",
                  abs(rect.get("left", -1)) < 2 and abs(rect.get("top", -1)) < 2
                  and abs(rect.get("width", 0) - W) < 3,
                  f"left={rect.get('left')} top={rect.get('top')} w={rect.get('width')}")
        else:
            check("首页仍是「居中 640 单栏」过渡档（既有设计保留）",
                  abs(rect.get("top", -1)) < 2 and 600 <= rect.get("width", 0) <= W
                  and abs(rect.get("left", 0) + rect.get("width", 0) / 2 - W / 2) < 3,
                  f"left={rect.get('left')} w={rect.get('width')}")
        shot(f"home_{W}")

        js("(async function(){try{await enterMode('story');}catch(e){}})()")
        time.sleep(2.5)
        r = js("JSON.stringify(document.getElementById('app').getBoundingClientRect())")
        rect = json.loads(r) if isinstance(r, str) and r.startswith("{") else {}
        check("聊天视图铺满可用高度且不越界",
              rect.get("height", 0) >= H - 2 and rect.get("width", 0) >= min(640, W) - 4
              and rect.get("left", 0) + rect.get("width", 0) <= W + 2,
              f"top={rect.get('top')} w={rect.get('width')} h={rect.get('height')}")
        check("手机端仍有输入栏与消息区", js("!!document.getElementById('input-bar')")
              and js("document.querySelectorAll('#messages .msg-row').length") > 0,
              f"{js('document.querySelectorAll(\"#messages .msg-row\").length')} 条消息")
        shot(f"chat_{W}")

        js("typeof openSettings==='function' && openSettings()")
        time.sleep(1.2)
        r = js("JSON.stringify(document.getElementById('settings-panel').getBoundingClientRect())")
        rect = json.loads(r) if isinstance(r, str) and r.startswith("{") else {}
        check("设置面板铺满可用宽度", rect.get("width", 0) >= min(640, W) - 4, f"w={rect.get('width')}")
        # 窄屏必须是"手风琴"（只开一个/默认那几个），**不能**被 PC 的"全展开"漏过来
        n_open = js("document.querySelectorAll('#settings-panel .set-group.open').length")
        n_all = js("document.querySelectorAll('#settings-panel .set-group').length")
        check("设置分组在窄屏**没有**被强制全展开（PC 行为没漏下来）",
              n_open < n_all and n_all > 0, f"展开 {n_open}/{n_all}")
        shot(f"settings_{W}")

        # 设定文件三段：窄屏必须**三段都在**（PC 的"一段一屏"绝不能漏下来）
        js("typeof openMenuTab==='function' && openMenuTab('char')")
        time.sleep(1.2)
        n_seg = js("document.querySelectorAll('#tab-char .pc-sec').length")
        vis = js("[].filter.call(document.querySelectorAll('#tab-char .pc-sec'),"
                 "function(s){return getComputedStyle(s).display!=='none';}).length")
        seg = js("JSON.stringify([].map.call(document.querySelectorAll('#tab-char .pc-sec'),"
                 "function(s){return [s.id, getComputedStyle(s).display];}))")
        check("设定文件三段在窄屏**全部可见**（PC 的一段一屏没漏下来）",
              n_seg >= 3 and vis == n_seg, f"{vis}/{n_seg} 可见  {str(seg)[:70]}")
        shot(f"memory_{W}")

    print(f"\n统计: PASS={PASS} FAIL={FAIL}")
    print(f"截图：{OUT}")
    sys.exit(1 if FAIL else 0)
finally:
    proc.terminate()
