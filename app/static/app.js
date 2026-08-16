// 流萤聊天 App — 前端逻辑（统一前端 0.8.0：本地 / 服务器双模式一套代码）

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const SESSION_ID = "firefly-" + Date.now();
let waiting = false;

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
function applyApiSource(isProxy) {
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
function initAuth() {
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
    loadCharFiles(); loadJournal(); loadUserMemory();
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

// ═══════════════════════════════════════════
// 检查更新（GitHub 优先，失败自动降级 Gitee——国内网络 Gitee 更稳）
// ═══════════════════════════════════════════
const CURRENT_VERSION = "0.8.0";   // 与 android versionName / 安装器 AppVersion 保持一致
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
        const lr = await fetch("/check-update", {cache: "no-store"});
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
        if (btn.dataset.tab === "char") { loadCharFiles(); loadJournal(); loadUserMemory(); }
        if (btn.dataset.tab === "state") loadStateTab();
        if (btn.dataset.tab === "log") loadRequestLog();
        if (btn.dataset.tab === "pipeline") loadPipeline();
    });
});

// ═══════════════════════════════════════════
// 配置管理
// ═══════════════════════════════════════════
// 配置管理（设置页分组：账号与连接 / 主动消息 / 模型与速度 / 外观 / 数据与系统）
const _CFG_DEFAULTS = {
    fast: {
        analyzer_model: "deepseek-v4-flash", retriever_model: "deepseek-v4-flash",
        organizer_model: "deepseek-v4-flash", polisher_model: "deepseek-v4-flash",
        retriever_effort: "none", analyzer_effort: "high",
        polisher_effort: "high", organizer_effort: "none",
    },
    strong: {
        analyzer_model: "deepseek-v4-pro", retriever_model: "deepseek-v4-pro",
        organizer_model: "deepseek-v4-pro", polisher_model: "deepseek-v4-pro",
        retriever_effort: "none", analyzer_effort: "high",
        polisher_effort: "high", organizer_effort: "none",
    },
};
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

function _modelPresetName(models) {
    return models.analyzer === "deepseek-v4-pro" && models.polisher === "deepseek-v4-pro"
        && models.retriever === "deepseek-v4-pro" && models.organizer === "deepseek-v4-pro"
        ? "strong" : "fast";
}

function _applyModelPreset(name) {
    const p = _CFG_DEFAULTS[name] || _CFG_DEFAULTS.fast;
    _$("analyzer-model-select").value = p.analyzer_model;
    _$("retriever-model-select").value = p.retriever_model;
    _$("organizer-model-select").value = p.organizer_model;
    _$("polisher-model-select").value = p.polisher_model;
    _$("retriever-effort-select").value = p.retriever_effort;
    _$("analyzer-effort-select").value = p.analyzer_effort;
    _$("polisher-effort-select").value = p.polisher_effort;
    _$("organizer-effort-select").value = p.organizer_effort;
    updateSettingsSummaries();
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
        const strong = _modelPresetName({
            analyzer: _$("analyzer-model-select").value,
            polisher: _$("polisher-model-select").value,
            retriever: _$("retriever-model-select").value,
            organizer: _$("organizer-model-select").value,
        }) === "strong";
        ms.textContent = strong ? "更强 · Pro" : "快速 · Flash";
    }
}

