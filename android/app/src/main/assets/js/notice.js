// 公告 / 更新说明通道（服务端下发 + 本地缓存 + 静态兜底）
// 见 docs/公告通道规范.md。
//
// 三条设计约束（都不是"顺手写的"，是有原因的）：
//  ① **不阻塞**：先渲染 localStorage 里的上次结果，再后台联网刷新。
//     公告是"顺带看一眼"的东西，绝不能让人打开面板时卡在网络上。
//  ② **不拼 HTML**：服务端文本一律 createTextNode/textContent 写入。
//     这样即便签名密钥泄露、服务端被投毒，也**注入不进来** —— 结构上不存在 XSS 面。
//  ③ **不依赖网络**：拿不到就什么都不加，Index.html 里的内置指南照常显示。
import { API_BASE } from "./api.js";

const CACHE_KEY = "firefly_notice_cache";     // 最近一次成功的 payload（含已读态镜像）
const SEEN_OPEN_KEY = "firefly_notice_opened"; // 上次打开面板的时间（节流刷新用）
const REFRESH_MS = 30 * 60 * 1000;            // 打开面板时最多 30 分钟联网一次

let _payload = null;
let _readTimer = null;

function _ls(key, val) {
    try {
        if (val === undefined) return localStorage.getItem(key);
        localStorage.setItem(key, val);
    } catch (e) { /* 隐私模式/配额满：静默降级为"不缓存" */ }
    return null;
}

function _readCache() {
    try {
        const raw = _ls(CACHE_KEY);
        const d = raw ? JSON.parse(raw) : null;
        return (d && typeof d === "object" && Array.isArray(d.entries)) ? d : null;
    } catch (e) { return null; }
}

function _writeCache(p) {
    try { _ls(CACHE_KEY, JSON.stringify(p)); } catch (e) {}
}

// ── 渲染（只用 DOM API + textContent；本文件刻意不出现任何标记字符串接口）─────
function _el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.appendChild(document.createTextNode(String(text)));
    return n;
}

const _BLOCK_TAG = { p: "p", h: "h4", li: "li", tip: "p" };

function _renderBlock(b) {
    if (b.t === "img") {
        const fig = _el("div", "notice-fig");
        const img = document.createElement("img");
        // 图片走独立端点：内容在服务端已按清单 sha256 校验过，文件名是白名单形态
        img.src = API_BASE + "/notice-image?name=" + encodeURIComponent(b.name);
        img.alt = b.alt || "";
        img.loading = "lazy";
        fig.appendChild(img);
        if (b.alt) fig.appendChild(_el("div", "notice-fig-cap", b.alt));
        return fig;
    }
    const tag = _BLOCK_TAG[b.t] || "p";
    return _el(tag, "notice-b-" + b.t, b.text);
}

function _renderEntry(e) {
    const box = _el("div", "notice-entry notice-lv-" + (e.level || "info"));
    if (e.pinned) box.classList.add("notice-pinned");
    const head = _el("div", "notice-ent-head");
    head.appendChild(_el("span", "notice-ent-title", e.title));
    if (e.date) head.appendChild(_el("span", "notice-ent-date", e.date));
    if (e.unread) head.appendChild(_el("span", "notice-ent-new", "新"));
    box.appendChild(head);
    for (const b of (e.blocks || [])) box.appendChild(_renderBlock(b));
    return box;
}

function _paint(p) {
    const host = document.getElementById("notice-server");
    if (!host) return;
    host.textContent = "";                       // 清空（只走 textContent，全程不碰标记字符串）
    const entries = (p && p.entries) || [];
    if (!entries.length) {
        // 一条都没有：整块不留痕迹（静态兜底照常显示）
        return;
    }
    for (const e of entries) host.appendChild(_renderEntry(e));
    const divider = _el("div", "notice-divider");
    divider.appendChild(document.createTextNode("以下为 App 内置使用指南"));
    host.appendChild(divider);
}

function _paintDot(n) {
    const dot = document.getElementById("notice-dot");
    if (dot) {
        dot.hidden = !n;
        dot.textContent = n > 9 ? "9+" : String(n || "");
        dot.title = n ? `有 ${n} 条新公告` : "";
    }
}

