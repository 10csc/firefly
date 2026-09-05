// 视图切换：首页 / 聊天 / 轮播 / 主题 / 界面缩放 / 粒子与降级开关
import { S, messagesEl } from "./state.js";
import { initAuth, showAuthModule, IS_SERVER } from "./api.js";
import { showToast } from "./util.js";
import { closeMenu, openSettings } from "./panels.js";
import { renderMessages, scrollToBottom } from "./chat_render.js";
import { resetBatchWindow } from "./chat.js";
import { loadHistory } from "./chat_history.js";
import { openFixView } from "./fix.js";
import { initAssets } from "./relay.js";

// ═══ 入口收敛（A5 底部导航已于 0.8.1 移除）：聊天页全屏只留输入栏，
// 首页轮播图进入聊天、返回键/首页按钮回首页、设置走首页右上角 ⚙ / 汉堡菜单 ═══
export function activateTab(tab) {
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
export const homeView = document.getElementById("home-view");
export const appView = document.getElementById("app");

// 当前模式：story=剧情模式；haruno=春日手信（流萤想象的普通学生生活）
// 角色预设化：模式清单来自后端注册表（GET /modes），前端不写死包列表
export let CURRENT_MODE = "story";
let _lastMode = null;   // 上次进入聊天时的模式（切换时重载历史）
export let _modeGen = 0;       // 模式代际：切换时递增，飞行中的异步渲染/历史加载任务作废丢弃
export const MODE_NAMES = { story: "剧情模式", haruno: "春日手信" };   // 默认两内置；loadModes 后按注册表刷新
export const PRESET_MODES = [];   // /modes 清单（id/name/presentation/desc/tagline/cover/avatar/has_opening）
let _modesLoaded = false;

export function modeName(id) { return MODE_NAMES[id] || id; }
export function currentPreset() {
    return PRESET_MODES.find(m => m.id === CURRENT_MODE) || PRESET_MODES[0] || null;
}

// 拉取预设包清单并渲染模式卡片（轮播 + PC 大卡）；失败兜底两内置包（与后端兜底一致）
export async function loadModes() {
    if (_modesLoaded) return;
    _modesLoaded = true;
    try {
        const resp = await fetch("/modes");
        const data = await resp.json();
        const list = Array.isArray(data.modes) ? data.modes : [];
        if (list.length) {
            PRESET_MODES.splice(0, PRESET_MODES.length, ...list);
            for (const m of list) MODE_NAMES[m.id] = m.name || m.id;
        }
    } catch (e) {}
    if (!PRESET_MODES.length) {
        PRESET_MODES.push(
            {id: "story", name: "剧情模式", presentation: "sticker", desc: "", tagline: "",
             cover: "/assets/character/story/assets/cover.png", avatar: "/assets/character/story/assets/avatar.png", has_opening: false, char_name: "流萤"},
            {id: "haruno", name: "春日手信", presentation: "narration", desc: "", tagline: "",
             cover: "/assets/character/haruno/assets/cover.png", avatar: "/assets/character/haruno/assets/avatar.png", has_opening: true, char_name: "流萤"});
    }
    renderModeCards();
    applyModeBranding();
    try { window.renderFixModes && window.renderFixModes(); } catch (e) {}   // 纠错页模式按钮随注册表刷新
}

// 包资产编辑后强制重拉注册表（panels.js 角色包管理调用）
window.__modesReload = async () => {
    _modesLoaded = false;
    await loadModes();
};

// 按 PRESET_MODES 渲染轮播图与 PC 大卡（卡片点击/滑动进入对应模式）
function renderModeCards() {
    carouselTrack.innerHTML = "";
    carouselDots.innerHTML = "";
    PRESET_MODES.forEach((m, i) => {
        const img = document.createElement("img");
        if (m.cover) img.src = m.cover;
        img.alt = m.name || m.id;
        carouselTrack.appendChild(img);
        const dot = document.createElement("span");
        if (i === 0) dot.classList.add("active");
        dot.addEventListener("click", () => goCarousel(i));
        carouselDots.appendChild(dot);
    });
    carouselCount = PRESET_MODES.length;
    if (carouselCount) goCarousel(0);
    const hm = document.getElementById("home-modes");
    if (hm) {
        hm.innerHTML = "";
        for (const m of PRESET_MODES) {
            const btn = document.createElement("button");
            btn.className = "hm-card";
            btn.type = "button";
            btn.onclick = () => enterMode(m.id);
            const img = document.createElement("img");
            if (m.cover) img.src = m.cover;
            img.alt = m.name || m.id;
            const n1 = document.createElement("span"); n1.className = "hm-name"; n1.textContent = m.name || m.id;
            const n2 = document.createElement("span"); n2.className = "hm-desc"; n2.textContent = m.desc || "";
            btn.append(img, n1, n2);
            hm.appendChild(btn);
        }
    }
}

// 聊天页/侧边栏的角色标识（头像/名字/签名）按当前包刷新
export function applyModeBranding() {
    const p = currentPreset() || {};
    const cname = p.char_name || "";
    for (const id of ["chat-avatar", "pcs-avatar"]) {
        const img = document.getElementById(id);
        if (img && p.avatar) img.src = p.avatar;
    }
    const pcsName = document.getElementById("pcs-name");
    if (pcsName) pcsName.textContent = cname;
    const chatName = document.getElementById("chat-char-name");
    if (chatName) chatName.textContent = cname;
    for (const id of ["pcs-status", "chat-status"]) {
        const el = document.getElementById(id);
        if (el) el.textContent = p.tagline || "";
    }
}

export function showHome() {
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
export async function showChat() {
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
    CURRENT_MODE = (m && m.id) || "story";
    showChat();
}

// PC 桌面模式大卡入口（home-modes）：与轮播图入口同语义，直接进入指定模式
function enterMode(mode) {
    if (!PRESET_MODES.some(m => m.id === mode)) mode = (PRESET_MODES[0] && PRESET_MODES[0].id) || "story";
    CURRENT_MODE = mode;
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
});