function _buildSettingsPayload() {
    return {
        analyzer_model: _$("analyzer-model-select").value,
        retriever_model: _$("retriever-model-select").value,
        organizer_model: _$("organizer-model-select").value,
        polisher_model: _$("polisher-model-select").value,
        retriever_effort: _$("retriever-effort-select").value,
        analyzer_effort: _$("analyzer-effort-select").value,
        polisher_effort: _$("polisher-effort-select").value,
        organizer_effort: _$("organizer-effort-select").value,
        retriever_temperature: parseFloat(_$("retriever-temp-slider").value) || 0,
        proactive_enabled: _$("proactive-enabled").checked,
        proactive_hard: parseInt(_$("proactive-hard-slider").value) || 6,
        proactive_soft: (parseInt(_$("proactive-soft-slider").value) || 35) / 100,
        prob_reply_enabled: _$("prob-reply-enabled").checked,
        prob_reply_value: (parseInt(_$("prob-reply-slider").value) || 10) / 100,
        hidden_reply_enabled: _$("hidden-reply-enabled").checked,
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
    _hiddenEnabled = payload.hidden_reply_enabled !== false;
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
    const baseSel = _$("api-base-select");
    const base = baseSel ? baseSel.value : "https://api.deepseek.com/v1";
    if (IS_SERVER && explicit) {
        try { localStorage.setItem("firefly_api_source", src); } catch (e) {}
        if (src !== "proxy") {
            if (k) { try { localStorage.setItem("firefly_api_key", k); } catch (e) {} }
            try { localStorage.setItem("firefly_api_base", base); } catch (e) {}
        }
        if (keyInput) keyInput.value = "";
    }
    const payload = _buildSettingsPayload();
    if (!IS_SERVER && explicit) {
        if (k) payload.api_key = k;
        payload.api_base = base;
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
        a: "analyzer-model-select", r: "retriever-model-select",
        o: "organizer-model-select", p: "polisher-model-select",
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

        const normEffort = v => (v === "low" ? "high" : v);
        if (el.a) el.a.value = data.analyzer_model || "deepseek-v4-flash";
        if (el.r) el.r.value = data.retriever_model || "deepseek-v4-flash";
        if (el.o) el.o.value = data.organizer_model || "deepseek-v4-flash";
        if (el.p) el.p.value = data.polisher_model || "deepseek-v4-flash";
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
        _hiddenEnabled = data.hidden_reply_enabled !== false;
        const pp = _$("proactive-preset");
        if (pp) pp.value = _proactivePresetName(hard, soft);
        const mp = _$("model-preset");
        if (mp) mp.value = _modelPresetName({
            analyzer: el.a ? el.a.value : "deepseek-v4-flash",
            polisher: el.p ? el.p.value : "deepseek-v4-flash",
            retriever: el.r ? el.r.value : "deepseek-v4-flash",
            organizer: el.o ? el.o.value : "deepseek-v4-flash",
        });

        const abSel = _$("api-base-select");
        const localBase = (() => { try { return localStorage.getItem("firefly_api_base") || ""; } catch (e) { return ""; } })();
        if (abSel) {
            const want = IS_SERVER ? localBase : data.api_base;
            if ((data.api_bases || []).includes(want)) abSel.value = want;
        }
        const srcSel = _$("api-source-select");
        const srcField = _$("api-source-field");
        const localSrc = (() => { try { return localStorage.getItem("firefly_api_source") || "own"; } catch (e) { return "own"; } })();
        if (srcSel) {
            srcSel.value = localSrc === "proxy" ? "proxy" : "own";
            applyApiSource(IS_SERVER && localSrc === "proxy");
        }
        if (srcField) srcField.style.display = IS_SERVER ? "" : "none";
        const syncFields = _$("sync-fields");
        if (syncFields) syncFields.style.display = IS_SERVER ? "" : "none";

        if (data.retriever_temperature != null && el.rt) {
            el.rt.value = data.retriever_temperature;
            if (el.rtv) el.rtv.textContent = Number(data.retriever_temperature).toFixed(1);
        }

        if (el.m) {
            const keyLabel = _$("key-label");
            if (IS_SERVER) {
                const localKey = (() => { try { return localStorage.getItem("firefly_api_key") || ""; } catch (e) { return ""; } })();
                el.m.textContent = localKey ? "Key 已设置（仅存本机浏览器）" : "尚未设置 API Key（不会上传服务器）";
                if (keyLabel) keyLabel.textContent = "API Key（存于本机浏览器，不会上传服务器）";
            } else {
                el.m.textContent = data.has_key ? "Key 已设置（" + (data.key_prefix || "仅本机") + "）" : "尚未设置 API Key";
                if (keyLabel) keyLabel.textContent = "API Key（存本机配置文件，仅本机使用）";
            }
        }
        if (el.k) {
            if (IS_SERVER) {
                const localKey = (() => { try { return localStorage.getItem("firefly_api_key") || ""; } catch (e) { return ""; } })();
                el.k.placeholder = localKey ? "已设置，留空则保留" : "sk-...";
            } else {
                el.k.placeholder = data.has_key ? "已设置，留空则保留原 Key" : "sk-...";
            }
            el.k.value = "";
        }

        const hiddenField = _$("hidden-reply-field");
        if (hiddenField) hiddenField.style.display = window.FireflyMode ? "" : "none";

        const exitRow = _$("app-exit-row");
        if (exitRow && data.platform === "pc") {
            exitRow.style.display = "";
            const exitBtn = _$("app-exit-btn");
            if (exitBtn) exitBtn.onclick = () => {
                if (!confirm("确定退出流萤吗？聊天数据已实时保存，下次启动继续。")) return;
                exitBtn.disabled = true;
                exitBtn.textContent = "正在退出…";
                fetch("/shutdown", {method: "GET"}).catch(() => {});
            };
        }

        _configLoaded = true;
        updateSettingsSummaries();
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

_$("model-preset")?.addEventListener("change", () => {
    _applyModelPreset(_$("model-preset").value);
    _scheduleAutoSave();
});
_$("proactive-preset")?.addEventListener("change", () => {
    _applyProactivePreset(_$("proactive-preset").value);
    _scheduleAutoSave();
});

["analyzer-model-select", "retriever-model-select", "organizer-model-select", "polisher-model-select",
 "retriever-effort-select", "analyzer-effort-select", "polisher-effort-select", "organizer-effort-select",
 "retriever-temp-slider", "proactive-enabled", "proactive-hard-slider", "proactive-soft-slider",
 "prob-reply-enabled", "prob-reply-slider", "hidden-reply-enabled"].forEach(id => {
    const el = _$(id);
    if (el) el.addEventListener("change", () => { updateSettingsSummaries(); _scheduleAutoSave(); });
});

const apiSourceSel = _$("api-source-select");
if (apiSourceSel) {
    apiSourceSel.addEventListener("change", () => applyApiSource(apiSourceSel.value === "proxy"));
}

_$("key-save")?.addEventListener("click", () => saveConfigNow(true));

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

function _addAvatar(row, who) {    const img = document.createElement("img");
    img.className = "msg-avatar";
    if (who === "user") {
        img.src = TB_AVATARS[tbChoice];
        img.classList.add("tb-toggle");
        img.title = "点击切换开拓者";
        img.addEventListener("click", openAvatarPicker);
        img.classList.add("tb-avatar");
    } else {
        img.src = "流萤_头像.png";
    }
    row.insertBefore(img, row.firstChild);
}

function addTextMessage(text, who, prepend = false, seq = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");   // 新消息从下方浮现（历史加载不带动画）
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    row.appendChild(bubble);
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { messagesEl.appendChild(row); scrollToBottom(); }
    return row;
}

function addSticker(stickerPath, who, prepend = false, seq = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");
    const img = document.createElement("img");
    img.className = "sticker-img";
    img.src = (IS_SERVER ? API_BASE : "") + "/assets/" + stickerPath;
    // 容错：表情包文件缺失（历史遗留/用户删除）时降级为文字占位，不显示裂图
    img.onerror = () => {
        if (img.dataset.fallback) return;
        img.dataset.fallback = "1";
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = "（表情包已失效）";
        row.replaceChild(span, img);
    };
    row.appendChild(img);
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
    const gen = _modeGen;   // 捕获渲染启动时的模式代际
    _rendering = true;   // 渲染动画开始：防主动轮询中途插入乱序
    // 时间标注：取第一条消息的时间，放居中分割线
    const ts = messages[0].time ? messages[0].time.slice(11, 16) : new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    addTimeDivider(ts);
    // 逐条消息加载：先显示三圆点占位，再替换为真实内容（消息含文本与表情包）
    // 加载时长按字数 0.7~1.5s（表情包按最短 0.7s）；消息之间留 0.5s 空白模拟游戏节奏
    let seq = 0;
    const showNext = () => {
        if (gen !== _modeGen) { _rendering = false; return; }   // 模式已切换：丢弃剩余动画
        if (seq >= messages.length) {
            _rendering = false;   // 渲染动画完成
            return;
        }
        const msg = messages[seq++];
        const chars = (msg.content || msg.text || "").length;
        const loadMs = Math.min(1500, Math.max(700, 700 + chars * 25));
        const typingRow = addTypingBubble(who);
        setTimeout(() => {
            if (gen !== _modeGen) { typingRow.remove(); _rendering = false; return; }
            typingRow.remove();
            if (msg.type === "sticker") addSticker(msg.path, who);
            else if (msg.type === "narration") addNarration(msg.text, msg.style);
            else addTextMessage(msg.content, who);
            setTimeout(showNext, 500);   // 消息间隔：0.5s 空白
        }, loadMs);
    };
    showNext();
}

// ═══════════════════════════════════════════
// 设定纠错助手（对齐 → 开始修改 → diff 审批 → 应用/回滚）
// ═══════════════════════════════════════════
const FIX_FILE_LABELS = {
    "core.md": "核心设定", "identity.md": "关系与习惯", "sms_samples.md": "短信风格",
    "用户设定.md": "用户补充设定", "memory.md": "跨会话记忆", "手账.md": "流萤手账",
};
let FIX_MODE = "story";   // 首页卡片选择的模式；进入聊天后跟随最近使用模式
let _fixBusy = false;

function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

function fixModeLabel(mode) { return MODE_NAMES[mode] || mode; }

function openFixView() {
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
    if (mode !== "story" && mode !== "haruno") mode = "story";
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
            <span class="fix-hist-who">${me ? "我" : "流萤"}</span>
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
        chat.innerHTML = `<div class="fix-empty">先说说她哪里说得不对，我会和你确认后再生成修改方案。</div>`
                       + `<div class="fix-hint">例如：她还说自己在医疗舱，但设定里已经恢复得不错、能开机甲了。</div>`;
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
            showToast(data.error || "分析失败，请稍后再试");
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

(function initFixModule() {
    const input = document.getElementById("fix-input");
    const sendBtn = document.getElementById("fix-send-btn");
    if (input && sendBtn) {
        sendBtn.addEventListener("click", () => sendFixMessage(input.value));
        input.addEventListener("keydown", e => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFixMessage(input.value); }
        });
    }
    document.querySelectorAll("#fix-view .fix-mode").forEach(b => {
        b.addEventListener("click", () => setFixMode(b.dataset.mode));
    });
    const histRefresh = document.getElementById("fix-chathist-refresh");
    if (histRefresh) histRefresh.addEventListener("click", loadFixChatHistory);
    if (FIX_MODE === "story") {
        const b = document.querySelector('#fix-view .fix-mode[data-mode="story"]');
        if (b) b.classList.add("active");
    }
})();

// ═══════════════════════════════════════════
// 界面大小调节（消息/头像/气泡缩放，设置面板滑条）
// ═══════════════════════════════════════════
function applyUiScale(percent) {
    document.body.style.setProperty("--ui-scale", (percent / 100).toFixed(2));
    const val = document.getElementById("ui-scale-value");
    if (val) val.textContent = percent + "%";
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

// ═══════════════════════════════════════════
// 首页：视图切换 / 滚动轮播 / 公告面板 / 模式入口
// ═══════════════════════════════════════════
const homeView = document.getElementById("home-view");
const appView = document.getElementById("app");

// 当前模式：story=剧情模式；haruno=春日手信（流萤想象的普通学生生活）
let CURRENT_MODE = "story";
let _lastMode = null;   // 上次进入聊天时的模式（切换时重载历史）
let _modeGen = 0;       // 模式代际：切换时递增，飞行中的异步渲染/历史加载任务作废丢弃
const MODE_NAMES = { story: "剧情模式", haruno: "春日手信" };

function showHome() {
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    homeView.classList.add("show");
    appView.style.display = "none";     // 首页独立视图：真正隐藏聊天页（避免半透明透视）
    closeMenu();
    stopCarousel();
    goCarousel(0);   // 回到首页重置轮播位置
    startCarousel();   // 重新开始自动轮播
}
async function showChat() {
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
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
        initAssets(); // 模式切换：同步该模式资产（story/haruno 设定不同；未登录时静默失败）
        // 未提交的提交窗口作废：旧模式的 flush/hint 计时器不跨模式触发（防串写历史）
        clearTimeout(_flushTimer);
        _flushTimer = null;
        clearTimeout(_hintTimer);
        _hintTimer = null;
        messagesEl.innerHTML = "";
        _hasMore = false;
        await loadHistory();   // 先加载历史（含已保存的开场）
        // 历史为空（haruno 首次进入）：触发开场生成一次并保存为对话内容。
        // 之后进入只走历史加载，不重复开场——与剧情模式行为一致。
        if (CURRENT_MODE === "haruno" && messagesEl.children.length === 0) {
            await openModeOpening();
        }
    }
    try { if (location.hash !== "#chat") history.pushState({chat: true}, "", "#chat"); } catch (e) {}
}



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

// 轮播图功能入口：剧情模式 / 春日手信 → 各自模式的对话
function enterCarouselAction() {
    CURRENT_MODE = carouselIndex === 0 ? "story" : "haruno";
    showChat();
}

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

// 返回键支持（PC 浏览器后退 / Android WebView goBack → popstate → 回首页）
window.addEventListener("popstate", () => {
    if (location.hash !== "#chat") showHome();
});
function toggleNotice() {
    const panel = document.getElementById("notice-panel");
    const open = panel.classList.toggle("show");
    document.getElementById("notice-arrow").textContent = open ? "▴" : "▾";
}
function closeNotice() {
    document.getElementById("notice-panel").classList.remove("show");
    document.getElementById("notice-arrow").textContent = "▾";
}

// 滚动轮播（自动 + 触摸滑动）
const carouselTrack = document.getElementById("carousel-track");
const carouselDots = document.getElementById("carousel-dots");
let carouselIndex = 0;
let carouselTimer = null;
const carouselCount = carouselTrack.children.length;

for (let i = 0; i < carouselCount; i++) {
    const dot = document.createElement("span");
    if (i === 0) dot.classList.add("active");
    dot.addEventListener("click", () => goCarousel(i));
    carouselDots.appendChild(dot);
}
function goCarousel(i) {
    carouselIndex = (i + carouselCount) % carouselCount;
    // 安卓 WebView bug：transform 移动后的 img 合成层光栅化模糊。
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
document.addEventListener("DOMContentLoaded", () => {
    // 初始化轮播显隐（opacity 叠放，transform 方案废弃避开 WebView 合成 bug）
    [...carouselTrack.children].forEach((img, di) => {
        img.style.opacity = di === 0 ? "1" : "0";
        img.style.pointerEvents = di === 0 ? "auto" : "none";
        img.style.zIndex = di === 0 ? "1" : "0";
    });
    if (location.hash === "#chat") showChat();
    else if (location.hash === "#fix") openFixView();
    else showHome();
    initAuth();   // 服务器版：轮播图下登录/用户模块
});

// ═══════════════════════════════════════════
// 主动性轮询 — 流萤在合适的时候主动找开拓者说话
// ═══════════════════════════════════════════
// 轮询纪律（避免冲突）：
// - 仅在聊天页可见且空闲时检查（不等待回复、不在打字、距离上次回复 > 2 分钟）
// - 服务端门控保证频率（主动式=轮次+概率；概率式=时间静默+前端概率），不通过则零成本返回空
// - 空闲判定（概率式硬性）：无输入、无提交、无处理中（waiting/pending/输入框非空）
let _lastRenderTs = 0;          // 上次消息渲染时间戳（含主动消息）
let _rendering = false;         // 消息动画渲染中标志（防主动轮询中途插入乱序）
const _PROACTIVE_INTERVAL = 10 * 1000;   // 轮询周期 10s
const _PROACTIVE_QUIET = 2 * 60 * 1000;  // 主动式：回复渲染后 2 分钟内不检查
const _PROB_QUIET = 10 * 60 * 1000;      // 概率式：距上次渲染 10 分钟内不检查（与服务端静默阈值一致）

function _chatVisible() {
    return appView && appView.style.display !== "none";
}

// 空闲判定：无输入 / 无请求在飞 / 无思考锁 / 无渲染动画
function _idleOk() {
    if (waiting) return false;
    if (_rendering) return false;         // 主动消息渲染动画中
    if (_inflight > 0) return false;      // 发送请求在飞（等待回复）
    if (inputEl && inputEl.value.trim()) return false;
    return _chatVisible();
}

// 渲染主动消息：先锁定输入（思考 2~5s 模拟"想了想/想起什么"），期间禁止用户输入防竞态
async function _renderProactiveWithThink(data) {
    _lastRenderTs = Date.now();
    waiting = true;
    inputEl.disabled = true; sendBtn.disabled = true;
    const statusEl = document.querySelector("#header .status");
    const defaultStatus = statusEl ? statusEl.textContent : "";
    if (statusEl) statusEl.textContent = "对方正在输入...";
    const thinkMs = 2000 + Math.floor(Math.random() * 3000);
    await new Promise(r => setTimeout(r, thinkMs));
    if (statusEl) statusEl.textContent = defaultStatus;
    waiting = false;
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
            _lastRenderTs = Date.now();
            waiting = true;
            inputEl.disabled = true; sendBtn.disabled = true;
            const statusEl = document.querySelector("#header .status");
            const defaultStatus = statusEl ? statusEl.textContent : "";
            if (statusEl) statusEl.textContent = "对方正在输入...";
            const thinkMs = 2000 + Math.floor(Math.random() * 3000);
            await new Promise(r => setTimeout(r, thinkMs));
            if (statusEl) statusEl.textContent = defaultStatus;
            waiting = false;
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
let _hiddenEnabled = true;   // 后端 hidden_reply_enabled 缓存（/config 下发后更新）

function _notifyFirefly(messages) {
    // 消息渲染后提醒：FireflyJs 桥仅 App 不在前台时发状态栏通知（前台不打扰，
    // 复刻本地版 _notify_reply_if_background 语义）
    try {
        if (window.FireflyJs && window.FireflyJs.notify && Array.isArray(messages)) {
            const texts = messages.filter(m => m && m.type === "text" && m.content)
                                  .map(m => m.content);
            if (texts.length) window.FireflyJs.notify("流萤 · AI", texts.join("\n").slice(0, 200));
        }
    } catch (e) {}
}

window.__serverProactive = async function () {
    // KeepAliveService 后台触发：hidden 开关判断 + 主动式/概率式门控在服务端，
    // relay 代发由页面 relay 引擎完成（本函数跑在页面，Key 可直达）
    if (!_hiddenEnabled) return;
    await checkProactive();
};

setInterval(checkProactive, _PROACTIVE_INTERVAL);

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
        const key = (() => { try { return localStorage.getItem("firefly_api_key") || ""; } catch (e) { return ""; } })();
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
            // 用户 Key 直连代发（DeepSeek 官方端点支持浏览器 CORS）
            ds = await _serverFetch(apiBase + "/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json", "Authorization": "Bearer " + key },
                body: JSON.stringify(pending.payload),
            });
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
if (IS_SERVER) setInterval(relayTick, 1000);   // relay 引擎仅服务器模式（本地为 direct 直发）

// 消息渲染后记录时间（renderMessages 内调用）
const _origRenderMessages = renderMessages;
renderMessages = function (messages, who, data) {
    _lastRenderTs = Date.now();
    return _origRenderMessages(messages, who, data);
};

// ═══════════════════════════════════════════
// 发送消息 — 四阶段模型：输入 → 发送 → 提交 → 回复
//   输入：打字（内容只在输入框，不触发队列）
//   发送：Enter / 发送按钮 / 点表情 → 消息**立即 POST 后端**（不等 5 秒，切后台不丢）
//   提交：后端 5 秒滑动窗口合并（后端控制；/chat/hint 重置窗口、/chat/flush 提前结束）
//   回复：流萤回复渲染
// 关键：发送 ≠ 提交。后端窗口合并连续消息；输入框未发送的内容永不提交（绝不自动发送）。
// ═══════════════════════════════════════════
let _flushTimer = null;   // 提交窗口计时：输入框清空后 5 秒 → /chat/flush（提前结束后端窗口）
let _hintTimer = null;    // 打字中 hint 防抖：输入停顿 2 秒 → /chat/hint（重置后端窗口）
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
    quota_exhausted: "今日服务器托管额度已用完，可在设置中切换为自带 Key 模式",
    unknown: "出了点问题，请稍后再试",
};

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

/** 导出当前模式数据备份（zip）。
 *  local：window.location.href（PC 浏览器直接下载 / 安卓壳 DownloadListener 接管下载目录）；
 *  server：fetch → blob → a.click()（file:// 页面跨域，不能直接 window.location）。 */
async function exportData() {
    if (!IS_SERVER) {
        window.location.href = `/export-data?mode=${encodeURIComponent(CURRENT_MODE)}`;
        _toast("正在导出备份…");
        return;
    }
    try {
        const resp = await fetch(`/export-data?mode=${encodeURIComponent(CURRENT_MODE)}`);
        if (!resp.ok) { _toast("导出失败，请稍后再试"); return; }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `firefly-backup-${CURRENT_MODE}.zip`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
        _toast("已开始导出备份");
    } catch (e) { _toast("导出失败，请检查网络"); }
}
window.exportData = exportData;

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

/** 备份到账号（服务器模式专属）：导出 zip → 上传 /sync/upload。 */
async function syncToAccount() {
    if (!IS_SERVER) return;
    _toast("正在备份到账号…");
    try {
        const resp = await fetch(`/export-data?mode=${encodeURIComponent(CURRENT_MODE)}`);
        if (!resp.ok) { _toast("导出失败，无法备份"); return; }
        const blob = await resp.blob();
        const fd = new FormData();
        fd.append("file", blob, `firefly-backup-${CURRENT_MODE}.zip`);
        fd.append("mode", CURRENT_MODE);
        const up = await fetch("/sync/upload", { method: "POST", body: fd });
        const data = await up.json().catch(() => ({}));
        if (up.ok && data.ok) _toast("已备份到账号（云端保留最近 3 份）");
        else _toast("备份失败：" + (data.error || ""));
    } catch (e) { _toast("备份失败，请检查网络"); }
}
window.syncToAccount = syncToAccount;

/** 从账号恢复（服务器模式专属）：下载最新云端备份 → 导入。 */
async function restoreFromAccount() {
    if (!IS_SERVER) return;
    if (!confirm(`从账号恢复将覆盖当前「${MODE_NAMES[CURRENT_MODE] || CURRENT_MODE}」的全部数据（导入前会自动备份现有数据）。\n\n确定恢复吗？`)) return;
    _toast("正在从账号恢复…");
    try {
        const resp = await fetch(`/sync/download?mode=${encodeURIComponent(CURRENT_MODE)}`);
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            _toast(d.error || "账号还没有备份");
            return;
        }
        const blob = await resp.blob();
        const fd = new FormData();
        fd.append("file", blob, `firefly-restore-${CURRENT_MODE}.zip`);
        fd.append("mode", CURRENT_MODE);
        const up = await fetch("/import-data", { method: "POST", body: fd });
        const data = await up.json().catch(() => ({}));
        if (up.ok && data.ok) {
            _toast("恢复成功，正在重新加载…");
            setTimeout(() => location.reload(), 800);
        } else {
            _toast("恢复失败：" + (data.error || ""));
        }
    } catch (e) { _toast("恢复失败，请检查网络"); }
}
window.restoreFromAccount = restoreFromAccount;

