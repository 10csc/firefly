/* ═══════════════════════════════════════════
   流萤前端运行时 bundle（classic script）—— 本文件由 tools/build_frontend_bundle.py 生成，
   请勿手改！源码在 app/static/js/*.js（ES Module），改完跑该脚本重新生成。
   ═══════════════════════════════════════════ */

/* ── 来源：js/state.js ── */
// 共享状态与 DOM 引用（原 app.js 头部 + 跨模块可变状态 S）
// Firefly 聊天 App — 前端逻辑（统一前端 0.8.0：本地 / 服务器双模式一套代码）

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const SESSION_ID = "firefly-" + Date.now();

// 跨模块共享可变状态（ESM 导入绑定只读，跨模块赋值的 let 统一收口到 S）
const S = {
    _flushTimer: null,
    _hasMore: false,
    _hiddenEnabled: true,
    _hintTimer: null,
    _lastRenderTs: 0,
    _rendering: false,
    waiting: false,
};


/* ── 来源：js/util.js ── */
// 通用小工具：toast / HTML 转义（从各节抠出合并）
// 轻提示（3s 自动消失）
let toastTimer = null;
function showToast(msg) {
    let el = document.getElementById("app-toast");
    if (!el) {
        el = document.createElement("div");
        el.id = "app-toast";
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}
function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
/** 轻量 toast（页面顶部浮层，3 秒消失；不打断输入） */
function _toast(msg) {
    let t = document.getElementById("app-toast");
    if (!t) {
        t = document.createElement("div");
        t.id = "app-toast";
        t.style.cssText = "position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:999;background:rgba(28,30,46,.96);color:#e8e0d0;border:1px solid rgba(255,196,107,.45);border-radius:10px;padding:9px 16px;font-size:0.8em;max-width:86vw;text-align:center;box-shadow:0 6px 22px rgba(0,0,0,.45);display:none;pointer-events:none";
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.style.display = "none"; }, 3200);
}
function _esc(s) {
    return String(s == null ? "" : s)
        .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

// ═══ 本机 API Key 单点读取（服务器模式）═══
// 正式存储：firefly_providers（W1 多供应商，key 在 active 供应商的 api_key 字段）；
// 遗留兜底：firefly_api_key（旧版直接存此处 + W1 后的兼容镜像字段）。
// providers 优先是为了防「兼容镜像字段被覆写为空、但主存储还有 key」的偶发丢失
// （用户反馈：服务器模式偶发每轮对话要求重新设置 Key）。
function getLocalApiKey() {
    try {
        const list = JSON.parse(localStorage.getItem("firefly_providers") || "[]");
        const arr = Array.isArray(list) ? list : [];
        const active = localStorage.getItem("firefly_active_provider") || "";
        const p = arr.find(x => x && x.id === active) || arr[0] || null;
        if (p && p.api_key) return p.api_key;
    } catch (e) { /* 解析失败走 legacy 兜底 */ }
    try { return localStorage.getItem("firefly_api_key") || ""; } catch (e) { return ""; }
}

// ═══ 媒体本地存储（A2：服务器只传输不保存图片；本体存 WebView IndexedDB）═══
// 键 = 内容 sha256（服务器返回的 local:<sha256>.<ext> 引用）；Blob 存取。
let _idbPromise = null;
function _idb() {
    if (!_idbPromise) {
        _idbPromise = new Promise((res, rej) => {
            try {
                if (!window.indexedDB) { rej(new Error("IndexedDB 不可用")); return; }
                const req = indexedDB.open("firefly_media", 1);
                req.onupgradeneeded = () => {
                    const db = req.result;
                    if (!db.objectStoreNames.contains("media")) db.createObjectStore("media");
                };
                req.onsuccess = () => res(req.result);
                req.onerror = () => rej(req.error);
            } catch (e) { rej(e); }
        });
    }
    return _idbPromise;
}

async function idbSaveMedia(key, blob) {
    try {
        const db = await _idb();
        return await new Promise((res, rej) => {
            const tx = db.transaction("media", "readwrite");
            tx.objectStore("media").put(blob, key);
            tx.oncomplete = () => res(true);
            tx.onerror = () => rej(tx.error);
        });
    } catch (e) { return false; }
}

async function idbGetMedia(key) {
    try {
        const db = await _idb();
        return await new Promise((res) => {
            const tx = db.transaction("media", "readonly");
            const rq = tx.objectStore("media").get(key);
            rq.onsuccess = () => res(rq.result || null);
            rq.onerror = () => res(null);
        });
    } catch (e) { return null; }
}

/** 表情包图片源解析：local: 引用 → IndexedDB dataURL（无图返回 null=占位）；否则服务器资产 URL。 */
async function stickerSrc(file, isServer, apiBase) {
    if (!file) return null;
    if (String(file).startsWith("local:")) {
        const blob = await idbGetMedia(String(file).slice(6));
        if (!blob) return null;
        try { return URL.createObjectURL(blob); } catch (e) { return null; }
    }
    return (isServer ? apiBase : "") + "/assets/" + encodeURI(String(file));
}


/* ── 来源：js/ui_select.js ── */
// 自绘下拉组件（ui_select）：替代原生 <select> 的系统弹窗
// 背景：安卓 WebView 的 <select> 点击弹系统级白色选项弹窗，页面 CSS 管不到（图1 实测）。
// 方案：原生 select 保留为值存储（display:none，所有既有 change 监听/取值代码零改动），
// 视觉层换成暗色「按钮 + 自绘弹层」。外观/键盘/触摸/点外关闭齐全。
// 用法：uiSelectEnhance(root=document) 扫描 select[data-ui] 或调用方指定选择器。

const _UI_SEL_KEY = "data-ui-select-enhanced";

function _closeAllPopups(except) {
    document.querySelectorAll(".ui-select-pop.show").forEach(p => {
        if (p !== except) p.classList.remove("show");
    });
}

// 全局关闭：点外 / Esc / 滚动（弹层跟随是 fixed，滚动时直接关，防错位）
document.addEventListener("pointerdown", e => {
    if (!e.target.closest(".ui-select-pop") && !e.target.closest(".ui-select-btn")) _closeAllPopups();
}, { capture: true });
document.addEventListener("keydown", e => { if (e.key === "Escape") _closeAllPopups(); });
window.addEventListener("scroll", () => _closeAllPopups(), { capture: true, passive: true });

function uiSelectEnhance(root) {
    (root || document).querySelectorAll("select").forEach(sel => {
        if (sel.hasAttribute(_UI_SEL_KEY)) return;
        sel.setAttribute(_UI_SEL_KEY, "1");
        sel.classList.add("ui-select-native");

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ui-select-btn";
        btn.setAttribute("aria-haspopup", "listbox");
        const label = document.createElement("span");
        label.className = "ui-select-label";
        const arrow = document.createElement("span");
        arrow.className = "ui-select-arrow";
        arrow.textContent = "▾";
        btn.append(label, arrow);

        const pop = document.createElement("div");
        pop.className = "ui-select-pop";
        pop.setAttribute("role", "listbox");

        const syncLabel = () => {
            const opt = sel.selectedOptions && sel.selectedOptions[0];
            label.textContent = opt ? opt.textContent : "";
        };
        const rebuild = () => {
            pop.innerHTML = "";
            [...sel.options].forEach(o => {
                const item = document.createElement("button");
                item.type = "button";
                item.className = "ui-select-opt" + (o.value === sel.value ? " on" : "");
                item.setAttribute("role", "option");
                item.textContent = o.textContent;
                item.addEventListener("click", () => {
                    sel.value = o.value;
                    sel.dispatchEvent(new Event("change", { bubbles: true }));
                    syncLabel(); rebuild(); _closeAllPopups();
                });
                pop.appendChild(item);
            });
        };
        btn.addEventListener("click", () => {
            const willOpen = !pop.classList.contains("show");
            _closeAllPopups();
            if (willOpen) {
                rebuild(); syncLabel();
                // 定位：fixed 贴按钮下缘；下方空间不足则翻上
                const r = btn.getBoundingClientRect();
                pop.style.minWidth = r.width + "px";
                pop.style.left = r.left + "px";
                pop.style.top = "";
                pop.style.bottom = "";
                pop.classList.add("show");
                const ph = pop.offsetHeight;
                if (r.bottom + ph + 8 > innerHeight && r.top - ph - 8 > 0) {
                    pop.style.top = (r.top - ph - 6) + "px";
                } else {
                    pop.style.top = (r.bottom + 6) + "px";
                }
            }
        });
        syncLabel();
        sel.insertAdjacentElement("afterend", btn);
        document.body.appendChild(pop);
        // 弹层随按钮销毁（本项目的 select 都是长驻元素，不销毁；防御性留个口）
        sel._uiSelectPopup = pop;
        sel._uiSelectBtn = btn;
        sel._uiSelectSync = () => { syncLabel(); rebuild(); };
    });
}

// 动态重建内容的 select（如 provider-select 会被 _renderProviderSelect 重写 options）：
// 调用方在重写后调 sel._uiSelectSync() 即可（settings.js 已接）。


/* ── 来源：js/imgzip.js ── */
// 图片压缩（发送前）：大图等比缩到单边 ≤1024 并重编码，控制上传/落盘体积。
// compressImage(file) → Promise<{blob, ext, wasCompressed}>
//   - GIF 原样返回（动帧过 canvas 会丢）；
//   - 单边 ≤1024 且 ≤300KB 原样返回（够小不折腾）；
//   - 否则 canvas 等比缩到单边 1024：JPEG / 无透明通道 PNG → image/jpeg q0.85；
//     有透明通道的 PNG 保持 image/png（只缩放，不丢透明）。
// 降级兜底：任何一步失败都原样返回 + console.warn（压缩是优化，不是发送门槛）。
const IMGZIP_MAX_SIDE = 1024;
const IMGZIP_SKIP_BYTES = 300 * 1024;

/** 从 MIME/文件名推扩展名（压缩失败原样返回时用） */
function _imgzipExt(file) {
    const m = /^image\/(png|jpe?g|webp|gif)$/.exec((file && file.type) || "");
    if (m) return m[1] === "jpeg" ? "jpg" : m[1];
    const n = ((file && file.name) || "").split(".").pop().toLowerCase();
    return /^[a-z0-9]{2,5}$/.test(n) ? n : "jpg";
}

/** 抽样检测画布是否含透明像素（每 16 像素采一个，够判透明 PNG） */
function _imgzipHasAlpha(ctx, w, h) {
    try {
        const data = ctx.getImageData(0, 0, w, h).data;
        for (let i = 3; i < data.length; i += 4 * 16) {
            if (data[i] < 255) return true;
        }
    } catch (e) {}
    return false;
}

async function compressImage(file) {
    const fallback = { blob: file, ext: _imgzipExt(file), wasCompressed: false };
    try {
        if (!file || typeof createImageBitmap !== "function") return fallback;
        if (file.type === "image/gif") return fallback;   // GIF 原样
        const bmp = await createImageBitmap(file);
        try {
            const w = bmp.width, h = bmp.height;
            if (!w || !h) return fallback;
            const needResize = Math.max(w, h) > IMGZIP_MAX_SIDE;
            if (!needResize && file.size <= IMGZIP_SKIP_BYTES) return fallback;
            const scale = needResize ? IMGZIP_MAX_SIDE / Math.max(w, h) : 1;
            const cw = Math.max(1, Math.round(w * scale));
            const ch = Math.max(1, Math.round(h * scale));
            const canvas = document.createElement("canvas");
            canvas.width = cw;
            canvas.height = ch;
            const ctx = canvas.getContext("2d");
            // 先画一次取 alpha：有透明才保留 png，否则转 jpeg 时白底重绘
            ctx.drawImage(bmp, 0, 0, cw, ch);
            const keepPng = file.type === "image/png" && _imgzipHasAlpha(ctx, cw, ch);
            if (!keepPng) {
                ctx.fillStyle = "#ffffff";
                ctx.fillRect(0, 0, cw, ch);
                ctx.drawImage(bmp, 0, 0, cw, ch);
            }
            const blob = await new Promise(res => {
                if (keepPng) canvas.toBlob(res, "image/png");
                else canvas.toBlob(res, "image/jpeg", 0.85);
            });
            // toBlob 为空/失败：原样返回（压缩失败不等于发送失败）
            if (!blob) return fallback;
            if (!needResize && !keepPng && blob.size >= file.size) return fallback;
            return { blob, ext: keepPng ? "png" : "jpg", wasCompressed: true };
        } finally {
            try { bmp.close(); } catch (e) {}
        }
    } catch (e) {
        console.warn("compressImage 压缩失败，原样返回", e);
        return fallback;
    }
}


/* ── 来源：js/api.js ── */
// 双模式与请求封装：FIREFLY_MODE / fetch 鉴权注入 / 登录与注册表单

// ═══════════════════════════════════════════
// 双模式（0.8.0）
// ═══════════════════════════════════════════
// FIREFLY_MODE：'local'（默认完全本地）/ 'server'（服务器后端处理）
// 来源：config.js（先于本文件加载）——
//   - 本地（PC 浏览器 / 安卓内置引擎）：app/static/config.js → local
//   - 服务器网页：server/frontend/config.js 定义 FIREFLY_SERVER_BASE → server
//   - 安卓服务器模式（file:// 加载）：壳拦截 config.js 请求动态注入
// 差异点：
// 1. server：登录态 Bearer token（30 天）+ 数据按 user_id 隔离；local：无账号概念
// 2. server：API Key 存本机浏览器（localStorage），每请求带 X-API-Key（服务器不落盘）；
//    local：Key 存本地后端 config.json（POST /set-config）
// 3. server：API 跨域到 FIREFLY_SERVER_BASE（file:// 页面）；local：同源相对请求
// 4. server：relay 引擎代发 LLM + 资产本地化；local：后端 direct 直发
// 5. 检查更新：server 读服务器 version.json；local 走 GitHub/Gitee（后端优先）
const FIREFLY_MODE = window.FIREFLY_MODE || (window.FIREFLY_SERVER_BASE ? "server" : "local");
const IS_SERVER = FIREFLY_MODE === "server";
const API_BASE = IS_SERVER ? (window.FIREFLY_SERVER_BASE || "") : "";
const _serverFetch = window.fetch;

// 服务器模式：账号 + Key 请求头注入（本地模式原样直通，同源无跨域）
window.fetch = function (url, opts) {
    if (!IS_SERVER) return _serverFetch(url, opts);
    opts = opts || {};
    const headers = new Headers(opts.headers || {});
    let k = getLocalApiKey();   // 单点读取：providers 优先 + legacy 兜底（防偶发“每轮需重设 Key”）
    let b = ""; try { b = localStorage.getItem("firefly_api_base") || ""; } catch (e) {}
    let src = ""; try { src = localStorage.getItem("firefly_api_source") || ""; } catch (e) {}
    if (src === "proxy") {
        // 托管模式：不传用户 Key，标记服务器用运营者 Key 直发（OpenCode Go）
        headers.set("X-API-Mode", "proxy");
    } else {
        if (k) headers.set("X-API-Key", k);
        if (b) headers.set("X-API-Base", b);
    }
    // 服务器版账号：登录态带 Bearer token（Key 仍只存本机，token 是账号会话）
    let t = ""; try { t = localStorage.getItem("firefly_token") || ""; } catch (e) {}
    if (t) headers.set("Authorization", "Bearer " + t);
    // 相对路径 → 服务器绝对 URL（本地 file:// 页面无同源相对路径）
    let fullUrl = String(url);
    if (fullUrl.startsWith("/")) fullUrl = API_BASE + fullUrl;
    opts = Object.assign({}, opts, { headers: headers });
    return _serverFetch(fullUrl, opts).then(resp => {
        // 401：登录失效/未登录。仅对用户主动操作（/chat）提示并亮出登录模块；
        // 后台轮询端点（proactive-status/config/history/relay 等）静默——否则
        // 未登录时「请先登录后使用」toast 每 10s 弹一次刷屏。
        if (resp.status === 401 && String(url).indexOf("/chat") >= 0 && !String(url).includes("/auth/")) {
            try { showToast("请先登录后使用"); } catch (e) {}
            try { showAuthModule(); } catch (e) {}
        }
        return resp;
    });
};

// API 来源切换：托管模式隐藏 Key/供应商输入，显示隐私提示
function applyApiSource(isProxy) {
    const ownFields = document.getElementById("api-own-fields");
    const providerField = document.getElementById("provider-field");
    const tip = document.getElementById("api-proxy-tip");
    if (ownFields) ownFields.style.display = isProxy ? "none" : "";
    if (providerField) providerField.style.display = isProxy ? "none" : "";
    if (isProxy) {
        // 托管模式：供应商/K 由服务器运营方指定，前端不管理
        const modelCustom = document.getElementById("model-custom");
        if (modelCustom) modelCustom.style.display = "none";
    }
    if (tip) tip.style.display = isProxy ? "block" : "none";
}

// ═══ 登录状态模块（双模式：服务器版 localStorage token / 本地版后端 auth.json）═══
function showAuthModule() {
    const mod = document.getElementById("auth-module");
    if (mod) mod.style.display = "block";
}
function initAuth() {
    initAuthForms();          // 内联登录/注册/重置表单接线（幂等；本地模式走后端 /auth/* 代理）
    // PC 服务器云端版入口：仅本地版 PC（无安卓壳）显示；安卓由壳自动回落、服务器网页自身不需要
    const serverWebEl = document.getElementById("server-web-entry");
    if (serverWebEl) serverWebEl.style.display = (!IS_SERVER && !window.androidWakeLock) ? "" : "none";
    const loginEntry = document.getElementById("auth-login-entry");
    const userEntry = document.getElementById("auth-user-entry");
    if (IS_SERVER) {
        const token = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
        if (!token) {
            showAuthModule();
            if (loginEntry) loginEntry.style.display = "flex";
            if (userEntry) userEntry.style.display = "none";
            return;
        }
        fetch("/auth/me").then(r => r.json()).then(d => {
            if (d.error) {
                try { localStorage.removeItem("firefly_token"); } catch (e) {}
                if (loginEntry) loginEntry.style.display = "flex";
                if (userEntry) userEntry.style.display = "none";
            } else {
                const emailEl = document.getElementById("auth-email");
                const meta = document.getElementById("auth-meta");
                if (emailEl) emailEl.textContent = "邮箱 " + (d.email || "");
                if (meta) meta.textContent = "注册于 " + (d.created_at || "-").slice(0, 10);
                if (loginEntry) loginEntry.style.display = "none";
                if (userEntry) userEntry.style.display = "flex";
                initAssets();   // 登录态确认：资产本地化（relay 代发前占位符填充用）
            }
            showAuthModule();
        }).catch(() => {});
        return;
    }
    // 本地版（A7）：登录态由本地后端持有（auth.json）；/auth/state 隐式 verify（滚动续期）
    fetch("/auth/state").then(r => r.json()).then(d => {
        showAuthModule();
        if (d.logged_in) {
            const emailEl = document.getElementById("auth-email");
            const meta = document.getElementById("auth-meta");
            if (emailEl) emailEl.textContent = "邮箱 " + (d.email || "");
            if (meta) meta.textContent = d.offline_ok ? "已登录 · 云端同步就绪" : "登录已过期，请重新登录";
            if (loginEntry) loginEntry.style.display = "none";
            if (userEntry) userEntry.style.display = "flex";
            try { window.autoSyncNow && window.autoSyncNow(); } catch (e) {}   // 登录态确认：补一次云端同步（10 分钟节流内自动跳过）
        } else {
            if (loginEntry) loginEntry.style.display = "flex";
            if (userEntry) userEntry.style.display = "none";
        }
    }).catch(() => { /* 本地后端未就绪：静默（首次启动拉服务时） */ });
}
function logout() {
    const t = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
    if (t) fetch("/auth/logout", {method: "POST"}).catch(() => {});
    try { localStorage.removeItem("firefly_token"); } catch (e) {}
    location.reload();
}
window.logout = logout;   // 内联 onclick（账号卡片「退出登录」）

// PC 服务器云端版入口：新标签打开云端网页主界面（config.js 单点地址只存纯 host——
// 安卓壳 loadServerBase 解析第一个 URL，必须无路径；主界面路径在此拼）
function openServerWeb() {
    const base = String(window.FIREFLY_SERVER_WEB || "http://101.200.14.126:8787").replace(/\/+$/, "");
    window.open(base + "/index.html", "_blank", "noopener");
}
window.openServerWeb = openServerWeb;

// ═══ 内联登录/注册/重置表单（0.8.0：单页完成，不跳转 login.html）═══
function toggleAuthForms() {
    const forms = document.getElementById("auth-forms");
    const btn = document.getElementById("auth-toggle-btn");
    if (!forms) return;
    const show = forms.style.display === "none";
    forms.style.display = show ? "block" : "none";
    if (btn) btn.textContent = show ? "收起 ▴" : "登录 / 注册 ▾";
}
window.toggleAuthForms = toggleAuthForms;

function initAuthForms() {
    // A7：本地版同样接线（表单请求走本地后端 /auth/* 代理 → 认证服务器）
    const $ = id => document.getElementById(id);
    const LOGIN = $("loginForm"), REG = $("registerForm"), RESET = $("resetForm");
    if (!LOGIN || !REG || !RESET || LOGIN.dataset.wired) return;
    LOGIN.dataset.wired = "1";

    const showErr = (el, msg) => { el.textContent = msg; el.style.display = "block"; el.classList.remove("green"); };
    const showOk = (el, msg) => { el.textContent = msg; el.style.display = "block"; el.classList.add("green"); };
    const hideErr = el => { el.style.display = "none"; };
    const qqRe = /^[^@\s]+@(qq\.com|foxmail\.com)$/;

    // 安装隐藏代码：首次访问生成，一个安装一个（注册门槛；crypto 随机防预测）
    const getInstallId = () => {
        try {
            let id = localStorage.getItem("firefly_install_id");
            if (!id) {
                const rand = () => {
                    const buf = new Uint32Array(1);
                    crypto.getRandomValues(buf);
                    return buf[0].toString(16).padStart(8, "0");
                };
                id = "inst-" + (rand() + rand() + rand() + rand());
                localStorage.setItem("firefly_install_id", id);
            }
            return id;
        } catch (e) { return ""; }
    };
    const startCountdown = btn => {
        let sec = 60;
        btn.textContent = sec + "s";
        btn.disabled = true;
        const t = setInterval(() => {
            sec--;
            if (sec <= 0) { clearInterval(t); btn.textContent = "获取验证码"; btn.disabled = false; }
            else btn.textContent = sec + "s";
        }, 1000);
    };

    // 表单切换（表单外的链接按钮独立显隐，与 login.html 同语义）
    $("toRegister").onclick = () => { LOGIN.style.display = "none"; REG.style.display = "block"; $("toLogin").style.display = "block"; };
    $("toLogin").onclick = () => { REG.style.display = "none"; LOGIN.style.display = "block"; $("toLogin").style.display = "none"; };
    $("toReset").onclick = () => {
        LOGIN.style.display = "none"; RESET.style.display = "block";
        $("toRegister").style.display = "none"; $("toReset").style.display = "none"; $("toLogin2").style.display = "block";
    };
    $("toLogin2").onclick = () => {
        RESET.style.display = "none"; LOGIN.style.display = "block";
        $("toLogin2").style.display = "none"; $("toRegister").style.display = "block"; $("toReset").style.display = "block";
    };

    // 登录
    LOGIN.onsubmit = e => {
        e.preventDefault(); hideErr($("loginErr"));
        fetch("/auth/login", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: $("loginEmail").value.trim(), password: $("loginPass").value, device: "app"})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                localStorage.setItem("firefly_token", d.token);
                location.reload();
            } else showErr($("loginErr"), d.error || "登录失败");
        }).catch(() => showErr($("loginErr"), "网络错误"));
    };

    // 注册：获取邮箱验证码（60s 倒计时）
    $("sendCodeBtn").onclick = () => {
        const email = $("regEmail").value.trim();
        const btn = $("sendCodeBtn");
        hideErr($("regErr"));
        if (!qqRe.test(email)) { showErr($("regErr"), "请使用 QQ 邮箱"); return; }
        if (btn.disabled) return;
        btn.disabled = true;
        fetch("/auth/mail-send", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: email})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                showOk($("regErr"), "验证码已发送，请查收邮箱");
                startCountdown(btn);
            } else {
                btn.disabled = false;
                showErr($("regErr"), d.error || "发送失败");
            }
        }).catch(() => { btn.disabled = false; showErr($("regErr"), "网络错误"); });
    };

    // 注册提交
    REG.onsubmit = e => {
        e.preventDefault(); hideErr($("regErr"));
        fetch("/auth/register", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: $("regEmail").value.trim(), password: $("regPass").value,
                qq_group: $("regGroup").value.trim(), mail_code: $("regCode").value.trim(),
                install_id: getInstallId()})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                // 注册成功 → 自动切回登录表单并预填邮箱
                REG.style.display = "none"; LOGIN.style.display = "block";
                $("toLogin").style.display = "none";
                $("loginEmail").value = $("regEmail").value.trim();
                $("loginPass").value = "";
                showOk($("loginErr"), "注册成功，请登录");
                $("loginPass").focus();
            } else showErr($("regErr"), d.error || "注册失败");
        }).catch(() => showErr($("regErr"), "网络错误"));
    };

    // 忘记密码：发送重置验证码
    $("rstSendBtn").onclick = () => {
        const email = $("rstEmail").value.trim();
        const btn = $("rstSendBtn");
        hideErr($("rstErr"));
        if (!qqRe.test(email)) { showErr($("rstErr"), "请使用 QQ 邮箱"); return; }
        if (btn.disabled) return;
        btn.disabled = true;
        fetch("/auth/reset-send", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: email})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                showOk($("rstErr"), "验证码已发送（若该邮箱已注册），请查收");
                startCountdown(btn);
            } else {
                btn.disabled = false;
                showErr($("rstErr"), d.error || "发送失败");
            }
        }).catch(() => { btn.disabled = false; showErr($("rstErr"), "网络错误"); });
    };

    // 重置密码提交
    RESET.onsubmit = e => {
        e.preventDefault(); hideErr($("rstErr"));
        fetch("/auth/reset-password", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: $("rstEmail").value.trim(), code: $("rstCode").value.trim(),
                password: $("rstPass").value})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                RESET.style.display = "none"; LOGIN.style.display = "block";
                $("toLogin2").style.display = "none"; $("toRegister").style.display = "block"; $("toReset").style.display = "block";
                $("loginEmail").value = $("rstEmail").value.trim();
                $("loginPass").value = "";
                showOk($("loginErr"), "密码已重置，请用新密码登录");
                $("loginPass").focus();
            } else showErr($("rstErr"), d.error || "重置失败");
        }).catch(() => showErr($("rstErr"), "网络错误"));
    };
}


/* ── 来源：js/panels.js ── */
// 面板族：菜单抽屉 / 各面板开关壳 / 头像选择 / 状态 / 收藏 / 请求记录 / 流程日志 / 用户记忆 / 设定文件 / 手账 / 表情包管理（阶段 2.5 拆分后 = 面板外壳）

// 开拓者头像
const TB_AVATARS = { 穹: "开拓者_穹.png", 星: "开拓者_星.png" };
let tbChoice = localStorage.getItem("tb_avatar") || "穹";

function openAvatarPicker() {
    const picker = document.getElementById("avatar-picker");
    const mask = document.getElementById("avatar-picker-mask");
    picker.style.display = "block";
    mask.style.display = "block";
    // 高亮当前选择
    document.querySelectorAll(".avatar-option").forEach(opt => {
        opt.classList.toggle("selected", opt.dataset.key === tbChoice);
    });
}
function closeAvatarPicker() {
    document.getElementById("avatar-picker").style.display = "none";
    document.getElementById("avatar-picker-mask").style.display = "none";
}
window.closeAvatarPicker = closeAvatarPicker;
document.querySelectorAll(".avatar-option").forEach(opt => {
    opt.addEventListener("click", () => {
        tbChoice = opt.dataset.key;
        localStorage.setItem("tb_avatar", tbChoice);
        document.querySelectorAll(".tb-avatar").forEach(el => { el.src = TB_AVATARS[tbChoice]; });
        closeAvatarPicker();
    });
});

// ═══════════════════════════════════════════
// 汉堡菜单
// ═══════════════════════════════════════════
const menuBtn = document.getElementById("menu-btn");
const menuDrawer = document.getElementById("menu-drawer");
const menuOverlay = document.getElementById("menu-overlay");

menuBtn.addEventListener("click", openMenu);
menuOverlay.addEventListener("click", closeMenu);
function openMenu() {
    menuDrawer.classList.add("open");
    menuOverlay.classList.add("show");
    // 默认 tab 是设定文件（DOM active），无点击事件，需主动加载
    // （2026-09-18：loadCharFiles 已随「用户设定」编辑器一起去掉——它属角色卡管理域）
    loadJournal(); loadUserMemory(); loadArchive();
}
function closeMenu() {
    menuDrawer.classList.remove("open");
    menuOverlay.classList.remove("show");
}
window.closeMenu = closeMenu;

// ═══════════════════════════════════════════
// 设置面板（首页 ⚙ 打开，API 配置独立于此）
// ═══════════════════════════════════════════
const settingsPanel = document.getElementById("settings-panel");
function openSettings() {
    settingsPanel.classList.add("show");
    loadConfig();
    try { loadSnapshots(); } catch (e) {}   // 快照列表（chat.js 同作用域函数）
    try { window.btActivate && window.btActivate("mine"); } catch (e) {}
}
function closeSettings() { settingsPanel.classList.remove("show"); }
window.openSettings = openSettings;
window.closeSettings = closeSettings;

// 反馈面板（首页 ✉ 打开）
const feedbackPanel = document.getElementById("feedback-panel");
function openFeedback() { feedbackPanel.classList.add("show"); }
function closeFeedback() { feedbackPanel.classList.remove("show"); }
window.openFeedback = openFeedback;
window.closeFeedback = closeFeedback;

// 点击 drawer 背景（非内容区域）也关闭菜单
menuDrawer.addEventListener("click", (e) => {
    if (e.target === menuDrawer) closeMenu();
});

// 菜单 tab 切换
document.querySelectorAll(".menu-tab").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".menu-tab").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".menu-content").forEach(c => c.classList.remove("active"));
        btn.classList.add("active");
        const target = document.getElementById("tab-" + btn.dataset.tab);
        if (target) target.classList.add("active");
        if (btn.dataset.tab === "char") { loadJournal(); loadUserMemory(); loadArchive(); }
        if (btn.dataset.tab === "state") loadStateTab();
        if (btn.dataset.tab === "fav") loadFavorites();
        if (btn.dataset.tab === "log") loadRequestLog();
        if (btn.dataset.tab === "pipeline") loadPipeline();
    });
});

// ═══════════════════════════════════════════
// 状态 tab（数值状态系统已下线，接回后再渲染条）
// ═══════════════════════════════════════════


/* ── 来源：js/panels/packs.js ── */
// 角色包面板：表情包管理 + 包详情页（人设文案 / 主动消息 / 专属表情包 / 头像封面）
// 阶段 2.5 自 panels.js 拆出。包详情页的写回一律用 _packViewMode（F-3 代际保护）。



// （A7c 运行模式切换已删除：本地优先 + 失败自动回落服务器——保留此注记防旧代码复活）

// ═══ 角色编辑页（全屏 #pack-view：封面横幅 + 大头像 + 人设文案编辑 + 自建角色删除）═══
const _PACK_PROMPT_LABELS = {
    "core.md": "核心设定（身份/经历/价值观）",
    "identity.md": "人际关系与认知边界",
    "sms_samples.md": "短信风格示例",
    "prompts/polisher.md": "回复器人设（核心文案）",
    "prompts/analyzer_extra.md": "分析器·剧本事实核查补充",
    "prompts/organizer_sticker.md": "组织器·表情包调度提示词",
    "prompts/organizer_narration.md": "组织器·旁白生成提示词",
    "prompts/proactive_context.md": "主动消息·情境文案",
    "prompts/env_suffix.md": "环境句·世界后缀",
};

// F-3（2026-09-13）包详情页的两个捕获量：
// - _packViewMode：本页展示的是哪个包。所有写回一律用它，**不读实时的 CURRENT_MODE** ——
//   原来 A→B 快速切换后，A 页残留的"保存"会把 A 的文案写进 B。
// - _packViewGen：进入本页时的模式代际，await 后模式已切换就丢弃本次结果。
let _packViewMode = "";
let _packViewGen = -1;

async function loadPackView() {
    const mode = CURRENT_MODE;      // 捕获：本次渲染/写回都属于这个包
    const gen = _modeGen;
    _packViewMode = mode;
    _packViewGen = gen;
    let data;
    try {
        const resp = await fetch(`/pack-files?mode=${encodeURIComponent(mode)}`);
        data = await resp.json();
    } catch (e) { showToast("加载失败（网络）"); return; }
    if (gen !== _modeGen) return;   // 模式已切换：丢弃本次结果，防止 A 的内容渲染进 B 的页面
    if (!data || !data.files) return;

    const presLabel = {sticker: "短信+表情包", narration: "短信+旁白", none: "纯短信"}[data.presentation] || data.presentation;
    const t = Date.now();
    const coverEl = document.getElementById("pv-cover");
    // F-5：无封面/头像的包必须清掉旧 src——否则详情页沿用上一个包的图（"看起来还是流萤"）
    if (coverEl) {
        if (data.assets && data.assets.cover) coverEl.src = data.assets.cover + "?t=" + t;
        else coverEl.removeAttribute("src");
    }
    const avatarEl = document.getElementById("pv-avatar");
    if (avatarEl) {
        if (data.assets && data.assets.avatar) avatarEl.src = data.assets.avatar + "?t=" + t;
        else avatarEl.removeAttribute("src");
    }
    document.getElementById("pv-name").textContent = "";
    document.getElementById("pv-scene").textContent = data.name || data.mode;
    document.getElementById("pv-pres").textContent = presLabel;
    document.getElementById("pv-tagline").textContent = "";
    // 角色名/签名从注册表取（经 views.js 的 window 桥——bundle 拼接后 import() 会产生第二份模块实例）
    try {
        const mods = (window.__getPresets && window.__getPresets()) || [];
        const p = mods.find(m => m.id === data.mode) || {};
        document.getElementById("pv-name").textContent = p.char_name || data.name || data.mode;
        document.getElementById("pv-tagline").textContent = p.tagline || "";
    } catch (e) {}
    // 封面/头像编辑按钮绑定
    document.getElementById("pv-cover-edit").onclick = () => _packAssetUpload("cover");
    document.getElementById("pv-avatar-edit").onclick = () => _packAssetUpload("avatar");

    // 主动消息区填值
    const pro = data.proactive || {};
    const setChk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    setChk("pv-pro-enabled", pro.enabled);
    setChk("pv-pro-prob", pro.pro_enabled);
    setChk("pv-pro-hidden", pro.hidden_enabled);
    // 后台主动消息的客户端门控跟随当前角色卡的「隐藏式」开关（服务端不含该判断）
    S._hiddenEnabled = !!pro.hidden_enabled;
    const hardEl = document.getElementById("pv-pro-hard");
    const softEl = document.getElementById("pv-pro-soft");
    if (hardEl) { hardEl.value = pro.hard ?? 6; document.getElementById("pv-pro-hard-v").textContent = hardEl.value; }
    if (softEl) { softEl.value = Math.round((pro.soft ?? 0.35) * 100); document.getElementById("pv-pro-soft-v").textContent = softEl.value + "%"; }

    _loadPackStickers(mode);

    // 用户形象区（05）：称呼 + 用户头像，数据来自 /modes（经 __getPresets 桥）
    try {
        const mods = (window.__getPresets && window.__getPresets()) || [];
        const pm = mods.find(m => m.id === mode) || {};
        const nameInp = document.getElementById("pv-user-name");
        if (nameInp) nameInp.value = pm.user_name || "";
        const uav = document.getElementById("pv-user-avatar");
        if (uav) {
            // 预览回落链：包头像 → 全局内置形象（穹/星）——空 src 会显示破图+alt，必须给兜底
            const tbNow = (() => { try { return localStorage.getItem("tb_avatar") || "穹"; } catch (e) { return "穹"; } })();
            uav.src = pm.user_avatar ? pm.user_avatar + "?t=" + t : TB_AVATARS[tbNow];
            document.querySelectorAll(".pv-id-choice").forEach(x => x.classList.toggle("on", !pm.user_avatar && x.dataset.key === tbNow));
        }
        const resetLink = document.getElementById("pv-user-avatar-reset");
        if (resetLink) resetLink.style.display = pm.user_avatar ? "" : "none";
    } catch (e) {}
    try { loadPackTree(mode); } catch (e) {}   // 角色卡管理树（数据驱动，2026-09-15）

    // 角色形象「恢复默认」—— 2026-09-19 从"危险区"挪到顶部大图旁。
    // 理由：它只是撤销用户自己换的图，**不是危险操作**；而官方包又不能删除，
    // 于是"危险区"对官方包既没内容也没存在理由。形象相关的操作现在全在顶部一处。
    for (const [slot, elId, label] of [["avatar", "pv-asset-reset-avatar", "恢复默认头像"],
                                       ["cover", "pv-asset-reset-cover", "恢复默认封面"]]) {
        const a = document.getElementById(elId);
        if (!a) continue;
        a.onclick = async () => {
            if (!confirm(`${label}？（删除你换的图，回落到角色卡自带的）`)) return;
            try {
                await fetch("/pack-asset/delete", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({mode, slot})});
                showToast("已恢复默认");
                loadPackView();
                try { window.__modesReload && window.__modesReload(); } catch (e) {}
            } catch (e) { showToast("操作失败"); }
        };
    }

    // 危险区（2026-09-19 重排）：
    //   · 「恢复头像默认 / 恢复封面默认」**不是危险操作**，已挪到顶部大图旁（.pv-asset-ops）；
    //   · 官方（内置）包不能归档/删除 ⇒ 这个区对它们**完全不渲染**（见 pack_tree.js 的 danger 分支），
    //     所以这里只在自建包时才会有内容。
    const danger = document.getElementById("pv-danger");
    danger.innerHTML = "";
    if (data.custom) {
        // 活跃包的危险区主按钮 = 「归档」（3.5 一级：不删数据，可反悔）。
        // 「彻底删除」**只在首页归档区**（views.js 的 _renderArchivedPacks）——那是二级操作，
        // 需要输入包名确认，且后端强制先打一份快照。放在这里当主按钮会诱导手滑。
        if ((data.state || "active") === "archived") {
            const note = document.createElement("div");
            note.style.cssText = "font-size:0.8em;color:var(--fg-muted)";
            note.textContent = "该包已归档：请在首页「已归档」区选择恢复或彻底删除。";
            danger.appendChild(note);
        } else {
            const archBtn = document.createElement("button");
            archBtn.className = "pv-danger-btn";
            archBtn.type = "button";
            archBtn.textContent = `归档「${data.name}」（数据保留，可随时恢复）`;
            archBtn.onclick = async () => {
                // 动作实现与首页归档区共用（window.__packLifecycle，见 views.js）
                if (!await window.__packLifecycle("archive", data.mode, data.name || data.mode)) return;
                try { window.closePackView(); } catch (e) {}
                await window.__modesReload();   // 重载后会切到合法包（归档包已不在清单里）
            };
            danger.appendChild(archBtn);
        }
    }

}
window.loadPackView = loadPackView;

