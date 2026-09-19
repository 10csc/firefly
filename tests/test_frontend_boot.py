# -*- coding: utf-8 -*-
"""前端 bundle **真跑一次**冒烟测试（Node + 极简 DOM 桩）。

**为什么需要它**（2026-09-19 真机事故，见 docs/错误总结.md #15）：
`bundle.js` 是各模块**拼进同一个作用域**的产物（不是什么模块系统），于是
"`main.js` 顶层调用 `notice.js` 里的函数"会撞上**顶层 `let` 的 TDZ** ——
报 `ReferenceError` 之后，**bundle 后半截（notice.js 全部）根本没执行**：
后端一切正常、界面什么都没变、52 项后端单测全绿，只有真机才看得出来。

静态守卫（`test_notice.py` 的 J 组）拦不住这种错：源码里每一行都对，错在**拼装后的执行顺序**。
所以这里用 Node 把**产物本身**跑一遍，只提供最小的 DOM/localStorage/fetch 桩，
断言：不抛异常 + 公告模块确实注册了入口（`window.noticeOnOpen`）。

没有 node 时跳过（不算失败——但会打印一行说明，别让它静默变成"永远没跑"）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "app" / "static" / "js" / "bundle.js"

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


if not shutil.which("node"):
    print("! 本机没有 node，跳过 bundle 冒烟（**不是通过**，请在装机时补跑）")
    sys.exit(0)

HARNESS = r"""
// ── 极简 DOM 桩：只实现 bundle 顶层真正会碰到的东西 ──
const _els = {};
function _mkEl(tag) {
    const el = {
        tagName: tag, style: {}, dataset: {}, children: [], _text: "", _cls: new Set(),
        hidden: false, title: "", className: "", value: "", placeholder: "", id: "",
        get textContent() { return this._text; },
        set textContent(v) { this._text = String(v); this.children = []; },
        get classList() {
            const s = this._cls;
            return { add: c => s.add(c), remove: c => s.delete(c),
                     toggle: c => (s.has(c) ? (s.delete(c), false) : (s.add(c), true)),
                     contains: c => s.has(c) };
        },
        appendChild(c) { this.children.push(c); return c; },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        insertBefore(c) { this.children.unshift(c); return c; },
        setAttribute(k, v) { this[k] = v; },
        getAttribute(k) { return this[k]; },
        removeAttribute(k) { delete this[k]; },
        addEventListener() {}, removeEventListener() {},
        querySelector() { return null; }, querySelectorAll() { return []; },
        closest() { return null; }, focus() {}, blur() {}, click() {},
        getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0,
                                           right: 0, bottom: 0 }; },
        scrollIntoView() {}, insertAdjacentHTML() {}, remove() {},
        setProperty() {}, getPropertyValue() { return ""; }, removeProperty() {},
        contains() { return false; }, matches() { return false; },
    };
    el.style = new Proxy({}, {
        get: (t, k) => (k in t ? t[k] : () => {}),
        set: (t, k, v) => { t[k] = v; return true; },
    });
    return el;
}
for (const id of ["notice-server", "notice-dot", "notice-arrow", "notice-panel",
                  "notice-static", "home-notice", "guide-mask", "guide-spot",
                  "guide-tip", "menu-drawer", "settings-panel", "feedback-panel",
                  "home-view", "messages", "app"]) { _els[id] = _mkEl("div"); _els[id].id = id; }

global.document = {
    readyState: "complete",
    documentElement: _mkEl("html"),
    body: _mkEl("body"),
    head: _mkEl("head"),
    // 任意 id 都返回一个桩（bundle 会按 id 抓几十个元素；缺一个就 null，会误报）
    getElementById: id => (_els[id] || (_els[id] = Object.assign(_mkEl("div"), { id }))),
    createElement: _mkEl,
    createTextNode: t => ({ nodeType: 3, textContent: String(t) }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    removeEventListener: () => {},
    cookie: "",
};
const _store = {};
global.localStorage = {
    getItem: k => (k in _store ? _store[k] : null),
    setItem: (k, v) => { _store[k] = String(v); },
    removeItem: k => { delete _store[k]; },
    clear: () => { for (const k of Object.keys(_store)) delete _store[k]; },
};
global.window = global;
global.addEventListener = () => {};
global.removeEventListener = () => {};
global.navigator = { userAgent: "node" };
global.location = { href: "http://127.0.0.1:8765/", hash: "", reload: () => {}, origin: "http://127.0.0.1:8765" };
global.sessionStorage = global.localStorage;
global.crypto = { getRandomValues: a => { for (let i = 0; i < a.length; i++) a[i] = i; return a; } };
// fetch：一律返回"空公告"，只为让链路走通（不测网络语义）
global.fetch = () => Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve({ ok: true, serial: 0, entries: [], unread: 0,
                                  refreshing: false, app_version: "0.9.0" }),
    text: () => Promise.resolve(""), headers: { get: () => null },
});
global.XMLHttpRequest = function () { this.open = () => {}; this.send = () => {}; };
global.WebSocket = function () { this.close = () => {}; };
global.alert = () => {}; global.setInterval = () => 0; global.clearInterval = () => {};
global.Android = undefined;

// ── 加载产物 ──
const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
const out = [];
try {
    (0, eval)(code);
} catch (e) {
    out.push("THREW:" + (e && e.constructor && e.constructor.name) + ":" + (e && e.message));
}
// 顶层执行完，检查关键入口是否注册（TDZ 事故会让它缺失）
out.push("noticeOnOpen=" + (typeof global.noticeOnOpen));
out.push("toggleNotice=" + (typeof global.toggleNotice));
out.push("showHome=" + (typeof global.showHome));
out.push("openSettings=" + (typeof global.openSettings));
// 再触发一次"打开公告面板"，看渲染链路会不会抛
let err2 = "";
try {
    if (typeof global.toggleNotice === "function") global.toggleNotice();
} catch (e2) { err2 = (e2 && e2.message) || String(e2); }
out.push("toggleNoticeError=" + err2);
// ★ 必须显式退出：bundle 会起 carousel/relay/proactive 的定时器，事件循环不会自己空。
//   用 stdout 回调再退，避免管道里的输出被截断（第一版就截过）。
process.stdout.write(out.join("\n") + "\n", () => process.exit(0));
"""

h = Path(tempfile.mkdtemp(prefix="firefly_bundle_")) / "harness.js"
h.write_text(HARNESS, encoding="utf-8")
r = subprocess.run(["node", str(h), str(BUNDLE)], capture_output=True, encoding="utf-8",
                   errors="replace", timeout=90)
out = (r.stdout or "").strip()
print("--- node 输出 ---")
print(out or "(空)")
if r.stderr:
    print("--- stderr ---")
    print(r.stderr[:1200])

lines = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
threw = next((l for l in out.splitlines() if l.startswith("THREW:")), "")
print("\n=== 断言 ===")
check("A1 bundle 顶层执行不抛异常", not threw, threw[:160])
check("A2 公告入口已注册（TDZ 事故会让它 undefined）",
      lines.get("noticeOnOpen") == "function", lines.get("noticeOnOpen"))
check("A3 面板开关入口已注册", lines.get("toggleNotice") == "function")
check("A4 首屏入口已注册（确认 bundle 没被中途打断）",
      lines.get("showHome") == "function" and lines.get("openSettings") == "function")
check("A5 打开公告面板不抛异常", lines.get("toggleNoticeError", "x") == "",
      lines.get("toggleNoticeError"))
check("A6 node 进程退出码 0", r.returncode == 0, str(r.returncode))

shutil.rmtree(h.parent, ignore_errors=True)
print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