/** 等待回复期间轮询流水线阶段（检索→分析→回复→表情包），把"对方正在输入…"换成具体阶段。
 *  仅在拿到 stage 时替换文本；请求结束由 _chatSend 的 finally 清除。 */
function _pollStage(statusEl) {
    clearInterval(_stageTimer);
    _stageTimer = setInterval(async () => {
        if (_inflight <= 0) { clearInterval(_stageTimer); _stageTimer = null; return; }
        try {
            const r = await fetch(`/chat-stage?sid=${encodeURIComponent(SESSION_ID)}&mode=${encodeURIComponent(CURRENT_MODE)}`);
            const d = await r.json();
            if (d.stage && d.label && _inflight > 0 && statusEl) statusEl.textContent = d.label;
        } catch (e) { /* 网络抖动静默，状态保持"对方正在输入" */ }
    }, 2000);
}

/** 打字中：重置后端合并窗口（流萤继续等开拓者说完）。
 * 输入框仍有内容 → 持续定时重置（前端在且输入框有残留 = 用户在打字 → 永不提交）；
 * 输入框清空/切后台 → 停止发 hint，后端窗口自然到期兜底。 */
function _sendHint() {
    fetch("/chat/hint", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
    }).catch(() => {});
    // 输入框仍有残留（用户还在打字/未清空）→ 继续定时重置窗口
    if (inputEl && inputEl.value.trim()) {
        _hintTimer = setTimeout(_sendHint, 2000);
    }
}