// 主动消息区：400ms 防抖自动保存（与设置页同风格）
let _proSaveTimer = null;
function _proScheduleSave() {
    clearTimeout(_proSaveTimer);
    _proSaveTimer = setTimeout(async () => {
        const msg = document.getElementById("pv-pro-msg");
        const payload = {
            // F-3：写回"本页展示的包"，不用实时 CURRENT_MODE（切换后定时器仍会烧到新包上）
            mode: _packViewMode || CURRENT_MODE,
            enabled: document.getElementById("pv-pro-enabled").checked,
            hard: parseInt(document.getElementById("pv-pro-hard").value) || 6,
            soft: (parseInt(document.getElementById("pv-pro-soft").value) || 35) / 100,
            prob_enabled: document.getElementById("pv-pro-prob").checked,
            hidden_enabled: document.getElementById("pv-pro-hidden").checked,
        };
        try {
            const r = await fetch("/pack-config", {method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)});
            const d = await r.json();
            if (msg) msg.textContent = d.ok ? "已保存" : ("保存失败：" + (d.error || ""));
            if (d.ok) S._hiddenEnabled = payload.hidden_enabled;   // 后台主动门控即时跟随
        } catch (e) { if (msg) msg.textContent = "保存失败（网络）"; }
    }, 400);
}
for (const id of ["pv-pro-enabled", "pv-pro-prob", "pv-pro-hidden"]) {
    document.getElementById(id)?.addEventListener("change", _proScheduleSave);
}
document.getElementById("pv-pro-hard")?.addEventListener("input", () => {
    const el = document.getElementById("pv-pro-hard");
    document.getElementById("pv-pro-hard-v").textContent = el.value;
    _proScheduleSave();
});
document.getElementById("pv-pro-soft")?.addEventListener("input", () => {
    const el = document.getElementById("pv-pro-soft");
    document.getElementById("pv-pro-soft-v").textContent = el.value + "%";
    _proScheduleSave();
});

// ═══════════════════════════════════════════
// 详情页表情包区（本包可用 = 全局共享 + 本包专属；专属可增删启停）
// ═══════════════════════════════════════════
async function _loadPackStickers(mode = _packViewMode || CURRENT_MODE) {
    const gen = _modeGen;
    const grid = document.getElementById("pv-stk-grid");
    if (!grid) return;
    grid.innerHTML = "";
    let items = [];
    try {
        const resp = await fetch("/stickers");
        const data = await resp.json();
        items = Array.isArray(data.stickers) ? data.stickers : [];
    } catch (e) { return; }
    if (gen !== _modeGen) return;   // 模式已切换：不把 A 的表情包渲染进 B 的格子
    const usable = items.filter(s => !s.pack || s.pack === mode);
    for (const s of usable) {
        if (gen !== _modeGen) return;
        const cell = document.createElement("div");
        cell.className = "pv-stk-item" + (s.enabled ? "" : " off");
        const img = document.createElement("img");
        try {
            const url = await stickerSrc(s.file, IS_SERVER, API_BASE);
            if (url) img.src = url;
        } catch (e) {}
        if (s.pack === mode) {
            const pk = document.createElement("span");
            pk.className = "pk";
            pk.textContent = "专属";
            cell.appendChild(pk);
        }
        const lb = document.createElement("div");
        lb.className = "lb";
        lb.textContent = s.label || "";
        const ops = document.createElement("div");
        ops.className = "ops";
        const tgl = document.createElement("button");
        tgl.textContent = s.enabled ? "◐" : "○";
        tgl.title = s.enabled ? "停用" : "启用";
        tgl.onclick = async () => {
            try {
                await fetch("/sticker-update", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({id: s.id, enabled: !s.enabled})});
                _loadPackStickers(mode);
            } catch (e) {}
        };
        ops.appendChild(tgl);
        if (s.pack === mode && s.editable) {
            const del = document.createElement("button");
            del.textContent = "×";
            del.title = "删除（仅专属）";
            del.onclick = async () => {
                if (!confirm(`删除表情包「${s.label}」？`)) return;
                try {
                    await fetch("/sticker-delete", {method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({id: s.id})});
                    _loadPackStickers(mode);
                } catch (e) {}
            };
            ops.appendChild(del);
        }
        cell.append(img, lb, ops);
        grid.appendChild(cell);
    }
    if (!usable.length) {
        const empty = document.createElement("div");
        empty.style.cssText = "color:var(--fg-muted);font-size:0.78em;grid-column:1/-1";
        empty.textContent = "还没有表情包——点下方按钮给这个角色添加第一张。";
        grid.appendChild(empty);
    }
}

// 详情页表情包添加（自动归属当前包）
document.getElementById("pv-stk-add")?.addEventListener("click", () => {
    const f = document.getElementById("pv-stk-addform");
    if (f) f.style.display = f.style.display === "none" ? "block" : "none";
});
document.getElementById("pv-stk-submit")?.addEventListener("click", async () => {
    const file = document.getElementById("pv-stk-file").files[0];
    const category = document.getElementById("pv-stk-category").value;
    const label = document.getElementById("pv-stk-label").value.trim();
    const msg = document.getElementById("pv-stk-msg");
    if (!file) { msg.textContent = "请先选择图片"; return; }
    if (!label) { msg.textContent = "请填写含义描述"; return; }
    const fd = new FormData();
    fd.append("file", file);
    fd.append("category", category);
    fd.append("label", label);
    fd.append("mode", _packViewMode || CURRENT_MODE);   // 归属"本页展示的包"（F-3）
    try {
        const resp = await fetch("/add-sticker", {method: "POST", body: fd});
        const data = await resp.json();
        if (data.ok) {
            if (data.local && data.file && file instanceof Blob) {
                try { await idbSaveMedia(String(data.file).slice("local:".length), file); } catch (e) {}
            }
            msg.textContent = "已添加";
            document.getElementById("pv-stk-label").value = "";
            document.getElementById("pv-stk-file").value = "";
            _loadPackStickers(_packViewMode || CURRENT_MODE);
        } else {
            msg.textContent = "失败：" + (data.error || "");
        }
    } catch (e) { msg.textContent = "网络错误"; }
});

// 头像/封面上传（复用图片压缩，选文件后上传为包资产）
function _packAssetUpload(slot) {    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "image/png,image/jpeg,image/webp";
    inp.onchange = async () => {
        const f = inp.files && inp.files[0];
        if (!f) return;
        if (f.size > 5 * 1024 * 1024) { showToast("图片过大（上限 5MB）"); return; }
        try {
            const fd = new FormData();
            fd.append("mode", _packViewMode || CURRENT_MODE);   // 本页展示的包（F-3）
            fd.append("slot", slot);
            fd.append("file", f, f.name || (slot + ".png"));
            const r = await fetch("/pack-asset", {method: "POST", body: fd});
            const d = await r.json();
            if (d.ok) {
                showToast("已替换");
                loadPackView();
                // 封面/头像换了：模式卡片与品牌标识刷新（重拉注册表资产 URL）
                try { window.__modesReload && window.__modesReload(); } catch (e) {}
            } else {
                showToast("上传失败：" + (d.error || ""));
            }
        } catch (e) { showToast("网络错误"); }
    };
    inp.click();
}

// ═══ 用户形象区接线（05：每包独立的用户称呼 + 头像）═══
document.getElementById("pv-user-name-save")?.addEventListener("click", async () => {
    const msg = document.getElementById("pv-user-msg");
    const v = document.getElementById("pv-user-name").value.trim();
    try {
        const r = await fetch("/pack-config", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _packViewMode || CURRENT_MODE, user_name: v})});
        const d = await r.json();
        if (d.ok) {
            if (msg) msg.textContent = "已保存（称呼：" + (d.user_name || "默认") + "）";
            try { window.__modesReload && window.__modesReload(); } catch (e) {}   // /modes 携带新称呼
        } else if (msg) msg.textContent = "保存失败：" + (d.error || "");
    } catch (e) { if (msg) msg.textContent = "网络错误"; }
});
document.getElementById("pv-user-avatar-edit")?.addEventListener("click", () => _packAssetUpload("user_avatar"));
document.getElementById("pv-user-avatar-reset")?.addEventListener("click", async () => {
    if (!confirm("恢复默认用户头像？（删除你上传的头像，回落到内置形象）")) return;
    try {
        const r = await fetch("/pack-asset/delete", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _packViewMode || CURRENT_MODE, slot: "user_avatar"})});
        const d = await r.json();
        if (d.ok) { showToast("已恢复默认"); loadPackView(); try { window.__modesReload && window.__modesReload(); } catch (e) {} }
        else showToast(d.error || "操作失败");
    } catch (e) { showToast("网络错误"); }
});

// 用户形象区：内置形象选择（穹/星，无包自定义头像时生效；复用全局 TB 选择存储）
document.querySelectorAll(".pv-id-choice").forEach(el => {
    el.addEventListener("click", () => {
        try {
            localStorage.setItem("tb_avatar", el.dataset.key);
            document.querySelectorAll(".tb-avatar").forEach(x => { x.src = TB_AVATARS[el.dataset.key]; });
        } catch (e) {}
        loadPackView();   // 预览刷新（无包头像时显示内置选择）
    });
});


/* ── 来源：js/panels/data.js ── */
// 资料面板：状态 / 收藏 / 用户记忆 / 设定文件 / 手账（阶段 2.5 自 panels.js 拆出）
// 只注册监听 + 挂 window.*（菜单 tab 切换在外壳 panels.js 里按 tab 调用这些 loader）。


function loadStateTab() {
    const list = document.getElementById("state-list");
    if (list) {
        list.innerHTML = '<div style="color:#8a8a8a;line-height:1.6">状态系统尚未接入。<br>当前流水线：检索 → 分析 → 回复 → 表情包。</div>';
    }
}

// ═══════════════════════════════════════════
// 收藏（长按消息 → 收藏；菜单 → 收藏 查看）
// ═══════════════════════════════════════════
async function loadFavorites() {
    const list = document.getElementById("fav-list");
    const count = document.getElementById("fav-count");
    if (!list) return;
    try {
        const resp = await fetch(`/favorites?mode=${encodeURIComponent(CURRENT_MODE)}`);
        const data = await resp.json();
        const items = Array.isArray(data.items) ? data.items : [];
        if (count) count.textContent = `收藏 ${items.length} 条`;
        if (!items.length) {
            list.innerHTML = `<div class="fav-empty">还没有收藏。<br>在聊天页长按一条消息，点「收藏」即可保存到这里。</div>`;
            return;
        }
        list.innerHTML = items.map(f => {
            const body = f.type === "sticker"
                ? "[表情包：" + escapeHtml(f.label || "") + "]"
                : f.type === "narration"
                    ? escapeHtml(f.text || "")
                    : escapeHtml(f.content || "");
            const who = f.who === "user" ? "我" : escapeHtml(charName());
            return `<div class="fav-item">
                <div class="fav-head"><span class="fav-who">${who}</span><span class="fav-time">${escapeHtml((f.time || "").slice(5, 16))}</span></div>
                <div class="fav-body">${body}</div>
                <button class="fav-del" type="button" data-id="${escapeHtml(String(f.id))}">删除</button>
            </div>`;
        }).join("");
        list.querySelectorAll(".fav-del").forEach(btn => {
            btn.addEventListener("click", async () => {
                try {
                    const resp = await fetch("/favorites/delete", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({mode: CURRENT_MODE, id: btn.dataset.id}),
                    });
                    const d = await resp.json();
                    if (d.ok) loadFavorites();
                    else showToast("删除失败：" + (d.error || ""));
                } catch (e) { showToast("删除失败，请重试"); }
            });
        });
    } catch (e) {
        if (count) count.textContent = "读取失败";
        list.innerHTML = `<div class="fav-empty">收藏读取失败（服务器模式需先登录；本地后端未就绪时也会这样）</div>`;
    }
}
window.loadFavorites = loadFavorites;

async function loadUserMemory() {
    const editor = document.getElementById("user-memory-editor");
    const msg = document.getElementById("user-memory-msg");
    if (!editor) return;
    try {
        const resp = await fetch(`/user-memory?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        editor.value = data.content || "";
        if (msg) msg.textContent = data.content ? `${data.content.length} 字` : "空";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}

document.getElementById("user-memory-save").addEventListener("click", async () => {
    const editor = document.getElementById("user-memory-editor");
    const msg = document.getElementById("user-memory-msg");
    msg.textContent = "保存中…";
    try {
        const resp = await fetch("/save-user-memory", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content: editor.value, mode: CURRENT_MODE}),
        });
        const data = await resp.json();
        msg.textContent = data.ok ? "✓ 已保存（下次对话生效）" : "失败：" + (data.error || "未知");
    } catch (e) { msg.textContent = "网络错误"; }
});
document.getElementById("user-memory-reload").addEventListener("click", loadUserMemory);

// ── 历史对话存档（整理后的原文；只进检索器）────────────────────────
// 用户 2026-09-18 口径：整理搬走的那段**原文**要看得见、改得动，
// 也能手动让它「AI 压缩」进「用户记忆」。存/读都走 /archive 与 /memory-action。
//
// （顺带说明：「用户设定」编辑器已从本页移除 —— 它是**用户手写的设定**、不是聊天产物，
//   与「清除历史会清空本页」的语义冲突；现统一在「角色卡管理 → 人设与口吻」编辑。）
async function loadArchive(month) {
    const sel = document.getElementById("archive-month");
    const editor = document.getElementById("archive-editor");
    const msg = document.getElementById("archive-msg");
    if (!editor) return;
    const m = month || (sel && sel.value) || "";
    try {
        const url = `/archive?mode=${encodeURIComponent(CURRENT_MODE)}` + (m ? `&month=${encodeURIComponent(m)}` : "");
        const data = await (await fetch(url)).json();
        if (sel) {
            const months = data.months || [];
            const keep = months.includes(m) ? m : (months[0] || "");
            sel.innerHTML = months.map(x => `<option value="${x}">${x}</option>`).join("");
            sel.value = keep;
            // 自绘下拉：options 被重写后要让它重读（否则弹层还是旧的）
            try {
                if (sel._uiSelectSync) sel._uiSelectSync();
                else uiSelectEnhance(document.getElementById("tab-char"));
            } catch (e) {}
        }
        editor.value = data.content || "";
        if (msg) msg.textContent = data.content ? `${data.content.length} 字` : "（还没有存档）";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}

async function _archiveAction(action, extra) {
    const msg = document.getElementById("archive-msg");
    if (msg) msg.textContent = action === "compress" ? "压缩中…（要调一次模型，稍等）" : "保存中…";
    try {
        const resp = await fetch("/memory-action", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(Object.assign({action: action, mode: CURRENT_MODE}, extra || {})),
        });
        const data = await resp.json();
        if (!data.ok) { if (msg) msg.textContent = "失败：" + (data.error || "未知"); return; }
        if (action === "compress") {
            if (msg) msg.textContent = `✓ 已压缩进「用户记忆」（新增 ${data.added || 0} 条，头部 ${data.head_chars || 0} 字）`;
            if (typeof loadUserMemory === "function") loadUserMemory();   // 摘要变了，顺手刷新上面那块
        } else if (msg) {
            msg.textContent = "✓ 已保存（检索器下次就用改后的原文）";
        }
    } catch (e) { if (msg) msg.textContent = "网络错误"; }
}

(function _wireArchive() {
    const sel = document.getElementById("archive-month");
    const save = document.getElementById("archive-save");
    const reload = document.getElementById("archive-reload");
    const comp = document.getElementById("archive-compress");
    if (save) save.addEventListener("click", () => {
        const editor = document.getElementById("archive-editor");
        _archiveAction("save_archive", {
            month: (sel && sel.value) || "",
            content: editor ? editor.value : "",
        });
    });
    if (reload) reload.addEventListener("click", () => loadArchive());
    if (sel) sel.addEventListener("change", () => loadArchive(sel.value));
    if (comp) comp.addEventListener("click", () => {
        const month = (sel && sel.value) || "";
        if (!month) { const m = document.getElementById("archive-msg"); if (m) m.textContent = "还没有存档可压缩"; return; }
        if (!confirm(`把 ${month} 的存档原文压缩进「用户记忆」？\n（原文会保留，可以反复压；压缩会调用一次模型）`)) return;
        _archiveAction("compress", {month: month});
    });
})();
window.loadArchive = loadArchive;

// 手账
async function loadJournal() {
    const editor = document.getElementById("journal-editor");
    const msg = document.getElementById("journal-msg");
    try {
        const resp = await fetch(`/journal?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (editor) editor.value = data.content || "";
        msg.textContent = data.content ? `${data.content.length} 字` : "空";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}
document.getElementById("journal-reload").addEventListener("click", loadJournal);
document.getElementById("journal-save").addEventListener("click", async () => {
    const content = document.getElementById("journal-editor").value;
    const msg = document.getElementById("journal-msg");
    msg.textContent = "保存中…";
    try {
        const resp = await fetch("/save-journal", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content, mode: CURRENT_MODE}),
        });
        const data = await resp.json();
        msg.textContent = data.ok ? `✓ 已保存（${content.length} 字）` : "失败";
    } catch (e) { msg.textContent = "网络错误"; }
});


/* ── 来源：js/panels/debug.js ── */
// 调试面板：请求记录 / 流水线日志（阶段 2.5 自 panels.js 拆出）


// 调试面板里的用户称呼随当前角色包（F-6.x 残留：曾硬编码「开拓者」，自建包下称呼错误）
// 2026-09-18：收敛到 views.userName()，且**不再回落字面量**（取不到就留空）。
function _userName() {
    return userName();
}

// ═══════════════════════════════════════════
// 请求记录
// ═══════════════════════════════════════════

async function loadRequestLog() {
    const list = document.getElementById("log-list");
    const countEl = document.getElementById("log-count");
    if (!list) return;
    try {
        const resp = await fetch("/requests");
        const data = await resp.json();
        if (countEl) {
            const totalCost = (data.requests || []).reduce((s, r) => s + (Number(r.cost_cny) || 0), 0);
            countEl.textContent = totalCost > 0
                ? `共 ${data.count} 次请求 · 累计约 ¥${totalCost.toFixed(3)}`
                : `共 ${data.count} 次请求`;
        }
        const rows = (data.requests || []).slice().reverse();
        if (rows.length === 0) {
            list.innerHTML = '<div style="color:#8a8a8a;padding:10px">暂无记录</div>';
            return;
        }
        // A6（审计 2026-09-15）：r.module / r.model / r.time 原先未转义直插 innerHTML
        // （model 来自上游响应/自定义模型名，注入面真实）——已套 _esc，与本文件其余字段一致
        list.innerHTML = rows.map(r => `
        <div style="display:flex;align-items:center;gap:4px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.8em;color:#c8d0e0">
            <span style="flex-shrink:0;width:50px;color:#8a8a8a">${_esc(r.time || "?")}</span>
            <span style="flex-shrink:0;width:64px">${_esc(r.module)}</span>
            <span style="flex-shrink:0;width:52px">${_esc(r.model || "?")}</span>
            <span style="flex-shrink:0;width:22px;text-align:center">${r.success ? '<span style="color:#6c8">✓</span>' : '<span style="color:#c66">✗</span>'}</span>
            <span style="flex:1;text-align:right">${r.total_tokens || 0}</span>
            <span style="flex-shrink:0;width:70px;text-align:right;color:#8a8a8a">¥${(r.cost_cny || 0).toFixed(6)}</span>
        </div>`).join("");
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;padding:10px">加载失败</div>';
    }
}
window.loadRequestLog = loadRequestLog;

// ═══════════════════════════════════════════
// 流程日志：每轮各阶段的输入/输出/思考过程
// ═══════════════════════════════════════════

function _stageBlock(title, elapsed, fields) {
    const rows = fields
        .filter(([, v]) => v != null && String(v).trim() !== "")
        .map(([k, v]) => {
            const body = _esc(typeof v === "string" ? v : JSON.stringify(v, null, 1));
            if (k === "思考过程") {
                return `<details style="margin:2px 0"><summary style="cursor:pointer;color:#8a8a8a">思考过程（点开）</summary><pre style="white-space:pre-wrap;word-break:break-all;color:#8a8a8a;margin:4px 0;font-size:0.95em">${body}</pre></details>`;
            }
            return `<div style="margin:2px 0"><span style="color:#8a8a8a">${k}:</span> <span style="white-space:pre-wrap;word-break:break-all">${body}</span></div>`;
        }).join("");
    return `<details open style="margin:4px 0;padding:4px 8px;background:rgba(255,255,255,0.03);border-radius:6px">
        <summary style="cursor:pointer;color:#c8d0e0">${title}${elapsed != null ? ` <span style="color:#8a8a8a;font-size:0.85em">${elapsed}s</span>` : ""}</summary>
        <div style="padding:4px 0 2px">${rows}</div></details>`;
}

