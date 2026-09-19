# -*- coding: utf-8 -*-
"""PC 端前端审计：用无头 Chrome + CDP 在**桌面宽度**下逐视图截图 + 采集布局证据。

为什么不用"看代码猜"：PC 端的毛病大多是**渲染出来才知道**的（层叠、fixed 定位、
断点漏项、横向溢出）。所以这里把真实页面按 1600×1000 / 1280×800 两档跑一遍，
截图存到 .tmp_test/ui/pc/，同时把每个视图的关键几何量（溢出、滚动条、元素宽度、
被裁切/重叠的元素）打印出来当证据。

用法：
    python .tmp_test/pc_audit.py            # 起 Chrome → 逐视图截图 + 打印几何
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".tmp_test" / "ui" / "pc"
OUT.mkdir(parents=True, exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9333
BASE = "http://127.0.0.1:8765/"


def _devtools(path="/json/list"):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10).read())


def start_chrome(w, h):
    prof = ROOT / ".tmp_test" / "chrome_prof"
    args = [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
            f"--window-size={w},{h}", "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={prof}",
            "--remote-allow-origins=*",
            "--force-device-scale-factor=1", "about:blank"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Page:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30,
                                             header=["Origin: devtools://devtools"])
        self.mid = 0

    def call(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                return msg

    def js(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"__js_error__": res["exceptionDetails"].get("text", ""),
                    "detail": str(res["exceptionDetails"].get("exception", {})
                                  .get("description", ""))[:400]}
        return res.get("result", {}).get("value")

    def shot(self, name, wait=1.2):
        time.sleep(wait)
        r = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        import base64
        data = r.get("result", {}).get("data", "")
        if not data:
            print(f"    ! {name} 截图失败: {str(r)[:200]}")
            return None
        p = OUT / f"{name}.png"
        p.write_bytes(base64.b64decode(data))
        return p


GEOM = r"""
(() => {
  const out = {vw: innerWidth, vh: innerHeight,
               hscroll: document.documentElement.scrollWidth > innerWidth + 1,
               scrollW: document.documentElement.scrollWidth};
  const vis = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // 只报"有 id 或在视口内且越界"的元素，避免刷屏
    const over = r.right > innerWidth + 2 || r.left < -2;
    if (over && el.id) vis.push({tag: el.tagName, id: el.id, cls: String(el.className).slice(0,50),
                                 left: Math.round(r.left), right: Math.round(r.right),
                                 w: Math.round(r.width), h: Math.round(r.height)});
  }
  out.overflowing = vis.slice(0, 20);
  const pick = id => { const e = document.getElementById(id); if (!e) return null;
    const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
            pos: cs.position, maxW: cs.maxWidth, overflowY: cs.overflowY}; };
  out.boxes = {};
  for (const id of ["pc-sidebar","home-view","app","header","messages","input-bar",
                    "settings-panel","notice-panel","menu-drawer","home-carousel","home-modes",
                    "feedback-panel","cards-view","pack-view","home-blank"]) {
    const b = pick(id); if (b) out.boxes[id] = b;
  }
  out.sidebarItems = [...document.querySelectorAll('#pc-sidebar .pcs-item')].map(e => e.textContent.trim());
  out.homeModes = [...document.querySelectorAll('.hm-card')].length;
  out.bodyClass = document.body.className;
  return out;
})()
"""


def main():
    print("=== 启动无头 Chrome（1600×1000）===")
    proc = start_chrome(1600, 1000)
    try:
        for _ in range(40):
            try:
                tabs = _devtools()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("X Chrome 没起来"); return 2
        tab = [t for t in tabs if t.get("type") == "page"][0]
        pg = Page(tab["webSocketDebuggerUrl"])
        pg.call("Page.enable")
        pg.call("Runtime.enable")
        pg.call("Emulation.setDeviceMetricsOverride",
                {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        print("V Chrome 就绪")

        def nav(url, wait=2.5):
            pg.call("Page.navigate", {"url": url})
            time.sleep(wait)

        # ★ 先关掉引导：否则引导遮罩把整页压暗、还会拦截点击（第一版就是这样，看到的全是暗的）
        nav(BASE, 2.0)
        pg.js("localStorage.setItem('firefly_guide_v2_done','1');"
              "localStorage.setItem('firefly_deep_guide_v2_done','1');"
              "localStorage.setItem('firefly_guide_v1_done','1');"
              "localStorage.setItem('firefly_deep_guide_v1_done','1'); 'ok'")
        print("  （已关闭引导，重新加载）")

        print("\n=== ① 首页（1600×1000）===")
        nav(BASE)
        g = pg.js(GEOM)
        print(json.dumps(g, ensure_ascii=False, indent=1)[:2600])
        pg.shot("01_home_1600")

        print("\n=== ② 进入聊天页 ===")
        # ★ 本地版有「登录前置」（A7）：/auth/state 返回 logged_in=false 时 showChat() 会回首页展开登录表单。
        #   审计要看的**是布局**，所以这里把 /auth/state 打桩成已登录（只影响本次截图，不改仓库代码）。
        pg.js("(function(){const o=window.fetch;window.fetch=function(u,opt){"
              "if(String(u).indexOf('/auth/state')>=0)return Promise.resolve({ok:true,status:200,"
              "json:()=>Promise.resolve({logged_in:true,offline_ok:true,email:'audit@local'})});"
              "return o(u,opt);};return 'shimmed';})()")
        pg.js("(async function(){ try{ await enterMode('story'); }catch(e){} })()")
        time.sleep(3.0)
        g = pg.js(GEOM)
        print(json.dumps({k: g.get(k) for k in ("vw","vh","hscroll","scrollW","overflowing","boxes")},
                         ensure_ascii=False, indent=1)[:2200])
        print("  消息条数:", pg.js("document.querySelectorAll('#messages .msg-row').length"))
        print("  #app 宽度:", pg.js("document.getElementById('app').getBoundingClientRect().width"))
        pg.shot("02_chat_1600")

        print("\n=== ③ 菜单（桌面应该是右区整页视图）===")
        pg.js("typeof openMenu==='function' ? openMenu() : document.getElementById('menu-btn').click()")
        time.sleep(1.2)
        pg.shot("03_menu_1600")
        print(json.dumps(pg.js(GEOM).get("boxes", {}).get("menu-drawer"), ensure_ascii=False))

        print("\n=== ④ 设置页 ===")
        pg.js("typeof closeMenu==='function' && closeMenu(); typeof openSettings==='function' && openSettings();")
        time.sleep(1.2)
        pg.shot("04_settings_1600")

        print("\n=== ⑤ 公告面板 ===")
        pg.js("typeof closeSettings==='function' && closeSettings(); typeof toggleNotice==='function' && toggleNotice();")
        time.sleep(1.2)
        pg.shot("05_notice_1600")

        print("\n=== ⑤b 设定文件（PC：列② 选段 → 右栏只看这一段）===")
        pg.js("typeof closeNotice==='function' && closeNotice()")
        pg.js("(function(){var b=document.querySelector('#pcr-items [data-section=\"memory\"]'); if(b) b.click();})()")
        time.sleep(1.6)
        pg.shot("05b_memory_1600")
        pg.js("(function(){var b=document.querySelector('#pcl-body [data-arg=\"archive\"]'); if(b) b.click();})()")
        time.sleep(1.6)
        print("  三段 display:", pg.js(
            "JSON.stringify([].map.call(document.querySelectorAll('#tab-char .pc-sec'),"
            "function(s){return [s.id, getComputedStyle(s).display];}))"))
        pg.shot("05c_archive_1600")

        print("\n=== ⑥ 角色卡管理 ===")
        pg.js("typeof closeNotice==='function' && closeNotice(); typeof openCards==='function' ? openCards() : document.getElementById('cards-manage-btn').click();")
        time.sleep(1.5)
        pg.shot("06_cards_1600")

        print("\n=== ⑥b 角色卡（PC：列② 选域 → 只展开该域）===")
        pg.js("(function(){var b=document.querySelector('#pcr-items [data-section=\"cards\"]'); if(b) b.click();})()")
        time.sleep(1.8)
        pg.shot("06b_cards_rail_1600")
        pg.js("(function(){var b=document.querySelector('#pcl-body [data-arg=\"proactive\"]'); if(b) b.click();})()")
        time.sleep(2.0)
        print("  展开的域:", pg.js(
            "JSON.stringify([].filter.call(document.querySelectorAll('#pv-tree .pt-domain'),"
            "function(d){return d.open;}).map(function(d){return d.dataset.key;}))"))
        pg.shot("06c_domain_proactive_1600")

        print("\n=== ⑦ 窄桌面 1280×800 回首页 ===")
        pg.call("Emulation.setDeviceMetricsOverride",
                {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        nav(BASE, 2.0)
        g = pg.js(GEOM)
        print(json.dumps({k: g.get(k) for k in ("vw","vh","hscroll","scrollW","boxes","homeModes")},
                         ensure_ascii=False, indent=1)[:1600])
        pg.shot("07_home_1280")

        print("\n=== ⑧ 平板临界 1024×768（应落到手机档）===")
        pg.call("Emulation.setDeviceMetricsOverride",
                {"width": 1024, "height": 768, "deviceScaleFactor": 1, "mobile": False})
        nav(BASE, 2.0)
        g = pg.js(GEOM)
        print(json.dumps({k: g.get(k) for k in ("vw","vw","hscroll","boxes","homeModes")},
                         ensure_ascii=False, indent=1)[:1200])
        pg.shot("08_home_1024")

        print(f"\n截图输出：{OUT}")
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