/** 提交窗口到期：立即结束后端合并窗口（前台加速；切后台冻结不触发，后端窗口兜底）。
 * 状态显示时机：只有提交（进入核心流水线）才显示"对方正在输入"，窗口等待期不显示。 */
function _sendFlush() {
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
 *  状态显示不在此处设置——窗口等待期不显示"正在输入"，由 _sendFlush（提交）触发。 */
async function _chatSend(msgs) {
    _inflight++;
    const statusEl = document.querySelector("#header .status");
    const defaultStatus = statusEl ? statusEl.textContent : "";
    const gen = _modeGen;   // 捕获发起时的模式代际
    // 后台保活（安卓 WebView JS Bridge）：回复流程（检索→分析→回复→调度）期间
    // 持 CPU/WiFi 锁，用户切后台/锁屏也能完成回复；引用计数归零才释放。
    // 浏览器端（PC/服务器版）无 androidWakeLock，此段安全跳过
    if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.acquire(); }
    _pollStage(statusEl);   // 启动阶段进度轮询（回复到达后 finally 清除）
    try {
        const resp = await fetch("/chat", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ messages: msgs, session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃回复（消息已写盘到原模式，不渲染）
        if (data.need_key) openSettings();
        else if (data.messages) {
            renderMessages(data.messages, "firefly", data);
            _notifyFirefly(data.messages);   // 切后台时回复完成通知（桥判断前台与否）
        }
        else if (data.reply) addTextMessage(data.reply, "firefly");
        if (data.error_code) _toast(ERROR_TIPS[data.error_code] || ERROR_TIPS.unknown);
        // data.queued：副请求，回复由主请求带回，无 UI 操作
    } catch (e) {
        if (gen === _modeGen && _inflight === 1) {
            addTextMessage("嗯…信号不太好，等会儿再试试？", "firefly");
        }
    } finally {
        if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.release(); }
        _inflight--;
        if (_inflight === 0) {
            clearTimeout(_flushTimer); _flushTimer = null;   // 请求完成：提交计时器作废
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
    if (!text || waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    inputEl.value = "";
    addTextMessage(text, "user");
    inputEl.focus();
    // 输入框清空 → 停止 hint 循环 + 重置 5 秒提交窗口（到期 flush 结束后端窗口）
    clearTimeout(_hintTimer);
    clearTimeout(_flushTimer);
    _flushTimer = setTimeout(_sendFlush, 5000);
    _chatSend([{type: "text", content: text}]);   // 统一消息对象类型，立即发送
}

// ═══════════════════════════════════════════
// 表情包面板（输入框内 😊 按钮）
// ═══════════════════════════════════════════
const stickerPanel = document.getElementById("sticker-panel");
const stickerGrid = document.getElementById("sticker-grid");
const stickerBtn = document.getElementById("sticker-btn");

stickerBtn.addEventListener("click", async () => {
    if (stickerPanel.classList.contains("show")) {
        stickerPanel.classList.remove("show");
        return;
    }
    stickerPanel.classList.add("show");
    if (!stickerGrid.dataset.loaded) {
        try {
            const resp = await fetch("/stickers?enabled=1");
            const data = await resp.json();
            const list = data.stickers || [];
            stickerGrid.innerHTML = list.map(s =>
                `<img src="${IS_SERVER ? API_BASE : ""}/assets/${escapeHtml(s.file)}" alt="${escapeHtml(s.label)}" data-label="${escapeHtml(s.label)}" data-file="${escapeHtml(s.file)}">`).join("");
            stickerGrid.dataset.loaded = "1";
            stickerGrid.querySelectorAll("img").forEach(img => {
                img.addEventListener("click", () => {
                    stickerPanel.classList.remove("show");
                    sendStickerMessage(img.dataset.label, img.dataset.file);
                });
            });
        } catch (e) { /* 静默 */ }
    }
});
// 点击聊天区关闭表情面板
messagesEl.addEventListener("click", () => stickerPanel.classList.remove("show"));
document.getElementById("sticker-panel-close").addEventListener("click", () => stickerPanel.classList.remove("show"));

/** 发送表情包：作为一条消息立即发送（与文字同一窗口合并，不碰输入框内容） */
function sendStickerMessage(label, file) {
    if (waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    if (file) addSticker(file, "user");   // 本地立即渲染表情图
    inputEl.focus();
    clearTimeout(_flushTimer);
    _flushTimer = setTimeout(_sendFlush, 5000);   // 表情入队 → 重置提交窗口
    _chatSend([{type: "sticker", label, file}]);
}

sendBtn.addEventListener("click", send);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
// 提交窗口控制（发送≠提交，窗口在后端）：
// - 输入框有内容（打字中）→ 暂停 flush + 防抖 hint（后端重置窗口，流萤等开拓者说完）
// - 输入框清空 → 重新 5 秒 flush 计时（到期结束后端窗口；切后台冻结则后端窗口兜底）
inputEl.addEventListener("input", () => {
    clearTimeout(_hintTimer);
    if (inputEl.value.trim()) {
        clearTimeout(_flushTimer);
        _flushTimer = null;   // 有未发送内容：暂停 flush，不提前结束后端窗口
        _hintTimer = setTimeout(_sendHint, 2000);   // 停顿 2 秒发 hint 重置后端窗口（持续打字则持续等待）
    } else {
        _flushTimer = setTimeout(_sendFlush, 5000);   // 清空：5 秒后提交
    }
});

// ═══════════════════════════════════════════
// 休息 / 清除 / 撤回
// ═══════════════════════════════════════════
const restOverlay = document.getElementById("rest-overlay");
document.getElementById("menu-rest-btn").addEventListener("click", async () => {
    if (!confirm("让流萤去休息吗？她会整理这段对话的记忆。")) return;
    closeMenu();
    restOverlay.style.display = "flex";
    document.getElementById("rest-text").textContent = "流萤正在整理记忆…";
    try {
        const resp = await fetch("/rest", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        restOverlay.style.display = "none";   // 无论成败都收起遮罩（失败信息已在 text 中展示）
        document.getElementById("rest-text").textContent = data.ok
            ? `流萤已休息。新增记忆 ${data.added} 条，解决 ${data.resolved} 条。下次见。`
            : "整理出了点问题：" + (data.error || "未知");
    } catch (e) {
        restOverlay.style.display = "none";
        document.getElementById("rest-text").textContent = "信号不好，等会儿再试。";
    }
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
// 状态 tab（数值状态系统已下线，接回后再渲染条）
// ═══════════════════════════════════════════
function loadStateTab() {
    const list = document.getElementById("state-list");
    if (list) {
        list.innerHTML = '<div style="color:#8a8a8a;line-height:1.6">状态系统尚未接入。<br>当前流水线：检索 → 分析 → 回复 → 表情包。</div>';
    }
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
        list.innerHTML = rows.map(r => `
        <div style="display:flex;align-items:center;gap:4px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.8em;color:#c8d0e0">
            <span style="flex-shrink:0;width:50px;color:#8a8a8a">${r.time || "?"}</span>
            <span style="flex-shrink:0;width:64px">${r.module}</span>
            <span style="flex-shrink:0;width:52px">${r.model || "?"}</span>
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
function _esc(s) {
    return String(s == null ? "" : s)
        .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

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
                <div style="margin-bottom:4px"><span style="color:#8a8a8a">${p.time || "?"}</span> 开拓者: <span style="color:#e0d5c1">${_esc(p.user_input)}</span>${p.hint ? ` <span style="color:#8a8a8a">(hint:${p.hint})</span>` : ""}</div>
                ${inner}
            </div>`;
        }).join("");
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;padding:10px">加载失败</div>';
    }
}
window.loadPipeline = loadPipeline;
// ═══════════════════════════════════════════
// 用户记忆（= 跨会话记忆 memory.md，休息时自动整理）/ 用户设定（补充设定）
// ═══════════════════════════════════════════
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

// 用户设定（补充剧情设定）
async function loadCharFiles() {
    const msg = document.getElementById("char-file-msg");
    try {
        const resp = await fetch(`/character-files?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        const byName = {};
        (data.files || []).forEach(f => { byName[f.name] = f.content; });
        const us = document.getElementById("user-setting-editor");
        if (us) us.value = byName["用户设定.md"] || "";   // ?? 为 ES2020（Chrome 80+），安卓 8.0 WebView 解析期 SyntaxError 全站失效，改用 ||（此处语义等价）
        if (msg) msg.textContent = "已加载";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}

async function saveUserFile(filename, editorId, msgEl) {
    const editor = document.getElementById(editorId);
    const msg = document.getElementById(msgEl);
    if (!editor) return;
    msg.textContent = "保存中…";
    try {
        const resp = await fetch("/character-file-update", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({filename, content: editor.value, mode: CURRENT_MODE}),
        });
        const data = await resp.json();
        msg.textContent = data.ok ? "✓ 已保存" : "失败：" + (data.error || "未知");
    } catch (e) { msg.textContent = "网络错误"; }
}

document.getElementById("user-setting-save").addEventListener("click", () => saveUserFile("用户设定.md", "user-setting-editor", "char-file-msg"));
document.getElementById("user-setting-reload").addEventListener("click", loadCharFiles);

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
    try {
        const resp = await fetch("/add-sticker", { method: "POST", body: fd });
        const data = await resp.json();
        if (data.ok) {
            msg.textContent = "已添加：" + data.label;
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
        list.innerHTML = stickers.map(s => `
        <div class="sticker-row" data-id="${escapeHtml(s.id)}">
            <div class="stk-head">
                <img class="stk-thumb" src="${IS_SERVER ? API_BASE : ""}/assets/${escapeHtml(s.file)}" loading="lazy" onerror="this.style.opacity=0.2">
                <button class="stk-toggle ${s.enabled ? "on" : ""}" data-on="${s.enabled ? "1" : ""}" ${(s.editable || s.is_default) ? "" : "disabled"}>${s.enabled ? "启用中" : "已停用"}</button>
            </div>
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
        </div>`).join("");
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

// ═══════════════════════════════════════════
// 历史加载
// ═══════════════════════════════════════════
let _hasMore = false, _loading = false;
let _lastWho = null;
function renderHistoryMessage(m, prepend=false) {
    const ts = m.time ? m.time.slice(11,16) : null;
    // 发送方变化时插入时间分割线
    if (m.who !== _lastWho && ts) {
        addTimeDivider(ts);
        _lastWho = m.who;
    }
    if (m.type==="sticker") addSticker(m.path, m.who, prepend, m.seq);
    else if (m.type==="narration") addNarration(m.text, m.style, prepend, m.seq);
    else addTextMessage(m.content, m.who, prepend, m.seq);
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
        if (!data.messages || data.messages.length===0) { _hasMore=false; return; }
        if (!beforeSeq) { data.messages.forEach(m=>renderHistoryMessage(m,false)); messagesEl.scrollTop=messagesEl.scrollHeight; undoBtn.disabled=false; }
        else { const ph=messagesEl.scrollHeight, ps=messagesEl.scrollTop; data.messages.slice().reverse().forEach(m=>renderHistoryMessage(m,true)); messagesEl.scrollTop=ps+(messagesEl.scrollHeight-ph); }
        _hasMore = !!data.has_more;
    } catch(e) {} finally { _loading=false; }
}
messagesEl.addEventListener("scroll", () => {
    if (messagesEl.scrollTop===0 && _hasMore && !_loading) {
        const first = messagesEl.firstChild;
        const seq = first ? parseInt(first.dataset.seq) : null;
        if (seq) loadHistory(seq);
    }
});

// ═══════════════════════════════════════════
// 起床检查
// ═══════════════════════════════════════════
async function checkWake() {
    try {
        const resp = await fetch(`/wake-status?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (data.interrupted) {
            document.getElementById("wake-overlay").style.display = "flex";
            document.getElementById("wake-text").textContent = "流萤正在起床，记忆还在整理中…";
        }
    } catch(e) {}
}

// ═══════════════════════════════════════════
// 运行模式切换（0.8.0）：仅安卓壳注入 FireflyMode 桥时显示。
// 切换 = 壳保存 SharedPreferences → 重启应用 → 按新模式加载数据源。
// PC 浏览器（本地）/ 服务器网页：无桥 → 该行隐藏。
// ═══════════════════════════════════════════
(function initModeSwitch() {
    const row = document.getElementById("mode-switch-row");
    if (!row || !window.FireflyMode) return;
    row.style.display = "";
    const sel = document.getElementById("mode-select");
    let cur = "local";
    try { cur = window.FireflyMode.getMode() || "local"; } catch (e) {}
    if (cur !== "local" && cur !== "server") cur = "local";
    sel.value = cur;
    sel.addEventListener("change", () => {
        const next = sel.value;
        if (next === cur) return;
        const desc = next === "server"
            ? "服务器模式：登录账号后使用，数据存服务器你的账号目录（可同步 / 可托管额度）。"
            : "本地模式：数据与 Key 全部在本机，无需登录、无网络要求（调 API 除外）。";
        if (!confirm("切换运行模式需要重新打开应用。\n\n" + desc + "\n\n确定切换吗？")) {
            sel.value = cur;
            return;
        }
        try { window.FireflyMode.setMode(next); } catch (e) { /* 壳负责重启 */ }
    });
})();

// ═══════════════════════════════════════════
// 使用引导（纯代码：高亮框 + 文字气泡 + CSS 呼吸边，不用任何图片）
// 基础引导 4 步；结束后可点「深入了解」进入详细引导（设置/菜单/纠错助手）。
// ═══════════════════════════════════════════
const GUIDE_KEY = "firefly_guide_v1_done";
const DEEP_GUIDE_KEY = "firefly_deep_guide_v1_done";
const GUIDE_STEPS = [
    { el: "#home-carousel", title: "从这里进入对话",
      text: "请实际操作：点「剧情模式」或「春日手信」卡片，进入和流萤的聊天页。\n操作成功会自动进入下一步；如果没反应，点「下一步」。",
      setup: () => { showHome(); },
      done: () => !document.getElementById("home-view").classList.contains("show") },
    { el: "#home-settings-btn", title: "先填 API Key",
      text: "请点右上角 ⚙ 打开设置，把 sk- 开头的 Key 粘进 API Key 输入框。\n没有 Key 之前，聊天只会提示你去设置。",
      setup: () => { showHome(); },
      done: () => document.getElementById("settings-panel").classList.contains("show") },
    { el: "#fix-module", title: "设定不对？直接告诉她",
      text: "请点这张卡上的「指出问题 →」进入设定纠错助手。\nAI 会先和你确认问题，再列出修改清单；你点「应用」才生效，随时可撤销。",
      setup: () => { closeSettings(); showHome(); },
      done: () => document.getElementById("fix-view").classList.contains("show") },
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
      text: "请点右上角 ⚙ 打开设置页（不要点“下一步”）。\n\n设置页有 5 组卡片：账号与连接 / 主动消息 / 模型与速度 / 外观 / 数据与系统；除 API Key 外，改动会自动保存。",
      setup: () => { showHome(); },
      done: () => document.getElementById("settings-panel").classList.contains("show") },
    { el: "#key-input", title: "② 试填 API Key",
      text: "请点 API Key 输入框，粘贴 sk- 开头的 Key；留空=保留原来的 Key。\n\n填完点「保存 Key 与连接设置」。服务器版还可以在「高级：接口地址」里切换 DeepSeek 官方 / OpenCode Go。",
      setup: () => { showHome(); openSettings(); },
      done: () => document.activeElement && document.activeElement.id === "key-input" },
    { el: '#settings-panel .set-head[data-group="proactive"]', title: "③ 点开「主动消息」",
      text: "请点「💬 主动消息」标题展开它。\n\n里面有三种主动行为：聊天里按轮次主动找你、你空闲时想起你（10 分钟一次）、后台通知（仅安卓）。用 较少/偶尔/经常 调频率，关掉开关就安静。",
      setup: () => { openSettings(); },
      done: () => _guideGroupOpen("proactive") },
    { el: '#settings-panel .set-head[data-group="model"]', title: "④ 点开「模型与速度」",
      text: "请点「🧠 模型与速度」展开。\n\n日常用「快速」，要更聪明选「更强」；展开「自定义」还能分别调检索/分析/回复/组织四个阶段和思考档位。",
      setup: () => { openSettings(); },
      done: () => _guideGroupOpen("model") },
    { el: '#settings-panel .set-head[data-group="system"]', title: "⑤ 点开「数据与系统」",
      text: "请点「🛠 数据与系统」展开。\n\n更新、导出/导入 zip 备份都在这里；服务器版还能备份到账号。导入会覆盖当前模式数据，但导入前会自动备份。",
      setup: () => { openSettings(); },
      done: () => _guideGroupOpen("system") },
    { el: "#menu-btn", title: "⑥ 到聊天页打开菜单",
      text: "已经帮你切到聊天页：请点右上角 ☰ 打开菜单。\n\n菜单里是五个页签：设定文件 / 表情包 / 流萤状态 / 请求记录 / 流程日志。",
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
      text: "请点「+ 添加表情包」展开表单（不用真的上传）。\n\n流程是：选图 → 选分类（可爱/帅气）→ 写一句含义描述 → 保存。描述写得越清楚，AI 选图越准。",
      setup: () => { openMenu(); try { document.querySelector('.menu-tab[data-tab="sticker"]')?.click(); } catch (e) {} },
      done: () => { const f = document.getElementById("sticker-add-form"); return !!(f && f.style.display !== "none" && f.style.display !== ""); } },
    { el: "#fix-module .am-btn", title: "⑩ 进入设定纠错",
      text: "已经回到首页：请点「指出问题 →」进入设定纠错助手。\n\n进去后先选模式：剧情模式 或 春日手信，两个模式的设定和历史完全独立。",
      setup: () => { closeMenu(); showHome(); },
      done: () => document.getElementById("fix-view").classList.contains("show") },
    { el: "#fix-chathist", title: "⑪ 展开最近聊天记录",
      text: "请点「📜 最近聊天记录」展开它。\n\n这里显示当前模式的最近 20 条聊天，描述问题时可以直接对照她具体说错了哪句，不用切页面。",
      setup: () => { closeMenu(); if (!document.getElementById("fix-view").classList.contains("show")) openFixView(); },
      done: () => { const d = document.getElementById("fix-chathist"); return !!(d && d.open); } },
    { el: "#fix-input", title: "⑫ 点输入框，试着描述问题",
      text: "请点底部输入框，试着输入一句“她哪里说得不对”（先不用发送，或只发一句真实问题）。\n\n流程是：AI 多轮确认 → 点「开始修改」→ 看修改清单 → 点「应用修改」才生效；顶部状态点会显示：状态正常/对齐中/已对齐/方案待确认。",
      setup: () => { closeMenu(); if (!document.getElementById("fix-view").classList.contains("show")) openFixView(); },
      done: () => document.activeElement && document.activeElement.id === "fix-input" },
    { el: "#home-feedback-btn", title: "⑬ 反馈页可随时重看",
      text: "最后请点左上角「✉ 反馈」。\n\n以后想复习：反馈页点「查看详细使用教程」即可重新开始这套实际操作教程；有问题可在 GitHub / QQ 群 / 邮箱反馈。",
      setup: () => { showHome(); },
      done: () => document.getElementById("feedback-panel").classList.contains("show") },
];

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

// ═══════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════
checkWake();
checkKey().then(() => { loadHistory(); });
initAssets();   // 服务器模式：已有 token 时立即资产本地化（未登录静默失败，登录后 initAuth 会再触发）
loadFixStatus();   // 设定纠错助手：恢复多轮对齐/待确认方案（本地与服务器模式都可用）
