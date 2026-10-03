// 双模式与请求封装：FIREFLY_MODE / fetch 鉴权注入 / 登录与注册表单
import { showToast, getLocalApiKey } from "./util.js";
import { initAssets } from "./relay.js";
import { encHead, ENC_HEADER, SECRET_HEADERS } from "./session_crypto.js";

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
export const IS_SERVER = FIREFLY_MODE === "server";
export const API_BASE = IS_SERVER ? (window.FIREFLY_SERVER_BASE || "") : "";
export const _serverFetch = window.fetch;

// 服务器模式：账号 + Key 请求头注入（本地模式原样直通，同源无跨域）
//
// ★ 2026-09-25 敏感头应用层加密（见 docs/审计-服务器与开发版-2026-09-18.md §2.1）：
//   全站明文 HTTP 的零成本对策 —— `Authorization` / `X-API-Key` / `X-API-Base` 三个头的
//   值用服务器公钥加密后放进 `X-Firefly-Enc`，链路上看不到明文 token/Key。
//   实现要点：
//     · 加密是**异步**的（crypto.subtle / BigInt 模幂）⇒ 本包装器改成 `async` 返回 Promise
//       （调用方本来就在 await / .then，语义不变）。
//     · **加密失败绝不放行加密头但漏掉明文头**：失败时按原样发明文（旧客户端行为），
//       服务器两种情况都收 —— 宁可暂时退回明文，也不能让用户登不进去。
//     · 只对**服务器模式**加密：本地版同源 127.0.0.1，加解密纯属浪费。
window.fetch = async function (url, opts) {
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
    // ── 敏感头加密（失败退回明文，见上方说明）──
    try {
        const pairs = [];
        SECRET_HEADERS.forEach(function (name) {
            const v = headers.get(name);
            if (v) pairs.push([name, v]);
        });
        if (pairs.length) {
            const r = await encHead(pairs);
            if (r && r.enc) {
                SECRET_HEADERS.forEach(function (name) { headers.delete(name); });
                headers.set(ENC_HEADER, r.enc);
            }
        }
    } catch (e) { /* 保持明文 —— 服务器两种都收 */ }
    // 相对路径 → 服务器绝对 URL（本地 file:// 页面无同源相对路径）
    let fullUrl = String(url);
    if (fullUrl.startsWith("/")) fullUrl = API_BASE + fullUrl;
    opts = Object.assign({}, opts, { headers: headers });
    const resp = await _serverFetch(fullUrl, opts);
    // 401：登录失效/未登录。仅对用户主动操作（/chat）提示并亮出登录模块；
    // 后台轮询端点（proactive-status/config/history/relay 等）静默——否则
    // 未登录时「请先登录后使用」toast 每 10s 弹一次刷屏。
    if (resp.status === 401 && String(url).indexOf("/chat") >= 0 && !String(url).includes("/auth/")) {
        try { showToast("请先登录后使用"); } catch (e) {}
        try { showAuthModule(); } catch (e) {}
    }
    return resp;
};

// API 来源切换：托管模式隐藏 Key/供应商输入，显示隐私提示
export function applyApiSource(isProxy) {
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
export function showAuthModule() {
    const mod = document.getElementById("auth-module");
    if (mod) mod.style.display = "block";
}
export function initAuth() {
    initAuthForms();          // 内联登录/注册/重置表单接线（幂等；本地模式走后端 /auth/* 代理）
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