async function loadPipeline() {
    const list = document.getElementById("pipeline-list");
    const countEl = document.getElementById("pipeline-count");
    if (!list) return;
    try {
        const resp = await fetch(`/pipeline?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (countEl) countEl.textContent = `最近 ${data.count} 轮`;
        const rows = (data.pipeline || []).slice().reverse();
        if (rows.length === 0) {
            list.innerHTML = '<div style="color:#8a8a8a;padding:10px">暂无记录（本次启动后还没聊过）</div>';
            return;
        }
        list.innerHTML = rows.map(p => {
            let inner = "";
            if (p.error) {
                inner = `<div style="color:#c66;padding:4px 0">流水线异常: ${_esc(p.error)}</div>`;
            } else {
                const a = p.analyzer || {}, o = p.organizer || {}, po = p.polisher || {}, rt = p.retriever || {};
                inner =
                    _stageBlock("⓪ 知识检索", rt.elapsed, [
                        ["摘要", rt.knowledge],
                    ]) +
                    _stageBlock("① 分析器", a.elapsed, [
                        ["意图", a.intent],
                        ["事实核查", (a.fact_check || []).length ? a.fact_check : ""],
                        ["摘要", a.summary],
                        ["原始输出", a.raw_json],
                        ["思考过程", a.reasoning],
                    ]) +
                    _stageBlock("② 回复器", po.elapsed, [
                        ["原始输出", po.raw],
                        ["思考过程", po.reasoning],
                    ]) +
                    _stageBlock("③ 工具调度（表情包）", o.elapsed, [
                        ["选图", o.sticker_label || "（不发）"],
                        ["原始输出", o.raw],
                        ["思考过程", o.reasoning],
                    ]);
            }
            return `<div style="margin-bottom:14px;padding:8px;border:1px solid rgba(255,255,255,0.08);border-radius:8px;font-size:0.8em;color:#c8d0e0">
                <div style="margin-bottom:4px"><span style="color:#8a8a8a">${_esc(p.time || "?")}</span> ${_esc(_userName())}: <span style="color:#e0d5c1">${_esc(p.user_input)}</span>${p.hint ? ` <span style="color:#8a8a8a">(hint:${_esc(p.hint)})</span>` : ""}</div>
                ${inner}
            </div>`;
        }).join("");
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;padding:10px">加载失败</div>';
    }
}
window.loadPipeline = loadPipeline;

// ═══════════════════════════════════════════
// 用户记忆（= memory.md，休息时自动整理的过往摘要）/ 用户设定（补充设定）
// ═══════════════════════════════════════════


/* ── 来源：js/panels/stickers.js ── */
// 表情包管理（菜单抽屉「表情包」页签：添加 / 映射表 / 启停 / 删除）
// 阶段 B（2026-09-15）自 panels/packs.js 拆出：这段属菜单域，与角色包详情页无关。

// ═══════════════════════════════════════════
// 表情包管理
// ═══════════════════════════════════════════
const stickerAddBtn = document.getElementById("sticker-add-btn");
const stickerAddForm = document.getElementById("sticker-add-form");
if (stickerAddBtn) stickerAddBtn.addEventListener("click", () => {
    stickerAddForm.style.display = stickerAddForm.style.display === "none" ? "flex" : "none";
});

document.getElementById("sticker-submit").addEventListener("click", async () => {
    const file = document.getElementById("sticker-file").files[0];
    const category = document.getElementById("sticker-category").value;
    const label = document.getElementById("sticker-label").value.trim();
    const msg = document.getElementById("sticker-add-msg");
    if (!file) { msg.textContent = "请先选择图片"; return; }
    if (!label) { msg.textContent = "请填写含义描述"; return; }
    const fd = new FormData(); fd.append("file", file); fd.append("category", category); fd.append("label", label);
    fd.append("mode", CURRENT_MODE);   // 归属当前包（阶段6）
    try {
        const resp = await fetch("/add-sticker", { method: "POST", body: fd });
        const data = await resp.json();
        if (data.ok) {
            // A2 媒体本地策略（服务器版）：图片本体存本机 IndexedDB（key=内容哈希），
            // 服务器只保留文字元数据（label/category/哈希）；上传后立即本地化
            if (data.local && data.file && file instanceof Blob) {
                await idbSaveMedia(String(data.file).slice("local:".length), file);
                msg.textContent = "已添加：" + data.label + "（图片仅存本机）";
            } else {
                msg.textContent = "已添加：" + data.label;
            }
            document.getElementById("sticker-file").value = "";
            document.getElementById("sticker-label").value = "";
            loadStickerList();
        } else msg.textContent = "失败：" + (data.error || "未知");
    } catch(e) { msg.textContent = "网络错误"; }
});

document.getElementById("sticker-manage-btn").addEventListener("click", () => {
    const panel = document.getElementById("sticker-manage-panel");
    panel.style.display = panel.style.display === "none" ? "flex" : "none";
    if (panel.style.display !== "none") loadStickerList();
});

async function loadStickerList() {
    const msg = document.getElementById("sticker-manage-msg");
    const list = document.getElementById("sticker-list");
    msg.textContent = "加载中…";
    try {
        const resp = await fetch("/stickers");
        const data = await resp.json();
        const stickers = data.stickers || [];
        msg.textContent = `共 ${stickers.length} 个`;
        // A2：缩略图异步解析（local: 引用 → IndexedDB；无图显示占位块）
        const rows = await Promise.all(stickers.map(async s => {
            const src = await stickerSrc(s.file, IS_SERVER, API_BASE);
            const thumb = src
                ? `<img class="stk-thumb" src="${escapeHtml(src)}" loading="lazy" onerror="this.style.opacity=0.2">`
                : `<div class="stk-thumb" style="display:flex;align-items:center;justify-content:center;opacity:0.35;font-size:0.6em">无图</div>`;
            return `
        <div class="sticker-row" data-id="${escapeHtml(s.id)}">
            <div class="stk-head">
                ${thumb}
                <button class="stk-toggle ${s.enabled ? "on" : ""}" data-on="${s.enabled ? "1" : ""}" ${(s.editable || s.is_default) ? "" : "disabled"}>${s.enabled ? "启用中" : "已停用"}</button>
            </div>${s.pack ? `<div style="font-size:0.62em;color:var(--fg-accent);margin-top:2px">专属：${escapeHtml(s.pack)}</div>` : ""}
            <div class="stk-main">
                <select class="stk-cat-sel" ${(s.editable || s.is_default) ? "" : "disabled"}>
                    <option value="可爱" ${s.category==="可爱"?"selected":""}>可爱</option>
                    <option value="帅气" ${s.category==="帅气"?"selected":""}>帅气</option>
                </select>
                <input class="stk-label-input" type="text" value="${escapeHtml(s.label)}" maxlength="120" ${(s.editable || s.is_default) ? "" : "readonly"}>
                <div class="stk-actions">
                    <button class="stk-save" disabled>保存</button>
                    <button class="stk-del" ${(s.is_default || !s.editable) ? "disabled" : ""}>删</button>
                </div>
            </div>
        </div>`;
        }));
        list.innerHTML = rows.join("");
        try { uiSelectEnhance(list); } catch (e) {}   // 动态生成的分类下拉也走自绘（守卫幂等）
        list.querySelectorAll(".sticker-row").forEach(row => {
            const id = row.dataset.id;
            const inp = row.querySelector(".stk-label-input");
            const cat = row.querySelector(".stk-cat-sel");
            const save = row.querySelector(".stk-save");
            const del = row.querySelector(".stk-del");
            const toggle = row.querySelector(".stk-toggle");
            const origLabel = inp.value;
            const origCat = cat.value;

            function checkChanged() {
                save.disabled = (inp.value.trim() === origLabel && cat.value === origCat) || (!inp.value.trim() && !cat.value);
            }
            inp.addEventListener("input", checkChanged);
            cat.addEventListener("change", checkChanged);

            toggle.addEventListener("click", async () => {
                const next = toggle.dataset.on !== "1";
                toggle.disabled = true;
                try {
                    const r = await fetch("/sticker-update", {
                        method:"POST",
                        headers:{"Content-Type":"application/json"},
                        body:JSON.stringify({id, enabled: next}),
                    });
                    const d = await r.json();
                    if (d.ok) {
                        toggle.dataset.on = next ? "1" : "";
                        toggle.classList.toggle("on", next);
                        toggle.textContent = next ? "启用中" : "已停用";
                        msg.textContent = next ? "已启用：" + d.label : "已停用：" + d.label;
                    }
                } catch(e) {}
                toggle.disabled = false;
            });

            save.addEventListener("click", async () => {
                const label = inp.value.trim();
                const category = cat.value;
                try {
                    const r = await fetch("/sticker-update", {
                        method:"POST",
                        headers:{"Content-Type":"application/json"},
                        body:JSON.stringify({id, label: label || undefined, category}),
                    });
                    const d = await r.json();
                    if (d.ok) {
                        inp.value = d.label;
                        cat.value = d.category;
                        save.textContent="已存"; save.disabled=true;
                        msg.textContent="已更新："+d.label;
                    }
                } catch(e) {}
            });
            del.addEventListener("click", async () => {
                if (!confirm("确认删除？")) return;
                try {
                    await fetch("/sticker-delete", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id}) });
                    row.remove();
                } catch(e) {}
            });
        });
    } catch(e) { msg.textContent = "加载失败"; }
}


/* ── 来源：js/panels/pack_assist.js ── */
// 逐文件 AI 辅助抽屉（阶段 E）：✨ 图标 → 与 AI 对话 → diff 提案 → 应用才写盘
// AI 永不直接写盘；提案经后端静态校验 + 备份 + 原子写（见 modules/pack_assist.py 边界表）。

let _av = { mode: "", file: "", history: [], pending: null };

function openAssist(mode, file, label) {
    _av = { mode: mode || CURRENT_MODE, file, history: [], pending: null };
    const v = document.getElementById("assist-view");
    if (!v) return;
    document.getElementById("av-title").textContent = `✨ AI 辅助 · ${label || file}`;
    document.getElementById("av-chat").innerHTML =
        `<div class="fix-hist">说说想把这个文件改成什么样，AI 会给出具体修改提案（应用前一定会给你看 diff）。</div>`;
    document.getElementById("av-proposal").style.display = "none";
    document.getElementById("av-input").value = "";
    v.style.display = "flex";
    setTimeout(() => document.getElementById("av-input").focus(), 200);
}
window.__openAssist = openAssist;

function closeAssist() {
    const v = document.getElementById("assist-view");
    if (v) v.style.display = "none";
}
window.closeAssist = closeAssist;

function _avMsg(who, text) {
    const chat = document.getElementById("av-chat");
    chat.insertAdjacentHTML("beforeend",
        `<div class="fix-msg ${who === "user" ? "me" : "ai"}"><div class="fix-who">${who === "user" ? "我" : "AI"}</div><div class="fix-text">${escapeHtml(text)}</div></div>`);
    chat.scrollTop = chat.scrollHeight;
}

function _renderProposal(changes) {
    const box = document.getElementById("av-proposal");
    box.style.display = "block";
    box.innerHTML = `<div class="fix-proposal-title"><span>修改提案（尚未生效）</span><span class="fix-op-tag">待确认</span></div>`
        + changes.map(c => `<div class="fix-change"><div class="fix-change-head">
            <span class="fix-op-tag ${c.op === "append" ? "add" : "fix"}">${c.op === "append" ? "补充" : "纠正"}</span></div>
            ${c.op === "replace" ? `<div class="fix-diff-old">− ${escapeHtml(c.old || "")}</div>` : ""}
            <div class="fix-diff-new">+ ${escapeHtml(c.new || "")}</div>
            <div class="fix-reason">${escapeHtml(c.reason || "")}</div></div>`).join("")
        + `<div class="fix-proposal-actions">
             <button class="hb-btn" type="button" id="av-discard">放弃</button>
             <button class="hb-btn primary" type="button" id="av-apply">应用修改</button>
           </div>`;
    document.getElementById("av-discard").onclick = () => {
        _av.pending = null;
        box.style.display = "none";
        _avMsg("ai", "已放弃这次提案。继续说想怎么改就行。");
    };
    document.getElementById("av-apply").onclick = _applyPending;
}

async function _applyPending() {
    if (!_av.pending) return;
    const changes = _av.pending;
    try {
        const r = await fetch("/pack-assist/apply", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _av.mode, file: _av.file, changes})});
        const d = await r.json();
        if (d.ok) {
            showToast("已应用（写入前有自动备份）");
            document.getElementById("av-proposal").style.display = "none";
            _av.pending = null;
            _avMsg("ai", "已写入并生效。还要改别的地方吗？");
            // 让背后的编辑页刷新（详情页/知识库列表/记忆区各自的重载函数都在）
            try { window.loadPackView && window.loadPackView(); } catch (e) {}
        } else {
            _avMsg("ai", "校验没通过：" + (d.error || ""));
        }
    } catch (e) { showToast("网络错误"); }
}

async function _avSend() {
    const inp = document.getElementById("av-input");
    const text = (inp.value || "").trim();
    if (!text) return;
    inp.value = "";
    _avMsg("user", text);
    _av.history.push({who: "user", text});
    _avMsg("ai", "（正在想…）");
    try {
        const r = await fetch("/pack-assist", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _av.mode, file: _av.file, message: text, history: _av.history})});
        const d = await r.json();
        // 摘掉"正在想"
        const chat = document.getElementById("av-chat");
        const last = chat.querySelectorAll(".fix-msg.ai");
        if (last.length) last[last.length - 1].remove();
        if (d.need_key) { _avMsg("ai", "需要先去 ⚙ 设置里填 API Key。"); return; }
        if (!d.ok) { _avMsg("ai", "出了点问题：" + (d.error || "")); return; }
        _av.history.push({who: "ai", text: d.reply});
        _avMsg("ai", (d.searched ? "🌐 已联网检索官方资料\n\n" : "") + d.reply);
        if (Array.isArray(d.changes) && d.changes.length) {
            _av.pending = d.changes;
            _renderProposal(d.changes);
        }
    } catch (e) {
        const chat = document.getElementById("av-chat");
        const last = chat.querySelectorAll(".fix-msg.ai");
        if (last.length) last[last.length - 1].remove();
        _avMsg("ai", "网络错误，稍后再试");
    }
}

document.getElementById("av-send")?.addEventListener("click", _avSend);
document.getElementById("av-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); _avSend(); }
});
document.getElementById("av-back")?.addEventListener("click", closeAssist);


/* ── 来源：js/panels/pack_tree.js ── */
// 角色卡管理树（数据驱动 · 2026-09-15）
// 数据源 = GET /pack-structure（core/pack_structure.py 的声明式规范）——
// 本模块只按返回的域/来源渲染树：可收起可展开，文件点击**就地展开**编辑（不跳页底）。
// 业务结构由后端规范决定；新域/新槽位出现后，本模块自动跟随，无需改代码。

let _treeMode = "";

async function loadPackTree(mode) {
    _treeMode = mode || CURRENT_MODE;
    const box = document.getElementById("pv-tree");
    if (!box) return;
    try {
        const resp = await fetch(`/pack-structure?mode=${encodeURIComponent(_treeMode)}`);
        const data = await resp.json();
        if (!data.ok) { box.innerHTML = "结构读取失败"; return; }
        _render(box, data);
    } catch (e) {
        box.innerHTML = "结构读取失败（网络）";
    }
}

function _render(box, data) {
    _parkEmbeds();   // 先把嵌入块泊出——innerHTML 清空会销毁树内一切（含事件接线）
    box.innerHTML = "";
    _domainLabels = {};
    for (const d of data.domains) _domainLabels[d.key] = d.label;
    for (const d of data.domains) {
        // ★ 2026-09-19：两个域整体不渲染（用户真机反馈）——
        //   · `assets`（形象资产）：功能早已挪到顶部大图，这个域只剩一句说明，是空壳；
        //   · `danger`（危险区）：官方包不能归档/删除 ⇒ 没有任何危险操作；而原来给官方包
        //     塞的"恢复头像/封面默认"根本不是危险操作（已挪到顶部大图旁）。
        //   两者都该"没有内容就没有这个区"，而不是留一个空的折叠条。
        if (d.key === "assets") continue;
        if (d.key === "danger" && !data.custom) continue;
        const det = document.createElement("details");
        det.className = "pt-domain";
        det.dataset.key = d.key;
        const sum = document.createElement("summary");
        sum.innerHTML = `<b>${escapeHtml(d.label)}</b> <span class="pt-desc">${escapeHtml(d.desc || "")}</span>`;
        det.appendChild(sum);
        for (const s of d.sources) {
            // 节点可能"故意不渲染"（见 _renderSource 的 assets / 官方包 danger 分支）——
            // appendChild(null) 会抛错，所以这里必须判空。
            const node = _renderSource(d, s, data);
            if (node) det.appendChild(node);
        }
        box.appendChild(det);
    }
}

// ── 嵌入块车位 ──
// 树重渲 = #pv-tree innerHTML 清空。被 _moveInto 搬进树的既有区块（用户形象/主动消息/
// 表情包/危险区）若不清空前泊出，会随清空被销毁：二次渲染后这些域全空，
// 且模块级事件接线（防抖保存/上传/称呼保存）随元素死亡。车位 = display:none 的隐藏容器，
// 泊入其中元素存活、getElementById 可达，渲染后再搬回新节点。
const _EMBED_GETTERS = {
    identity: () => document.getElementById("pv-identity"),
    config: () => document.querySelector(".pv-proactive"),
    stickers: () => document.querySelector(".pv-stickers"),
    danger: () => document.getElementById("pv-danger"),
};
let _embedParking = null;
let _domainLabels = {};

function _parkEmbeds() {
    if (!_embedParking) {
        _embedParking = document.createElement("div");
        _embedParking.style.display = "none";
        document.body.appendChild(_embedParking);
    }
    for (const get of Object.values(_EMBED_GETTERS)) {
        const el = get();
        if (el) _embedParking.appendChild(el);
    }
}

function _renderSource(domain, s, data) {
    const t = s.type;
    if (t === "slot" || t === "file") return _fileNode(s, data);
    if (t === "dir") return _kbNode(s);
    if (t === "sys_prompt") return _sysPromptNode(s);
    if (t === "ref") return _refNode(s);
    // ★ 2026-09-19：`assets` 节点不再渲染 —— 它只剩一句"去顶部大图换图"的说明，
    //   留一个空折叠区纯属噪音（用户："形象自从在上面改就不要在下面放个空的"）。
    //   角色形象的所有操作（换封面/换头像/恢复默认）现在都在顶部大图区。
    if (t === "assets") return null;
    // ★ 2026-09-19：危险区**只对自建包**渲染。
    //   · 官方（内置）包不能归档、不能删除 ⇒ 里面没有任何"危险"操作；
    //   · 原来给官方包塞的"恢复头像/封面默认"根本不是危险操作，已挪到顶部大图旁。
    //   两个理由都指向同一结论：官方包不该有这个区（用户："官方角色卡删不了就别放危险区"）。
    if (t === "danger") return (data && data.custom) ? _moveInto(t) : null;
    if (t === "identity" || t === "config" || t === "stickers") return _moveInto(t);
    const ph = document.createElement("div");
    ph.className = "pt-src";
    return ph;
}

// 控件型来源：把既有工作区块搬入树节点（不重写功能，只换归属；元素来自车位或初始位置）
function _moveInto(type) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src pt-embed";
    const get = _EMBED_GETTERS[type];
    const el = get ? get() : null;
    if (el) wrap.appendChild(el);
    return wrap;
}

// 引用型来源：只读跳转行——交叉文件的唯一编辑点在目标域，不重复放编辑器（避免两处改同一文件）
function _refNode(s) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src";
    const row = document.createElement("div");
    row.className = "pt-row pt-ref";
    const target = _domainLabels[s.target] || s.target || "";
    row.title = s.note || "";
    row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span><span class="pt-tag">见「${escapeHtml(target)}」→</span>`;
    row.addEventListener("click", () => {
        const dom = document.querySelector(`#pv-tree .pt-domain[data-key="${s.target}"]`);
        if (dom) { dom.open = true; dom.scrollIntoView({block: "start", behavior: "smooth"}); }
    });
    wrap.appendChild(row);
    return wrap;
}

// ── 文件节点（slot/file）：行 + 就地展开编辑器 ──
const _STATE_LABEL = {baseline: "", inherited: "", customized: "（已修改）"};

function _fileNode(s, data) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src pt-file";
    const row = document.createElement("div");
    row.className = "pt-row";
    const shared = (s.used_by || []).length > 1
        ? `<span class="pt-tag pt-shared" title="本文件被多个阶段读取：${escapeHtml(s.used_by.join("、"))}">共用</span>` : "";
    const feed = s.feeds ? `<span class="pt-tag" title="本文件的输出流向">→ ${escapeHtml(s.feeds)}</span>` : "";
    const ai = s.assistable === false ? "" : `<span class="assist-ico" title="AI 辅助修改本文件">✨</span>`;
    // 包未附带的文件（如 sticker 包无 opening.json）：灰态只展示，不提供编辑入口
    if (s.exists === false && !s.user_copy) {
        row.classList.add("pt-row-missing");
        row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span><span class="pt-tag">本包未附带</span>`;
        wrap.appendChild(row);
        return wrap;
    }
    row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span>
        ${s.state && _STATE_LABEL[s.state] ? `<span class="pt-tag">${_STATE_LABEL[s.state]}</span>` : ""}
        ${!s.state && s.user_copy ? `<span class="pt-tag">已改</span>` : ""}
        ${shared}${feed}${ai}`;
    const ed = document.createElement("div");
    ed.className = "pt-editor";
    ed.style.display = "none";
    wrap.append(row, ed);
    row.addEventListener("click", () => _toggleFile(ed, s));
    const ico = row.querySelector(".assist-ico");
    if (ico) ico.addEventListener("click", e => {
        e.stopPropagation();
        const file = s.type === "slot" ? s.file : s.path;
        if (window.__openAssist) window.__openAssist(_treeMode, file, s.label);
    });
    return wrap;
}

async function _loadFileContent(s) {
    if (s.type === "slot") {
        const resp = await fetch(`/pack-files?mode=${encodeURIComponent(_treeMode)}`);
        const data = await resp.json();
        const f = (data.files || []).find(x => x.name === s.file);
        return f ? (f.content || "") : "";
    }
    // file 类型统一走 /pack-file（白名单 = 结构规范声明的 file 路径）——
    // 修复：此前硬编码 /pack-memory，opening.json 的读写会落在出厂记忆上
    const resp = await fetch(`/pack-file?mode=${encodeURIComponent(_treeMode)}&path=${encodeURIComponent(s.path)}`);
    const data = await resp.json();
    return data.content || "";
}

async function _toggleFile(ed, s) {
    if (ed.style.display === "none") {
        if (!ed.dataset.loaded) {
            ed.innerHTML = `<div class="pt-editing">${escapeHtml(s.type === "slot" ? s.file : s.path)}</div>
                <textarea class="pt-ta"></textarea>
                <div class="btn-row" style="display:flex;gap:8px;margin-top:6px">
                    <button class="pt-save" type="button" style="flex:1">保存</button>
                    <button class="pt-revert" type="button" style="flex:1">恢复默认</button>
                    <button class="pt-close" type="button" style="flex:1">收起</button>
                </div><div class="pt-msg" style="font-size:0.72em;color:var(--fg-muted);margin-top:4px"></div>`;
            const content = await _loadFileContent(s);
            ed.querySelector(".pt-ta").value = content;
            ed.dataset.loaded = "1";
            ed.querySelector(".pt-save").onclick = () => _saveFile(ed, s);
            ed.querySelector(".pt-revert").onclick = () => _revertFile(ed, s);
            ed.querySelector(".pt-close").onclick = () => { ed.style.display = "none"; };
        }
        ed.style.display = "block";
    } else {
        ed.style.display = "none";
    }
}

async function _saveFile(ed, s) {
    const msg = ed.querySelector(".pt-msg");
    const content = ed.querySelector(".pt-ta").value;
    if (!content.trim()) { msg.textContent = "内容不能为空"; return; }
    msg.textContent = "保存中…";
    const isSlot = s.type === "slot";
    try {
        const resp = await fetch(isSlot ? "/character-file-update" : "/pack-file/update", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(isSlot
                ? {mode: _treeMode, filename: s.file, content}
                : {mode: _treeMode, path: s.path, content}),
        });
        const d = await resp.json();
        msg.textContent = d.ok ? "已保存（下轮对话生效）" : "保存失败：" + (d.error || "");
    } catch (e) { msg.textContent = "网络错误"; }
}

async function _revertFile(ed, s) {
    if (!confirm("恢复默认？（删除你的修改）")) return;
    const msg = ed.querySelector(".pt-msg");
    const isSlot = s.type === "slot";
    try {
        // file 类型走 /pack-file/delete 删用户副本——修复：此前向 /pack-memory/update 发空串，
        // 后端拒绝空内容，「恢复默认」永远失败
        const resp = await fetch(isSlot ? "/character-file/delete" : "/pack-file/delete", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(isSlot ? {mode: _treeMode, filename: s.file} : {mode: _treeMode, path: s.path}),
        });
        const d = await resp.json();
        if (d.ok) { showToast("已恢复默认"); ed.style.display = "none"; loadPackTree(_treeMode); }
        else msg.textContent = d.error || "操作失败";
    } catch (e) { msg.textContent = "网络错误"; }
}

// ── 知识库目录节点：组内文件列表 + 就地展开 ──
const _KB_GROUPS = {world: "世界观", factions: "势力", story: "主线剧情", character: "角色个人", dialogues: "对话"};

function _kbNode(s) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src";
    const head = document.createElement("div");
    head.className = "pt-row";
    const feed = s.feeds ? `<span class="pt-tag" title="本目录内容的输出流向">→ ${escapeHtml(s.feeds)}</span>` : "";
    head.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span> <span class="pt-tag">${s.count || 0} 个文件</span>${feed}`;
    const body = document.createElement("div");
    body.className = "pt-kb";
    body.style.display = "none";
    wrap.append(head, body);
    head.addEventListener("click", () => _toggleKB(body, head));
    return wrap;
}

async function _toggleKB(body, head) {
    if (body.style.display === "none") {
        if (!body.dataset.loaded) {
            const resp = await fetch(`/pack-knowledge?mode=${encodeURIComponent(_treeMode)}`);
            const data = await resp.json();
            const files = (data.ok && data.files) || [];
            const groups = {};
            for (const f of files) (groups[f.path.split("/")[0]] = groups[f.path.split("/")[0]] || []).push(f);
            let html = "";
            for (const top of Object.keys(groups)) {
                html += `<div class="pt-kb-grp">${escapeHtml(_KB_GROUPS[top] || top)}/</div>`;
                for (const f of groups[top]) {
                    html += `<div class="pt-row pt-kb-file" data-path="${escapeHtml(f.path)}">
                        <span class="pt-name" style="font-size:0.78em">${escapeHtml(f.path.split("/").slice(1).join("/"))}</span>
                        ${f.user_copy ? '<span class="pt-tag">已改</span>' : ""}
                        <span class="assist-ico" data-ai="${escapeHtml(f.path)}" title="AI 辅助修改本文件">✨</span>
                    </div>`;
                }
            }
            body.innerHTML = html || `<div class="pt-note">还没有知识文件</div>`;
            body.dataset.loaded = "1";
            body.querySelectorAll(".pt-kb-file").forEach(row => {
                row.addEventListener("click", () => _toggleKBFile(row));
            });
            body.querySelectorAll(".assist-ico").forEach(ico => {
                ico.addEventListener("click", e => {
                    e.stopPropagation();
                    if (window.__openAssist) window.__openAssist(_treeMode, "knowledge/" + ico.dataset.ai, ico.dataset.ai);
                });
            });
        }
        body.style.display = "block";
    } else {
        body.style.display = "none";
    }
}

async function _toggleKBFile(row) {
    let ed = row.nextElementSibling;
    if (ed && ed.classList && ed.classList.contains("pt-editor")) {
        ed.style.display = ed.style.display === "none" ? "block" : "none";
        return;
    }
    ed = document.createElement("div");
    ed.className = "pt-editor";
    const path = row.dataset.path;
    ed.innerHTML = `<div class="pt-editing">${escapeHtml(path)}</div>
        <textarea class="pt-ta"></textarea>
        <div class="btn-row" style="display:flex;gap:8px;margin-top:6px">
            <button class="pt-save" type="button" style="flex:1">保存</button>
            <button class="pt-revert" type="button" style="flex:1">恢复默认</button>
            <button class="pt-close" type="button" style="flex:1">收起</button>
        </div><div class="pt-msg" style="font-size:0.72em;color:var(--fg-muted);margin-top:4px"></div>`;
    row.after(ed);
    try {
        const resp = await fetch(`/pack-knowledge/file?mode=${encodeURIComponent(_treeMode)}&path=${encodeURIComponent(path)}`);
        const data = await resp.json();
        ed.querySelector(".pt-ta").value = data.ok ? (data.content || "") : "";
    } catch (e) { ed.querySelector(".pt-ta").value = ""; }
    ed.querySelector(".pt-save").onclick = async () => {
        const msg = ed.querySelector(".pt-msg");
        const content = ed.querySelector(".pt-ta").value;
        if (!content.trim()) { msg.textContent = "内容不能为空"; return; }
        const resp = await fetch("/pack-knowledge/update", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _treeMode, path, content})});
        const d = await resp.json();
        msg.textContent = d.ok ? "已保存（下轮对话生效）" : "保存失败：" + (d.error || "");
    };
    ed.querySelector(".pt-revert").onclick = async () => {
        if (!confirm("恢复默认？（删除你的修改）")) return;
        const resp = await fetch("/pack-knowledge/delete", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _treeMode, path})});
        const d = await resp.json();
        if (d.ok) { showToast("已恢复默认"); ed.remove(); row.remove(); }
        else ed.querySelector(".pt-msg").textContent = d.error || "操作失败";
    };
    ed.querySelector(".pt-close").onclick = () => { ed.style.display = "none"; };
}

// ── 系统提示词节点（只读）──
function _sysPromptNode(s) {
    const det = document.createElement("details");
    det.className = "pt-src pt-sysprompt";
    const sum = document.createElement("summary");
    sum.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span> <span class="pt-tag">代码内置 · 只读</span>`;
    const pre = document.createElement("pre");
    pre.className = "pt-pre";
    pre.textContent = s.text || "";
    det.append(sum, pre);
    return det;
}


/* ── 来源：js/settings.js ── */
// 设置面板配置：供应商与模型管理 / 主动性预设 / 配置加载（loadConfig）与自动保存（_scheduleAutoSave）

// ═══════════════════════════════════════════
// 配置管理
// ═══════════════════════════════════════════
// 配置管理（设置页分组：账号与连接 / 主动消息 / 模型与速度 / 外观 / 数据与系统）
// A8 多供应商：_providers 列表 + _activeId（本地存后端 config.json；服务器版存浏览器 localStorage）
let _providers = [];
let _activeId = "deepseek";
const PROVIDERS_LS_KEY = "firefly_providers";
const ACTIVE_PROVIDER_LS_KEY = "firefly_active_provider";
const _MODEL_INPUT_IDS = ["retriever-model-input", "analyzer-model-input", "polisher-model-input", "organizer-model-input"];

const _PROACTIVE_PRESETS = {
    less:   { hard: 8, soft: 0.25 },
    medium: { hard: 6, soft: 0.35 },
    often:  { hard: 4, soft: 0.50 },
};
let _configLoaded = false;
let _saveTimer = null;

function _$(id) { return document.getElementById(id); }

function _proactivePresetName(hard, soft) {
    if (hard <= 4 && soft >= 0.45) return "often";
    if (hard >= 8) return "less";
    return "medium";
}

function _activeProviderObject() {
    return _providers.find(p => p.id === _activeId) || _providers[0] || null;
}

function _providersLS() {
    try { return JSON.parse(localStorage.getItem(PROVIDERS_LS_KEY) || "[]"); } catch (e) { return []; }
}

function _setProvidersLS(list, active) {
    try {
        localStorage.setItem(PROVIDERS_LS_KEY, JSON.stringify(list));
        localStorage.setItem(ACTIVE_PROVIDER_LS_KEY, active);
    } catch (e) {}
    // 兼容旧字段：relay.js / fetch 包装器仍读 firefly_api_key / firefly_api_base
    try {
        const p = list.find(x => x.id === active);
        if (p) {
            localStorage.setItem("firefly_api_key", p.api_key || "");
            localStorage.setItem("firefly_api_base", p.base_url || "");
        }
    } catch (e) {}
}

async function _getProviderModels(provider) {
    // 本地版：后端带 Key 转发；服务器版：浏览器直连（Key 在本机）
    if (!provider.base_url || !/^https?:\/\//.test(provider.base_url)) {
        throw new Error("接口地址必须是 http(s) 开头");
    }
    if (!provider.api_key) throw new Error("请先填写该供应商的 API Key");
    let resp;
    if (IS_SERVER) {
        resp = await fetch(provider.base_url.replace(/\/+$/, "") + "/models", {
            headers: { "Authorization": "Bearer " + provider.api_key },
        });
    } else {
        resp = await fetch("/models?provider=" + encodeURIComponent(provider.id));
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok && !IS_SERVER) throw new Error(data.error || "获取失败");
    if (IS_SERVER && !resp.ok) {
        // 直连失败（CORS/网络）→ 让后端代查（服务器转发自带 Key 的请求）
        throw new Error("直连获取失败（" + (data.error && data.error.message ? data.error.message : resp.status) + "）");
    }
    const models = IS_SERVER ? (data.data || []).map(m => m.id).filter(Boolean)
                             : (data.models || []);
    if (!models.length) throw new Error(IS_SERVER ? "供应商未返回模型清单" : "未返回模型清单");
    return models;
}

function _renderProviderSelect() {
    const sel = _$("provider-select");
    if (!sel) return;
    sel.innerHTML = "";
    for (const p of _providers) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name + ((p.api_key || p._has_key) ? "" : "（未填 Key）");
        sel.appendChild(opt);
    }
    if (_providers.length) sel.value = _activeId;
    try { sel._uiSelectSync && sel._uiSelectSync(); } catch (e) {}   // 自绘层跟随 options 重建
}

function _renderModelSuggest() {
    const dl = _$("model-suggest");
    const p = _activeProviderObject();
    if (!dl) return;
    const models = (p && p.models && p.models.length) ? p.models
        : ["deepseek-flash", "deepseek-v4-pro"];
    dl.innerHTML = "";
    for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m;
        dl.appendChild(opt);
    }
}

function _updateKeyGuide() {
    const box = _$("provider-guide");
    if (!box) return;
    const p = _activeProviderObject();
    if (!p) { box.innerHTML = ""; return; }
    const isDp = /deepseek\.com/.test(p.base_url || "");
    // A5（审计 2026-09-15）：p.name / p.base_url 是用户表单原样输入（持久化在
    // localStorage），原样插 innerHTML = 存储型 XSS——可直接读同域 firefly_providers
    // 里的 API Key 与 firefly_token。先转义再拼模板。
    const pname = escapeHtml(p.name || "");
    const pbase = escapeHtml(String(p.base_url || "").replace(/\/+$/, ""));
    box.innerHTML = isDp
        ? `① 浏览器打开 <b>platform.deepseek.com</b>，注册并登录<br>② 左侧「API Keys」→ 创建，复制 <b>sk-</b> 开头的 Key<br>③ 粘贴到「${pname}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`
        : `① 打开供应商控制台（<b>${pbase}</b> 所在站点主页）创建 API Key<br>② 粘贴到「${pname}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`;
}

function _openProviderForm(provider) {
    const form = _$("provider-form");
    if (!form) return;
    form.style.display = "block";
    _$("provider-name").value = provider ? provider.name : "";
    _$("provider-base").value = provider ? provider.base_url : "https://";
    _$("provider-key").value = "";
    _$("provider-key").placeholder = provider && provider.api_key ? "已设置，留空保留" : "sk-...";
    _$("provider-form-msg").textContent = "";
    form.dataset.editing = provider ? provider.id : "";
    _$("provider-delete").style.display = provider ? "" : "none";
}

function _closeProviderForm() {
    const form = _$("provider-form");
    if (form) form.style.display = "none";
}

async function _fetchAndFillModels() {
    const msg = _$("provider-form-msg");
    const id = _$("provider-form").dataset.editing;
    const name = _$("provider-name").value.trim();
    const base = _$("provider-base").value.trim().replace(/\/+$/, "");
    const key = _$("provider-key").value.trim();
    const cur = _providers.find(p => p.id === id) || {};
    if (!/^https?:\/\//.test(base)) { msg.textContent = "接口地址必须是 http(s) 开头"; return; }
    msg.textContent = "获取中…";
    try {
        const models = await _getProviderModels({ id: id || cur.id || "custom", base_url: base, api_key: key || cur.api_key });
        const p = _providers.find(x => x.id === id);
        if (p) { p.models = models; _renderModelSuggest(); msg.textContent = "已获取 " + models.length + " 个模型，记得点「保存供应商」"; }
        else { msg.textContent = "模型清单：" + models.slice(0, 5).join("、") + (models.length > 5 ? " 等" + models.length + " 个" : "") + "（保存供应商后生效）"; }
    } catch (e) { msg.textContent = "获取失败：" + e.message; }
}

async function _saveProviderForm() {
    const msg = _$("provider-form-msg");
    const name = _$("provider-name").value.trim();
    const base = _$("provider-base").value.trim().replace(/\/+$/, "");
    const key = _$("provider-key").value.trim();
    const editing = _$("provider-form").dataset.editing;
    if (!name) { msg.textContent = "请填写名称"; return; }
    if (!/^https?:\/\//.test(base)) { msg.textContent = "接口地址必须是 http(s) 开头"; return; }
    const existing = editing && _providers.find(p => p.id === editing);
    if (existing) {
        existing.name = name; existing.base_url = base;
        if (key) existing.api_key = key;
    } else {
        const id = "p" + Date.now().toString(36);
        _providers.push({ id, name, base_url: base, api_key: key,
                          models: [], caps: {} });
        _activeId = id;
    }
    _renderProviderSelect();
    _renderModelSuggest();
    _updateKeyGuide();
    updateSettingsSummaries();
    await saveConfigNow(true);
    _closeProviderForm();
}

function _deleteProviderForm() {
    const editing = _$("provider-form").dataset.editing;
    if (!editing || _providers.length <= 1) { _$("provider-form-msg").textContent = "至少保留一个供应商"; return; }
    if (!confirm("删除供应商「" + editing + "」？")) return;
    _providers = _providers.filter(p => p.id !== editing);
    if (_activeId === editing) _activeId = _providers[0].id;
    _renderProviderSelect();
    _renderModelSuggest();
    _updateKeyGuide();
    updateSettingsSummaries();
    saveConfigNow(true);
    _closeProviderForm();
}

function _applyProactivePreset(name) {
    const p = _PROACTIVE_PRESETS[name] || _PROACTIVE_PRESETS.medium;
    _$("proactive-hard-slider").value = p.hard;
    _$("proactive-soft-slider").value = Math.round(p.soft * 100);
    _$("proactive-hard-value").textContent = p.hard;
    _$("proactive-soft-value").textContent = Math.round(p.soft * 100) + "%";
    updateSettingsSummaries();
}

function updateSettingsSummaries() {
    const ps = _$("proactive-summary");
    if (ps) {
        const on = _$("proactive-enabled").checked;
        const preset = _$("proactive-preset");
        ps.textContent = on ? ("开启 · " + (preset && preset.selectedOptions[0] ? preset.selectedOptions[0].textContent : "偶尔")) : "已关闭";
    }
    const ms = _$("model-summary");
    if (ms) {
        const p = _activeProviderObject();
        const pm = _$("polisher-model-input") ? _$("polisher-model-input").value.trim() : "";
        ms.textContent = (p ? p.name : "DeepSeek") + " · " + (pm || "Flash");
    }
}

function _buildSettingsPayload() {
    // 主动消息设置已移入角色详情页（包级配置），此处不再提交——后端保留既有值
    return {
        analyzer_model: _$("analyzer-model-input").value.trim(),
        retriever_model: _$("retriever-model-input").value.trim(),
        organizer_model: _$("organizer-model-input").value.trim(),
        polisher_model: _$("polisher-model-input").value.trim(),
        retriever_effort: _$("retriever-effort-select").value,
        analyzer_effort: _$("analyzer-effort-select").value,
        polisher_effort: _$("polisher-effort-select").value,
        organizer_effort: _$("organizer-effort-select").value,
        retriever_temperature: parseFloat(_$("retriever-temp-slider").value) || 0,
    };
}

async function _postSettings(payload, msg) {
    const resp = await fetch("/set-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        msg.textContent = "保存失败：" + (data.error || "请稍后再试");
        return false;
    }
    // 注意：这里不再改 S._hiddenEnabled——本页已无「隐藏式」控件，
    // 该开关按角色卡存（角色详情页 → 主动消息），由 panels.js 同步。
    msg.textContent = "已保存 ✓";
    clearTimeout(msg._timer);
    msg._timer = setTimeout(() => { msg.textContent = ""; }, 2500);
    return true;
}

async function saveConfigNow(explicit) {
    const msg = _$("config-msg");
    const srcSel = _$("api-source-select");
    const src = srcSel ? srcSel.value : "own";
    const keyInput = _$("key-input");
    const k = keyInput ? keyInput.value.trim() : "";
    const p0 = _activeProviderObject();
    if (p0 && k) p0.api_key = k;   // Key 输入框 → 当前激活供应商
    if (IS_SERVER) {
        try { localStorage.setItem("firefly_api_source", src); } catch (e) {}
        if (src !== "proxy") _setProvidersLS(_providers, _activeId);
        if (keyInput) keyInput.value = "";
    }
    const payload = _buildSettingsPayload();
    if (!IS_SERVER) {
        // 本地版：供应商列表 + 激活项随配置落盘（Key 在 providers 内）
        payload.providers = _providers;
        payload.active_provider = _activeId;
        if (k) payload.api_key = k;   // 后端写入激活供应商
    }
    if (explicit) msg.textContent = "保存中…";
    const ok = await _postSettings(payload, msg);
    if (ok && keyInput) keyInput.value = "";
}

function _scheduleAutoSave() {
    if (!_configLoaded) return;
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        const msg = _$("config-msg");
        if (msg) msg.textContent = "自动保存中…";
        saveConfigNow(false);
    }, 400);
}

async function loadConfig() {
    const ids = {
        a: "analyzer-model-input", r: "retriever-model-input",
        o: "organizer-model-input", p: "polisher-model-input",
        re: "retriever-effort-select", ae: "analyzer-effort-select",
        pe: "polisher-effort-select", oe: "organizer-effort-select",
        rt: "retriever-temp-slider", rtv: "retriever-temp-value",
        k: "key-input", m: "config-msg",
        pe_: "proactive-enabled", ph: "proactive-hard-slider",
        phv: "proactive-hard-value", ps: "proactive-soft-slider",
        psv: "proactive-soft-value",
        pr_: "prob-reply-enabled", pr: "prob-reply-slider",
        prv: "prob-reply-value",
        hr_: "hidden-reply-enabled",
    };
    try {
        const resp = await fetch("/config");
        const data = await resp.json();
        const el = {};
        for (const [k, id] of Object.entries(ids)) el[k] = document.getElementById(id);

        const normEffort = v => (v === "low" ? "low" : v);
        if (el.a) el.a.value = data.analyzer_model || "deepseek-flash";
        if (el.r) el.r.value = data.retriever_model || "deepseek-flash";
        if (el.o) el.o.value = data.organizer_model || "deepseek-flash";
        if (el.p) el.p.value = data.polisher_model || "deepseek-flash";
        if (el.re) el.re.value = normEffort(data.retriever_effort || "none");
        if (el.ae) el.ae.value = normEffort(data.analyzer_effort || "high");
        if (el.pe) el.pe.value = normEffort(data.polisher_effort || "high");
        if (el.oe) el.oe.value = normEffort(data.organizer_effort || "none");

        const hard = data.proactive_hard != null ? data.proactive_hard : 6;
        const soft = data.proactive_soft != null ? data.proactive_soft : 0.35;
        const prob = data.prob_reply_value != null ? data.prob_reply_value : 0.10;
        if (el.pe_) el.pe_.checked = data.proactive_enabled !== false;
        if (el.ph) {
            el.ph.value = hard;
            if (el.phv) el.phv.textContent = hard;
        }
        if (el.ps) {
            el.ps.value = Math.round(soft * 100);
            if (el.psv) el.psv.textContent = Math.round(soft * 100) + "%";
        }
        if (el.pr_) el.pr_.checked = data.prob_reply_enabled !== false;
        if (el.pr) {
            el.pr.value = Math.round(prob * 100);
            if (el.prv) el.prv.textContent = Math.round(prob * 100) + "%";
        }
        if (el.hr_) el.hr_.checked = data.hidden_reply_enabled !== false;
        S._hiddenEnabled = data.hidden_reply_enabled !== false;
        const pp = _$("proactive-preset");
        if (pp) pp.value = _proactivePresetName(hard, soft);

        // 供应商（A8）：本地版走后端 /config；服务器版供应商配置在浏览器 localStorage
        if (IS_SERVER) {
            _providers = _providersLS();
            _activeId = (() => { try { return localStorage.getItem(ACTIVE_PROVIDER_LS_KEY) || ""; } catch (e) { return ""; } })();
            if (!_providers.length) {
                // 服务器模式首次初始化：继承 legacy 字段（firefly_api_key/api_base——
                // 旧版/升级前直接存这里），避免升级后首个会话把已有 Key 当“未设置”丢一次
                const legacyKey = getLocalApiKey();
                const legacyBase = (() => { try { return localStorage.getItem("firefly_api_base") || ""; } catch (e) { return ""; } })();
                _providers = [{ id: "deepseek", name: "DeepSeek",
                                base_url: legacyBase || "https://api.deepseek.com/v1",
                                api_key: legacyKey || "", models: [], caps: {} }];
                _activeId = "deepseek";
                _setProvidersLS(_providers, _activeId);
            }
            const p0 = _providers.find(p => p.id === _activeId) || _providers[0];
            if (p0) _activeId = p0.id;
        } else {
            _providers = (data.providers || []).map(p => ({
                id: p.id, name: p.name, base_url: p.base_url,
                api_key: "", models: p.models || [], caps: p.caps || {},
                _has_key: !!p.has_key, _key_prefix: p.key_prefix || "",
            }));
            _activeId = data.active_provider || (_providers[0] && _providers[0].id) || "deepseek";
        }

        const srcSel = _$("api-source-select");
        const srcField = _$("api-source-field");
        const localSrc = (() => { try { return localStorage.getItem("firefly_api_source") || "own"; } catch (e) { return "own"; } })();
        if (srcSel) {
            srcSel.value = localSrc === "proxy" ? "proxy" : "own";
            applyApiSource(IS_SERVER && localSrc === "proxy");
        }
        if (srcField) srcField.style.display = IS_SERVER ? "" : "none";
        // A5：增量同步（本地版登录后可用；服务器版数据天然在云端）
        const syncNowField = _$("sync-now-field");
        if (syncNowField) syncNowField.style.display = IS_SERVER ? "none" : "";

        if (data.retriever_temperature != null && el.rt) {
            el.rt.value = data.retriever_temperature;
            if (el.rtv) el.rtv.textContent = Number(data.retriever_temperature).toFixed(1);
        }

        if (el.m) {
            const keyLabel = _$("key-label");
            const pA = _activeProviderObject();
            const providerLabel = pA ? pA.name : "DeepSeek";
            if (IS_SERVER) {
                const localKey = getLocalApiKey();
                el.m.textContent = localKey ? "Key 已设置（仅存本机浏览器）" : "尚未设置 API Key（不会上传服务器）";
                if (keyLabel) keyLabel.textContent = "API Key（存于本机浏览器，不会上传服务器）";
            } else {
                el.m.textContent = pA && pA._has_key
                    ? "Key 已设置（" + (pA._key_prefix || "仅本机") + "）"
                    : "尚未设置 API Key";
                if (keyLabel) keyLabel.textContent = "API Key（存本机配置文件，仅本机使用）";
            }
        }
        if (el.k) {
            if (IS_SERVER) {
                const localKey = getLocalApiKey();
                el.k.placeholder = localKey ? "已设置，留空则保留" : "sk-...";
            } else {
                const pA = _activeProviderObject();
                el.k.placeholder = pA && pA._has_key ? "已设置，留空则保留原 Key" : "sk-...";
            }
            el.k.value = "";
        }

        // 供应商 UI（选择器 / 模型建议 / 获取 Key 教程）
        _renderProviderSelect();
        _renderModelSuggest();
        _updateKeyGuide();

        const hiddenField = _$("hidden-reply-field");
        if (hiddenField) hiddenField.style.display = window.androidWakeLock ? "" : "none";

        const exitRow = _$("app-exit-row");
        if (exitRow && data.platform === "pc") {
            exitRow.style.display = "";
            const exitBtn = _$("app-exit-btn");
            if (exitBtn) exitBtn.onclick = () => {
                if (!confirm("确定退出 Firefly 吗？聊天数据已实时保存，下次启动继续。")) return;
                exitBtn.disabled = true;
                exitBtn.textContent = "正在退出…";
                fetch("/shutdown", {method: "GET"}).catch(() => {});
            };
        }

        _configLoaded = true;
        updateSettingsSummaries();
        uiSelectEnhance(document.getElementById("settings-panel"));   // 自绘下拉（原生 select 弹窗无法主题化）
        return data;
    } catch (e) { return {has_key: false}; }
}

