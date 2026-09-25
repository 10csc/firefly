// 公告 / 更新说明通道（服务端下发 + 本地缓存 + 静态兜底）
// 见 docs/公告通道规范.md。
//
// 三条设计约束（都不是"顺手写的"，是有原因的）：
//  ① **不阻塞**：先渲染 localStorage 里的上次结果，再后台联网刷新。
//     公告是"顺带看一眼"的东西，绝不能让人打开面板时卡在网络上。
//  ② **不拼 HTML**：服务端文本一律 createTextNode/textContent 写入。
//     这样即便签名密钥泄露、服务端被投毒，也**注入不进来** —— 结构上不存在 XSS 面。
//  ③ **不依赖网络**：拿不到就什么都不加，「使用指南」Tab 里的内置指南照常显示。
//
// 2026-09-24 重构（用户报障："公告没做好上下滚动和内容区分，0.9.0 的公告
// 还是塞在一个使用手册里"）：
//   · 拆两个 Tab：「公告」（服务端下发）与「使用指南」（内置常驻）。
//   · 公告按 level 分档筛选（全部 / 更新 / 提醒 / 重要），按日期分组 + sticky 组头。
//   · 三态：有内容 / 空 / 拉取失败——**失败必须说出来**，否则空面板会被当成"功能没做"。
//   · 滚动位置按 Tab 记住（切回来还在原处）。
//   · 面板头徽标**只计未读公告**，不含使用指南（否则点掉也不灭，等于失效）。
import { API_BASE } from "./api.js";

const CACHE_KEY = "firefly_notice_cache";     // 最近一次成功的 payload（含已读态镜像）
const SEEN_OPEN_KEY = "firefly_notice_opened"; // 上次打开面板的时间（节流刷新用）
const TAB_KEY = "firefly_notice_tab";         // 上次停留的 Tab（下次打开还回到那儿）
const REFRESH_MS = 30 * 60 * 1000;            // 打开面板时最多 30 分钟联网一次

let _payload = null;
let _readTimer = null;
let _tab = "entries";                         // "entries"（公告）| "guide"（使用指南）
let _filter = "";                             // ""（全部）| info | warn | critical
let _scroll = { entries: 0, guide: 0 };       // 两个 Tab 各自的滚动位置
let _fetchState = "idle";                     // idle | loading | ok | fail

// 没有日期、或日期格式异常时归到这里，永远排最后
const _NO_DATE = "更早";

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

// 日期分组：服务端 date 是 "YYYY-MM-DD"。今天/昨天用人话，其余原样。
function _groupLabel(date) {
    if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return _NO_DATE;
    const d = new Date(date + "T00:00:00");
    if (isNaN(d.getTime())) return _NO_DATE;
    const now = new Date();
    const day = (x) => Math.floor((x.getTime() - x.getTimezoneOffset() * 60000) / 86400000);
    const diff = day(now) - day(d);
    if (diff === 0) return "今天";
    if (diff === 1) return "昨天";
    if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + " 月 " + d.getDate() + " 日";
    return date;
}

function _filtered() {
    const all = (_payload && _payload.entries) || [];
    return _filter ? all.filter(e => (e.level || "info") === _filter) : all;
}

function _paintEmpty(host, icon, title, sub) {
    const box = _el("div", "notice-empty");
    box.appendChild(_el("span", "notice-empty-ico", icon));
    box.appendChild(_el("div", null, title));
    if (sub) box.appendChild(_el("div", "notice-empty-sub", sub));
    host.appendChild(box);
}