// ── 联网（失败一律静默：公告不配打断用户）────────────
export async function refreshNotice(force) {
    try {
        const url = force ? "/notice/action" : "/notice";
        const opts = force
            ? { method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "check" }) }
            : {};
        const r = await fetch(url, opts);
        if (!r.ok) return null;
        const d = await r.json();
        const p = force ? (d.payload || null) : d;
        if (p && p.ok) {
            _payload = p;
            _writeCache(p);
            _paint(p);
            _paintDot(p.unread);
            // ★ 后端是「先回缓存、后台联网刷新」（见 app/notice.py::payload 的说明）：
            //   首次安装 / 缓存过期时，这一次拿到的就是**空的**，而刷新结果要几百毫秒后才有。
            //   不跟一次的话：首页永远不亮小圆点、面板永远只有内置指南，
            //   直到用户下次再打开面板 —— 表现为"公告功能像没做"。
            //   （2026-09-19 真机实测抓到的：后端 serial=1、前端渲染 0 条。）
            if (p.refreshing) _scheduleRetry();
            else _retry = 0;
        }
        return p;
    } catch (e) {
        return null;                             // 离线/服务端没开：保持现状
    }
}

// 后台刷新还没落地 → 过几秒自己再拉一次（最多 3 次，之后交给下次打开面板）
let _retry = 0;
function _scheduleRetry() {
    if (_retry >= 3) return;
    _retry++;
    setTimeout(() => { refreshNotice(false); }, 4000);
}

// 标记已读（面板打开 1.5 秒后）——延迟是为了让"新"标记真的被眼睛扫到
function _markReadSoon() {
    clearTimeout(_readTimer);
    _readTimer = setTimeout(async () => {
        const p = _payload;
        const ids = ((p && p.entries) || []).filter(e => e.unread).map(e => e.id);
        if (!ids.length) return;
        try {
            await fetch("/notice/action", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "read", ids }),
            });
        } catch (e) { /* 没记上也没关系：下次打开还会提示 */ }
        const cached = _readCache();
        if (cached) {
            cached.entries = (cached.entries || []).map(e => Object.assign({}, e, { unread: false }));
            cached.unread = 0;
            _writeCache(cached);
        }
        if (_payload) {
            _payload.entries = (_payload.entries || []).map(e => Object.assign({}, e, { unread: false }));
            _payload.unread = 0;
        }
        _paint(_payload);
        _paintDot(0);
    }, 1500);
}

// 面板打开时调用（views.js 的 toggleNotice 会调）
export function noticeOnOpen() {
    _markReadSoon();
    let last = 0;
    try { last = parseInt(_ls(SEEN_OPEN_KEY) || "0", 10) || 0; } catch (e) {}
    if (Date.now() - last < REFRESH_MS) return;
    _ls(SEEN_OPEN_KEY, String(Date.now()));
    refreshNotice(false);
}

// 启动：先画缓存（瞬时），再后台刷新（不 await）
export function initNotice() {
    _payload = _readCache();
    if (_payload) {
        _paint(_payload);
        _paintDot(_payload.unread);
    }
    // 冷启动先让首页把首帧画完，别跟聊天/历史的启动请求抢带宽
    setTimeout(() => { refreshNotice(false); }, 2500);
}

// ★ 自启动，**不要**让 main.js 在顶层调 initNotice()（2026-09-19 真机踩到，见 docs/错误总结.md #15）：
//   bundle 是**单作用域**的拼接产物，`main` 排在 `notice` 前面 —— 从 main 的顶层调进本模块时，
//   本模块的顶层 `let _payload` 还没执行 ⇒ **TDZ ReferenceError** ⇒ main 顶层从这里中断，
//   连本文件末尾的 `window.noticeOnOpen = ...` 都没跑到 ⇒ 公告功能整体静默失效
//   （后端一切正常、界面什么都没有，52 项单测全绿）。
//   挂在 DOMContentLoaded 上还有第二个好处：DOM 一定就绪，首帧就能画。
window.noticeOnOpen = noticeOnOpen;

function _bootNotice() { try { initNotice(); } catch (e) { /* 公告不配拖垮启动 */ } }
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bootNotice);
} else {
    _bootNotice();
}