async function checkKey() {
    try { await loadConfig(); } catch (e) { /* 服务未就绪，静默 */ }
}

(function initSetGroups() {
    document.querySelectorAll("#settings-panel .set-head").forEach(head => {
        head.addEventListener("click", () => {
            const group = head.closest(".set-group");
            const willOpen = !group.classList.contains("open");
            document.querySelectorAll("#settings-panel .set-group").forEach(g => g.classList.remove("open"));
            if (willOpen) group.classList.add("open");
        });
    });
})();

_$("retriever-temp-slider")?.addEventListener("input", () => {
    _$("retriever-temp-value").textContent = Number(_$("retriever-temp-slider").value).toFixed(1);
});
_$("proactive-hard-slider")?.addEventListener("input", () => {
    _$("proactive-hard-value").textContent = _$("proactive-hard-slider").value;
});
_$("proactive-soft-slider")?.addEventListener("input", () => {
    _$("proactive-soft-value").textContent = _$("proactive-soft-slider").value + "%";
});
_$("prob-reply-slider")?.addEventListener("input", () => {
    _$("prob-reply-value").textContent = _$("prob-reply-slider").value + "%";
});

_$("proactive-preset")?.addEventListener("change", () => {
    _applyProactivePreset(_$("proactive-preset").value);
    _scheduleAutoSave();
});

["retriever-model-input", "analyzer-model-input", "polisher-model-input", "organizer-model-input",
 "retriever-effort-select", "analyzer-effort-select", "polisher-effort-select", "organizer-effort-select",
 "retriever-temp-slider", "proactive-enabled", "proactive-hard-slider", "proactive-soft-slider",
 "prob-reply-enabled", "prob-reply-slider", "hidden-reply-enabled"].forEach(id => {
    const el = _$(id);
    if (el) el.addEventListener("change", () => { updateSettingsSummaries(); _scheduleAutoSave(); });
});

// A8 供应商 UI 接线
_$("provider-select")?.addEventListener("change", () => {
    const sel = _$("provider-select");
    if (sel && sel.value) {
        _activeId = sel.value;
        _renderModelSuggest();
        _updateKeyGuide();
        updateSettingsSummaries();
        _scheduleAutoSave();
    }
});
_$("provider-add")?.addEventListener("click", () => _openProviderForm(null));
_$("provider-edit")?.addEventListener("click", () => {
    const p = _activeProviderObject();
    if (p) _openProviderForm(p);
});
_$("provider-save")?.addEventListener("click", () => _saveProviderForm());
_$("provider-delete")?.addEventListener("click", () => _deleteProviderForm());
_$("provider-cancel")?.addEventListener("click", () => _closeProviderForm());
_$("provider-fetch-models")?.addEventListener("click", () => _fetchAndFillModels());

const apiSourceSel = _$("api-source-select");
if (apiSourceSel) {
    apiSourceSel.addEventListener("change", () => applyApiSource(apiSourceSel.value === "proxy"));
}

_$("key-save")?.addEventListener("click", () => saveConfigNow(true));


/* ── 来源：js/update.js ── */
// 检查更新（GitHub 优先，失败自动降级 Gitee）与自动更新下载
// 注意：CURRENT_VERSION 是前端版本号单一来源，tools/check_version.py 校验本文件（及 server/frontend 同步副本）

// ═══════════════════════════════════════════
// 检查更新（GitHub 优先，失败自动降级 Gitee——国内网络 Gitee 更稳）
// ═══════════════════════════════════════════
const CURRENT_VERSION = "0.9.0";   // 与 android versionName / 安装器 AppVersion 保持一致
// PC 三栏外壳（pc_shell.js，独立 classic script）底部状态栏要显示版本号，
// 它看不到 bundle 作用域，所以暴露一个只读副本（不要在这里写版本，单一来源仍是本文件）。
window.__appVersion = CURRENT_VERSION;
// 设置面板版本号动态显示（单一版本源：CURRENT_VERSION；替代 index.html 硬编码文案）
const curVersionEl = document.getElementById("current-version");
if (curVersionEl) curVersionEl.textContent = "v" + CURRENT_VERSION;
const UPDATE_SOURCES = [
    { api: "https://api.github.com/repos/10csc/firefly/releases/latest", html: "https://github.com/10csc/firefly/releases" },
    { api: "https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest", html: "https://gitee.com/cpt-asymmetry/firefly/releases" },
];
// 公共下载页：APK 主通道走 Gitee，微信/QQ 等不支持 blob 下载的内置浏览器会自动走服务器直连（正确 MIME）
const DOWNLOAD_PAGE_URL = "http://101.200.14.126:8787/download/";
function compareVersions(a, b) {
    const pa = String(a).split(".").map(n => parseInt(n) || 0);
    const pb = String(b).split(".").map(n => parseInt(n) || 0);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const d = (pa[i] || 0) - (pb[i] || 0);
        if (d !== 0) return d;
    }
    return 0;
}
// 资产匹配：PC 装包 exe / 安卓 apk（Gitee 资产名可能带前缀，模糊匹配）
function _matchAsset(assets, re) {
    if (!Array.isArray(assets)) return "";
    for (const a of assets) {
        const n = String(a.name || a.browser_download_url || "");
        if (re.test(n)) return a.browser_download_url || n;
    }
    return "";
}
async function checkUpdate() {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "检查中…";
    if (IS_SERVER) {
        // 服务器模式：检查更新读服务器 version.json（由服务器管理员维护），不走 GitHub/Gitee
        try {
            const resp = await fetch("/version.json", {cache: "no-store"});
            const d = await resp.json();
            const latest = String(d.tag || "").replace(/^v/i, "");
            const cur = String(CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            if (compareVersions(latest, cur) > 0) {
                msg.innerHTML = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(cur)}）<br>新版本由服务器管理员发布`;
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
        } catch (e) {
            msg.textContent = "检查失败（服务器 version.json 不可达）";
        }
        return;
    }
    // 本地模式：优先走本地后端（权威版本源 + 自动下载能力），失败退回纯前端双源检测
    try {
        // /check-update 只注册在 POST_ROUTES（GET 会 404，曾长期被前端双源兜底掩盖）
        const lr = await fetch("/check-update", {method: "POST", cache: "no-store"});
        if (lr.ok) {
            const d = await lr.json();
            if (!d.ok) throw new Error(d.error || "check fail");
            const latest = String(d.tag || "").replace(/^v/i, "");
            const cur = String(d.current || CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            const isAndroid = /Android/i.test(navigator.userAgent) && !/Windows|Mac|Linux/i.test(navigator.userAgent);
            if (compareVersions(latest, cur) > 0) {
                msg.innerHTML = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(cur)}）<br>` +
                    `<button id="auto-update-btn" style="margin-top:6px;padding:4px 12px;border-radius:6px;border:none;background:var(--fg-accent);color:#fff;cursor:pointer">自动更新</button>` +
                    ` ｜ <a href="${escapeHtml(d.html_url || "#")}" target="_blank" rel="noopener" style="color:var(--fg-muted)">发行说明</a>`;
                const btn = document.getElementById("auto-update-btn");
                if (btn) btn.addEventListener("click", () => autoUpdate(isAndroid));
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
            return;
        }
    } catch (e) { /* 降级到前端直连 */ }
    // 前端直连双源（后端接口不可用时）
    for (const src of UPDATE_SOURCES) {
        try {
            const resp = await fetch(src.api, {cache: "no-store"});
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            const data = await resp.json();
            const latest = String(data.tag_name || "").replace(/^v/i, "");
            if (!latest) throw new Error("no tag");
            const isAndroid = /Android/i.test(navigator.userAgent) && !/Windows|Mac|Linux/i.test(navigator.userAgent);
            const exeUrl = _matchAsset(data.assets, /\.exe$/i);
            const apkUrl = _matchAsset(data.assets, /\.apk$/i);
            const dlUrl = isAndroid ? (apkUrl || src.html) : (exeUrl || src.html);
            if (compareVersions(latest, CURRENT_VERSION) > 0) {
                msg.innerHTML = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(CURRENT_VERSION)}）<br>` +
                    `<a href="${escapeHtml(dlUrl || "#")}" target="_blank" rel="noopener" style="color:var(--fg-bright)">下载安装包</a>` +
                    ` ｜ <a href="${escapeHtml(src.html || "#")}" target="_blank" rel="noopener" style="color:var(--fg-muted)">发行说明</a>`;
            } else {
                msg.textContent = `已是最新版本 ${CURRENT_VERSION} ✓`;
            }
            return;
        } catch (e) {
            msg.textContent = "检查失败（网络或仓库不可达）";
        }
    }
}
// 检查更新按钮接线（设置面板版本区；修复前该按钮无任何事件绑定，点击无反应）
const checkUpdateBtn = document.getElementById("check-update-btn");
if (checkUpdateBtn) checkUpdateBtn.addEventListener("click", checkUpdate);

// 自动更新：后端下载安装包 → PC 静默安装并重启；安卓引导系统安装器
async function autoUpdate(isAndroid) {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "下载中…（约 30-60 秒，请勿关闭应用）";
    try {
        const resp = await fetch("/update-download", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({kind: isAndroid ? "apk" : "exe"}),
        });
        const data = await resp.json();
        if (!data.ok) { msg.textContent = "下载失败：" + (data.error || ""); return; }
        if (isAndroid) {
            // WebView 无法直接用 file:// 装 APK：跳系统浏览器打开公共下载页
            // （下载页自动分流：标准浏览器走 Gitee，微信/QQ 等走服务器直连正确 MIME）
            msg.innerHTML = `下载完成 → 请从 <a href="${DOWNLOAD_PAGE_URL}" target="_blank" rel="noopener" style="color:var(--fg-bright)">下载页</a> 下载 APK 安装（系统限制需手动确认；如从 Gitee 页下载变成 .zip，把文件名改回 firefly.apk 即可）`;
            return;
        }
        if (data.installing) {
            msg.textContent = "下载完成，安装程序即将启动…应用会自动关闭，请稍候。";
            setTimeout(() => { location.href = "about:blank"; }, 1500);
        } else {
            msg.innerHTML = `下载完成 → <a href="file://${data.path}" target="_blank" rel="noopener" style="color:var(--fg-bright)">点击运行安装</a>`;
        }
    } catch (e) {
        msg.textContent = "自动更新失败：" + e;
    }
}


/* ── 来源：js/sync.js ── */
// 增量同步 UI（/sync/now）：进度条 / 冲突警告 toast / 10 分钟节流 / 回前台补同步

/** A1 增量同步（W4 编排端点 /sync/now）：登录后把本地文字数据与云端账号双向合并。
 *  window.autoSyncNow(force)：仅本地版 + 已登录（/auth/state 确认）才发起；
 *  force=false 时 10 分钟节流（localStorage 时间戳），force=true（手动「立即同步」）跳过节流。
 *  进行中显示非阻塞状态条「正在同步…」；完成 toast 计数；冲突橙色警告；失败 toast 原因。
 *  挂接点：登录态确认（api.js initAuth）/ 进聊天页与模式切换（views.js showChat）/ 回前台（下方 visibilitychange）。 */
const _SYNC_THROTTLE_MS = 10 * 60 * 1000;
let _syncing = false;
let _syncPill = null;
let _warnToastTimer = null;

function _syncPillShow(text) {
    if (!_syncPill) {
        _syncPill = document.createElement("div");
        _syncPill.style.cssText = "position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:998;background:rgba(28,30,46,.96);color:#e8e0d0;border:1px solid rgba(255,196,107,.45);border-radius:10px;padding:7px 14px;font-size:0.75em;box-shadow:0 6px 22px rgba(0,0,0,.45);pointer-events:none;display:none";
        document.body.appendChild(_syncPill);
    }
    _syncPill.textContent = text;
    _syncPill.style.display = "block";
}
function _syncPillHide() { if (_syncPill) _syncPill.style.display = "none"; }

/** 橙色警告 toast（样式沿用 _toast 浮层，边框改橙）：冲突备份等需要用户注意但非阻断的提醒 */
function _warnToast(msg) {
    let t = document.getElementById("app-warn-toast");
    if (!t) {
        t = document.createElement("div");
        t.id = "app-warn-toast";
        t.style.cssText = "position:fixed;top:56px;left:50%;transform:translateX(-50%);z-index:999;background:rgba(46,34,22,.97);color:#ffd9b0;border:1px solid rgba(255,150,80,.65);border-radius:10px;padding:9px 16px;font-size:0.8em;max-width:86vw;text-align:center;box-shadow:0 6px 22px rgba(0,0,0,.45);pointer-events:none;display:none";
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(_warnToastTimer);
    _warnToastTimer = setTimeout(() => { t.style.display = "none"; }, 4000);
}

window.autoSyncNow = async function (force) {
    if (IS_SERVER) return;   // 服务器版数据天然在云端，无需同步
    if (_syncing) return;    // 并发护栏：上一次同步还在飞
    if (!force) {
        let last = 0;
        try { last = parseInt(localStorage.getItem("firefly_last_sync") || "0", 10) || 0; } catch (e) {}
        if (Date.now() - last < _SYNC_THROTTLE_MS) return;   // 节流期内静默跳过
    }
    try {
        const st = await (await fetch("/auth/state")).json();
        if (!st.logged_in || st.offline_ok === false) return;   // 未登录/登录已过期：静默跳过
    } catch (e) { return; }   // 本地后端未就绪：静默
    _syncing = true;
    try { localStorage.setItem("firefly_last_sync", String(Date.now())); } catch (e) {}
    const msg = document.getElementById("sync-now-msg");
    if (msg) msg.textContent = "同步中…";
    _syncPillShow("正在同步…");
    try {
        const r = await fetch("/sync/now", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: "all"}),   // 全包同步（全部角色包）
        });
        const d = await r.json().catch(() => ({}));
        if (d.ok) {
            const text = `同步完成：上传 ${(d.uploaded || []).length} · 下载 ${(d.downloaded || []).length} · 合并 ${(d.merged || []).length}`;
            const conflicts = d.conflicts || 0;
            if (msg) msg.textContent = "✓ " + text + (conflicts ? `，冲突 ${conflicts}（见 .sync_backups）` : "");
            showToast(text);
            if (conflicts > 0) _warnToast(`${conflicts} 处冲突已各自备份`);
        } else {
            const err = "同步未完成：" + (d.error || "请先登录账号");
            if (msg) msg.textContent = err;
            showToast(err);
        }
    } catch (e) {
        if (msg) msg.textContent = "网络错误，稍后再试";
        showToast("同步失败：网络错误，稍后再试");
    } finally {
        _syncPillHide();
        _syncing = false;
    }
};
window.syncNow = function () { window.autoSyncNow(true); };   // 手动「立即同步」：强制不节流

// 回前台补一次同步（节流期内自动跳过，不打扰）
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") window.autoSyncNow();
});


/* ── 来源：js/chat_render.js ── */
// 聊天渲染：消息行（文本/表情包/图片/旁白）/ 引用小卡片 / 时间分割 / 打字机占位 / 逐条渲染动画

// 消息渲染
// ═══════════════════════════════════════════
// ═══════════════════════════════════════════
// 滚动到底部（rAF 延迟：等 DOM 更新/键盘 resize 后再滚，QQ/微信式自动拉底）
// ═══════════════════════════════════════════
function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    });
}
// 键盘弹起/收起导致可视高度变化时：若用户原本在底部则自动补滚
if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", () => {
        if (messagesEl.scrollTop + messagesEl.clientHeight >= messagesEl.scrollHeight - 60) {
            scrollToBottom();
        }
    });
}

/** 发送者显示名 —— 统一走 views.js 的 charName()/userName()（**只从当前角色卡取**）。
 *  取不到返回空串，调用方不渲染名字行。 */
function _whoName(who) {
    return who === "user" ? userName() : charName();
}

/** 消息内容外壳：`.msg-col` = 名字行 +（可选引用卡片）+ 内容。
 *
 *  为什么现在**总是**用它（以前只有带引用时才包）：
 *  名字行要与气泡同一侧对齐，就得有个纵向容器；顺带让 `align-items:flex-start`
 *  能把头像对到**名字行顶部**（官方就是这样）——以前没有名字行时用 flex-end，
 *  两行气泡的头像会被拽到气泡底部（用户真机发现"第二行头像又下去了"）。
 */
function _mkCol(who, inner, quote) {
    const col = document.createElement("div");
    col.className = "msg-col";
    const nm = _whoName(who);
    if (nm) {                       // 角色卡没填称呼 → 不渲染名字行（不写死假名字）
        const el = document.createElement("div");
        el.className = "msg-who";
        el.textContent = nm;
        col.appendChild(el);
    }
    if (quote) col.appendChild(_buildQuotePreview(quote));
    if (inner) col.appendChild(inner);
    return col;
}

/** 容错替换：内容节点现在可能不在 row 的直接子层（被 .msg-col 包住），
 *  所以不能用 `row.replaceChild`（会抛 NotFoundError，表现为"占位不显示"）。 */
function _replaceNode(oldNode, newNode) {
    if (oldNode && oldNode.parentNode) oldNode.parentNode.replaceChild(newNode, oldNode);
}

function _addAvatar(row, who) {
    const p = who === "user" ? null : currentPreset();
    if (who !== "user" && !(p && p.avatar)) {
        // F-5：无头像包 → 首字占位圆（否则每条消息行都是破图）
        const d = document.createElement("div");
        d.className = "msg-avatar pack-noimg";
        d.textContent = ((p && (p.char_name || p.name)) || "？").slice(0, 1);
        row.insertBefore(d, row.firstChild);
        return;
    }
    const img = document.createElement("img");
    img.className = "msg-avatar";
    if (who === "user") {
        // 05：用户头像随当前角色包（包内 user_avatar 资产优先，回落内置穹/星选择）
        const p = typeof currentPreset === "function" ? currentPreset() : null;
        img.src = (p && p.user_avatar) || TB_AVATARS[tbChoice];
        img.classList.add("tb-toggle");
        img.title = "点击切换形象";
        img.addEventListener("click", openAvatarPicker);
        img.classList.add("tb-avatar");
    } else {
        // 角色头像按当前预设包（角色预设化）
        img.src = p.avatar;
    }
    row.insertBefore(img, row.firstChild);
}

/** 按 seq 插序：DOM 顺序 = 记录顺序（不管渲染先后）。
 * 有 seq → 找到第一个 seq 更大的行，插它前面（同 seq 追加末尾）；无 seq → 追加。
 * 修复：多批回复动画交错时表情包"堆积"错位——插序保证显示与记录一致。 */
function _insertRow(row, seq) {
    if (seq === null || seq === undefined) { messagesEl.appendChild(row); return; }
    const rows = messagesEl.querySelectorAll(".msg-row[data-seq]");
    let anchor = null;
    for (const r of rows) {
        const s = parseInt(r.dataset.seq, 10);
        if (!isNaN(s) && s > seq) { anchor = r; break; }
    }
    if (anchor) messagesEl.insertBefore(row, anchor);
    else messagesEl.appendChild(row);
}

function addTextMessage(text, who, prepend = false, seq = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");   // 新消息从下方浮现（历史加载不带动画）
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    // 名字行 + 引用卡片 + 气泡统一进 .msg-col（官方每条消息都有名字行；
    // 头像靠 align-items:flex-start 对到名字行顶部）
    row.appendChild(_mkCol(who, bubble, quote));
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    // 已有语音的消息：立刻把语音条贴到气泡下面（缓存表已在内存时走这条快路径；
    // 首次进聊天还没拉到表的情况由 voiceRestoreBarsAuto() 事后补扫）。
    try {
        if (seq !== null && seq !== undefined
            && typeof voiceHas === "function" && voiceHas(seq)
            && typeof voiceAttachBar === "function") {
            const m = (typeof _voiceSeqMap !== "undefined" && _voiceSeqMap)
                ? _voiceSeqMap[CURRENT_MODE] : null;
            voiceAttachBar(row, seq, m ? m[String(seq)] : 0);
        }
    } catch (e) { /* 语音条失败绝不影响消息渲染 */ }
    return row;
}

/** 引用快照 → 气泡上方的小卡片（谁 + 内容摘要） */
function _quoteContentText(q) {
    if (!q) return "";
    if (q.type === "sticker") return "[表情包：" + (q.label || "") + "]";
    if (q.type === "image") return "[图片：" + (q.desc || "（无描述）") + "]";
    if (q.type === "narration") return q.text || "";
    return q.content || "";
}
// 引用卡片里的"谁"：**从当前角色卡取**（原来写死了「流萤」）。
// 用户侧固定「我」——第一人称代词，不是角色名字面量。
function _quoteWhoName(q) {
    return (q && q.who === "user") ? "我" : charName();
}
function _buildQuotePreview(q) {
    const div = document.createElement("div");
    div.className = "quote-preview";
    const who = document.createElement("span");
    who.className = "qp-who";
    who.textContent = _quoteWhoName(q);
    const txt = document.createElement("span");
    txt.className = "qp-text";
    txt.textContent = _quoteContentText(q);
    div.appendChild(who);
    div.appendChild(txt);
    return div;
}

async function addSticker(stickerPath, who, prepend = false, seq = null, label = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (label) row.dataset.stickerLabel = label;   // 长按菜单需要表情含义
    if (!prepend) row.classList.add("float-in");
    // A2 媒体本地策略：local: 引用 → IndexedDB 取本体；缺失降级占位（服务器不保存图片）
    const src = await stickerSrc(stickerPath, IS_SERVER, API_BASE);
    let img = null;
    if (src) {
        img = document.createElement("img");
        img.className = "sticker-img";
        img.src = src;
        img.dataset.stickerPath = stickerPath;
        // 容错：表情包文件缺失（历史遗留/用户删除）时降级为文字占位，不显示裂图
        img.onerror = () => {
            if (img.dataset.fallback) return;
            img.dataset.fallback = "1";
            const span = document.createElement("span");
            span.className = "sticker-fallback";
            span.textContent = "（表情包已失效）";
            _replaceNode(img, span);
        };
    } else {
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = "（表情包已失效）";
        img = span;
    }
    row.appendChild(_mkCol(who, img, quote));
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    return row;
}

/** A9 图片消息渲染：两端统一走后端 /image?id=&mode= 取字节（服务器版也上传落盘，IndexedDB 图片存储已退役）。
 *  服务器版 <img> 标签带不了 Bearer：经鉴权 fetch 拿字节转 objectURL；本地版同源直接 <img src>。
 *  缺失/失败 → 显示 desc 文字占位。 */
async function addImage(msg, who, prepend = false, seq = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly") + " image-row";
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");
    const imgId = (msg.img_id || "").toString().slice(0, 200);
    const desc = (msg.desc || "").toString().slice(0, 300);
    let src = null;
    if (imgId) {
        const url = "/image?id=" + encodeURIComponent(imgId) + "&mode=" + encodeURIComponent(CURRENT_MODE);
        if (IS_SERVER) {
            try {
                const resp = await fetch(url);
                if (resp.ok) { try { src = URL.createObjectURL(await resp.blob()); } catch (e) {} }
            } catch (e) {}
        } else {
            src = url;
        }
    }
    let content;
    if (src) {
        const img = document.createElement("img");
        img.className = "image-img";
        img.style.cssText = "max-width:min(56vw,320px);max-height:280px;border-radius:12px;display:block";
        img.src = src;
        img.dataset.imgId = imgId;
        img.dataset.desc = desc;
        img.onerror = () => {
            if (img.dataset.fallback) return;
            img.dataset.fallback = "1";
            const span = document.createElement("span");
            span.className = "sticker-fallback";
            span.textContent = desc ? `（图片：${desc}）` : "（图片已失效）";
            _replaceNode(img, span);
        };
        content = img;
    } else {
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = desc ? `（图片：${desc}）` : "（图片已失效）";
        span.dataset.imgId = imgId;
        span.dataset.desc = desc;
        content = span;
    }
    row.appendChild(_mkCol(who, content, quote));
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { messagesEl.appendChild(row); scrollToBottom(); }
    return row;
}

function addNarration(text, style, prepend = false, seq = null) {
    // 视觉小说式旁白：scene=居中小字（环境/事件），action=居中括号（动作）
    // 防御：历史数据/LLM 可能自带括号，先剥离避免双重括号
    let t = (text || "").trim();
    if ((t.startsWith("（") && t.endsWith("）")) || (t.startsWith("(") && t.endsWith(")"))) {
        t = t.slice(1, -1).trim();
    }
    const row = document.createElement("div");
    row.className = "msg-row narration-row";
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");
    const el = document.createElement("div");
    el.className = "narration " + (style === "scene" ? "narration-scene" : "narration-action");
    if (style === "action") el.textContent = "（" + t + "）";
    else el.textContent = t;
    row.appendChild(el);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { messagesEl.appendChild(row); scrollToBottom(); }
    return row;
}

function addTimeDivider(timeStr) {
    const div = document.createElement("div");
    div.className = "time-divider";
    div.textContent = timeStr;
    messagesEl.appendChild(div);
}

/** 消息加载占位：三个流水灯圆点（0.5~1s 后替换为真实内容） */
function addTypingBubble(who) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    const bubble = document.createElement("div");
    bubble.className = "bubble typing-bubble";
    bubble.innerHTML = "<span></span><span></span><span></span>";
    row.appendChild(bubble);
    _addAvatar(row, who);
    messagesEl.appendChild(row);
    scrollToBottom();
    return row;
}

function renderMessages(messages, who, data) {
    if (!messages || messages.length === 0) return;
    S._lastRenderTs = Date.now();   // 渲染时间戳（供主动性轮询门控；原二次包装已合并进来）
    const gen = _modeGen;   // 捕获渲染启动时的模式代际
    S._rendering = true;   // 渲染动画开始：防主动轮询中途插入乱序
    // 时间标注：取第一条消息的时间，放居中分割线
    const ts = messages[0].time ? messages[0].time.slice(11, 16) : new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    addTimeDivider(ts);
    // 逐条消息加载：先显示三圆点占位，再替换为真实内容（消息含文本与表情包）
    // 加载时长按字数 0.7~1.5s（表情包按最短 0.7s）；消息之间留 0.5s 空白模拟游戏节奏
    let seq = 0;
    const showNext = () => {
        if (gen !== _modeGen) { S._rendering = false; return; }   // 模式已切换：丢弃剩余动画
        if (seq >= messages.length) {
            S._rendering = false;   // 渲染动画完成
            return;
        }
        const msg = messages[seq++];
        const chars = (msg.content || msg.text || "").length;
        const loadMs = Math.min(1500, Math.max(700, 700 + chars * 25));
        const typingRow = addTypingBubble(who);
        setTimeout(() => {
            if (gen !== _modeGen) { typingRow.remove(); S._rendering = false; return; }
            typingRow.remove();
            if (msg.type === "sticker") addSticker(msg.path, who);
            else if (msg.type === "narration") addNarration(msg.text, msg.style);
            else if (msg.type === "image") addImage(msg, who);
            else addTextMessage(msg.content, who);
            setTimeout(showNext, 500);   // 消息间隔：0.5s 空白
        }, loadMs);
    };
    showNext();
}

// 消息渲染后记录时间（renderMessages 内调用；0.9.0 起已合并进函数本体，保留此注释防回归）


/* ── 来源：js/chat.js ── */
// 聊天核心：长按菜单 / 引用 / 发送四阶段 / 提交窗口状态机 / 数据备份导入导出

// ═══════════════════════════════════════════
// 长按消息菜单（QQ 式）：引用 / 收藏
// ═══════════════════════════════════════════
const _LONG_PRESS_MS = 500;
let _lpTimer = null, _lpRow = null, _lpStart = null;
let _msgMenu = null;
let _quoteTarget = null;   // 引用快照（发送后随消息提交，然后清空）

/** 从消息行提取快照（who/type/content…；seq 只在历史渲染的消息上有） */
function _msgSnapshot(row) {
    const who = row.classList.contains("user") ? "user" : "firefly";
    const snap = {who};
    const seq = parseInt(row.dataset.seq, 10);
    if (!isNaN(seq)) snap.seq = seq;
    if (row.classList.contains("narration-row")) {
        snap.type = "narration";
        const el = row.querySelector(".narration");
        let t = el ? el.textContent : "";
        t = t.replace(/^（|）$/g, "").trim();   // 剥掉旁白自带括号，引用内容更干净
        snap.text = t;
    } else if (row.querySelector(".sticker-img")) {
        snap.type = "sticker";
        snap.label = row.dataset.stickerLabel || "";
        const img = row.querySelector(".sticker-img");
        if (img) snap.path = img.dataset.stickerPath || "";
    } else if (row.querySelector(".image-img")) {
        snap.type = "image";
        const img = row.querySelector(".image-img");
        if (img) {
            snap.img_id = img.dataset.imgId || "";
            snap.desc = img.dataset.desc || "";
        }
    } else {
        snap.type = "text";
        const b = row.querySelector(".bubble");
        snap.content = b ? b.textContent : "";
    }
    return snap;
}

function _snapHasContent(s) {
    if (!s) return false;
    if (s.type === "text") return !!(s.content && s.content.trim());
    if (s.type === "sticker") return !!(s.label || s.path);
    if (s.type === "narration") return !!(s.text && s.text.trim());
    if (s.type === "image") return !!(s.img_id || s.desc);
    return false;
}

function _closeMsgMenu() {
    if (_msgMenu) { _msgMenu.remove(); _msgMenu = null; }
}
window._closeMsgMenu = _closeMsgMenu;

/** PC 侧栏导航用：打开菜单抽屉并切到指定 tab（双栏下抽屉即右栏视图） */
function openMenuTab(tab) {
    openMenu();
    const b = document.querySelector('.menu-tab[data-tab="' + tab + '"]');
    if (b) b.click();
}
window.openMenuTab = openMenuTab;

// ═══════════════════════════════════════════
// 语音插件：长按消息 →「转语音」（docs/工具/tts.md §2）
//  · 语音是**消息的附属产物**（v{seq}.wav），不是新消息类型 → 不写 conversation.jsonl
//  · 已有缓存直接播；没有才合成
//  · **同时只能有一个合成**：本地 _voiceBusy 拦重复点击，后端还会再串行一次
//  · 插件不可用 → 按钮点击时给出**原因**（不静默失败）
// ═══════════════════════════════════════════
let _voiceBusy = false;
let _voiceMood = localStorage.getItem("firefly_voice_mood") || "happy";

async function _voiceStatus(force) {
    if (window.__voiceStatusCache && !force) return window.__voiceStatusCache;
    try {
        const r = await fetch("/voice/status");
        window.__voiceStatusCache = await r.json();
    } catch (e) {
        window.__voiceStatusCache = {engine_ok: false, reason: "无法连接后端"};
    }
    return window.__voiceStatusCache;
}

/** 语音文件 URL（播放由消息下方的语音条负责，见 voice_plugin.js） */
function _voiceUrl(mode, seq) {
    return "/voice-file?mode=" + encodeURIComponent(mode) + "&name=v" + seq + ".wav";
}

/**
 * 转语音。**每次都强制重生成**（force）—— 用户要求"再点一次就自动重新生成"；
 * 单纯回放由消息下方的语音条承担。生成期间先在消息下面贴一个占位条（⏳ …）。
 */
async function _voiceConvert(mode, snap) {
    if (_voiceBusy) { showToast("正在生成上一句语音，请稍候"); return; }
    if (snap.who !== "firefly") { showToast(`只能给${charName() || "角色"}的消息转语音`); return; }
    if (snap.seq == null) { showToast("这条消息还没有序号（刷新页面后可用）"); return; }
    if (snap.type !== "text" && snap.type !== "narration") {
        showToast("这条消息没有可念的文本"); return;
    }
    const st = await _voiceStatus(true);
    if (!st.engine_ok) {
        showToast("语音插件不可用：" + (st.reason || "未知原因"));
        return;
    }

    const had = (typeof voiceHas === "function") && voiceHas(snap.seq);
    const row = (typeof messagesEl !== "undefined" && messagesEl)
        ? messagesEl.querySelector('.msg-row[data-seq="' + snap.seq + '"]') : null;
    let bar = null;
    if (row && typeof voiceAttachBar === "function") {
        bar = voiceAttachBar(row, snap.seq, 0);
        if (bar) {
            bar.classList.add("generating");
            bar.querySelector(".vb-ico").textContent = "⏳";
        }
    }

    _voiceBusy = true;
    showToast(had ? "正在重新生成语音…（约 20 秒）" : "正在生成语音…（首次约 20 秒）");
    try {
        const r = await fetch("/voice/tts", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: mode, seq: snap.seq, mood: _voiceMood, force: true}),
        });
        const d = await r.json();
        if (d && d.ok) {
            if (typeof voiceSyncCache === "function") await voiceSyncCache();
            if (!bar && row && typeof voiceAttachBar === "function") {
                bar = voiceAttachBar(row, snap.seq, d.dur);
            }
            if (bar) {
                bar.classList.remove("generating");
                bar.querySelector(".vb-ico").textContent = "🔊";
                const dd = bar.querySelector(".vb-dur");
                if (dd) dd.textContent = Math.round(d.dur || 0) + "″";
                if (typeof _voicePlayBar === "function") _voicePlayBar(bar, snap.seq);
            }
            showToast((had ? "已重新生成" : "已生成") + "（" + (d.seconds || "?") + "s）");
        } else {
            if (bar) bar.remove();
            showToast("生成失败：" + ((d && d.error) || "未知原因"));
            _voiceStatus(true);   // 失败后刷新状态，下次点能拿到新原因
        }
    } catch (e) {
        if (bar) bar.remove();
        showToast("生成失败：无法连接后端");
    } finally {
        _voiceBusy = false;
    }
}
window._voiceMood = () => _voiceMood;
window.setVoiceMood = (m) => { _voiceMood = m; localStorage.setItem("firefly_voice_mood", m); };