// 公告 Tab 的正文
function _paintEntries() {
    const host = document.getElementById("notice-server");
    if (!host) return;
    host.textContent = "";                       // 清空（只走 textContent，全程不碰标记字符串）

    // 缓存过、但联网失败过 → 先说清楚"你看到的可能不是最新的"
    if (_fetchState === "fail") {
        host.appendChild(_el("div", "notice-stale",
            "暂时连不上公告服务，以下为上次同步的内容。"));
    }

    const list = _filtered();
    if (!list.length) {
        const total = ((_payload && _payload.entries) || []).length;
        if (!total) {
            // 真的没有公告。给一句人话，别让空面板看起来像坏了
            if (_fetchState === "loading") {
                _paintEmpty(host, "⏳", "正在获取公告…");
            } else if (_fetchState === "fail") {
                _paintEmpty(host, "📡", "暂时连不上公告服务",
                    "你的网络可能不稳定：切到「使用指南」仍可正常阅读。");
            } else {
                _paintEmpty(host, "📢", "暂无新公告",
                    "更新说明与临时提醒会出现在这里。");
            }
        } else {
            // 有公告，只是这个筛选档下没有
            _paintEmpty(host, "🔍", "这个分类下暂无公告", "换一个筛选条件看看。");
        }
        return;
    }

    // 分组：置顶的永远最前；其余按日期倒序（服务端已排过，这里只做分组不做重排）
    let lastGroup = null;
    for (const e of list) {
        const label = e.pinned ? "置顶" : _groupLabel(e.date);
        if (label !== lastGroup) {
            lastGroup = label;
            host.appendChild(_el("div", "notice-group", label));
        }
        host.appendChild(_renderEntry(e));
    }
}

// 徽标与 chips 计数：**只算公告，不算使用指南**
function _paintBadges() {
    const entries = (_payload && _payload.entries) || [];
    const unread = entries.filter(e => e.unread).length;

    const badge = document.getElementById("notice-badge");
    if (badge) {
        badge.hidden = !unread;
        badge.textContent = unread > 9 ? "9+" : String(unread || "");
        badge.title = unread ? `有 ${unread} 条新公告` : "";
    }
    const tab = document.getElementById("notice-tab-entries");
    if (tab) {
        tab.textContent = "公告";
        if (unread) {
            const b = _el("span", "notice-badge", unread > 9 ? "9+" : String(unread));
            tab.appendChild(b);
        }
    }
    // 首页公告栏的小圆点（结构在 index.html，样式 .notice-dot）
    const dot = document.getElementById("notice-dot");
    if (dot) {
        dot.hidden = !unread;
        dot.textContent = unread > 9 ? "9+" : String(unread || "");
        dot.title = unread ? `有 ${unread} 条新公告` : "";
    }
    // chips 上的计数
    const counts = { "": entries.length, info: 0, warn: 0, critical: 0 };
    for (const e of entries) {
        const lv = e.level || "info";
        if (counts[lv] !== undefined) counts[lv]++;
    }
    const chips = document.querySelectorAll("#notice-chips .notice-chip");
    for (const c of chips) {
        const lv = c.getAttribute("data-lv") || "";
        const n = counts[lv] || 0;
        // 计数值重建（避免重复追加）：先清掉上次追加的计数节点
        const old = c.querySelector(".notice-chip-n");
        if (old) old.remove();
        const base = { "": "全部", info: "更新", warn: "提醒", critical: "重要" }[lv] || lv;
        c.textContent = base;
        if (n) c.appendChild(_el("span", "notice-chip-n", " " + n));
        c.hidden = (lv !== "" && n === 0);      // 空档位直接收起，别留一排没用的按钮
    }
}

function _paint(p) {
    if (p) _payload = p;
    _paintEntries();
    _paintBadges();
}

// ── Tab / 筛选 / 滚动位置 ────────────────────────
export function noticeTab(name) {
    if (name !== "entries" && name !== "guide") return;
    _scroll[_tab] = _bodyScroll();
    _tab = name;
    _ls(TAB_KEY, name);

    const entries = document.getElementById("notice-pane-entries");
    const guide = document.getElementById("notice-pane-guide");
    const tE = document.getElementById("notice-tab-entries");
    const tG = document.getElementById("notice-tab-guide");
    const chips = document.getElementById("notice-chips");
    if (entries) entries.hidden = name !== "entries";
    if (guide) guide.hidden = name !== "guide";
    if (tE) tE.setAttribute("aria-selected", name === "entries" ? "true" : "false");
    if (tG) tG.setAttribute("aria-selected", name === "guide" ? "true" : "false");
    if (chips) chips.hidden = name !== "entries";

    _setBodyScroll(_scroll[name] || 0);
}

