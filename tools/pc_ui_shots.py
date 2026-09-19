# -*- coding: utf-8 -*-
"""PC 端 UI 局部原尺寸截图（用于"自己看它好不好看"）。

和 `tools/pc_audit.py` 的区别：那个出**整页**图（1600 宽会被读图工具压到 ~1011 再给我看，
细节看不清）；这个用 CDP 的 `clip` **按区域裁**，并把窄栏放大 2×，
保证送进读图工具时**不被二次压缩**（宽度都 ≤ ~1000）。

    python tools/pc_ui_shots.py            # 出图到 .tmp_test/ui/pc_zoom/
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
OUT = ROOT / ".tmp_test" / "ui" / "pc_zoom"
OUT.mkdir(parents=True, exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9337
W, H = 1600, 1000


class Page:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=40,
                                              header=["Origin: devtools://devtools"])
        self.mid = 0

    def call(self, m, p=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": m, "params": p or {}}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == self.mid:
                return r

    def js(self, e):
        r = self.call("Runtime.evaluate", {"expression": e, "returnByValue": True,
                                          "awaitPromise": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return "JSERR: " + str(res["exceptionDetails"].get("exception", {})
                                   .get("description", ""))[:200]
        return res.get("result", {}).get("value")

    def rect(self, sel):
        v = self.js(f"(function(){{var e=document.querySelector('{sel}');"
                    f"if(!e) return null; var r=e.getBoundingClientRect();"
                    f"return JSON.stringify({{x:r.left,y:r.top,w:r.width,h:r.height}});}})()")
        return json.loads(v) if isinstance(v, str) and v.startswith("{") else None

    def shot(self, name, sel=None, rect=None, scale=2, pad=0):
        r = rect or (self.rect(sel) if sel else None)
        if not r or r["w"] < 2 or r["h"] < 2:
            print(f"  ! {name}: 找不到区域 {sel}")
            return None
        x = max(0, r["x"] - pad)
        y = max(0, r["y"] - pad)
        w = min(W - x, r["w"] + pad * 2)
        h = min(H - y, r["h"] + pad * 2)
        res = self.call("Page.captureScreenshot", {
            "format": "png",
            "clip": {"x": x, "y": y, "width": w, "height": h, "scale": scale},
        })
        data = res.get("result", {}).get("data", "")
        if not data:
            print(f"  ! {name}: 截图失败 {str(res)[:120]}")
            return None
        p = OUT / f"{name}.png"
        p.write_bytes(base64.b64decode(data))
        print(f"  V {name}: {int(w)}×{int(h)} CSS → {int(w*scale)}×{int(h*scale)} px  {p.name}")
        return p


def main():
    prof = ROOT / ".tmp_test" / "chrome_prof_zoom"
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
                             f"--window-size={W},{H}", "--no-first-run", "--disable-gpu",
                             "--hide-scrollbars", "--remote-allow-origins=*",
                             "--force-device-scale-factor=1", f"--user-data-dir={prof}",
                             "about:blank"],
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
        pg = Page(tab["webSocketDebuggerUrl"])
        pg.call("Page.enable"); pg.call("Runtime.enable")
        pg.call("Emulation.setDeviceMetricsOverride",
                {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
        pg.call("Page.navigate", {"url": "http://127.0.0.1:8765/"})
        time.sleep(3)
        pg.js("localStorage.setItem('firefly_guide_v2_done','1');"
              "localStorage.setItem('firefly_deep_guide_v2_done','1');"
              "localStorage.setItem('firefly_guide_v1_done','1')")
        pg.call("Page.navigate", {"url": "http://127.0.0.1:8765/"})
        time.sleep(3)
        pg.js("(function(){const o=window.fetch;window.fetch=function(u,opt){"
              "if(String(u).indexOf('/auth/state')>=0)return Promise.resolve({ok:true,status:200,"
              "json:()=>Promise.resolve({logged_in:true,offline_ok:true})});return o(u,opt);};})()")

        print("=== ① 首页（局部）===")
        pg.js("localStorage.setItem('firefly_notice_cache', JSON.stringify({ok:true,serial:1,unread:1,entries:[{id:'x',title:'Firefly 0.9.0 更新说明',date:'2026-09-19',level:'info',pinned:true,unread:true,blocks:[]}]}))")
        pg.shot("01_home_topbar", sel=".home-topbar", scale=2)
        pg.shot("02_home_card", sel=".hm-card", scale=2)
        pg.shot("03_home_actions", sel=".home-entry-row", scale=2, pad=6)
        pg.shot("04_home_auth", sel="#auth-module", scale=1, pad=6)

        print("=== ② 聊天页（局部）===")
        pg.js("(async function(){try{await enterMode('story');}catch(e){}})()")
        time.sleep(3.0)
        pg.shot("05_rail", sel="#pc-rail", scale=2)
        pg.shot("06_list", sel="#pc-list", scale=2)
        pg.shot("07_main_head", sel="#pc-main-head", scale=1)
        pg.shot("08_statusbar", sel="#pc-statusbar", scale=1)
        pg.shot("09_chat_head", sel="#app #header", scale=2)
        pg.shot("10_chat_msgs", sel="#messages", scale=1)
        pg.shot("11_chat_aside", sel="#pc-chat-side", scale=2)
        pg.shot("12_input_bar", sel="#input-bar", scale=1, pad=8)

        print("=== ③ 设定文件 / 角色卡 / 设置 / 公告（局部）===")
        pg.js("(function(){var b=document.querySelector('#pcr-items [data-section=\"memory\"]');if(b)b.click();})()")
        time.sleep(1.6)
        pg.shot("13_mem_list", sel="#pc-list", scale=2)
        pg.shot("14_mem_editor", sel="#tab-char", scale=1)

        pg.js("(function(){var b=document.querySelector('#pcr-items [data-section=\"cards\"]');if(b)b.click();})()")
        time.sleep(1.8)
        pg.js("(function(){var b=document.querySelector('#pcl-body [data-arg=\"proactive\"]');if(b)b.click();})()")
        time.sleep(2.0)
        pg.shot("15_pack_header", sel="#pack-view .pv-head, #pack-view header, #pack-view .pv-top", scale=2)
        pg.shot("16_pack_domains", sel="#pv-tree", scale=1)

        pg.js("(function(){var b=document.querySelector('#pcr-foot [data-section=\"settings\"]');if(b)b.click();})()")
        time.sleep(1.6)
        pg.shot("17_settings_groups", sel="#settings-panel .settings-body", scale=1)

        pg.js("(function(){var b=document.querySelector('#pcr-items [data-section=\"notice\"]');if(b)b.click();})()")
        time.sleep(1.6)
        pg.shot("18_notice", sel="#notice-panel", scale=1)

        print(f"\n输出：{OUT}")
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
