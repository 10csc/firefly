// 双模式与请求封装：FIREFLY_MODE / fetch 鉴权注入 / 登录与注册表单
import { showToast } from "./util.js";
import { initAssets } from "./relay.js";

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
window.fetch = function (url, opts) {
    if (!IS_SERVER) return _serverFetch(url, opts);
    opts = opts || {};
    const headers = new Headers(opts.headers || {});
    let k = ""; try { k = localStorage.getItem("firefly_api_key") || ""; } catch (e) {}
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

// API 来源切换：托管模式隐藏 Key/接口地址输入，显示隐私提示
export function applyApiSource(isProxy) {
    const ownFields = document.getElementById("api-own-fields");
    const baseField = document.getElementById("api-base-field");
    const tip = document.getElementById("api-proxy-tip");
    if (ownFields) ownFields.style.display = isProxy ? "none" : "";
    if (baseField) baseField.style.display = isProxy ? "none" : "";
    if (tip) tip.style.display = isProxy ? "block" : "none";
}

// ═══ 服务器版：登录状态模块（轮播图下） ═══
function showAuthModule() {
    const mod = document.getElementById("auth-module");
    if (mod && IS_SERVER) mod.style.display = "block";
}
export function initAuth() {
    if (!IS_SERVER) return;   // 本地模式无账号概念：登录模块不显示
    initAuthForms();          // 内联登录/注册/重置表单接线（幂等）
    const token = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
    const loginEntry = document.getElementById("auth-login-entry");
    const userEntry = document.getElementById("auth-user-entry");
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
    if (!IS_SERVER) return;
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