export function noticeFilter(lv) {
    _filter = lv || "";
    const chips = document.querySelectorAll("#notice-chips .notice-chip");
    for (const c of chips) {
        c.setAttribute("aria-pressed", (c.getAttribute("data-lv") || "") === _filter ? "true" : "false");
    }
    _paintEntries();
    _scroll[_tab] = 0;                        // 换筛选回到顶部，并把记录一起归零
    _setBodyScroll(0);
}

// 滚动的是 .notice-body（两个 Tab 共用它，所以必须自己记位置）
function _bodyEl() { return document.querySelector("#notice-panel .notice-body"); }
function _bodyScroll() { const b = _bodyEl(); return b ? b.scrollTop : 0; }

// 恢复滚动位置。★ 必须"先取消上一次待执行的 rAF"：
//   切 Tab 是"存旧位置 → 恢复新位置"两步，若用户连点两个 Tab（或代码里连续调两次），
//   两个 rAF 都会在下一帧排队执行，**先入队的那个会后跑**的概率存在 ⇒ 恢复成错的 Tab 的位置。
//   2026-09-24 实测 G1 用例就是这么失败的（连续 noticeTab × 2，最终 scrollTop 被盖回 0）。
let _scrollRaf = 0;
function _setBodyScroll(v) {
    const b = _bodyEl();
    if (!b) return;
    if (_scrollRaf) cancelAnimationFrame(_scrollRaf);
    _scrollRaf = requestAnimationFrame(() => {
        _scrollRaf = 0;
        try { b.scrollTop = v; } catch (e) {}
    });
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
        if (!r.ok) { _fetchState = "fail"; _paintEntries(); return null; }
        const d = await r.json();
        const p = force ? (d.payload || null) : d;
        if (p && p.ok) {
            _payload = p;
            _writeCache(p);
            // 注意顺序：先落状态再 paint，否则空态文案会慢一帧
            _fetchState = "ok";
            _paint(p);
            // ★ 后端是「先回缓存、后台联网刷新」（见 app/notice.py::payload 的说明）：
            //   首次安装 / 缓存过期时，这一次拿到的就是**空的**，而刷新结果要几百毫秒后才有。
            //   不跟一次的话：首页永远不亮小圆点、面板永远只有内置指南，
            //   直到用户下次再打开面板 —— 表现为"公告功能像没做"。
            //   （2026-09-19 真机实测抓到的：后端 serial=1、前端渲染 0 条。）
            if (p.refreshing) { _fetchState = "loading"; _scheduleRetry(); }
            else { _retry = 0; }
            return p;
        }
        _fetchState = "fail";
        _paintEntries();
        return p;
    } catch (e) {
        _fetchState = "fail";                    // 离线/服务端没开：说清楚，别装作没公告
        _paintEntries();
        return null;
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
    }, 1500);
}

// 面板打开时调用（views.js 的 toggleNotice 会调）
export function noticeOnOpen() {
    // 回到上次停留的 Tab（第一次打开默认"公告"）
    const saved = _ls(TAB_KEY);
    noticeTab(saved === "guide" ? "guide" : "entries");
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
    } else {
        _paintBadges();                          // 无缓存也要把 chips/徽标置成干净的初始态
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
window.noticeTab = noticeTab;         // 面板内联 onclick
window.noticeFilter = noticeFilter;

function _bootNotice() { try { initNotice(); } catch (e) { /* 公告不配拖垮启动 */ } }
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bootNotice);
} else {
    _bootNotice();
}
