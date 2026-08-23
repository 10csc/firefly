// 面板族：菜单抽屉 / 设置 / 反馈 / 检查更新 / 配置管理 / 收藏 / 日志 / 表情包管理
import { S } from "./state.js";
import { _esc, escapeHtml, showToast, stickerSrc, idbSaveMedia, getLocalApiKey } from "./util.js";
import { IS_SERVER, applyApiSource } from "./api.js";
import { CURRENT_MODE } from "./views.js";

// 开拓者头像
export const TB_AVATARS = { 穹: "开拓者_穹.png", 星: "开拓者_星.png" };
export let tbChoice = localStorage.getItem("tb_avatar") || "穹";

export function openAvatarPicker() {
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
export function openMenu() {
    menuDrawer.classList.add("open");
    menuOverlay.classList.add("show");
    // 默认 tab 是设定文件（DOM active），无点击事件，需主动加载
    loadCharFiles(); loadJournal(); loadUserMemory();
}
export function closeMenu() {
    menuDrawer.classList.remove("open");
    menuOverlay.classList.remove("show");
}
window.closeMenu = closeMenu;

// ═══════════════════════════════════════════
// 设置面板（首页 ⚙ 打开，API 配置独立于此）
// ═══════════════════════════════════════════
const settingsPanel = document.getElementById("settings-panel");
export function openSettings() {
    settingsPanel.classList.add("show");
    loadConfig();
    try { window.loadBackups && window.loadBackups(); } catch (e) {}   // 本地备份列表（chat.js 注册）
    try { window.btActivate && window.btActivate("mine"); } catch (e) {}
}
export function closeSettings() { settingsPanel.classList.remove("show"); }
window.openSettings = openSettings;
window.closeSettings = closeSettings;

// 反馈面板（首页 ✉ 打开）
const feedbackPanel = document.getElementById("feedback-panel");
function openFeedback() { feedbackPanel.classList.add("show"); }
export function closeFeedback() { feedbackPanel.classList.remove("show"); }
window.openFeedback = openFeedback;
window.closeFeedback = closeFeedback;

// ═══════════════════════════════════════════
// 检查更新（GitHub 优先，失败自动降级 Gitee——国内网络 Gitee 更稳）
// ═══════════════════════════════════════════
const CURRENT_VERSION = "0.8.1";   // 与 android versionName / 安装器 AppVersion 保持一致
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
        if (btn.dataset.tab === "fav") loadFavorites();
        if (btn.dataset.tab === "log") loadRequestLog();
        if (btn.dataset.tab === "pipeline") loadPipeline();
    });
});

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
}

function _renderModelSuggest() {
    const dl = _$("model-suggest");
    const p = _activeProviderObject();
    if (!dl) return;
    const models = (p && p.models && p.models.length) ? p.models
        : ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"];
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
    box.innerHTML = isDp
        ? `① 浏览器打开 <b>platform.deepseek.com</b>，注册并登录<br>② 左侧「API Keys」→ 创建，复制 <b>sk-</b> 开头的 Key<br>③ 粘贴到「${p.name}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`
        : `① 打开供应商控制台（<b>${p.base_url.replace(/\/+$/, "")}</b> 所在站点主页）创建 API Key<br>② 粘贴到「${p.name}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`;
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
    S._hiddenEnabled = payload.hidden_reply_enabled !== false;
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
        if (el.a) el.a.value = data.analyzer_model || "deepseek-v4-flash-vision-exp";
        if (el.r) el.r.value = data.retriever_model || "deepseek-v4-flash-vision-exp";
        if (el.o) el.o.value = data.organizer_model || "deepseek-v4-flash-vision-exp";
        if (el.p) el.p.value = data.polisher_model || "deepseek-v4-flash-vision-exp";
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

export async function checkKey() {
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

// ═══════════════════════════════════════════
// 状态 tab（数值状态系统已下线，接回后再渲染条）
// ═══════════════════════════════════════════
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
            body: JSON.stringify({mode: CURRENT_MODE}),
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
function loadStateTab() {
    const list = document.getElementById("state-list");
    if (list) {
        list.innerHTML = '<div style="color:#8a8a8a;line-height:1.6">状态系统尚未接入。<br>当前流水线：检索 → 分析 → 回复 → 表情包。</div>';
    }
}

// ═══════════════════════════════════════════
// 请求记录
// ═══════════════════════════════════════════
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
            const who = f.who === "user" ? "我" : "流萤";
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
// 用户记忆（= memory.md，休息时自动整理的过往摘要）/ 用户设定（补充设定）
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
        </div>`;
        }));
        list.innerHTML = rows.join("");
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
// 运行模式切换（A7c 已删除）：本地优先 + 后端失败自动回落服务器——无手动切换入口。
// 保留此注释防止旧代码/测试引用复活。
// ═══════════════════════════════════════════