function _showMsgMenu(row, x, y) {
    const snap = _msgSnapshot(row);
    if (!_snapHasContent(snap)) return;
    _closeMsgMenu();   // 唯一性：先清掉旧菜单，保证同一时刻只有一个「引用/收藏」菜单
    const menu = document.createElement("div");
    menu.id = "msg-long-menu";
    _msgMenu = menu;   // 登记为当前菜单（_closeMsgMenu 靠它清理；漏掉会越积越多）
    const mkBtn = (label, icon, fn) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "mlm-btn";
        b.textContent = icon + " " + label;
        b.addEventListener("click", fn);
        return b;
    };
    menu.appendChild(mkBtn("引用", "📎", () => { _closeMsgMenu(); _setQuote(snap); }));
    menu.appendChild(mkBtn("收藏", "⭐", async () => {
        _closeMsgMenu();
        try {
            const resp = await fetch("/favorite", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({mode: CURRENT_MODE, message: snap}),
            });
            const d = await resp.json();
            showToast(d.ok ? "已收藏（菜单 → 收藏可查看）" : "收藏失败：" + (d.error || ""));
        } catch (e) { showToast("收藏失败，请重试"); }
    }));
    // 转语音：只对流萤的 text/narration 且有 seq 的消息显示。
    // 已有语音时**再点一次即重新生成** —— 不再单列「重新生成」：
    //   2026-09-18 真机实测 4 项会把菜单撑出屏幕（最左「引用」被裁到屏幕外）。
    // 播放改由消息下方的语音条承担（见 voice_plugin.js 的 voiceAttachBar）。
    if (snap.who === "firefly" && snap.seq != null
        && (snap.type === "text" || snap.type === "narration")) {
        menu.appendChild(mkBtn("转语音", "🔊", () => { _closeMsgMenu(); _voiceConvert(CURRENT_MODE, snap); }));
    }
    document.body.appendChild(menu);
    // 定位：消息在上半屏 → 菜单放下方；下半屏 → 放上方（QQ 式，且不超出视口）
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    const r = row.getBoundingClientRect();
    const vw = innerWidth, vh = innerHeight;
    let left = Math.min(Math.max(8, x - mw / 2), vw - mw - 8);
    // 菜单比视口还宽时上式会得到负数 → 左边被裁（真机踩过）。兜到 8。
    left = Math.max(8, left);
    const cy = r.top + r.height / 2;
    let top = cy < vh / 2 ? r.bottom + 10 : r.top - mh - 10;
    top = Math.max(8, Math.min(top, vh - mh - 8));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
}

function _cancelLongPress() {
    if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
    _lpRow = null; _lpStart = null;
}

// 长按（触屏/鼠标按住）与 PC 右键都弹菜单
messagesEl.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const row = e.target.closest(".msg-row");
    if (!row || e.target.closest(".msg-avatar")) return;          // 头像长按留给形象切换
    if (row.querySelector(".typing-bubble")) return;              // 加载占位不可长按
    _cancelLongPress();
    _lpRow = row;
    _lpStart = {x: e.clientX, y: e.clientY};
    _lpTimer = setTimeout(() => {
        _lpTimer = null;
        if (_lpRow && _lpRow.isConnected) {
            try { if (navigator.vibrate) navigator.vibrate(20); } catch (e) {}   // 触觉反馈
            _showMsgMenu(_lpRow, _lpStart.x, _lpStart.y);
        }
    }, _LONG_PRESS_MS);
});
window.addEventListener("pointermove", (e) => {
    if (_lpTimer && _lpStart) {
        const dx = e.clientX - _lpStart.x, dy = e.clientY - _lpStart.y;
        if (dx * dx + dy * dy > 64) _cancelLongPress();   // 移动超 8px = 滚动/滑动，取消长按
    }
}, {passive: true});
window.addEventListener("pointerup", _cancelLongPress);
window.addEventListener("pointercancel", _cancelLongPress);
messagesEl.addEventListener("scroll", _closeMsgMenu, {passive: true});
// 点击菜单外任意处关闭
document.addEventListener("pointerdown", (e) => {
    if (_msgMenu && !e.target.closest("#msg-long-menu")) _closeMsgMenu();
}, {capture: true});
// PC 右键同样弹菜单
messagesEl.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".msg-row");
    if (!row) return;
    e.preventDefault();
    _showMsgMenu(row, e.clientX, e.clientY);
});

// ── 引用：设置引用条（输入框上方） ──
function _setQuote(snap) {
    _quoteTarget = snap;
    const bar = document.getElementById("quote-bar");
    if (!bar) return;
    const whoEl = document.getElementById("quote-who");
    const txtEl = document.getElementById("quote-text");
    if (whoEl) whoEl.textContent = "引用 " + _quoteWhoName(snap);
    if (txtEl) txtEl.textContent = _quoteContentText(snap);
    bar.style.display = "flex";
    inputEl.focus();
}
function _clearQuote() {
    _quoteTarget = null;
    const bar = document.getElementById("quote-bar");
    if (bar) bar.style.display = "none";
}
document.getElementById("quote-cancel")?.addEventListener("click", _clearQuote);

// ═══════════════════════════════════════════
// 发送消息 — 四阶段模型：输入 → 发送 → 提交 → 回复
//   输入：打字（内容只在输入框，不触发队列）
//   发送：Enter / 发送按钮 / 点表情 → 消息**立即 POST 后端**（不等 5 秒，切后台不丢）
//   提交：后端 5 秒滑动窗口合并（后端控制；/chat/hint 重置窗口、/chat/flush 提前结束）
//   回复：流萤回复渲染
// 关键：发送 ≠ 提交。后端窗口合并连续消息；输入框未发送的内容永不提交（绝不自动发送）。
// ═══════════════════════════════════════════
let _inflight = 0;        // 在飞请求数（WakeLock 引用计数：全部完成才释放）
let _stageTimer = null;   // 阶段进度轮询句柄（等待回复期间轮询 /chat-stage）

// LLM 错误分类 → 人话提示（后端 /chat 返回 error_code 时展示）
const ERROR_TIPS = {
    key_invalid: "API Key 无效或已过期，请到设置中检查",
    no_balance: "API 余额不足，请到 DeepSeek 平台充值后再试",
    rate_limit: "请求太频繁，稍等一会儿再试试",
    network: "网络不通，请检查网络后重试",
    server_error: "服务端暂时出错，请稍后再试",
    bad_response: "服务返回异常，请稍后再试",
    relay_timeout: "代发超时，请检查网络后重试",
    timeout: "回复超时了，稍后再试一次吧",
    cooldown: "上游服务波动中，休息一下再试试",
    quota_exhausted: "今日服务器托管额度已用完，可在设置中切换为自带 Key 模式",
    unknown: "出了点问题，请稍后再试",
};



/** 导入 zip 备份（覆盖当前模式数据；导入前后端自动备份现有数据）。 */
function importData() {
    const input = document.getElementById("import-file");
    if (!input) return;
    input.onchange = async () => {
        const f = input.files && input.files[0];
        input.value = "";   // 允许重复选同一文件
        if (!f) return;
        if (!confirm(`导入将覆盖当前「${MODE_NAMES[CURRENT_MODE] || CURRENT_MODE}」的全部数据（导入前会自动备份现有数据）。\n\n确定导入 ${f.name} 吗？`)) return;
        _toast("正在导入…");
        try {
            const fd = new FormData();
            fd.append("file", f);
            fd.append("mode", CURRENT_MODE);
            const resp = await fetch("/import-data", { method: "POST", body: fd });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok && data.ok) {
                _toast("导入成功，正在重新加载…");
                setTimeout(() => location.reload(), 800);
            } else {
                _toast("导入失败：" + (data.error || "请检查文件"));
            }
        } catch (e) { _toast("导入失败，请检查网络"); }
    };
    input.click();
}
window.importData = importData;

// ══ 导出当前角色（2026-09-10 加）══
// 为什么需要它：此前前端只有「导入」没有「导出」——用户无法自助备份，唯一的备份入口是
// 全量快照，而快照对自建角色包曾存在丢包问题（R-01）。导出走既有 GET /export-data?mode=，
// 产出本地 zip（含该角色的包定义与全部对话/手账/记忆，_config.json 已剥离 Key），
// 可离线留存、也可用「导入」在本机或其他设备还原。
function exportData() {
    const name = MODE_NAMES[CURRENT_MODE] || CURRENT_MODE;
    if (!confirm(`导出「${name}」的全部数据为本地 zip？\n\n包含：角色设定、对话记录、手账、记忆（不含 API Key）。\n可用于离线备份或换机迁移。`)) return;
    _toast("正在打包导出…");
    const url = API_BASE + "/export-data?mode=" + encodeURIComponent(CURRENT_MODE);
    // 用隐藏链接触发下载（保留 Content-Disposition 文件名；window.open 在部分 WebView 会被拦）
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1500);
}
window.exportData = exportData;

// ══ 数据快照（保存全部角色到服务器；每账号留最近 5 份，换机可恢复）══
function _fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
}

async function createSnapshot() {
    _toast("正在打包快照…");
    try {
        const resp = await fetch("/snapshot/create", { method: "POST" });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast(data.pushed ? "快照已保存到服务器（" + _fmtSize(data.size) + "）"
                               : "快照已保存到本机（" + _fmtSize(data.size) + "）"
                                 + (data.push_error ? "；" + data.push_error : ""));
            loadSnapshots();
        } else {
            _toast("快照失败：" + (data.error || ""));
        }
    } catch (e) { _toast("快照失败，请检查网络"); }
}

async function loadSnapshots() {
    const list = document.getElementById("snapshot-list");
    if (!list) return;
    try {
        const resp = await fetch("/snapshot/list");
        const data = await resp.json();
        const items = data.snapshots || [];
        if (!items.length) {
            list.innerHTML = '<div style="color:var(--fg-muted);font-size:0.75em">还没有快照，点「保存快照」存一份</div>';
            return;
        }
        list.innerHTML = items.map(s => `
        <div class="snapshot-row" data-name="${escapeHtml(s.name)}" style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.75em">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.name)}">${escapeHtml(s.time || s.name)}</span>
            <span style="color:var(--fg-muted);flex-shrink:0">${_fmtSize(s.size)}</span>
            <button type="button" class="sn-restore">恢复</button>
            <button type="button" class="sn-download">下载</button>
            <button type="button" class="sn-del">删除</button>
        </div>`).join("");
        list.querySelectorAll(".snapshot-row").forEach(row => {
            const name = row.dataset.name;
            row.querySelector(".sn-restore").addEventListener("click", () => restoreSnapshot(name));
            row.querySelector(".sn-del").addEventListener("click", () => deleteSnapshot(name));
            row.querySelector(".sn-download").addEventListener("click", () => {
                if (!IS_SERVER) {
                    window.location.href = `/snapshot/download?name=${encodeURIComponent(name)}`;
                } else {
                    fetch(`/snapshot/download?name=${encodeURIComponent(name)}`)
                        .then(r => r.blob()).then(blob => {
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement("a");
                            a.href = url; a.download = name;
                            document.body.appendChild(a); a.click();
                            setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
                        }).catch(() => _toast("下载失败"));
                }
            });
        });
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;font-size:0.75em">快照列表加载失败</div>';
    }
}

async function restoreSnapshot(name) {
    if (!confirm(`用快照「${name}」覆盖全部角色与聊天记录？\n恢复前会自动保存当前状态的快照。\n\n确定恢复吗？`)) return;
    _toast("正在恢复快照…");
    try {
        const resp = await fetch("/snapshot/restore", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            // E-1 配套：恢复前自动备份失败时后端会回落逐包备份并标 backup_ok=false——
            // 此时若快照本身有问题，损失不可回滚，必须让用户知道
            _toast(data.backup_ok === false
                ? "已恢复（注意：恢复前的保险快照生成失败）"
                : "恢复成功，正在重新加载…");
            setTimeout(() => location.reload(), 800);
        } else {
            _toast("恢复失败：" + (data.error || ""));
        }
    } catch (e) { _toast("恢复失败，请检查网络"); }
}

async function deleteSnapshot(name) {
    if (!confirm(`删除快照「${name}」？此操作不可撤销。`)) return;
    try {
        const resp = await fetch("/snapshot/delete", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast("快照已删除");
            loadSnapshots();
        } else {
            _toast("删除失败：" + (data.error || ""));
        }
    } catch (e) { _toast("删除失败，请检查网络"); }
}

document.getElementById("snapshot-create-btn")?.addEventListener("click", createSnapshot);
document.getElementById("snapshot-list") && loadSnapshots();


/** 等待回复期间轮询流水线阶段（检索→分析→回复→表情包），把"对方正在输入…"换成具体阶段。
 *  仅在拿到 stage 时替换文本；请求结束由 _chatSend 的 finally 清除。
 *  A3：消费后端 waited 字段——等待偏久（>60s）提示"上游有点慢"，不再像卡死。 */
let _waitedWarned = false;
function _pollStage(statusEl) {
    clearInterval(_stageTimer);
    _waitedWarned = false;
    _stageTimer = setInterval(async () => {
        if (_inflight <= 0) { clearInterval(_stageTimer); _stageTimer = null; return; }
        try {
            const r = await fetch(`/chat-stage?sid=${encodeURIComponent(SESSION_ID)}&mode=${encodeURIComponent(CURRENT_MODE)}`);
            const d = await r.json();
            if (d.stage && d.label && _inflight > 0 && statusEl) statusEl.textContent = d.label;
            if (d.waited != null && d.waited > 60 && !_waitedWarned && _inflight > 0 && statusEl) {
                _waitedWarned = true;
                statusEl.textContent = "上游有点忙，正在努力回复…";
            }
        } catch (e) { /* 网络抖动静默，状态保持"对方正在输入" */ }
    }, 2000);
}

// ═══════════════════════════════════════════
// 提交窗口状态机（0.8.1 简化，两判定 + 上限）
// 提交流程：消息入队（气泡上屏+写盘，不丢）→ 等待提交。
// 提交条件（两个都满足）：
//   ① 输入框为空（没在打下一句）
//   ② 距最新一条消息 ≥ 5 秒（_lastMsgTs 每次发送重置，以最新消息为准）
// 打字中（输入框有内容）→ 持续 hint 续期后端窗口（2s 节流），流萤继续等，绝不提交；
// 合并上限：单批连续消息 ≥ _MAX_BATCH_MSGS 条 → 立即提交（即使输入框有内容）。
// 与后端 _CHAT_WINDOW_MAX_MSGS=10 一致。
// ═══════════════════════════════════════════
const _MAX_BATCH_MSGS = 10;
let _lastMsgTs = 0;              // 最新一条已发送消息的时间戳
let _pendingBatch = 0;           // 本批已入队未提交条数
let _mediaBusy = false;          // 媒体选择中（图片相册/表情面板）：暂停提交（长期等待）

// 常驻检查器（页面级）：每 1 秒拍一次，只在有 pending 批时判定。
// 简化语义：
//   _pendingBatch === 0 → 无事可做，跳过；
//   _mediaBusy（相册/表情面板打开）→ 与打字中同构：持续 hint 续期后端窗口，
//     不提交（后端窗口是 5 秒滑动的，不续期照样到期——前端暂停 flush 不够，必须续期）；
//   输入框有内容（打字）→ 续期后端窗口，不提交；
//   距最新消息 ≥ 5 秒 → 提交（flush）。
let _checkTimer = setInterval(() => {
    if (_pendingBatch <= 0) return;
    if (_pendingBatch >= _MAX_BATCH_MSGS) { _batchCommit(); return; }   // 合并上限 10 条
    if (_mediaBusy) { _sendHint(); return; }                            // 媒体选择中：续期窗口+不提交
    if (inputEl && inputEl.value.trim()) { _sendHint(); return; }       // 打字中：续期窗口
    if (Date.now() - _lastMsgTs < 5000) return;                         // 距最新消息 <5 秒：继续等
    _batchCommit();
}, 1000);

function _batchArmSubmit() {
    _lastMsgTs = Date.now();
    _pendingBatch++;
}

/** 媒体选择结束后：重新计时 5 秒（不增加批计数——未产生新消息）。 */
function _batchRefreshTimer() {
    _lastMsgTs = Date.now();
}

function _batchCommit() {
    _pendingBatch = 0; _lastMsgTs = 0;
    _sendFlush();
}

/** 模式切换/离开聊天页时调用：提交批立即作废（旧模式消息不跨模式提交）。timer 常驻不清。 */
function resetBatchWindow() {
    _pendingBatch = 0; _lastMsgTs = 0; _mediaBusy = false;
}

/** 打字中：重置后端合并窗口（流萤继续等开拓者说完）。
 * 输入框仍有内容 → 持续定时重置（前端在且输入框有残留 = 用户在打字 → 永不提交）。 */
function _sendHint() {
    fetch("/chat/hint", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
    }).catch(() => {});
    // 输入框仍有残留（用户还在打字/未清空）→ 继续定时重置窗口（2s 节流）
    if (inputEl && inputEl.value.trim()) {
        clearTimeout(S._hintTimer);
        S._hintTimer = setTimeout(_sendHint, 2000);
    }
}

/** 提交窗口到期：立即结束后端合并窗口（前台加速；切后台冻结不触发，后端窗口兜底）。
 * 状态显示时机：只有提交（进入核心流水线）才显示"对方正在输入"，窗口等待期不显示。 */
function _sendFlush() {
    if (inputEl) inputEl.placeholder = "说点什么…";   // 提交窗口结束：还原补话提示（3.6）
    if (_inflight === 0) return;   // 无在飞请求（已完成）：不显示状态，防止卡"正在输入"
    const statusEl = document.querySelector("#header .status");
    if (statusEl) statusEl.textContent = "对方正在输入...";
    fetch("/chat/flush", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
    }).catch(() => {});
}

/** 发送消息到后端并处理响应：
 *  副请求（窗口内）→ 后端返回 {queued:true}，回复由主请求带回，忽略；
 *  主请求（窗口结束/新窗口）→ 挂起等回复，返回后渲染。
 *  状态显示不在此处设置——窗口等待期不显示"正在输入"，由 _sendFlush（提交）触发。
 *  超时护栏：/chat 挂 4 分钟 AbortController，上游（LLM 端点）卡住时
 *  自动放弃等待并提示，避免页面无限挂起"对方正在输入…"（历史上游 5xx 重试
 *  + 单阶段 120s 超时可拖 15 分钟以上，会话锁全线阻塞）。 */
const _CHAT_FETCH_TIMEOUT = 4 * 60 * 1000;   // 4 分钟（覆盖 4 阶段最长流水线）

async function _chatSend(msgs) {
    _inflight++;
    const statusEl = document.querySelector("#header .status");
    // 修复：不用"请求开始时的快照"恢复（快照可能已被 _sendFlush / 阶段轮询污染成
    // "对方正在输入"/"正在理解你的话…"），统一恢复为当前角色包的签名（无签名用默认句）。
    const defaultStatus = (currentPreset() || {}).tagline || "会找到的，属于我的梦...";
    const gen = _modeGen;   // 捕获发起时的模式代际
    // 后台保活（安卓 WebView JS Bridge）：回复流程（检索→分析→回复→调度）期间
    // 持 CPU/WiFi 锁，用户切后台/锁屏也能完成回复；引用计数归零才释放。
    // 浏览器端（PC/服务器版）无 androidWakeLock，此段安全跳过
    if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.acquire(); }
    _pollStage(statusEl);   // 启动阶段进度轮询（回复到达后 finally 清除）
    const abortCtrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const abortTimer = abortCtrl ? setTimeout(() => abortCtrl.abort(), _CHAT_FETCH_TIMEOUT) : null;
    try {
        const fetchOpts = {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ messages: msgs, session_id: SESSION_ID, mode: CURRENT_MODE }),
        };
        // 旧 WebView 无 AbortController：不带 signal 字段，行为与之前一致
        if (abortCtrl) fetchOpts.signal = abortCtrl.signal;
        const resp = await fetch("/chat", fetchOpts);
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃回复（消息已写盘到原模式，不渲染）
        if (data.need_key) openSettings();
        else if (data.messages) {
            renderMessages(data.messages, "firefly", data);
            _notifyFirefly(data.messages);   // 切后台时回复完成通知（桥判断前台与否）
        }
        else if (data.reply) addTextMessage(data.reply, "firefly");
        if (data.error_code) _toast(ERROR_TIPS[data.error_code] || ERROR_TIPS.unknown);
        // R-07（服务器版）：请求的包在本端不存在时后端会静默回退默认包并打 mode_fallback 标记——
        // 必须显式告知（否则用户以为"角色坏了"而不知原因）
        if (data.mode_fallback) {
            _toast("该角色包在服务器模式不可用，已回退到「" +
                   (MODE_NAMES[data.mode_used] || data.mode_used || "默认包") + "」");
        }
        // data.queued：副请求，回复由主请求带回，无 UI 操作
    } catch (e) {
        if (gen === _modeGen && _inflight === 1) {
            if (abortCtrl && e && e.name === "AbortError") {
                // 上游卡住被超时中止：消息已即时写盘不丢，提示用户稍后再试
                addTextMessage("嗯…上游有点忙，我先不打扰了，过会儿再试试？", "firefly");
            } else {
                // 与后端 polisher.DEGRADED_TEXT 保持一致（降级话术单一来源，2026-09-04 合并）
                addTextMessage("嗯…信号不太好，等会儿再试试？", "firefly");
            }
        }
    } finally {
        if (abortTimer) clearTimeout(abortTimer);
        if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.release(); }
        _inflight--;
        if (_inflight === 0) {
            // 请求完成（主请求带回回复 / 全部副请求结束）：批提交作废（timer 常驻不清）
            _pendingBatch = 0; _lastMsgTs = 0;
            clearTimeout(S._hintTimer); S._hintTimer = null;
            clearTimeout(S._flushTimer); S._flushTimer = null;   // 兼容旧引用
            clearInterval(_stageTimer); _stageTimer = null;  // 阶段轮询结束
            if (statusEl) statusEl.textContent = defaultStatus;
            inputEl.focus();
            // 响应式回复完成后 ≥1s 防抖，触发主动式判断（主动式未触发则服务端串联概率式）
            setTimeout(() => { if (_idleOk()) checkProactive(); }, 1000);
        }
    }
}

async function send() {
    const text = inputEl.value.trim();
    if (!text || S.waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    inputEl.value = "";
    const q = _quoteTarget;
    addTextMessage(text, "user", false, null, q);
    inputEl.focus();
    // 0.8.1：进入提交窗口状态机（两判定+10条上限；窗口到期由 _batchCommit 触发 flush）
    clearTimeout(S._hintTimer);
    inputEl.placeholder = "还可以继续说…";   // 3.6：提交窗口内提示"还能补话"
    const msg = {type: "text", content: text};
    if (q) msg.quote = q;   // 引用随消息提交（后端写盘 + LLM 上下文）
    _chatSend([msg]);   // 统一消息对象类型，立即发送
    _batchArmSubmit();
    _clearQuote();
}

sendBtn.addEventListener("click", send);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
// 提交窗口控制（0.8.1 简化，两判定+上限，见 _batchArmSubmit 注释）：
// - 输入框有内容（打字中）→ 暂停提交 + hint 续期后端窗口（流萤继续等）
// - 输入框清空 → 由 1s 检查器按「距最新消息 5 秒」判定提交
inputEl.addEventListener("input", () => {
    clearTimeout(S._hintTimer);
    if (inputEl.value.trim()) {
        inputEl.placeholder = "说点什么…";   // 开始打字即还原提示（3.6）
        _sendHint();   // 立即续期一次 + 2s 节流循环
    } else {
        // 清空输入框：hint 停止（_sendHint 循环见框空即止），交给检查器按 5s 判定
        clearTimeout(S._hintTimer);
        S._hintTimer = null;
    }
});


/* ── 来源：js/chat_media.js ── */
// 聊天媒体：发图片（选图/压缩/上传/描述）/ 表情包面板与发送

// ═══════════════════════════════════════════
// 发图片（A9）：🖼 按钮 → 选图 → 压缩 → 上传落盘 → 描述 → 发送 image 消息
// （图片链路统一：本地版/服务器版都走 /upload-image；服务器版请求由 fetch 包装器带登录 Bearer）
// ═══════════════════════════════════════════
const imageBtn = document.getElementById("image-btn");
const imageFileInput = document.getElementById("image-file-input");
if (imageBtn && imageFileInput) {
    // 点击 🖼：进入媒体选择状态（暂停提交计时——相册选择是长期操作，
    // 否则第一条消息会在选图中途被 5 秒窗口提前提交）
    imageBtn.addEventListener("click", () => {
        _mediaBusy = true;
        imageFileInput.value = "";
        imageFileInput.click();
    });
    imageFileInput.addEventListener("change", async () => {
        // 选择结束（无论成功/取消/失败）：恢复计时（重新 5 秒）
        const endMedia = (arm = false) => {
            _mediaBusy = false;
            if (arm) {   // 成功发送：进入新批计数
                _batchArmSubmit();
            } else {     // 取消/失败：重新计时但不计入批数
                _batchRefreshTimer();
            }
        };
        // PC/浏览器取消选图不触发 change（只发 cancel）→ _mediaBusy 永远为真、批次永不提交。
        // 补 cancel 监听：取消/重开选择器都恢复计时（Kimi 复审 #2；安卓 WebView 取消同理）。
        imageFileInput.addEventListener("cancel", () => { endMedia(false); });
        const f = imageFileInput.files && imageFileInput.files[0];
        imageFileInput.value = "";
        if (!f || S.waiting) { endMedia(false); return; }
        if (!/^image\/(png|jpe?g|webp|gif)$/.test(f.type || "")) { _toast("仅支持 png/jpg/webp/gif 图片"); endMedia(false); return; }
        if (f.size > 10 * 1024 * 1024) { _toast("图片过大（上限 10MB）"); endMedia(false); return; }
        // 发送前压缩（imgzip.js）：GIF/小图原样；任何失败降级原图不阻塞发送
        const {blob, ext} = await compressImage(f);
        let imgId = "";
        try {
            const fd = new FormData();
            fd.append("file", blob, "upload." + ext);   // 压缩产物是匿名 Blob：补文件名让后端拿到正确扩展名
            fd.append("mode", CURRENT_MODE);
            const resp = await fetch("/upload-image", {method: "POST", body: fd});
            const data = await resp.json();
            if (!data.ok) { _toast("图片上传失败：" + (data.error || "")); endMedia(false); return; }   // 配额满等错误直接透传后端文案
            imgId = data.img_id || "";
        } catch (e) { _toast("网络错误，图片未发送"); endMedia(false); return; }
        // 2026-08-29：不再弹窗让用户描述——图片理解统一由模型原生识图（发图当轮注入原图）；
        // 模型真不支持时由后端 polisher 降级为"看不清"说明。
        const q = _quoteTarget;
        addImage({img_id: imgId, desc: ""}, "user");
        const msg = {type: "image", img_id: imgId};
        if (q) msg.quote = q;
        _chatSend([msg]);
        endMedia(true);   // 0.8.1：图片与文字同批合并（两判定+10条上限）
        _clearQuote();
    });
}

// ═══════════════════════════════════════════
// 表情包面板（输入框内 😊 按钮）
// ═══════════════════════════════════════════
const stickerPanel = document.getElementById("sticker-panel");
const stickerGrid = document.getElementById("sticker-grid");
const stickerBtn = document.getElementById("sticker-btn");

// 表情面板打开/关闭的提交计时管理（媒体选择中暂停提交）
function _stickerPanelClose() {
    stickerPanel.classList.remove("show");
    if (_mediaBusy) { _mediaBusy = false; _batchRefreshTimer(); }   // 重新计时
}

stickerBtn.addEventListener("click", async () => {
    if (stickerPanel.classList.contains("show")) {
        _stickerPanelClose();
        return;
    }
    _mediaBusy = true;   // 面板打开期间暂停提交（长期等待用户选择）
    stickerPanel.classList.add("show");
    // F-6.2（2026-09-14）：面板缓存按包失效 + 按包过滤——原来 dataset.loaded 一旦置位永不过期，
    // 切包后仍显示旧包（含他包专属）的表情；现在切包必重拉，且只显示 全局共享 + 本包专属。
    if (stickerGrid.dataset.loaded && stickerGrid.dataset.mode === CURRENT_MODE) return;
    try {
        const resp = await fetch("/stickers?enabled=1");
        const data = await resp.json();
        const list = (data.stickers || []).filter(s => !s.pack || s.pack === CURRENT_MODE);
        stickerGrid.innerHTML = list.map(s =>
            `<img src="${IS_SERVER ? API_BASE : ""}/assets/${escapeHtml(s.file)}" alt="${escapeHtml(s.label)}" data-label="${escapeHtml(s.label)}" data-file="${escapeHtml(s.file)}">`).join("");
        stickerGrid.dataset.loaded = "1";
        stickerGrid.dataset.mode = CURRENT_MODE;
        stickerGrid.querySelectorAll("img").forEach(img => {
            img.addEventListener("click", () => {
                _mediaBusy = false;   // 选中即结束媒体状态（sendStickerMessage 内 _batchArmSubmit 重新计时）
                _stickerPanelClose();
                sendStickerMessage(img.dataset.label, img.dataset.file);
            });
        });
    } catch (e) { /* 静默 */ }
});
// 点击聊天区关闭表情面板
messagesEl.addEventListener("click", _stickerPanelClose);
document.getElementById("sticker-panel-close").addEventListener("click", _stickerPanelClose);

/** 发送表情包：作为一条消息立即发送（与文字同一窗口合并，不碰输入框内容） */
function sendStickerMessage(label, file) {
    if (S.waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    const q = _quoteTarget;
    if (file) addSticker(file, "user", false, null, label, q);   // 本地立即渲染表情图
    inputEl.focus();
    clearTimeout(S._hintTimer);
    const msg = {type: "sticker", label, file};
    if (q) msg.quote = q;
    _chatSend([msg]);
    _batchArmSubmit();   // 0.8.1：表情与文字同批合并（两判定+10条上限）
    _clearQuote();
}


/* ── 来源：js/chat_history.js ── */
// 聊天历史：历史加载（滚动翻页）/ 休息 / 清除 / 撤回

// 当前角色名统一走 views.charName()（角色卡化：框架不含角色名字面量）。
// 休息/起床的**提示句**需要一个语法上的主语，取不到时用中性词「角色」。
function _cn() {
    return charName() || "角色";
}

// ═══════════════════════════════════════════
// 记忆窗口提醒（聊天页头部的名字行尾，2026-09-18）
//
// 活跃窗口 = "自上次整理以来"的对话原文，除刚开场外常驻 30–100 轮：
//   · 分析器 / 回复器读它（所以压缩后也照样看得到最近 30 轮完整对话）
//   · 整理只把"最近 keep_turns 轮以外"的部分搬进「历史对话存档」（只进检索器）
// 阈值故意放得很宽（100 轮才触发）——整理一次要花 2 次 LLM 调用，频繁整理就是烧用户 token。
//
// 显示形态（用户 2026-09-18 反馈"太影响观感"后定的）：
//   **只是名字行尾一小截灰字**（`· 记忆 92/100`），不加行、不加边框、不加按钮。
//   理由是它属"低优先级知会"——手动整理入口本来就在菜单「让流萤休息」，
//   到上限后下一条回复也会自动整理，不需要再给按钮。
// 数据源 GET /memory-status（只读计数 + 游标文件，很轻）。
// ═══════════════════════════════════════════
const memHint = document.getElementById("chat-mem");
const appViewEl = document.getElementById("app");

function _chatVisible() {
    return !!appViewEl && appViewEl.style.display !== "none";
}

async function memoryBarRefresh() {
    if (!memHint) return;
    if (!_chatVisible() || typeof CURRENT_MODE === "undefined" || !CURRENT_MODE) {
        memHint.hidden = true;
        return;
    }
    try {
        const r = await fetch(`/memory-status?mode=${encodeURIComponent(CURRENT_MODE)}`);
        const d = await r.json();
        const lv = d.level || "ok";
        // ok/empty/off：不打扰（off = 自动整理被 config 关掉，用户既然关了就别提醒）
        if (lv !== "warn" && lv !== "full") { memHint.hidden = true; return; }
        memHint.hidden = false;
        memHint.className = "chat-mem " + lv;
        memHint.textContent = lv === "full"
            ? `· 记忆 ${d.active_turns}/${d.window_max} · 将整理`
            : `· 记忆 ${d.active_turns}/${d.window_max}`;
    } catch (e) {
        memHint.hidden = true;   // 提醒永不阻塞聊天：取不到就干脆不显示
    }
}
window.memoryBarRefresh = memoryBarRefresh;

// 轮询：本地端点、响应 ~200 字节；聊天页不可见时函数自己直接返回
setInterval(memoryBarRefresh, 15000);

// ═══════════════════════════════════════════
// 休息 / 清除 / 撤回
// ═══════════════════════════════════════════
const restOverlay = document.getElementById("rest-overlay");
// 休息结果展示后 3 秒自动收起（修复：原来先隐藏遮罩再写文案，用户看不见结果）
function _showRestResult(txt) {
    document.getElementById("rest-text").textContent = txt;
    setTimeout(() => { restOverlay.style.display = "none"; }, 3000);
}
/** 执行一次「休息」（整理）。菜单按钮与顶部状态条的「现在整理」共用同一条链。 */
async function _doRest() {
    restOverlay.style.display = "flex";
    document.getElementById("rest-text").textContent = `${_cn()}正在整理记忆…`;
    try {
        const resp = await fetch("/rest", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        // 0.8.1：文案用真实变化——added/resolved 为 LLM 解析数组（可能为空但头部/手账已更新）
        let msg = "";
        if (data.ok) {
            if (data.skipped) {
                msg = `${_cn()}已休息。这边没有新的对话内容需要整理，下次聊完再叫我吧。`;
            } else {
                const parts = [];
                if (data.added) parts.push(`新增记忆 ${data.added} 条`);
                if (data.resolved) parts.push(`解决 ${data.resolved} 条`);
                if (!parts.length && data.head_changed) parts.push("记忆已更新");
                msg = `${_cn()}已休息。${parts.join("，") || "记忆已更新"}。下次见。`;
            }
        } else {
            msg = "整理出了点问题：" + (data.error || "未知");
        }
        _showRestResult(msg);
    } catch (e) {
        _showRestResult("信号不好，等会儿再试。");
    }
}

document.getElementById("menu-rest-btn").addEventListener("click", async () => {
    if (!confirm(`让${_cn()}去休息吗？${_cn()}会整理这段对话的记忆。`)) return;
    closeMenu();
    await _doRest();
    memoryBarRefresh();
});

document.getElementById("menu-clear-btn").addEventListener("click", async () => {
    if (!confirm("确认清除全部对话历史？此操作不可撤销。")) return;
    closeMenu();
    try {
        const resp = await fetch("/clear-history", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (data.ok) { messagesEl.innerHTML = ""; }
    } catch (e) { alert("网络错误"); }
});

const undoBtn = document.getElementById("menu-undo-btn");
undoBtn.addEventListener("click", async () => {
    if (!confirm("撤回上一轮对话？")) return;
    closeMenu();
    undoBtn.disabled = true;
    try {
        const resp = await fetch("/undo", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (data.ok) {
            // 删除最后一段连续 user 块及其后的所有消息（整轮）
            const rows = messagesEl.querySelectorAll(".msg-row");
            const users = [...rows].filter(r => r.classList.contains("user"));
            if (users.length > 0) {
                let start = users[users.length - 1];
                let sib = start.previousElementSibling;
                while (sib && sib.classList.contains("msg-row") && sib.classList.contains("user")) {
                    start = sib; sib = sib.previousElementSibling;
                }
                let node = start;
                while (node) {
                    const nxt = node.nextElementSibling;
                    node.remove();
                    node = nxt;
                }
            }
        }
    } catch (e) {}
    undoBtn.disabled = false;
});

// ═══════════════════════════════════════════
// 历史加载
// ═══════════════════════════════════════════
let _loading = false;
let _lastWho = null;
function renderHistoryMessage(m, prepend=false) {
    const ts = m.time ? m.time.slice(11,16) : null;
    // 发送方变化时插入时间分割线
    if (m.who !== _lastWho && ts) {
        addTimeDivider(ts);
        _lastWho = m.who;
    }
    if (m.type==="sticker") addSticker(m.path, m.who, prepend, m.seq, m.label, m.quote);
    else if (m.type==="narration") addNarration(m.text, m.style, prepend, m.seq);
    else if (m.type==="image") addImage(m, m.who, prepend, m.seq, m.quote);
    else addTextMessage(m.content, m.who, prepend, m.seq, m.quote);
}
async function loadHistory(beforeSeq=null) {
    if (_loading) return; _loading = true;
    const gen = _modeGen;   // 捕获发起时的模式代际
    const url = beforeSeq ? `/history?limit=150&before_seq=${beforeSeq}&mode=${CURRENT_MODE}` : `/history?limit=150&mode=${CURRENT_MODE}`;
    _lastWho = null;
    try {
        const resp = await fetch(url);
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃旧模式历史，防止渲染进新模式界面
        if (!data.messages || data.messages.length===0) { S._hasMore=false; return; }
        if (!beforeSeq) { data.messages.forEach(m=>renderHistoryMessage(m,false)); messagesEl.scrollTop=messagesEl.scrollHeight; undoBtn.disabled=false; }
        else { const ph=messagesEl.scrollHeight, ps=messagesEl.scrollTop; data.messages.slice().reverse().forEach(m=>renderHistoryMessage(m,true)); messagesEl.scrollTop=ps+(messagesEl.scrollHeight-ph); }
        S._hasMore = !!data.has_more;
        // 历史渲染完再补扫一次语音条（首次进来时缓存表是异步拉的，赶不上逐条渲染）
        if (typeof voiceRestoreBarsAuto === "function") voiceRestoreBarsAuto();
        if (!beforeSeq) memoryBarRefresh();   // 进聊天页时同步一次记忆窗口状态
    } catch(e) {} finally { _loading=false; }
}
messagesEl.addEventListener("scroll", () => {
    if (messagesEl.scrollTop===0 && S._hasMore && !_loading) {
        const first = messagesEl.firstChild;
        const seq = first ? parseInt(first.dataset.seq) : null;
        if (seq) loadHistory(seq);
    }
});


/* ── 来源：js/voice_plugin.js ── */
// ═══════════════════════════════════════════
// 语音插件页（首页 →「语音插件」）
//   GET  /voice/plugin              → 状态（5 态 + 缺什么 + 下载进度）
//   POST /voice/plugin {action,...} → install/cancel/enable/disable/uninstall/rescan/set_mood/set_repo
// 设计与边界见 docs/工具/tts.md。本页只做展示与派发，不做任何模型逻辑。
// ═══════════════════════════════════════════
let _vpTimer = null;

const _VP_LABEL = {
    downloading: "⏳ 下载中",
    not_installed: "⬇ 未安装",
    installed_disabled: "⏸ 已安装 · 未启用",
    unavailable: "⚠ 已启用 · 不可用",
    ready: "✅ 可用",
};

function _vpEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
        {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function _vpMB(n) {
    return (n / 1048576).toFixed(1) + " MB";
}

async function _vpFetchState() {
    try {
        const r = await fetch("/voice/plugin");
        return await r.json();
    } catch (e) {
        return {id: "unavailable", label: "无法连接后端", reason: String(e)};
    }
}

async function _vpAct(action, extra) {
    try {
        const r = await fetch("/voice/plugin", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(Object.assign({action: action}, extra || {})),
        });
        const d = await r.json();
        if (d && d.ok === false) showToast("操作失败：" + (d.error || "未知原因"));
        return d;
    } catch (e) {
        showToast("操作失败：无法连接后端");
        return null;
    }
}

function _vpRender(st) {
    const box = document.getElementById("vv-body");
    if (!box) return;
    const dl = st.download || {};
    const moodRows = Object.keys(st.moods || {}).map(k => {
        const on = (st.mood === k);
        return `<button class="vv-mood${on ? " on" : ""}" type="button"
             onclick="voiceSetMood('${_vpEsc(k)}')">${_vpEsc(st.moods[k])}</button>`;
    }).join(" ");

    // 操作按钮：按状态给出可用的那几个
    const btns = [];
    if (st.id === "downloading") {
        btns.push(`<button class="vv-btn" onclick="voiceAct('cancel')">取消下载</button>`);
    } else {
        if (st.repo_configured) {
            btns.push(`<button class="vv-btn primary" onclick="voiceAct('install')">${st.models_found ? "重新下载" : "下载模型"}</button>`);
        }
        btns.push(`<button class="vv-btn" onclick="voiceAct('rescan')">重新扫描</button>`);
        const vc = st.voice_cache || {};
        if (vc.files) {
            btns.push(`<button class="vv-btn danger" onclick="voiceAct('purge_voice')">清除语音缓存（${vc.files} 条）</button>`);
        }
        if (st.models_found) {
            btns.push(st.enabled
                ? `<button class="vv-btn" onclick="voiceAct('disable')">停用</button>`
                : `<button class="vv-btn primary" onclick="voiceAct('enable')">启用</button>`);
            btns.push(`<button class="vv-btn danger" onclick="voiceAct('uninstall')">卸载</button>`);
        }
    }

    // 下载进度：带 sha256 对账状态（2026-09-18）。
    //   verify=on  → 拿到了仓库清单，每个文件下完都会比对 sha256（坏文件不会落盘）
    //   verify=off → 拿不到清单（自建镜像无清单端点等），降级为只比大小
    const _vfy = dl.verify === "on"
        ? `· 已校验 ${dl.verified || 0} 个`
        : (dl.verify === "off" ? "· 未校验（仓库无清单）" : "");
    const _pct = Math.max(0, Math.min(100, Number(dl.percent) || 0));   // 纵深防御：后端若给出越界值也不显示
    const dlBar = dl.running ? `
      <div class="vv-dl">
        <div class="vv-dl-bar"><i style="width:${_pct}%"></i></div>
        <div class="vv-dl-txt">${_vpEsc(dl.file || "")} · ${_pct}%
          ${dl.total ? "（" + _vpMB(dl.done) + " / " + _vpMB(dl.total) + "）" : ""}
          · 已用 ${dl.elapsed || 0}s ${_vfy}</div>
      </div>` : (dl.error ? `<div class="vv-err">上次下载：${_vpEsc(dl.error)}</div>`
        : (dl.verified ? `<div class="vv-hint">上次下载已通过 sha256 校验（${dl.verified} 个文件）</div>` : ""));

    const missing = (st.missing_files || []).length ? `
      <div class="vv-err">缺 ${st.missing_files.length} 个文件：${_vpEsc(st.missing_files.join("、"))}</div>` : "";

    const repoBox = st.repo_configured ? "" : `
      <div class="vv-field">
        <div class="vv-hint">模型仓库地址（含 <code>{file}</code> 占位；留空则无法下载）</div>
        <input id="vv-repo" type="text" placeholder="https://www.modelscope.cn/api/v1/models/&lt;ns&gt;/&lt;name&gt;/repo?Revision=master&amp;FilePath={file}">
        <button class="vv-btn" onclick="voiceAct('set_repo',{url:document.getElementById('vv-repo').value})">保存地址</button>
      </div>`;

    box.innerHTML = `
      <div class="vv-card vv-state vv-state-${_vpEsc(st.id)}">
        <div class="vv-state-label">${_vpEsc(_VP_LABEL[st.id] || st.label || st.id)}</div>
        ${st.reason ? `<div class="vv-reason">${_vpEsc(st.reason)}</div>` : ""}
      </div>

      ${dlBar}${missing}

      <div class="vv-card">
        <div class="vv-h">语气</div>
        <div class="vv-moods">${moodRows}</div>
        <div class="vv-hint">作用范围：长按消息 →「转语音」时使用所选语气</div>
      </div>

      <div class="vv-card">
        <div class="vv-h">模型</div>
        <div class="vv-kv"><span>状态</span><b>${st.models_found ? "已就位" : "未安装"}</b></div>
        <div class="vv-kv"><span>体积</span><b>${st.models_found ? _vpMB(st.model_bytes) : "—"}</b></div>
        <div class="vv-kv"><span>目录</span><code>${_vpEsc(st.models_dir || (st.search_dirs || [])[0] || "")}</code></div>
        ${st.engine && st.engine.ready ? `<div class="vv-kv"><span>引擎</span><b>已装载</b></div>` : ""}
        ${(st.voice_cache && st.voice_cache.files)
            ? `<div class="vv-kv"><span>已生成语音</span><b>${st.voice_cache.files} 条 · ${_vpMB(st.voice_cache.bytes)}</b></div>`
            : ""}
      </div>

      <div class="vv-actions">${btns.join(" ")}</div>
      ${repoBox}

      <div class="vv-foot">
        语音模型来自第三方整合包；<b>语音原声版权归米哈游所有</b>，仅供个人二次创作使用，
        禁止售卖与再分发。模型仅存放在本机，不会上传到服务器。
      </div>`;
}

async function renderVoiceView() {
    const box = document.getElementById("vv-body");
    if (box && !box.innerHTML.trim()) box.innerHTML = `<div class="vv-loading">读取中…</div>`;
    const st = await _vpFetchState();
    _vpRender(st);
    // 下载中 → 自动轮询刷新进度
    if (_vpTimer) { clearTimeout(_vpTimer); _vpTimer = null; }
    if (st.id === "downloading") {
        _vpTimer = setTimeout(renderVoiceView, 1200);
    }
}

function openVoiceView() {
    homeView.classList.remove("show");
    const cv = document.getElementById("cards-view");
    if (cv) cv.classList.remove("show");
    const fv = document.getElementById("fix-view");
    if (fv) fv.classList.remove("show");
    const v = document.getElementById("voice-view");
    if (!v) return;
    v.classList.add("show");
    try { if (location.hash !== "#voice") history.pushState({voice: true}, "", "#voice"); } catch (e) {}
    renderVoiceView();
}
window.openVoiceView = openVoiceView;

function closeVoiceView() {
    const v = document.getElementById("voice-view");
    if (v) v.classList.remove("show");
    if (_vpTimer) { clearTimeout(_vpTimer); _vpTimer = null; }
    showHome();
    try { if (location.hash === "#voice") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closeVoiceView = closeVoiceView;

async function voiceAct(action, extra) {
    if (action === "uninstall" && !confirm("确定卸载语音插件？\n\n会删除本机的模型文件（约 872 MB）。\n已生成的语音不受影响（那些随「清理历史」处理）。")) return;
    if (action === "purge_voice" && !confirm("清除所有已生成的语音？\n\n只删音频文件 —— 模型、语气库、聊天记录都不受影响。\n清除后重新点「转语音」会用当前前端重新合成（旧音频可能带有已修复的问题）。")) return;
    await _vpAct(action, extra);
    renderVoiceView();
}
window.voiceAct = voiceAct;

async function voiceSetMood(mood) {
    await _vpAct("set_mood", {mood: mood});
    // 同步长按菜单用的默认语气（chat.js 的 _voiceMood）
    if (window.setVoiceMood) window.setVoiceMood(mood);
    renderVoiceView();
}
window.voiceSetMood = voiceSetMood;

// ═══════════════════════════════════════════
// 消息下面的「语音条」
//   参考微信「语音转文字」的形态：语音生成后**贴在消息气泡下方**（不是弹播放器）。
//   数据来源：/voice/plugin 的 voice_cache.seqs = {mode: {seq: 时长}}。
//   · 生成成功后立刻贴上去
//   · 历史渲染后再扫一遍贴回（刷新/翻页后仍在）
//   · 点它播放 / 再点暂停（播放区在条上，长按菜单不再承担"播放"职责）
// ═══════════════════════════════════════════
let _voiceSeqMap = null;        // {mode: {seq: dur}}
let _voiceAudio = null;
let _voicePlayingBar = null;

async function voiceSyncCache() {
    try {
        const st = await (await fetch("/voice/plugin")).json();
        _voiceSeqMap = (st && st.voice_cache && st.voice_cache.seqs) || {};
    } catch (e) {
        _voiceSeqMap = null;
    }
    return _voiceSeqMap;
}
window.voiceSyncCache = voiceSyncCache;

/** 该 seq 是否已有语音（同步查本地缓存表） */
function voiceHas(seq) {
    const m = _voiceSeqMap && _voiceSeqMap[CURRENT_MODE];
    return !!(m && Object.prototype.hasOwnProperty.call(m, String(seq)));
}
window.voiceHas = voiceHas;

function _voiceStopPlay() {
    if (_voiceAudio) { try { _voiceAudio.pause(); } catch (e) {} _voiceAudio = null; }
    if (_voicePlayingBar) {
        _voicePlayingBar.classList.remove("playing");
        const i = _voicePlayingBar.querySelector(".vb-ico");
        if (i) i.textContent = "🔊";
        _voicePlayingBar = null;
    }
}

function _voicePlayBar(bar, seq) {
    // 再点 = 暂停
    if (_voicePlayingBar === bar) { _voiceStopPlay(); return; }
    _voiceStopPlay();
    const a = new Audio("/voice-file?mode=" + encodeURIComponent(CURRENT_MODE)
                        + "&name=v" + seq + ".wav");
    _voiceAudio = a;
    _voicePlayingBar = bar;
    bar.classList.add("playing");
    const ico = bar.querySelector(".vb-ico");
    if (ico) ico.textContent = "⏸";
    const stop = () => { if (_voicePlayingBar === bar) _voiceStopPlay(); };
    a.addEventListener("ended", stop);
    a.addEventListener("error", () => { stop(); showToast("播放失败"); });
    a.play().catch(() => { stop(); showToast("播放失败（再点一次试试）"); });
}

/** 把语音条贴到某条消息下面（幂等）。返回条元素。
 *
 *  ★ 语音条是 `.msg-row` 的**直接子元素**，靠 `.msg-row.has-voice{flex-wrap:wrap}`
 *    换到第二行 —— 这样**头像仍与气泡底部对齐**（微信/QQ 的形态）。
 *    早先把条塞进 `.msg-col` 时，列变高 → `align-items:flex-end` 把头像拉到语音条底，
 *    头像会比气泡低一截（真机截图发现）。
 */
function voiceAttachBar(row, seq, dur) {
    if (!row || seq === null || seq === undefined) return null;
    const exist = row.querySelector(".voice-bar");
    if (exist) {
        if (dur) {
            const d = exist.querySelector(".vb-dur");
            if (d) d.textContent = Math.round(dur) + "″";
        }
        return exist;
    }
    if (!row.querySelector(".bubble")) return null;   // 没气泡的（表情包/旁白）不贴
    row.classList.add("has-voice");
    // 外层 .voice-row 占满一行（强制换行 → 头像仍与气泡底部对齐），
    // 内层 .voice-bar 保持内容宽度（早先直接给条 flex-basis:100% 会把它拉成整行宽）
    const wrap = document.createElement("div");
    wrap.className = "voice-row";
    const bar = document.createElement("div");
    bar.className = "voice-bar";
    bar.dataset.seq = String(seq);
    bar.title = "点击播放";
    bar.innerHTML = `<span class="vb-ico">🔊</span><span class="vb-dur">`
        + (dur ? Math.round(dur) + "″" : "…") + `</span>`;
    bar.addEventListener("click", (e) => { e.stopPropagation(); _voicePlayBar(bar, seq); });
    wrap.appendChild(bar);
    row.appendChild(wrap);
    return bar;
}
window.voiceAttachBar = voiceAttachBar;

/** 历史渲染后扫一遍，把已有语音的条贴回去 */
function voiceRestoreBars() {
    const m = _voiceSeqMap && _voiceSeqMap[CURRENT_MODE];
    if (!m) return 0;
    const root = (typeof messagesEl !== "undefined" && messagesEl) ? messagesEl : document;
    let n = 0;
    root.querySelectorAll(".msg-row[data-seq]").forEach((row) => {
        const s = parseInt(row.dataset.seq, 10);
        if (isNaN(s)) return;
        const k = String(s);
        if (Object.prototype.hasOwnProperty.call(m, k) && voiceAttachBar(row, s, m[k])) n++;
    });
    return n;
}
window.voiceRestoreBars = voiceRestoreBars;

/** 首次进聊天时：拉一次缓存表再贴 */
async function voiceInitBars() {
    await voiceSyncCache();
    voiceRestoreBars();
}
window.voiceInitBars = voiceInitBars;

/** 渲染后自动贴（首次会先拉一次缓存表；之后直接用内存里的表） */
let _voiceCacheSynced = false;
async function voiceRestoreBarsAuto() {
    try {
        if (!_voiceCacheSynced) {
            await voiceSyncCache();
            _voiceCacheSynced = true;
        }
        voiceRestoreBars();
    } catch (e) { /* 语音条失败绝不影响消息渲染 */ }
}
window.voiceRestoreBarsAuto = voiceRestoreBarsAuto;


/* ── 来源：js/fix.js ── */
// 设定纠错助手视图

// ═══════════════════════════════════════════
// 设定纠错助手（对齐 → 开始修改 → diff 审批 → 应用/回滚）
// ═══════════════════════════════════════════
const FIX_FILE_LABELS = {
    "core.md": "核心设定", "identity.md": "关系与习惯", "sms_samples.md": "短信风格",
    "用户设定.md": "用户补充设定", "memory.md": "过往摘要", "手账.md": "手账",
};
let FIX_MODE = "story";   // 首页卡片选择的模式；进入聊天后跟随最近使用模式
let _fixBusy = false;


function fixModeLabel(mode) { return MODE_NAMES[mode] || mode; }

function openFixView() {
    // F-6.1（2026-09-14）：进入纠错页跟随当前聊天包——原来 FIX_MODE 恒为初值 story，
    // 用户在 haruno/自建包里点「指出问题」，读的是 story 的历史与设定、写的也是 story。
    // 页内模式按钮仍可手动切换（setFixMode 语义不变）。
    if (PRESET_MODES.some(m => m.id === CURRENT_MODE)) FIX_MODE = CURRENT_MODE;
    document.querySelectorAll("#fix-view .fix-mode").forEach(b => {
        b.classList.toggle("active", b.dataset.mode === FIX_MODE);
    });
    homeView.classList.remove("show");
    appView.style.display = "none";
    const view = document.getElementById("fix-view");
    if (view) view.classList.add("show");
    try { if (location.hash !== "#fix") history.pushState({fix: true}, "", "#fix"); } catch (e) {}
    loadFixStatus();
    loadFixChatHistory();
    const input = document.getElementById("fix-input");
    // 引导教程演示纠错页时不要弹键盘（会遮住底部讲解气泡）
    if (input && !document.getElementById("guide-mask")) setTimeout(() => input.focus(), 300);
}
window.openFixView = openFixView;

function closeFixView() {
    const view = document.getElementById("fix-view");
    if (view) view.classList.remove("show");
    showHome();
    try { if (location.hash === "#fix") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closeFixView = closeFixView;

function toggleFixForms() {
    // 兼容旧入口：现在一律打开独立全屏页
    openFixView();
}
window.toggleFixForms = toggleFixForms;

function setFixMode(mode) {
    // 模式校验按预设包注册表（不写死两模式）
    if (!PRESET_MODES.some(m => m.id === mode)) mode = (PRESET_MODES[0] && PRESET_MODES[0].id) || "story";
    FIX_MODE = mode;
    document.querySelectorAll("#fix-view .fix-mode").forEach(b => {
        b.classList.toggle("active", b.dataset.mode === FIX_MODE);
    });
    loadFixStatus();
    loadFixChatHistory();
}
window.setFixMode = setFixMode;

function _fixChatScroll() {
    const el = document.getElementById("fix-chat");
    if (el) el.scrollTop = el.scrollHeight;
}

function _setFixStatus(stage) {
    const dot = document.getElementById("fix-status-dot");
    const text = document.getElementById("fix-view-status");
    if (!dot || !text) return;
    const map = {
        idle:     ["ok", "状态正常 · 等待你描述问题"],
        aligning: ["busy", "AI 正在和你对齐问题…"],
        ready:    ["ready", "已对齐 · 点「开始修改」生成清单"],
        proposal: ["warn", "方案待确认 · 点「应用修改」才生效"],
        busy:     ["busy", "AI 正在处理，请稍候…"],
        error:    ["error", "处理出错 · 请重试"],
    };
    const v = map[stage] || map.idle;
    dot.className = "fix-dot " + v[0];
    text.textContent = v[1];
}

function _fixHistText(m) {
    if (!m) return "";
    if (m.type === "sticker") return "[表情包：" + (m.label || m.path || m.file || "") + "]";
    if (m.type === "narration") return (m.text || m.content || "");
    return m.content || m.text || "";
}

function _fixHistMsgHtml(m) {
    const me = m.who === "user";
    return `<div class="fix-hist-msg ${me ? "me" : ""}">
        <div class="fix-hist-line">
            <span class="fix-hist-who">${me ? "我" : escapeHtml(charName())}</span>
            <span class="fix-hist-time">${escapeHtml((m.time || "").slice(5, 16))}</span>
        </div>
        <div class="fix-hist-text">${escapeHtml(_fixHistText(m))}</div>
    </div>`;
}

async function loadFixChatHistory() {
    const meta = document.getElementById("fix-chathist-meta");
    const count = document.getElementById("fix-chathist-count");
    const list = document.getElementById("fix-chathist-list");
    if (!list) return;
    try {
        const resp = await fetch(`/history?limit=20&mode=${encodeURIComponent(FIX_MODE)}`);
        const data = await resp.json();
        const msgs = Array.isArray(data.messages) ? data.messages : [];
        const total = data.total != null ? data.total : msgs.length;
        const label = fixModeLabel(FIX_MODE);
        if (meta) meta.textContent = msgs.length ? `${label} · 最近 ${msgs.length} 条` : `${label} · 暂无聊天记录`;
        if (count) count.textContent = msgs.length ? `显示最近 ${msgs.length} 条 / 共 ${total} 条` : "这个模式还没有聊天记录";
        list.innerHTML = msgs.length
            ? msgs.map(_fixHistMsgHtml).join("")
            : `<div class="fix-hist">这个模式还没有聊天记录；先去聊几句，再来描述问题会更方便。</div>`;
    } catch (e) {
        if (count) count.textContent = "聊天记录读取失败";
        list.innerHTML = `<div class="fix-hist">聊天记录读取失败（本地后端未就绪时会这样，不影响对齐功能）</div>`;
    }
}
window.loadFixChatHistory = loadFixChatHistory;

function _fixMsgHtml(m) {
    const who = m.who === "user" ? "我" : "设定助手";
    const cls = m.who === "user" ? "me" : "ai";
    const opts = (m.options || []).map((o, i) =>
        `<button class="fix-opt" data-opt="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join("");
    return `<div class="fix-msg ${cls}"><div class="fix-who">${who}</div>`
         + `<div class="fix-text">${escapeHtml(m.text)}</div>`
         + (opts ? `<div class="fix-options">${opts}</div>` : "") + `</div>`;
}

function _fixChangeHtml(ch) {
    const tag = ch.op === "append" ? "补充" : "纠正";
    const oldHtml = ch.op === "replace"
        ? `<div class="fix-diff-old">− ${escapeHtml(ch.old)}</div>` : "";
    return `<div class="fix-change">
        <div class="fix-change-head">
            <span class="fix-file-tag">${escapeHtml(FIX_FILE_LABELS[ch.file] || ch.file)}</span>
            <span class="fix-op-tag ${ch.op === "append" ? "add" : "fix"}">${tag}</span>
        </div>
        ${oldHtml}
        <div class="fix-diff-new">+ ${escapeHtml(ch.new)}</div>
        <div class="fix-reason">${escapeHtml(ch.reason || "")}</div>
    </div>`;
}

function _renderFix(status) {
    const chat = document.getElementById("fix-chat");
    const proposal = document.getElementById("fix-proposal");
    const startBtn = document.getElementById("fix-start-btn");
    const historyBox = document.getElementById("fix-history-box");
    if (!chat || !proposal || !startBtn) return;

    _setFixStatus(status.stage || "idle");

    if (Array.isArray(status.messages) && status.messages.length) {
        chat.innerHTML = status.messages.map(_fixMsgHtml).join("");
        _fixChatScroll();
    } else {
        chat.innerHTML = `<div class="fix-empty">先说说角色哪里说得不对，我会和你确认后再生成修改方案。</div>`
                       + `<div class="fix-hint">例如：角色还说自己在医疗舱，但设定里已经恢复得不错、能开机甲了。</div>`;
    }

    // 选项 chips：只在没有 pending 时启用（有 pending 时是改方案，选项已过期）
    chat.querySelectorAll(".fix-opt").forEach(btn => {
        btn.addEventListener("click", () => {
            if (_fixBusy || status.stage === "proposal") return;
            sendFixMessage(btn.dataset.opt);
        });
    });

    startBtn.style.display = (status.stage === "ready") ? "" : "none";
    startBtn.disabled = !!_fixBusy;

    // 提案面板
    if (status.stage === "proposal" && status.proposal) {
        const p = status.proposal;
        const changes = Array.isArray(p.changes) ? p.changes : [];
        proposal.style.display = "block";
        proposal.innerHTML = `<div class="fix-proposal-title"><span>修改清单（尚未生效）</span><span class="fix-op-tag">待确认</span></div>`
            + `<div class="fix-diag">${escapeHtml(p.diagnosis || "已生成修改方案，请确认后应用。")}</div>`
            + (changes.length ? changes.map(_fixChangeHtml).join("") : `<div class="fix-nochange">这次不需要修改设定文件。</div>`)
            + `<div class="fix-proposal-actions">
                 <button class="hb-btn" type="button" onclick="dismissFix()">放弃</button>
                 ${changes.length ? `<button class="hb-btn primary" type="button" onclick="applyFix()">应用修改</button>` : ""}
               </div>
               <div class="fix-refine-hint">想调整某一条？直接在下方说，例如：第二条先别改，橡木蛋糕卷那段保留。</div>`;
    } else {
        proposal.style.display = "none";
    }

    // 修正记录：列表 + 静态撤销按钮（全屏页固定位置，便于拇指操作）
    const hist = Array.isArray(status.history) ? status.history : [];
    const historyList = document.getElementById("fix-history-list") || historyBox;
    let histHtml = "";
    if (hist.length) {
        histHtml = hist.map(h => `<div class="fix-hist">v${h.v} · ${escapeHtml(h.action === "apply" ? "应用" : "回滚")} · ${escapeHtml((h.time || "").slice(5, 16))}${h.files ? " · " + escapeHtml(h.files.join("、")) : ""}</div>`).join("");
    } else {
        histHtml = `<div class="fix-hist">还没有修改记录</div>`;
    }
    historyList.innerHTML = histHtml;
    const rb = document.getElementById("fix-rollback-btn");
    if (rb) {
        rb.style.display = status.active_version > 0 ? "" : "none";
        rb.textContent = `撤销上次修改（回到 v${Math.max(0, status.active_version - 1)}）`;
    }
}

async function loadFixStatus(force) {
    if (_fixBusy && !force) return;
    try {
        const resp = await fetch(`/setting-fix/status?mode=${encodeURIComponent(FIX_MODE)}`);
        const data = await resp.json();
        if (data.ok) _renderFix(data);
        else if (data.error) _setFixStatus("error");
    } catch (e) { _setFixStatus("error"); }
}
window.loadFixStatus = loadFixStatus;

async function sendFixMessage(text) {
    text = (text || "").trim();
    if (!text || _fixBusy) return;
    const input = document.getElementById("fix-input");
    if (input) input.value = "";
    _fixBusy = true;
    _setFixStatus("busy");
    const sendBtn = document.getElementById("fix-send-btn");
    if (sendBtn) sendBtn.disabled = true;
    const chat = document.getElementById("fix-chat");
    if (chat) {
        chat.insertAdjacentHTML("beforeend", _fixMsgHtml({who: "user", text: text}));
        _fixChatScroll();
    }
    try {
        const resp = await fetch("/setting-fix/message", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE, text: text }),
        });
        const data = await resp.json();
        if (data.ok) {
            await loadFixStatus(true);
        } else if (data.need_key) {
            showToast("请先到 ⚙ 设置里填写 API Key");
            openSettings();
        } else {
            // A3：LLM 错误分类 → 人话提示（与聊天页 ERROR_TIPS 同口径）
            const FIX_ERROR_TIPS = {
                key_invalid: "API Key 无效或已过期，请到设置中检查",
                no_balance: "API 余额不足，请充值后再试",
                rate_limit: "请求太频繁，稍等一会儿再试试",
                network: "网络不通，请检查网络后重试",
                server_error: "服务端暂时出错，请稍后再试",
                relay_timeout: "代发超时，请检查网络后重试",
                timeout: "回复超时了，稍后再试一次吧",
                cooldown: "上游服务波动中，休息一下再试试",
                quota_exhausted: "今日服务器托管额度已用完，可切换为自带 Key 模式",
                unknown: "出了点问题，请稍后再试",
            };
            showToast(data.error_code
                ? (FIX_ERROR_TIPS[data.error_code] || FIX_ERROR_TIPS.unknown)
                : (data.error || "分析失败，请稍后再试"));
        }
    } catch (e) {
        showToast("网络错误，请稍后再试");
    } finally {
        _fixBusy = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}
window.sendFixMessage = sendFixMessage;

async function startFix() {
    if (_fixBusy) return;
    _fixBusy = true;
    _setFixStatus("busy");
    const btn = document.getElementById("fix-start-btn");
    if (btn) { btn.disabled = true; btn.textContent = "正在生成修改清单…"; }
    try {
        const resp = await fetch("/setting-fix/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        if (data.ok) {
            showToast("修改清单已生成，确认后再点应用");
            await loadFixStatus(true);
        } else if (data.need_key) {
            showToast("请先到 ⚙ 设置里填写 API Key");
            openSettings();
        } else {
            showToast(data.error || "生成失败，请稍后再试");
        }
    } catch (e) {
        showToast("网络错误，请稍后再试");
    } finally {
        _fixBusy = false;
        if (btn) { btn.disabled = false; btn.textContent = "开始修改"; }
    }
}
window.startFix = startFix;

async function applyFix() {
    if (_fixBusy) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/apply", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE, session_id: SESSION_ID }),
        });
        const data = await resp.json();
        if (data.ok) {
            showToast(data.message || "修改已生效");
            await loadFixStatus(true);
            loadFixChatHistory();
        } else {
            showToast(data.error || "应用失败");
        }
    } catch (e) {
        showToast("网络错误，请稍后再试");
    } finally {
        _fixBusy = false;
    }
}
window.applyFix = applyFix;

async function dismissFix() {
    if (_fixBusy) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/dismiss", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        showToast(data.ok ? "已放弃本次修改方案" : (data.error || "操作失败"));
        await loadFixStatus(true);
    } catch (e) {
        showToast("网络错误，请稍后再试");
    } finally {
        _fixBusy = false;
    }
}
window.dismissFix = dismissFix;

async function rollbackFix() {
    if (!confirm("撤销上次设定修改？将恢复到上一个版本，对话数据不受影响。")) return;
    if (_fixBusy) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/rollback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        if (data.ok) showToast("已撤销，设定恢复到上一版本");
        else showToast(data.error || "撤销失败");
        await loadFixStatus(true);
    } catch (e) {
        showToast("网络错误，请稍后再试");
    } finally {
        _fixBusy = false;
    }
}
window.rollbackFix = rollbackFix;

async function resetFix() {
    if (!confirm("清空当前的问题描述和待确认方案？已应用的修改记录会保留。")) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        showToast(data.ok ? "已清空当前问题" : (data.error || "操作失败"));
        await loadFixStatus(true);
    } catch (e) {
        showToast("网络错误，请稍后再试");
    } finally {
        _fixBusy = false;
    }
}
window.resetFix = resetFix;

// 模式按钮按预设包注册表动态渲染（容器在 index.html 留空，此处填充）
function renderFixModes() {
    const box = document.getElementById("fix-modes");
    if (!box) return;
    box.innerHTML = "";
    for (const m of PRESET_MODES) {
        const b = document.createElement("button");
        b.className = "fix-mode" + (m.id === FIX_MODE ? " active" : "");
        b.type = "button";
        b.dataset.mode = m.id;
        b.textContent = m.name || m.id;
        b.addEventListener("click", () => setFixMode(m.id));
        box.appendChild(b);
    }
}
window.renderFixModes = renderFixModes;   // loadModes 完成后由 views.js 调用重渲染（ESM 循环规避）

(function initFixModule() {
    const input = document.getElementById("fix-input");
    const sendBtn = document.getElementById("fix-send-btn");
    if (input && sendBtn) {
        sendBtn.addEventListener("click", () => sendFixMessage(input.value));
        input.addEventListener("keydown", e => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFixMessage(input.value); }
        });
    }
    // 模式按钮由 loadModes 完成后桥调 window.renderFixModes() 渲染
    // （不可在此直接调：bundle 拼接顺序 fix 段在 views 段前，PRESET_MODES 此时 TDZ 未初始化）
    const histRefresh = document.getElementById("fix-chathist-refresh");
    if (histRefresh) histRefresh.addEventListener("click", loadFixChatHistory);})();


/* ── 来源：js/views.js ── */
// 视图切换：首页 / 聊天 / 轮播 / 主题 / 界面缩放 / 粒子与降级开关

// ═══ 入口收敛（A5 底部导航已于 0.8.1 移除）：聊天页全屏只留输入栏，
// 首页轮播图进入聊天、返回键/首页按钮回首页、设置走首页右上角 ⚙ / 汉堡菜单 ═══
function activateTab(tab) {
    // 兼容遗留调用（panels.js / 历史代码 window.btActivate）：导航已移除，空实现
}
window.btActivate = activateTab;

// ═══════════════════════════════════════════
// 界面大小调节（消息/头像/气泡缩放，设置面板滑条）
// ═══════════════════════════════════════════
function applyUiScale(percent) {
    document.body.style.setProperty("--ui-scale", (percent / 100).toFixed(2));
    const val = document.getElementById("ui-scale-value");
    if (val) val.textContent = percent + "%";
    const sum = document.getElementById("ui-scale-summary");   // 外观组头摘要（4.4）
    if (sum) sum.textContent = percent + "%";
}
const uiSlider = document.getElementById("ui-scale-slider");
if (uiSlider) {
    let saved = 100;
    try { saved = parseInt(localStorage.getItem("ui-scale")) || 100; } catch (e) {}
    uiSlider.value = saved;
    applyUiScale(saved);
    uiSlider.addEventListener("input", () => {
        const v = parseInt(uiSlider.value) || 100;
        applyUiScale(v);
        try { localStorage.setItem("ui-scale", String(v)); } catch (e) {}
    });
}

// ═══════════════════════════════════════════
// 配色切换（暗色 / 游戏亮色，首页右上角）
// ═══════════════════════════════════════════
function toggleTheme() {
    const light = document.body.classList.toggle("theme-light");
    try { localStorage.setItem("theme", light ? "light" : "dark"); } catch (e) {}
    const icon = document.getElementById("theme-icon");
    if (icon) icon.src = light ? "assets/theme_moon.png" : "assets/theme_sun.png";
}
window.toggleTheme = toggleTheme;
(function applyTheme() {
    let t = "dark";
    try { t = localStorage.getItem("theme") || "dark"; } catch (e) {}
    if (t === "light") {
        document.body.classList.add("theme-light");
        const icon = document.getElementById("theme-icon");
        if (icon) icon.src = "assets/theme_moon.png";
    }
})();

// ═══ 萤火粒子开关（5.1：设置 → 外观；关闭省电）+ 低端机降级（5.5）═══
(function initPerfToggles() {
    // 低端机探测：内存 ≤4GB 或 核数 ≤4 → body.low-end（CSS 关毛玻璃）
    try {
        const dm = navigator.deviceMemory || 8, hc = navigator.hardwareConcurrency || 8;
        if (dm <= 4 || hc <= 4) document.body.classList.add("low-end");
    } catch (e) {}
    // 粒子开关：window.__particlesOff 供 index.html 内联 canvas 脚本逐帧检查
    const canvas = document.getElementById("firefly-field");
    let off = false;
    try { off = localStorage.getItem("firefly_particles") === "0"; } catch (e) {}
    function applyParticles() {
        window.__particlesOff = off;
        if (canvas) canvas.style.display = off ? "none" : "";
    }
    applyParticles();
    const cb = document.getElementById("particles-enabled");
    if (cb) {
        cb.checked = !off;
        cb.addEventListener("change", () => {
            off = !cb.checked;
            try { localStorage.setItem("firefly_particles", off ? "0" : "1"); } catch (e) {}
            applyParticles();
        });
    }
})();

// ═══════════════════════════════════════════
// 首页：视图切换 / 滚动轮播 / 公告面板 / 模式入口
// ═══════════════════════════════════════════
const homeView = document.getElementById("home-view");
const appView = document.getElementById("app");

// 模式（仅两类）：story=故事/剧情模式；haruno=剧本模式（有旁白与环境描写）
// 角色预设化：模式清单来自后端注册表（GET /modes），前端不写死包列表
let CURRENT_MODE = "story";
let _lastMode = null;   // 上次进入聊天时的模式（切换时重载历史）
let _modeGen = 0;       // 模式代际：切换时递增，飞行中的异步渲染/历史加载任务作废丢弃
const MODE_NAMES = { story: "剧情模式", haruno: "剧本模式" };   // 默认两内置；loadModes 后按注册表刷新
const PRESET_MODES = [];   // /modes 清单（id/name/presentation/desc/tagline/cover/avatar/has_opening）
// 归档包清单（3.5）：归档 = 不进 PRESET_MODES（不在 /modes 的 modes 里），但数据与清单条目都在。
// 单独存一份是为了让首页能画出"已归档"区块——否则归档就等于把包弄丢：看不见、恢复不了。
const ARCHIVED_PACKS = [];
let _modesLoaded = false;

// ── 当前包持久化（F-6.3，2026-09-13）──
// 原状：localStorage 只存 ui-scale/theme/particles，当前包从不落盘，而 /modes 的
// `default` 字段前端也没读——于是切到 haruno 后一刷新就弹回 story，
// 用户以为"我的角色卡丢了"。
// 恢复顺序：last_mode → /modes.default → "story"；每个候选都必须在注册表里，否则继续回退。
const _MODE_KEY = "firefly_last_mode";
function _rememberMode(id) {
    try { if (id) localStorage.setItem(_MODE_KEY, id); } catch (e) {}
}
function _savedMode() {
    try { return localStorage.getItem(_MODE_KEY) || ""; } catch (e) { return ""; }
}
// 切包的唯一入口：校验 → 赋值 → 持久化。所有入口都走这里，
// 避免"某些入口记得存、某些不存"（那会让持久化时灵时不灵）。
function _applyMode(id) {
    if (!id || !PRESET_MODES.some(m => m.id === id)) return false;
    CURRENT_MODE = id;
    _rememberMode(id);
    return true;
}
// 面向用户的"进入某包"：非法/空 id 回退注册表首包，最后兜底 "story"
function _switchMode(id) {
    if (_applyMode(id)) return;
    if (_applyMode(PRESET_MODES[0] && PRESET_MODES[0].id)) return;
    CURRENT_MODE = "story";
}

function modeName(id) { return MODE_NAMES[id] || id; }
function currentPreset() {
    return PRESET_MODES.find(m => m.id === CURRENT_MODE) || PRESET_MODES[0] || null;
}

/** 当前角色卡的**角色名**（角色卡化铁律：框架任何地方都不许写角色名字面量）。
 *  取不到返回空串 —— 调用方自行决定降级（名字行不渲染 / 文案省略称呼），
 *  **不要**回落到某个具体角色名，否则自建卡会显示别人的名字。 */
function charName() {
    const p = currentPreset();
    return ((p && (p.char_name || p.name)) || "").toString().trim();
}

/** 当前角色卡的**用户称呼**（同上：取不到返回空串，不回落字面量）。 */
function userName() {
    const p = currentPreset();
    return ((p && p.user_name) || "").toString().trim();
}
function setCurrentMode(mode) {   // ESM 导出只读绑定，外部经此切换
    _applyMode(mode);
}

// 拉取预设包清单并渲染模式卡片（轮播 + PC 大卡）；失败兜底两内置包（与后端兜底一致）
async function loadModes() {
    if (_modesLoaded) return;
    _modesLoaded = true;
    let serverDefault = "";
    try {
        const resp = await fetch("/modes");
        const data = await resp.json();
        const list = Array.isArray(data.modes) ? data.modes : [];
        serverDefault = typeof data.default === "string" ? data.default : "";
        const arch = Array.isArray(data.archived) ? data.archived : [];
        ARCHIVED_PACKS.splice(0, ARCHIVED_PACKS.length, ...arch);
        if (list.length) {
            PRESET_MODES.splice(0, PRESET_MODES.length, ...list);
            for (const m of list) MODE_NAMES[m.id] = m.name || m.id;
        }
    } catch (e) {}
    if (!PRESET_MODES.length) {
        // 离线兜底：只给**渲染必需**的最小信息（id/模式名/演出形态/图）。
        // **不带 char_name / user_name** —— 那是角色卡数据，正常从 /modes 取；
        // 兜底路径下名字行为空（宁可不显示，也不在框架里写死某个角色名）。
        PRESET_MODES.push(
            {id: "story", name: "剧情模式", presentation: "sticker", desc: "", tagline: "",
             cover: "/assets/character/story/assets/cover.png", avatar: "/assets/character/story/assets/avatar.png", has_opening: false},
            {id: "haruno", name: "剧本模式", presentation: "narration", desc: "", tagline: "",
             cover: "/assets/character/haruno/assets/cover.png", avatar: "/assets/character/haruno/assets/avatar.png", has_opening: true});
    }
    // 启动恢复当前包（F-6.3）：候选逐个校验，全不合法才回退注册表首包
    const pick = [_savedMode(), serverDefault, "story"].find(id => id && PRESET_MODES.some(m => m.id === id))
        || (PRESET_MODES[0] && PRESET_MODES[0].id) || "story";
    _applyMode(pick);
    renderModeCards();
    applyModeBranding();
    try { window.renderFixModes && window.renderFixModes(); } catch (e) {}   // 纠错页模式按钮随注册表刷新
}

// 包资产编辑后强制重拉注册表（panels.js 角色包管理调用）
window.__modesReload = async () => {
    _modesLoaded = false;
    await loadModes();
};
// bundle 拼接单作用域下，panels.js 取注册表/切模式的桥（避免 import("./views.js")
// 动态导入产生第二份 ESM 模块实例）
window.__getPresets = () => PRESET_MODES;
window.__setCurrentMode = (mode) => setCurrentMode(mode);
// PC 三栏外壳（pc_shell.js，独立 classic script）读当前包用：它看不到 bundle 作用域里的
// CURRENT_MODE，所以给一个只读 getter（不要给它写入口，切包一律走 enterMode）。
window.__getCurrentMode = () => CURRENT_MODE;

// 进入某包的管理页（卡片角标/轮播角标入口）：切到该包 + 打开角色编辑页。
// （2026-09-14 修复：此处原有第二个同名 managePack 定义（openPackView 包装），函数声明提升下
//  后者覆盖前者，菜单里又没有 "pack" 这个 tab，旧定义实为死代码+误导——已删，统一走 openPackView。）
document.getElementById("carousel-manage-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const m = PRESET_MODES[carouselIndex];
    if (m) openPackView(m.id);
});

// ── 新建角色卡（阶段7，本地版）──
// 命名统一（2026-09-18）：角色名_模式名_创建时间（月日_时分），如「流萤_剧情_0918_0041」。
// 同角色允许建多个包（后端不校验 char_name 唯一），靠这个名字区分。
function _defaultPackName(charName, presentation) {
    const d = new Date();
    const p2 = n => String(n).padStart(2, "0");
    const label = presentation === "narration" ? "剧本" : "剧情";
    return `${charName || "角色"}_${label}_${p2(d.getMonth() + 1)}${p2(d.getDate())}_${p2(d.getHours())}${p2(d.getMinutes())}`;
}

function togglePackCreate(show) {
    const p = document.getElementById("pack-create-panel");
    if (!p) return;
    const visible = p.style.display !== "none";
    const target = (show === undefined) ? !visible : !!show;
    p.style.display = target ? "block" : "none";
    if (target) {
        const nameEl = document.getElementById("pc-name");
        // 名称留空时预填默认名（用户可改；留空提交后端也会按同规则生成）
        if (nameEl && !nameEl.value.trim()) {
            const cn = (document.getElementById("pc-char")?.value || "").trim();
            const pres = document.getElementById("pc-presentation")?.value || "sticker";
            nameEl.value = _defaultPackName(cn, pres);
        }
        nameEl?.focus();
    }
}
window.togglePackCreate = togglePackCreate;

document.getElementById("pc-submit")?.addEventListener("click", async () => {
    const name = document.getElementById("pc-name").value.trim();
    const charName = document.getElementById("pc-char").value.trim();
    const userName = document.getElementById("pc-user").value.trim();
    const presentation = document.getElementById("pc-presentation").value;
    // 名称可留空：后端按「角色名_模式名_月日_时分」生成（同角色多包靠它区分）
    if (!charName || !userName) { showToast("角色名、对方称呼都必填"); return; }
    try {
        const resp = await fetch("/pack-create", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name, char_name: charName, user_name: userName, presentation})});
        const data = await resp.json();
        if (data.ok) {
            showToast(`已创建「${data.name}」——点列表里的卡片进详情编辑人设`);
            togglePackCreate(false);
            await window.__modesReload();
            renderCardsList();
        } else {
            showToast("创建失败：" + (data.error || ""));
        }
    } catch (e) { showToast("网络错误"); }
});



// 轮播图上的角色信息条：跟随当前轮播位置
function _updateCarouselChar() {
    const c = document.getElementById("carousel-char");
    if (!c) return;
    const m = PRESET_MODES[carouselIndex];
    if (!m) { c.style.display = "none"; return; }
    c.style.display = "";
    const img = c.querySelector("img");
    _setImgSrc(img, m.avatar);   // F-5：无头像清掉旧 src（持久元素，否则沿用上一包的图）
    c.querySelector(".cc-name").textContent = m.char_name || "";
    c.querySelector(".cc-scene").textContent = m.name || "";
}

// 角色编辑页（独立全屏）：切换目标包 + 打开；_packFrom 记录来源（cards=列表页 / 其他=首页）
let _packFrom = "";
function openPackView(mode, from) {
    if (mode) _applyMode(mode);
    _packFrom = from || "";
    homeView.classList.remove("show");
    const cv = document.getElementById("cards-view");
    if (cv) cv.classList.remove("show");
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    const v = document.getElementById("pack-view");
    if (!v) return;
    v.classList.add("show");
    try { if (location.hash !== "#pack") history.pushState({pack: true}, "", "#pack"); } catch (e) {}
    try { window.loadPackView && window.loadPackView(); } catch (e) {}
}
window.openPackView = openPackView;
function closePackView() {
    const v = document.getElementById("pack-view");
    if (v) v.classList.remove("show");
    if (_packFrom === "cards") { showCardsView(); return; }   // 从列表来则回列表
    showHome();
    try { if (location.hash === "#pack") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closePackView = closePackView;

// ── 角色卡管理页（全屏：卡片列表 → 点卡进详情编辑）──
function openCardsView() {
    homeView.classList.remove("show");
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    const v = document.getElementById("cards-view");
    if (!v) return;
    v.classList.add("show");
    // 服务器版不做自定义整包：隐藏新建区
    const newBox = document.querySelector("#cards-view .cv-new");
    if (newBox) newBox.style.display = IS_SERVER ? "none" : "";
    try { if (location.hash !== "#cards") history.pushState({cards: true}, "", "#cards"); } catch (e) {}
    renderCardsList();
}
window.openCardsView = openCardsView;
function showCardsView() { openCardsView(); }
function closeCardsView() {
    const v = document.getElementById("cards-view");
    if (v) v.classList.remove("show");
    showHome();
    try { if (location.hash === "#cards") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closeCardsView = closeCardsView;

// 卡片列表：每张卡 = 头像 + 角色名 + 剧本/形态，点击进详情编辑页
function renderCardsList() {
    const list = document.getElementById("cv-list");
    if (!list) return;
    list.innerHTML = "";
    const presLabel = {sticker: "短信+表情包", narration: "短信+旁白", none: "纯短信"};
    for (const m of PRESET_MODES) {
        const card = document.createElement("button");
        card.className = "cv-card";
        card.type = "button";
        card.onclick = () => openPackView(m.id, "cards");
        const img = _coverImg(m, "cv-thumb");
        img.alt = "";
        const mid = document.createElement("div");
        const cn = document.createElement("div");
        cn.className = "cv-cname";
        cn.textContent = (m.char_name || "") + (m.custom ? "" : "");
        const sub = document.createElement("div");
        sub.className = "cv-csub";
        // name 形如「流萤_剧情_0918_0041」；副标题去掉与大字重复的角色名，
        // 留下「模式_创建时间 · 演出形态」——同角色多包靠这段区分（不分组方案）
        const _nm = m.name || m.id;
        const _shown = (m.char_name && _nm.startsWith(m.char_name + "_"))
            ? _nm.slice(m.char_name.length + 1) : _nm;
        sub.textContent = _shown + " · " + (presLabel[m.presentation] || m.presentation);
        mid.append(cn, sub);
        const go = document.createElement("span");
        go.className = "cv-cgo";
        go.textContent = "›";
        card.append(img, mid, go);
        list.appendChild(card);
    }
}

// 卡片角标/轮播角标入口（保留 managePack 名兼容）：经 _applyMode 校验后开详情页
function managePack(mode) { if (_applyMode(mode)) openPackView(); }
window.managePack = managePack;

// F-5（2026-09-14）：无封面/头像的包给「首字占位块」，不再显示破图或沿用上一张图。
// 返回值是元素（img 或 div），尺寸由调用处既有 class 决定。
function _coverImg(m, cls) {
    if (m.cover) {
        const img = document.createElement("img");
        img.className = cls;
        img.src = m.cover;
        img.alt = m.name || m.id;
        return img;
    }
    const d = document.createElement("div");
    d.className = cls + " pack-noimg";
    d.textContent = (m.char_name || m.name || "?").slice(0, 1);
    return d;
}

// 持久 <img> 元素（详情页/品牌位/轮播信息条）：无图必须清掉旧 src，否则沿用上一包的图
function _setImgSrc(img, url) {
    if (!img) return;
    if (url) img.src = url;
    else img.removeAttribute("src");
}

// 按 PRESET_MODES 渲染角色卡（轮播 + PC 大卡）：卡的主角是角色（头像+角色名），
// 模式名（剧情模式/剧本模式）是小标签；卡上 ✎ 角标进角色编辑页
function renderModeCards() {
    carouselTrack.innerHTML = "";
    carouselDots.innerHTML = "";
    PRESET_MODES.forEach((m, i) => {
        carouselTrack.appendChild(_coverImg(m, "pack-noimg-abs"));
        const dot = document.createElement("span");
        if (i === 0) dot.classList.add("active");
        dot.addEventListener("click", () => goCarousel(i));
        carouselDots.appendChild(dot);
    });
    // 轮播图上叠加当前包的角色信息条（左下）
    _updateCarouselChar();
    carouselCount = PRESET_MODES.length;
    // 轮播初始位置跟随当前包（F-6.3 配套）：否则恢复成 haruno 后首页却显示 story 封面，
    // 用户点封面进入会被 enterCarouselAction 记成 story，刚恢复的记忆立刻被覆盖。
    if (carouselCount) {
        const i = PRESET_MODES.findIndex(m => m.id === CURRENT_MODE);
        goCarousel(i > 0 ? i : 0);
    }
    const hm = document.getElementById("home-modes");
    if (hm) {
        hm.innerHTML = "";
        for (const m of PRESET_MODES) {
            const btn = document.createElement("button");
            btn.className = "hm-card";
            btn.type = "button";
            btn.onclick = () => enterMode(m.id);
            const img = _coverImg(m, "hm-cover");
            // 编辑角标（毛玻璃，hover 卡面时显现）
            const edit = document.createElement("span");
            edit.className = "hm-edit";
            edit.title = `编辑「${m.char_name || m.name || m.id}」`;
            edit.textContent = "✎";
            edit.onclick = (e) => { e.stopPropagation(); openPackView(m.id); };
            // 角色信息叠加层：底部渐变压暗 + 头像 + 角色名 + 剧本标签
            const ov = document.createElement("div");
            ov.className = "hm-overlay";
            const av = _coverImg(m, "hm-avatar");
            const info = document.createElement("div");
            info.className = "hm-info";
            const cn = document.createElement("div");
            cn.className = "hm-charname";
            cn.textContent = m.char_name || m.name || m.id;
            const sc = document.createElement("div");
            sc.className = "hm-scene";
            sc.textContent = m.name || "";
            info.append(cn, sc);
            ov.append(av, info);
            btn.append(img, edit, ov);
            hm.appendChild(btn);
        }
        _renderArchivedPacks(hm);
    }
}

// 包生命周期动作（3.5）：归档 / 恢复 / 彻底删除 —— 首页归档区与包详情页**共用一套**调用与提示。
// 为什么收在一处：三个动作的端点、确认文案、失败提示一旦两处各写一遍就会漂移，
// 而漂移的后果是"按钮写着归档、实际调的是删除"这种灾难性不一致。
// 返回 true 表示动作成功（调用方据此刷新列表/关闭详情页）。
async function packLifecycle(action, id, label) {
    const paths = {archive: "/pack-archive", restore: "/pack-restore", erase: "/pack-delete"};
    const names = {archive: "归档", restore: "恢复", erase: "删除"};
    const path = paths[action];
    if (!path || !id) return false;
    if (action === "archive") {
        if (!confirm(`归档「${label}」？\n该包会从列表与聊天里隐藏，人设与聊天记录全部保留。`)) return false;
    } else if (action === "restore") {
        if (!confirm(`恢复「${label}」？`)) return false;
    } else {
        const typed = prompt(`彻底删除「${label}」后无法恢复（删除前会自动备份一份快照）。\n` +
            `请输入包名以确认：`);
        if (typed === null) return false;
        if (typed.trim() !== label) { showToast("包名不匹配，已取消"); return false; }
    }
    try {
        const r = await fetch(path, {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({id})});
        const d = await r.json();
        if (!d.ok) { showToast(names[action] + "失败：" + (d.error || "")); return false; }
        // 归档/删除后要重载（归档包不在清单里，留着旧视图只会显示别的包的数据）
        showToast(action === "archive" ? "已归档（数据保留）" :
            (action === "restore" ? "已恢复" : "已删除"));
        return true;
    } catch (e) { showToast("网络错误"); return false; }
}
window.__packLifecycle = packLifecycle;

// 已归档区块（3.5）：每个归档包给「恢复」与「彻底删除」两个出口。
// 彻底删除要**输入包名**确认（比 confirm 更难误触），后端还会在删除前强制打一份全量快照；
// 恢复只是把 state 改回 active（数据一直没动）。
function _renderArchivedPacks(container) {
    if (!container || !ARCHIVED_PACKS.length) return;
    const box = document.createElement("div");
    box.id = "archived-packs";
    box.style.cssText = "flex-basis:100%;margin-top:14px;padding:10px 12px;border:1px dashed #6666;" +
        "border-radius:10px;font-size:0.8em;color:var(--fg-muted, #999)";
    const title = document.createElement("div");
    title.style.cssText = "margin-bottom:8px;font-weight:600";
    title.textContent = `已归档（${ARCHIVED_PACKS.length}）—— 数据仍在，可随时恢复`;
    box.appendChild(title);
    for (const a of ARCHIVED_PACKS) {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:10px;padding:4px 0";
        const nm = document.createElement("span");
        nm.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
        nm.textContent = a.char_name || a.name || a.id;
        const back = document.createElement("button");
        back.type = "button";
        back.textContent = "恢复";
        back.onclick = async () => {
            if (await window.__packLifecycle("restore", a.id, a.name || a.id)) {
                await window.__modesReload();
            }
        };
        const kill = document.createElement("button");
        kill.type = "button";
        kill.textContent = "彻底删除";
        kill.onclick = async () => {
            if (await window.__packLifecycle("erase", a.id, a.name || a.id)) {
                await window.__modesReload();
            }
        };
        row.append(nm, back, kill);
        box.appendChild(row);
    }
    container.appendChild(box);
}

/** 把带 `data-tpl` 的静态文案按当前角色卡重填（角色卡化：HTML 里不写死角色名）。
 *
 *  用法：HTML 写通用默认文案（无 JS/首屏时不露角色名），标签带模板：
 *    `<span data-tpl="让{c}休息">让角色休息</span>`
 *    `<div data-tpl-ph="…让{c}休息…">` → 填 placeholder
 *  `{c}` = charName()，`{u}` = userName()；两者取不到时用中性词（角色／你）。
 *  覆盖了菜单「休息」按钮、手账标签与说明、休息/起床遮罩、用户形象弹窗标题等。 */
function applyTextTemplates() {
    const c = charName() || "角色";
    const u = userName() || "你";
    const fill = (s) => String(s).replace(/\{c\}/g, c).replace(/\{u\}/g, u);
    document.querySelectorAll("[data-tpl]").forEach(el => {
        el.textContent = fill(el.dataset.tpl || "");
    });
    document.querySelectorAll("[data-tpl-ph]").forEach(el => {
        el.setAttribute("placeholder", fill(el.dataset.tplPh || ""));
    });
}

// 聊天页/侧边栏的角色标识（头像/名字/签名）按当前包刷新
function applyModeBranding() {
    const p = currentPreset() || {};
    const cname = p.char_name || "";
    for (const id of ["chat-avatar", "pcs-avatar"]) {
        _setImgSrc(document.getElementById(id), p.avatar);   // F-5：无头像清旧 src
    }
    const pcsName = document.getElementById("pcs-name");
    if (pcsName) pcsName.textContent = cname;
    const chatName = document.getElementById("chat-char-name");
    if (chatName) chatName.textContent = cname;
    for (const id of ["pcs-status", "chat-status"]) {
        const el = document.getElementById(id);
        if (el) el.textContent = p.tagline || "";
    }
    applyTextTemplates();   // 静态文案里的角色名/称呼一并刷新（换卡后立刻生效）
}

function showHome() {
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    homeView.classList.add("show");
    appView.style.display = "none";     // 首页独立视图：真正隐藏聊天页（避免半透明透视）
    closeMenu();
    stopCarousel();
    goCarousel(0);   // 回到首页重置轮播位置
    startCarousel();   // 重新开始自动轮播
    try { activateTab("home"); } catch (e) {}
}
window.showHome = showHome;   // ESM 拆分后供 pc_nav.js（classic script）与内联 onclick 使用
async function showChat() {
    // A7 登录前置（本地版）：未登录 → 回首页展开登录表单（本地后端离线时放行）；
    // 已登录但离线宽限外（offline_ok===false）→ 显式 POST /auth/verify 一次再定夺：
    //   200 放行；401 回首页展开登录表单；网络/状态异常回首页提示联网验证。
    if (!IS_SERVER) {
        try {
            const st = await (await fetch("/auth/state")).json();
            if (st.logged_in === false) {
                showHome();
                showAuthModule();
                try { toggleAuthForms(); } catch (e) {}
                try { showToast("请先登录（登录后本地数据可云端同步，Key 仍只存本机）"); } catch (e) {}
                return;
            }
            if (st.logged_in && st.offline_ok === false) {
                let vResp = null;
                try { vResp = await fetch("/auth/verify", {method: "POST"}); } catch (e) { vResp = null; }
                if (vResp && vResp.status === 200) {
                    // verify 通过：放行
                } else if (vResp && vResp.status === 401) {
                    showHome();
                    showAuthModule();
                    try { toggleAuthForms(); } catch (e) {}
                    try { showToast("登录已过期，请重新登录"); } catch (e) {}
                    return;
                } else {
                    // 网络异常/意外状态码：本地登录态无法联网核验
                    showHome();
                    showAuthModule();
                    try { showToast("登录已过期，需联网验证一次"); } catch (e) {}
                    return;
                }
            }
        } catch (e) { /* 本地后端未就绪：放行（聊天不依赖账号） */ }
        // 进聊天页/模式切换成功后：补一次云端同步（不 await 不阻塞页面；节流期内自动跳过）
        try { window.autoSyncNow && window.autoSyncNow(); } catch (e) {}
    }
    const fixView = document.getElementById("fix-view");    if (fixView) fixView.classList.remove("show");
    homeView.classList.remove("show");
    appView.style.display = "flex";     // 恢复聊天页
    stopCarousel();   // 聊天页轮播不可见，停止自动轮播（避免返回首页时位置已乱）
    scrollToBottom();
    // 顶部显示当前模式名
    const modeTag = document.getElementById("chat-mode-tag");
    if (modeTag) modeTag.textContent = MODE_NAMES[CURRENT_MODE] || CURRENT_MODE;
    // 模式可能已切换：清空并重载当前模式历史（story/haruno 数据隔离）
    if (_lastMode !== CURRENT_MODE) {
        _lastMode = CURRENT_MODE;
        _modeGen++;   // 模式代际递增：作废所有飞行中的异步渲染任务（防止串模式显示）
        applyModeBranding();   // 头部头像/角色名/签名刷新为当前包
        initAssets(); // 模式切换：同步该模式资产（story/haruno 设定不同；未登录时静默失败）
        // 未提交的提交窗口作废：旧模式的 flush/hint/批检查器不跨模式触发（防串写历史）
        clearTimeout(S._flushTimer);
        S._flushTimer = null;
        clearTimeout(S._hintTimer);
        S._hintTimer = null;
        try { resetBatchWindow(); } catch (e) {}   // 0.8.1 批状态机作废（chat.js）
        try { _clearQuote(); } catch (e) {}   // F-6.4：引用条不跨包——否则 A 包引用的消息会随下一条发送写进 B 包历史
        messagesEl.innerHTML = "";
        S._hasMore = false;
        await loadHistory();   // 先加载历史（含已保存的开场）
        // 历史为空且当前包有开场脚本：触发开场生成一次并保存为对话内容。
        // 之后进入只走历史加载，不重复开场——与剧情模式行为一致。
        const _p = currentPreset();
        if (_p && _p.has_opening && messagesEl.children.length === 0) {
            await openModeOpening();
        }
    }
    try { if (location.hash !== "#chat") history.pushState({chat: true}, "", "#chat"); } catch (e) {}
    try { activateTab("chat"); } catch (e) {}
}
window.showChat = showChat;   // 同上：pc_nav.js / home-start 按钮 onclick



// haruno 模式开场：服务端幂等（无历史才生成），返回旁白+首条消息
async function openModeOpening() {
    const gen = _modeGen;   // 捕获发起时的模式代际
    try {
        const resp = await fetch("/open-mode", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃开场消息，防止渲染进新模式界面
        if (data.opened && data.messages && data.messages.length) {
            renderMessages(data.messages, "firefly", data);
        }
    } catch (e) {}
}

// 轮播图功能入口：进入当前轮播位置对应的模式
function enterCarouselAction() {
    const m = PRESET_MODES[carouselIndex];
    _switchMode(m && m.id);
    showChat();
}

// PC 桌面模式大卡入口（home-modes）：与轮播图入口同语义，直接进入指定模式
function enterMode(mode) {
    _switchMode(mode);
    showChat();
}
window.enterMode = enterMode;


// 返回键支持（PC 浏览器后退 / Android WebView goBack → popstate → 回首页）
window.addEventListener("popstate", () => {
    if (location.hash !== "#chat") showHome();
});
function toggleNotice() {
    const panel = document.getElementById("notice-panel");
    const open = panel.classList.toggle("show");
    document.getElementById("notice-arrow").textContent = open ? "▴" : "▾";
    // 打开时才联网刷新 + 延迟记已读（见 js/notice.js）。
    // 走 window 全局而不是 import：本文件在 bundle 里先于 notice 出现，
    // 调用期解析既能避免顶层顺序约束，也少了 views ↔ notice 的模块耦合。
    if (open && typeof window.noticeOnOpen === "function") {
        try { window.noticeOnOpen(); } catch (e) {}
    }
}
function closeNotice() {
    document.getElementById("notice-panel").classList.remove("show");
    document.getElementById("notice-arrow").textContent = "▾";
}
window.toggleNotice = toggleNotice;   // 内联 onclick（公告栏）
window.closeNotice = closeNotice;

// 滚动轮播（自动 + 触摸滑动）
const carouselTrack = document.getElementById("carousel-track");
const carouselDots = document.getElementById("carousel-dots");
let carouselIndex = 0;
let carouselTimer = null;
let carouselCount = 0;   // 由 renderModeCards 按 PRESET_MODES 设置（loadModes 后）

function goCarousel(i) {
    if (!carouselCount) return;
    carouselIndex = (i + carouselCount) % carouselCount;
    // 安卓 WebView bug：transform 移动后的 img 合成层光栅化模糊。
    _updateCarouselChar();   // 角色信息条跟随当前卡
    // 改用 opacity 淡入淡出切换（无 transform、无 display 硬切，过渡平滑）。
    [...carouselTrack.children].forEach((img, di) => {
        const active = di === carouselIndex;
        img.style.opacity = active ? "1" : "0";
        img.style.pointerEvents = active ? "auto" : "none";   // 隐藏层不挡点击
        img.style.zIndex = active ? "1" : "0";
    });
    [...carouselDots.children].forEach((d, di) => d.classList.toggle("active", di === carouselIndex));
}
function startCarousel() {
    stopCarousel();
    carouselTimer = setInterval(() => goCarousel(carouselIndex + 1), 10000);
}
function stopCarousel() { if (carouselTimer) { clearInterval(carouselTimer); carouselTimer = null; } }
// 手动滑动/点击后暂停自动轮播：用户主动浏览时不打扰（避免"滑不回来"的错觉）。
// 返回首页时 showHome 会 stopCarousel；再次进入聊天页不会自动轮播。
// 触摸滑动/点击：只绑定轮播图图片区（carouselTrack），其余区域不触发
let touchX = null;
carouselTrack.addEventListener("touchstart", (e) => {
    touchX = e.touches[0].clientX;
    stopCarousel();   // 手动触摸时暂停自动轮播
}, {passive: true});
carouselTrack.addEventListener("touchend", (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 40) goCarousel(carouselIndex + (dx < 0 ? 1 : -1));
    else enterCarouselAction();   // 触摸点击轮播图 → 按功能入口进入
    touchX = null;
    // 手动交互后不恢复自动轮播（用户已接管）
}, {passive: true});

// ── PC 鼠标支持：拖拽滑动 + 滚轮切换 + 点击进入对话 ──
let dragState = null;
carouselTrack.addEventListener("mousedown", (e) => {
    dragState = { startX: e.clientX, curX: e.clientX, moved: false };
    stopCarousel();
    e.preventDefault();
});
window.addEventListener("mousemove", (e) => {
    if (!dragState) return;
    dragState.curX = e.clientX;
    const dx = dragState.curX - dragState.startX;
    if (Math.abs(dx) > 5) dragState.moved = true;
});
window.addEventListener("mouseup", (e) => {
    if (!dragState) return;
    const dx = dragState.curX - dragState.startX;
    const moved = dragState.moved;
    dragState = null;
    if (Math.abs(dx) > 40) {
        goCarousel(carouselIndex + (dx < 0 ? 1 : -1));   // 拖拽切换
    } else if (!moved) {
        enterCarouselAction();   // 点击（未拖动）→ 按功能入口进入
    }
    startCarousel();
});
carouselTrack.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (e.deltaY > 0) goCarousel(carouselIndex + 1);
    else goCarousel(carouselIndex - 1);
}, {passive: false});

// 页面加载默认显示首页（若从对话页刷新则恢复对话页）
document.addEventListener("DOMContentLoaded", async () => {
    await loadModes();   // 先拉预设包清单（渲染轮播/大卡/角色标识），再决定进哪个视图
    if (location.hash === "#chat") showChat();
    else if (location.hash === "#fix") openFixView();
    // 桌面双栏（≥1100px）：默认直接进聊天，首页降级为侧栏「首页」视图
    else if (window.matchMedia && matchMedia("(min-width:1100px)").matches) showChat();
    else showHome();
    initAuth();   // 服务器版：轮播图下登录/用户模块
    uiSelectEnhance(document);   // 自绘下拉全站接管（原生 select 弹窗无法主题化；幂等有守卫）
});


/* ── 来源：js/proactive.js ── */
// 主动性轮询与后台主动消息

// ═══════════════════════════════════════════
// 主动性轮询 — 流萤在合适的时候主动找开拓者说话
// ═══════════════════════════════════════════
// 轮询纪律（避免冲突）：
// - 仅在聊天页可见且空闲时检查（不等待回复、不在打字、距离上次回复 > 2 分钟）
// - 服务端门控保证频率（主动式=轮次+概率；概率式=时间静默+前端概率），不通过则零成本返回空
// - 空闲判定（概率式硬性）：无输入、无提交、无处理中（S.waiting/pending/输入框非空）
const _PROACTIVE_INTERVAL = 10 * 1000;   // 轮询周期 10s
const _PROACTIVE_QUIET = 2 * 60 * 1000;  // 主动式：回复渲染后 2 分钟内不检查
const _PROB_QUIET = 10 * 60 * 1000;      // 概率式：距上次渲染 10 分钟内不检查（与服务端静默阈值一致）

function _chatVisible() {
    return appView && appView.style.display !== "none";
}

// 空闲判定：无输入 / 无请求在飞 / 无思考锁 / 无渲染动画
function _idleOk() {
    if (S.waiting) return false;
    if (S._rendering) return false;         // 主动消息渲染动画中
    if (_inflight > 0) return false;      // 发送请求在飞（等待回复）
    if (inputEl && inputEl.value.trim()) return false;
    return _chatVisible();
}

// 渲染主动消息：先锁定输入（思考 2~5s 模拟"想了想/想起什么"），期间禁止用户输入防竞态
async function _renderProactiveWithThink(data) {
    S._lastRenderTs = Date.now();
    S.waiting = true;
    inputEl.disabled = true; sendBtn.disabled = true;
    const statusEl = document.querySelector("#header .status");
    const defaultStatus = (currentPreset() || {}).tagline || "会找到的，属于我的梦...";   // 随当前角色包签名（原来是硬编码流萤签名）
    if (statusEl) statusEl.textContent = "对方正在输入...";
    const thinkMs = 2000 + Math.floor(Math.random() * 3000);
    await new Promise(r => setTimeout(r, thinkMs));
    if (statusEl) statusEl.textContent = defaultStatus;
    S.waiting = false;
    inputEl.disabled = false; sendBtn.disabled = false;
    renderMessages(data.messages, "firefly", data);
}

async function checkProactive() {
    if (!_idleOk()) return;
    const gen = _modeGen;   // 捕获发起时的模式代际
    try {
        // 轮询探测：不改任何前端状态（大部分概率未中，闪状态是错的）
        const resp = await fetch("/proactive-status", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃旧模式主动消息（已写盘原模式）
        if (data.proactive && data.messages && data.messages.length) {
            // 确定要回复：才锁输入框 + 显示状态 + 思考延迟 + 渲染（全程锁防乱序）
            S._lastRenderTs = Date.now();
            S.waiting = true;
            inputEl.disabled = true; sendBtn.disabled = true;
            const statusEl = document.querySelector("#header .status");
            const defaultStatus = statusEl ? statusEl.textContent : "";
            if (statusEl) statusEl.textContent = "对方正在输入...";
            const thinkMs = 2000 + Math.floor(Math.random() * 3000);
            await new Promise(r => setTimeout(r, thinkMs));
            if (statusEl) statusEl.textContent = defaultStatus;
            S.waiting = false;
            inputEl.disabled = false; sendBtn.disabled = false;
            renderMessages(data.messages, "firefly", data);
            _notifyFirefly(data.messages);   // 后台触发的主动消息 → 状态栏通知（桥判断前台与否）
        }
        // 无消息：前端状态完全不动
    } catch (e) {
        // 网络异常：无状态变更，无需恢复（静默等下一轮）
    }
}
// ═══════════════════════════════════════════
// 服务器版后台主动（KeepAliveService 定时触发）
// ═══════════════════════════════════════════

function _notifyFirefly(messages) {
    // 消息渲染后提醒：FireflyJs 桥仅 App 不在前台时发状态栏通知（前台不打扰，
    // 复刻本地版 _notify_reply_if_background 语义）
    try {
        if (window.FireflyJs && window.FireflyJs.notify && Array.isArray(messages)) {
            const texts = messages.filter(m => m && m.type === "text" && m.content)
                                  .map(m => m.content);
            if (texts.length) window.FireflyJs.notify((charName() || "角色") + " · AI", texts.join("\n").slice(0, 200));
        }
    } catch (e) {}
}

window.__serverProactive = async function () {
    // KeepAliveService 后台触发：hidden 开关判断 + 主动式/概率式门控在服务端，
    // relay 代发由页面 relay 引擎完成（本函数跑在页面，Key 可直达）
    if (!S._hiddenEnabled) return;
    await checkProactive();
};

// 10s 轮询 + 抖动（2026-09-19 压测）：原来是固定 10s 的 setInterval。
// 固定周期会让所有客户端在同一秒对齐（惊群）；加 0-5s 抖动把负载摊平，
// 首次延迟也保持 10s 量级（避免刚进页面就判定"该主动了"）。
(function _proactiveLoop() {
    setTimeout(() => {
        checkProactive();
        _proactiveLoop();
    }, _PROACTIVE_INTERVAL + Math.random() * 5000);
})();


/* ── 来源：js/relay.js ── */
// 服务器版 relay 引擎与资产本地化

// ═══════════════════════════════════════
// 后端代理（relay）— 服务器不持有用户 Key 的完整链路
// ═══════════════════════════════════════
// 服务器流水线的 LLM 请求在服务器入队（资产用 __CORE__ 等占位符表示），
// 本页 1s 轮询取件 → 占位符填充（本地资产）→ 用户 Key 直连 api_base 代发 → 回传。
// 资产（知识库/核心设定/身份/短信样本，~500KB）下载后缓存 localStorage（5MB 上限绰绰有余）。
let _assets = { knowledge: "", core: "", identity: "", sms_samples: "" };
let _relayBusy = false;

function _assetStoreKey(name) { return "firefly_asset_" + name + "_" + CURRENT_MODE; }

async function initAssets() {
    // 资产本地化：清单指纹对比 → 差异下载 → 缓存。登录后/模式切换时调用。
    if (!IS_SERVER) return;   // 本地模式：知识库/设定由本地后端直接注入，无需本地化
    const mode = CURRENT_MODE;
    try {
        const idxResp = await fetch("/assets/index?mode=" + mode);
        const idx = await idxResp.json();
        const local = (() => { try { return JSON.parse(localStorage.getItem("firefly_assets_idx") || "{}"); } catch (e) { return {}; } })();
        const assetGroups = {
            knowledge: idx.knowledge, core: idx.character.core,
            identity: idx.character.identity, sms_samples: idx.character.sms_samples,
        };
        for (const [name, info] of Object.entries(assetGroups || {})) {
            const cacheKey = name + ":" + mode;
            const ver = (info && info.version) || "0";
            if ((local[cacheKey] || "") !== ver && ver !== "0") {
                try {
                    const rawResp = await fetch("/assets/raw?name=" + name + "&mode=" + mode);
                    const raw = await rawResp.json();
                    if (raw.content) {
                        localStorage.setItem(_assetStoreKey(name), raw.content);
                        local[cacheKey] = ver;
                    }
                } catch (e) { /* 单项失败不阻塞其余资产 */ }
            }
        }
        try { localStorage.setItem("firefly_assets_idx", JSON.stringify(local)); } catch (e) {}
    } catch (e) {
        // 同步失败（未登录/网络）：用已有缓存兜底（首次无缓存时占位符以空串填充，模型仍可聊天）
    }
    // 无论同步成败，从缓存装配当前模式资产
    for (const name of ["knowledge", "core", "identity", "sms_samples"]) {
        try { _assets[name] = localStorage.getItem(_assetStoreKey(name)) || ""; } catch (e) { _assets[name] = ""; }
    }
}

function fillPlaceholders(payload) {
    const msgs = payload && payload.messages;
    if (!Array.isArray(msgs)) return;
    for (const m of msgs) {
        if (typeof m.content === "string" && m.content.indexOf("__") >= 0) {
            m.content = m.content
                .replaceAll("__CORE__", _assets.core || "")
                .replaceAll("__IDENTITY__", _assets.identity || "")
                .replaceAll("__SMS_SAMPLES__", _assets.sms_samples || "")
                .replaceAll("__KNOWLEDGE__", _assets.knowledge || "");
        }
    }
}

// A8 能力探测（服务器版）：供应商拒绝 DeepSeek 私有参数（thinking/reasoning_effort）时
// 剥离后重试一次，并按结果缓存 caps 状态（后续 payload 直接剥离，避免每次探错）。
// 缓存按供应商隔离：key = firefly_no_thinking_<base_url>（换供应商后重新探测，互不污染）；
// 旧全局 key firefly_no_thinking 首次读取时迁移到当前供应商键并删除。
function _noThinkingKey(apiBase) {
    return "firefly_no_thinking_" + String(apiBase || "").replace(/\/+$/, "");
}
function _loadNoThinking(apiBase) {
    try {
        const old = localStorage.getItem("firefly_no_thinking");
        if (old !== null) {
            localStorage.setItem(_noThinkingKey(apiBase), old);
            localStorage.removeItem("firefly_no_thinking");
        }
        return localStorage.getItem(_noThinkingKey(apiBase)) === "1";
    } catch (e) { return false; }
}

function _stripThinking(payload) {
    if (!payload || typeof payload !== "object") return payload;
    try {
        const p = JSON.parse(JSON.stringify(payload));
        delete p.thinking;
        delete p.reasoning_effort;
        return p;
    } catch (e) { return payload; }
}

function _looksUnknownParam(text) {
    if (!text) return false;
    const low = String(text).toLowerCase();
    return low.indexOf("unknown") >= 0 || low.indexOf("unexpected parameter") >= 0
        || low.indexOf("not a valid parameter") >= 0 || low.indexOf("not supported") >= 0;
}

async function relayTick() {
    // 1s 轮询取件。用原始 fetch 手动带头：避开包装器的 401 toast（未登录/过期时静默）。
    if (_relayBusy) return;
    const token = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
    if (!token) return;   // 未登录：服务器不会入队
    let pending = null;
    try {
        const resp = await _serverFetch(API_BASE + "/relay/pending", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
            body: "{}",
        });
        if (resp.status !== 200) return;
        pending = await resp.json();
    } catch (e) { return; }
    if (!pending || !pending.pending) return;
    _relayBusy = true;
    try {
        const apiBase = pending.api_base || "https://api.deepseek.com/v1";
        const key = getLocalApiKey();   // 单点读取：providers 优先 + legacy 兜底
        if (!key) throw new Error("no key");
        fillPlaceholders(pending.payload);
        // 中转降级：服务器用本请求 X-API-Key 头代发（Key 内存即弃不落盘），
        // call_id 必须匹配服务器队列中真实 pending 项（非开放代理），
        // 服务器回传时已唤醒流水线（带状态码做错误分类），前端无需再调 /relay/result
        const proxyFallback = async () => {
            const p = await fetch("/relay/proxy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ call_id: pending.call_id, payload: pending.payload }),
            });
            const pd = await p.json();
            if (!pd.ok || !pd.response) throw new Error(pd.error || "proxy failed");
        };
        let ds;
        try {
            // 用户 Key 直连代发（DeepSeek 官方端点支持浏览器 CORS）；已知不支持的供应商先剥离私有参数
            let directPayload = _loadNoThinking(apiBase) ? _stripThinking(pending.payload) : pending.payload;
            ds = await _serverFetch(apiBase + "/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json", "Authorization": "Bearer " + key },
                body: JSON.stringify(directPayload),
            });
            // 能力探测：4xx 未知参数 → 剥离后重试一次（成功后按该供应商缓存，后续直接剥离）
            if (!ds.ok && _loadNoThinking(apiBase) === false) {
                const errText = await ds.clone().text().catch(() => "");
                if (_looksUnknownParam(errText)) {
                    const stripped = _stripThinking(pending.payload);
                    ds = await _serverFetch(apiBase + "/chat/completions", {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + key },
                        body: JSON.stringify(stripped),
                    });
                    if (ds.ok) {
                        try { localStorage.setItem(_noThinkingKey(apiBase), "1"); } catch (e) {}
                    }
                }
            }
        } catch (e1) {
            // 直连失败（如 OpenCode Go 端点不支持 CORS）→ 中转降级
            await proxyFallback();
            _relayBusy = false;
            return;
        }
        let respData = null;
        if (ds.ok) {
            try { respData = await ds.json(); }
            catch (e2) {
                // 200 但响应体异常 → 降级重试
                await proxyFallback();
                _relayBusy = false;
                return;
            }
        } else {
            // API 错误响应（401 Key 无效 / 402 余额不足 / 429 限流…）：
            // 照常回传（带状态码），服务器转成分类错误唤醒流水线 → 前端人话提示
            try { respData = await ds.json(); } catch (e3) { respData = {}; }
        }
        await _serverFetch(API_BASE + "/relay/result", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
            body: JSON.stringify({ call_id: pending.call_id, response: respData, status: ds.status }),
        });
    } catch (e) {
        // 直连与中转都失败：不回传 → 服务器侧 120s 超时，流水线自动降级话术
    }
    _relayBusy = false;
}
// 由 main.js 在全部模块求值后调用（直接顶层 setInterval 会在 api↔relay 循环导入中撞 TDZ）
//
// 2026-09-19 压力测试后的调整：1s → 4s + 抖动。
// 为什么：1s 轮询 = **60 请求/分钟/用户**，是全站最大的流量来源。10× 并发（22 → 220 用户）时：
//   · 网关限流是"每 IP 300 请求/分钟"→ 同一出口 IP（手机 CGNAT / 家庭 WiFi / 办公室）
//     只够 ~4 个用户，第 5 个起全 429（实测：第 301 次请求返回 429）；
//   · 220 用户 × 70 请求/分钟 ≈ 257 请求/秒，同时逼近 3Mbps 上行。
// 降到 4s 后请求量变 1/4，代价只是"回复最多晚几秒到达"—— 而后端合并窗口本来就是 5 秒级的，
// 用户感知不到差别。**抖动**是避免所有客户端在同一秒对齐（惊群）。
function startRelay() {
    const tick = () => {
        relayTick();
        setTimeout(tick, 4000 + Math.random() * 1500);
    };
    setTimeout(tick, 1200 + Math.random() * 1200);   // 首次也错开
}


/* ── 来源：js/guide.js ── */
// 使用引导（基础 + 深度）

// ═══════════════════════════════════════════
// 使用引导（纯代码：高亮框 + 文字气泡 + CSS 呼吸边，不用任何图片）
// 基础引导 4 步；结束后可点「深入了解」进入详细引导（设置/菜单/纠错助手）。
// ═══════════════════════════════════════════
// 基础引导 5 步；结束后可点「深入了解」进入详细引导（设置/菜单/纠错助手）。
// ★ 0.9.0 把 KEY 从 v1 升到 v2：引导内容变了（新增"公告与更新说明""自动修复"两步），
//   不升 key 的话老用户永远看不到新步骤 —— 那等于没更新引导。
const GUIDE_KEY = "firefly_guide_v2_done";
const DEEP_GUIDE_KEY = "firefly_deep_guide_v2_done";
const GUIDE_STEPS = [
    { el: "#home-carousel", title: "从这里进入对话",
      text: "请实际操作：点任意一张角色卡片（如「剧情模式」），进入和角色的聊天页。\n操作成功会自动进入下一步；如果没反应，点「下一步」。",
      setup: () => { showHome(); },
      done: () => !document.getElementById("home-view").classList.contains("show") },
    { el: "#home-settings-btn", title: "先填 API Key",
      text: "请点右上角 ⚙ 打开设置，把 sk- 开头的 Key 粘进 API Key 输入框。\n没有 Key 之前，聊天只会提示你去设置。",
      setup: () => { showHome(); },
      done: () => document.getElementById("settings-panel").classList.contains("show") },
    { el: "#cards-manage-btn", title: "角色卡管理",
      text: "请点「角色卡管理」。每个角色卡的人设、用户设定、知识库、出厂记忆、表情包都在里面单独编辑（聊天产生的记忆与手账在菜单页「设定文件」里，清除历史会一并清空）。",
      setup: () => { closeSettings(); showHome(); },
      done: () => document.getElementById("cards-view").classList.contains("show") },
    { el: "#home-notice", title: "公告与更新说明",
      text: "请点首页上方的「公告 · 使用指南」。\n\n更新说明与临时提醒会由服务端下发到这里（有新内容时标题旁会亮一个小圆点）；万一拉不到，这里仍显示 App 内置的使用指南，功能不受影响。",
      setup: () => { showHome(); },
      done: () => document.getElementById("notice-panel").classList.contains("show") },
    { el: "#home-feedback-btn", title: "其他问题",
      text: "请点左上角「✉ 反馈」看看。功能建议、安装问题、联系开发者（GitHub / QQ 群 / 邮箱）都在这页。",
      setup: () => { showHome(); },
      done: () => document.getElementById("feedback-panel").classList.contains("show") },
];

function _guideOpenGroup(name) {
    try {
        const head = document.querySelector(`#settings-panel .set-head[data-group="${name}"]`);
        const group = head && head.closest(".set-group");
        if (group && !group.classList.contains("open")) head.click();
    } catch (e) {}
}

function _guideGroupOpen(name) {
    try {
        const head = document.querySelector(`#settings-panel .set-head[data-group="${name}"]`);
        const group = head && head.closest(".set-group");
        return !!(group && group.classList.contains("open"));
    } catch (e) { return false; }
}

function _guideStickerTabOpen() {
    const content = document.getElementById("tab-sticker");
    return !!(content && content.classList.contains("active"));
}

// 详细引导 = 实际操作教程：每一步让用户真的点对应功能，操作成功自动进下一步。
const DEEP_GUIDE_STEPS = [
    { el: "#home-settings-btn", title: "① 实际点开设置",
      text: "请点右上角 ⚙ 打开设置页（不要点“下一步”）。\n\n设置页有 4 组卡片：账号与连接 / 模型与速度 / 外观 / 数据；除 API Key 外，改动会自动保存。",
      setup: () => { showHome(); },
      done: () => document.getElementById("settings-panel").classList.contains("show") },
    { el: "#key-input", title: "② 试填 API Key",
      text: "请点 API Key 输入框，粘贴 sk- 开头的 Key；留空=保留原来的 Key。\n\n填完点「保存 Key 与连接设置」。需要换模型可在「模型与速度」里改 API 供应商。",
      setup: () => { showHome(); openSettings(); },
      done: () => document.activeElement && document.activeElement.id === "key-input" },
    { el: '#settings-panel .set-head[data-group="model"]', title: "③ 点开「模型与速度」",
      text: "请点「🧠 模型与速度」展开。\n\n默认真接 DeepSeek 官方（填 Key 即用）；展开「自定义」可分别调检索/分析/回复/组织四个阶段的模型与思考档位。",
      setup: () => { openSettings(); },
      done: () => _guideGroupOpen("model") },
    { el: '#settings-panel .set-head[data-group="system"]', title: "④ 点开「数据」",
      text: "请点「📦 数据」展开。\n\n版本更新、自动修复、数据快照（保存/恢复/导入 zip）、云端同步都在这里；主动消息则按角色卡配（角色卡管理 → 点角色卡 → 主动消息）。",
      setup: () => { openSettings(); },
      done: () => _guideGroupOpen("system") },
    { el: "#hotupdate-check-btn", title: "⑤ 看一眼「自动修复」",
      text: "请点「检查修复」。\n\n小 bug 修好后服务器会推一个小补丁，App 空闲时自动装上，不必重装；这里能开关、查看当前修复版本，有问题还能「回退修复」退回安装包自带的版本。",
      setup: () => { openSettings(); _guideOpenGroup("system"); _guideArmHotupdate(); },
      done: () => _guideHotTouched },
    { el: "#menu-btn", title: "⑥ 到聊天页打开菜单",
      text: "已经帮你切到聊天页：请点右上角 ☰ 打开菜单。\n\n菜单里是分组页签：与角色相关（收藏 / 表情包 / 设定文件）、与系统相关（请求记录 / 流程日志）。",
      setup: () => { closeSettings(); showChat(); },
      done: () => document.getElementById("menu-drawer").classList.contains("open") },
    { el: '.menu-tab[data-tab="sticker"]', title: "⑦ 点「表情包」页签",
      text: "请在菜单顶部点「表情包」。\n\n这一页能添加新表情、打开映射表逐个启用/停用；停用的表情不会出现在聊天面板，也不会被 AI 使用。",
      setup: () => { openMenu(); },
      done: () => _guideStickerTabOpen() },
    { el: "#sticker-manage-btn", title: "⑧ 展开映射表试开关",
      text: "请点「表情包映射表」。\n\n展开后可以试试点某张表情的「启用中 / 已停用」按钮，状态会立刻切换；改分类和描述后要点该卡片「保存」。内置默认表情的「删」是灰色保护。",
      setup: () => { openMenu(); try { document.querySelector('.menu-tab[data-tab="sticker"]')?.click(); } catch (e) {} },
      done: () => { const p = document.getElementById("sticker-manage-panel"); return !!(p && p.style.display !== "none" && p.style.display !== ""); } },
    { el: "#sticker-add-btn", title: "⑨ 看看添加表情包表单",
      text: "请点「+ 添加表情包」展开表单（不用真的上传）。\n\n流程是：选图 → 选分类（可爱/帅气）→ 写一句含义描述 → 保存。描述越清楚，AI 选图越准。",
      setup: () => { openMenu(); try { document.querySelector('.menu-tab[data-tab="sticker"]')?.click(); } catch (e) {} },
      done: () => { const f = document.getElementById("sticker-add-form"); return !!(f && f.style.display !== "none" && f.style.display !== ""); } },
    { el: "#home-feedback-btn", title: "⑩ 反馈页可随时重看",
      text: "最后请点左上角「✉ 反馈」。\n\n以后想复习：反馈页点「查看详细使用教程」即可重新开始这套实际操作教程；有问题可在 GitHub / QQ 群 / 邮箱反馈。",
      setup: () => { showHome(); },
      done: () => document.getElementById("feedback-panel").classList.contains("show") },
];

// 步骤⑤的判定：用户真的点了「检查修复」（而不是"这个按钮存在"——那不叫操作成功）。
// 监听是一次性的：进入该步时挂上，离开后自然失效（按钮不存在时直接算完成，不卡住流程）。
let _guideHotTouched = false;
function _guideArmHotupdate() {
    _guideHotTouched = false;
    const btn = document.getElementById("hotupdate-check-btn");
    if (!btn) { _guideHotTouched = true; return; }
    btn.addEventListener("click", () => { _guideHotTouched = true; }, { once: true });
}

let _guideIndex = 0;
let _guideSteps = GUIDE_STEPS;
let _guideKey = GUIDE_KEY;
let _guideMask = null, _guideSpot = null, _guideTip = null;
let _guideBlocks = null;
let _guideAdvanceTimer = null;

function _guideEnsureHome() {
    try { if (typeof closeFeedback === "function") closeFeedback(); } catch (e) {}
    if (typeof showHome === "function") showHome();
}

function _guideMarkDone(key) {
    try { localStorage.setItem(key, "1"); } catch (e) {}
}

function _guideOnUserClick(e) {
    if (!_guideMask || _guideIndex >= _guideSteps.length) return;
    // 教程气泡上的按钮（跳过/上一步/下一步）不走自动判定
    if (e.target && e.target.closest && e.target.closest("#guide-tip")) return;
    const idx = _guideIndex;
    const step = _guideSteps[idx];
    if (!step || typeof step.done !== "function") return;
    clearTimeout(_guideAdvanceTimer);
    _guideAdvanceTimer = setTimeout(() => {
        if (!_guideMask || _guideIndex !== idx) return;
        try {
            if (step.done()) {
                if (idx >= _guideSteps.length - 1) _guideClose();
                else _guideTo(idx + 1);
            }
        } catch (err) {}
    }, 350);
}

function _guideClose() {
    clearTimeout(_guideAdvanceTimer);
    document.removeEventListener("click", _guideOnUserClick, true);
    if (_guideMask) _guideMask.remove();
    _guideMask = _guideSpot = _guideTip = null;
    _guideBlocks = null;
    _guideMarkDone(_guideKey);
    try { closeSettings(); closeMenu(); showHome(); } catch (e) {}
}

function _guideCreateMask() {
    if (_guideMask) _guideMask.remove();
    _guideMask = document.createElement("div");
    _guideMask.id = "guide-mask";
    _guideSpot = document.createElement("div");
    _guideSpot.id = "guide-spot";
    _guideTip = document.createElement("div");
    _guideTip.id = "guide-tip";
    _guideBlocks = {};
    ["top", "bottom", "left", "right"].forEach(name => {
        const d = document.createElement("div");
        d.className = "guide-block";
        d.id = "guide-block-" + name;
        _guideBlocks[name] = d;
        _guideMask.appendChild(d);
    });
    _guideMask.appendChild(_guideSpot);
    _guideMask.appendChild(_guideTip);
    document.body.appendChild(_guideMask);
    document.addEventListener("click", _guideOnUserClick, true);
}

function _guideTo(i) {
    _guideIndex = Math.max(0, Math.min(i, _guideSteps.length - 1));
    const step = _guideSteps[_guideIndex];
    if (step.setup) { try { step.setup(); } catch (e) {} }
    const target = document.querySelector(step.el);
    if (!target) { _guideIndex++; if (_guideIndex >= _guideSteps.length) { _guideClose(); return; } _guideTo(_guideIndex); return; }

    const r = target.getBoundingClientRect();
    const pad = 6;
    Object.assign(_guideSpot.style, {
        left: (r.left - pad) + "px", top: (r.top - pad) + "px",
        width: (r.width + pad * 2) + "px", height: (r.height + pad * 2) + "px",
    });
    // 透明拦截片：盖住高亮目标以外的全部区域，其他按钮真的不可点；目标区域保持可点
    if (_guideBlocks) {
        const vw = document.documentElement.clientWidth || window.innerWidth;
        const vh = document.documentElement.clientHeight || window.innerHeight;
        const x0 = Math.max(0, r.left - pad), x1 = Math.min(vw, r.right + pad);
        const y0 = Math.max(0, r.top - pad), y1 = Math.min(vh, r.bottom + pad);
        Object.assign(_guideBlocks.top.style, { left: "0px", top: "0px", width: vw + "px", height: Math.max(0, y0) + "px" });
        Object.assign(_guideBlocks.bottom.style, { left: "0px", top: y1 + "px", width: vw + "px", height: Math.max(0, vh - y1) + "px" });
        Object.assign(_guideBlocks.left.style, { left: "0px", top: y0 + "px", width: Math.max(0, x0) + "px", height: Math.max(0, y1 - y0) + "px" });
        Object.assign(_guideBlocks.right.style, { left: x1 + "px", top: y0 + "px", width: Math.max(0, vw - x1) + "px", height: Math.max(0, y1 - y0) + "px" });
    }
    // 目标在屏幕下半部时，把讲解气泡放到顶部，避免气泡盖住要点击的目标
    const vh = window.innerHeight || document.documentElement.clientHeight || 800;
    _guideTip.classList.toggle("top", (r.top + r.height / 2) > vh * 0.55);
    const isBasic = _guideSteps === GUIDE_STEPS;
    const isLast = _guideIndex === _guideSteps.length - 1;
    _guideTip.innerHTML =
        `<div class="guide-step">${isBasic ? "基础引导" : "实际操作教程"} · ${_guideIndex + 1} / ${_guideSteps.length}</div>` +
        `<div class="guide-title">${escapeHtml(step.title)}</div>` +
        `<div class="guide-text">${escapeHtml(step.text)}</div>` +
        `<div class="guide-actions">` +
        `<button type="button" class="guide-btn skip" id="guide-skip">跳过教程</button>` +
        (_guideIndex > 0 ? `<button type="button" class="guide-btn prev" id="guide-prev">上一步</button>` : "") +
        (isBasic && isLast ? `<button type="button" class="guide-btn deep" id="guide-deep">深入了解</button>` : "") +
        `<button type="button" class="guide-btn next" id="guide-next">${isLast ? "完成" : "没反应？下一步"}</button>` +
        `</div>`;
    document.getElementById("guide-skip").onclick = _guideClose;
    const prev = document.getElementById("guide-prev");
    if (prev) prev.onclick = () => _guideTo(_guideIndex - 1);
    document.getElementById("guide-next").onclick = () => {
        if (isLast) _guideClose();
        else _guideTo(_guideIndex + 1);
    };
    const deep = document.getElementById("guide-deep");
    if (deep) deep.onclick = _startDeepGuide;
    try { target.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
}

function _guideStart(steps, key, showHomeFirst, force) {
    if (!force) { try { if (localStorage.getItem(key)) return; } catch (e) { return; } }
    if (_guideMask) return;
    if (showHomeFirst) _guideEnsureHome();
    _guideSteps = steps;
    _guideKey = key;
    _guideCreateMask();
    setTimeout(() => _guideTo(0), 150);
}

function _startBasicGuide() {
    _guideStart(GUIDE_STEPS, GUIDE_KEY, true, false);
}

function _startDeepGuide() {
    _guideMarkDone(GUIDE_KEY);   // 从「深入了解」进入时，基础引导视为已完成
    _guideClose();               // 移除旧气泡与点击监听（并回到首页）
    _guideStart(DEEP_GUIDE_STEPS, DEEP_GUIDE_KEY, true, true);
}
window.startDeepGuide = _startDeepGuide;

if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", () => setTimeout(_startBasicGuide, 900));
} else {
    setTimeout(_startBasicGuide, 900);
}


/* ── 来源：js/main.js ── */
// 启动编排：合并原文件两套启动路径（DOMContentLoaded 在 views.js）

// ═══════════════════════════════════════════
// 起床检查
// ═══════════════════════════════════════════
async function checkWake() {
    try {
        const resp = await fetch(`/wake-status?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (data.interrupted) {
            document.getElementById("wake-overlay").style.display = "flex";
            document.getElementById("wake-text").textContent = (charName() || "角色") + "正在起床，记忆还在整理中…";
        }
    } catch(e) {}
}

// ═══════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════
checkWake();
checkKey().then(() => { loadHistory(); });
initAssets();   // 服务器模式：已有 token 时立即资产本地化（未登录静默失败，登录后 initAuth 会再触发）
loadFixStatus();   // 设定纠错助手：恢复多轮对齐/待确认方案（本地与服务器模式都可用）
// 公告通道**不在这里启动**：由 js/notice.js 自己挂 DOMContentLoaded（原因见该文件末尾的说明
// —— bundle 单作用域，从 main 顶层调进去会撞 TDZ，把 notice 整块静默打死）。
if (IS_SERVER) startRelay();   // relay 引擎仅服务器模式（本地为 direct 直发）


/* ── 来源：js/hotupdate.js ── */
// 热更新前端（见 docs/热更新规范.md 与 热更新/02_实现契约.md §五/§六/§七）
//
// 前端在这条链路上有四件不可省的事：
//   ① 上报「忙/闲」——后端据此决定什么时候允许 reload（规范 §5.2.1：绝不打断用户）
//   ② 首帧渲染完成后上报 boot-ok —— 这是"启动成功判据"，坏补丁靠它才敢确认（§5.5）
//   ③ reload 前把草稿存 sessionStorage —— 一个会吞掉用户正在打的字的"静默修复"比不修还糟（§5.2.2）
//   ④ 把运行版本三元组露给用户，并给一个手动回滚的出口（信任问题，不只是合规）
//
// 写成自包含 IIFE：bundle 是单作用域拼接，不污染其它模块的名字。
(function () {
    "use strict";

    var POLL_MS = 10000;          // 本地请求，代价可忽略；10s 内完成"立即生效"
    var DRAFT_KEY = "firefly_hu_draft";
    var _busy = false;
    var _busyWhy = "";
    var _last = null;

    function $(id) { return document.getElementById(id); }

    function post(path, obj) {
        try {
            return fetch(path, {
                method: "POST", cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(obj || {}),
            });
        } catch (e) { return Promise.resolve(null); }
    }

    // ── ① 忙/闲上报 ────────────────────────────────
    function setBusy(busy, why) {
        if (busy === _busy && why === _busyWhy) return;
        _busy = busy; _busyWhy = why;
        post("/hotupdate/activity", { busy: busy, why: why });
    }

    function computeBusy() {
        // 输入框有内容 = 用户正在打字（后端还会叠加"模型下载中/正在应用"两个自有判据）
        var inp = $("msg-input");
        if (inp && inp.value && inp.value.trim()) return "typing";
        if (typeof _voicePlaying !== "undefined" && _voicePlaying) return "playing";
        return "";
    }

    function watchActivity() {
        var inp = $("msg-input");
        if (inp) {
            ["input", "focus", "compositionstart"].forEach(function (ev) {
                inp.addEventListener(ev, function () { setBusy(!!computeBusy(), computeBusy()); });
            });
            ["blur", "compositionend"].forEach(function (ev) {
                inp.addEventListener(ev, function () {
                    var w = computeBusy();
                    setBusy(!!w, w);
                });
            });
        }
        setInterval(function () {
            var w = computeBusy();
            setBusy(!!w, w);
        }, 3000);
    }

    // ── ③ 草稿保留 ──────────────────────────────────
    function saveDraft() {
        try {
            var inp = $("msg-input");
            var msgs = $("messages");
            sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
                text: inp ? inp.value : "",
                scroll: msgs ? msgs.scrollTop : 0,
                at: Date.now(),
            }));
        } catch (e) { /* 存不下也不能拦着 reload */ }
    }

    function restoreDraft() {
        try {
            var raw = sessionStorage.getItem(DRAFT_KEY);
            if (!raw) return;
            sessionStorage.removeItem(DRAFT_KEY);
            var d = JSON.parse(raw);
            if (Date.now() - (d.at || 0) > 60000) return;   // 太久的草稿不要（可能是上次崩的）
            var inp = $("msg-input");
            if (inp && d.text) {
                inp.value = d.text;
                if (typeof setBusy === "function") setBusy(true, "typing");
            }
            var msgs = $("messages");
            if (msgs && d.scroll) msgs.scrollTop = d.scroll;
        } catch (e) { /* 恢复失败无所谓 */ }
    }

    // ── ④ 状态展示 + 操作 ───────────────────────────
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function render(st) {
        var box = $("hotupdate-box");
        if (!box || !st) return;
        var run = st.running || {};
        var lines = [];
        var ver = esc(run.base_version || "") +
            (run.hot_serial ? ' <b style="color:var(--fg-accent)">hot.' + run.hot_serial + "</b>" : "");
        lines.push('<div>运行版本：' + ver +
            (run.patch_hash ? ' <span style="opacity:.6">' + esc(run.patch_hash) + "</span>" : "") +
            "</div>");
        if (!st.enabled) {
            lines.push('<div style="opacity:.75">热更新已关闭（只保留安全吊销）</div>');
        } else if (st.available) {
            lines.push('<div>可更新：修复 ' + st.available.serial + " · " +
                esc(st.available.note || "") + "</div>");
        } else if (st.overlay_files) {
            lines.push('<div style="opacity:.75">已应用 ' + st.overlay_files + " 个文件</div>");
        } else {
            lines.push('<div style="opacity:.6">没有待安装的修复</div>');
        }
        if (st.rolled_back_reason) {
            lines.push('<div style="color:#e0a05c">上次修复已回退：' + esc(st.rolled_back_reason) + "</div>");
        }
        if (st.last_error) {
            lines.push('<div style="opacity:.7">' + esc(st.last_error) + "</div>");
        }
        box.innerHTML = lines.join("");
        var rb = $("hotupdate-rollback-btn");
        if (rb) rb.style.display = st.applied_serial ? "" : "none";
        var ap = $("hotupdate-apply-btn");
        if (ap) ap.style.display = (st.enabled && st.available) ? "" : "none";
        var tg = $("hotupdate-toggle");
        if (tg) tg.checked = !!st.enabled;
    }

    function poll() {
        fetch("/hotupdate/status", { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (st) {
                _last = st;
                render(st);
                // ★ 生效：后端说可以刷新了，而且此刻确实空闲 → 存草稿后刷新
                if (st.reload_pending && st.idle) {
                    saveDraft();
                    location.reload();
                }
            })
            .catch(function () { /* 后端在重启/不可用：下一轮再试 */ });
    }

    function wire() {
        var tg = $("hotupdate-toggle");
        if (tg) {
            tg.addEventListener("change", function () {
                post("/hotupdate/action", { action: "set_enabled", enabled: tg.checked })
                    .then(poll);
            });
        }
        var ck = $("hotupdate-check-btn");
        if (ck) {
            ck.addEventListener("click", function () {
                var m = $("hotupdate-msg");
                if (m) m.textContent = "检查中…";
                post("/hotupdate/action", { action: "check" }).then(function (r) {
                    return r ? r.json() : null;
                }).then(function (d) {
                    if (m) m.textContent = (d && d.ok) ? "已检查" : ("检查失败：" + ((d && d.error) || "网络不可达"));
                    poll();
                }).catch(function () { if (m) m.textContent = "检查失败"; });
            });
        }
        var ap = $("hotupdate-apply-btn");
        if (ap) {
            ap.addEventListener("click", function () {
                var m = $("hotupdate-msg");
                if (m) m.textContent = "下载并安装中…";
                post("/hotupdate/action", { action: "apply" }).then(function (r) {
                    return r ? r.json() : null;
                }).then(function (d) {
                    if (m) m.textContent = (d && d.ok) ? "已安装，即将生效" : ("安装失败：" + ((d && d.error) || ""));
                    poll();
                }).catch(function () { if (m) m.textContent = "安装失败"; });
            });
        }
        var rb = $("hotupdate-rollback-btn");
        if (rb) {
            rb.addEventListener("click", function () {
                if (!confirm("回退到当前安装包的版本？")) return;
                post("/hotupdate/action", { action: "rollback" }).then(poll);
            });
        }
    }

    function boot() {
        restoreDraft();
        wire();
        // ★ 启动成功判据：首帧渲染完成（这里就是）→ 告诉后端"补丁是好的"
        post("/hotupdate/boot-ok", {});
        poll();
        setInterval(poll, POLL_MS);
        watchActivity();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();


/* ── 来源：js/notice.js ── */
// 公告 / 更新说明通道（服务端下发 + 本地缓存 + 静态兜底）
// 见 docs/公告通道规范.md。
//
// 三条设计约束（都不是"顺手写的"，是有原因的）：
//  ① **不阻塞**：先渲染 localStorage 里的上次结果，再后台联网刷新。
//     公告是"顺带看一眼"的东西，绝不能让人打开面板时卡在网络上。
//  ② **不拼 HTML**：服务端文本一律 createTextNode/textContent 写入。
//     这样即便签名密钥泄露、服务端被投毒，也**注入不进来** —— 结构上不存在 XSS 面。
//  ③ **不依赖网络**：拿不到就什么都不加，Index.html 里的内置指南照常显示。

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
async function refreshNotice(force) {
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
function noticeOnOpen() {
    _markReadSoon();
    let last = 0;
    try { last = parseInt(_ls(SEEN_OPEN_KEY) || "0", 10) || 0; } catch (e) {}
    if (Date.now() - last < REFRESH_MS) return;
    _ls(SEEN_OPEN_KEY, String(Date.now()));
    refreshNotice(false);
}

// 启动：先画缓存（瞬时），再后台刷新（不 await）
function initNotice() {
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
