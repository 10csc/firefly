// 视图切换：首页 / 聊天 / 轮播 / 主题 / 界面缩放 / 粒子与降级开关
import { S, messagesEl } from "./state.js";
import { initAuth } from "./api.js";
import { closeMenu } from "./panels.js";
import { loadHistory, renderMessages, scrollToBottom } from "./chat.js";
import { openFixView } from "./fix.js";
import { initAssets } from "./relay.js";

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
export let CURRENT_MODE = "story";
let _lastMode = null;   // 上次进入聊天时的模式（切换时重载历史）
export let _modeGen = 0;       // 模式代际：切换时递增，飞行中的异步渲染/历史加载任务作废丢弃
export const MODE_NAMES = { story: "剧情模式", haruno: "春日手信" };

export function showHome() {
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    homeView.classList.add("show");
    appView.style.display = "none";     // 首页独立视图：真正隐藏聊天页（避免半透明透视）
    closeMenu();
    stopCarousel();
    goCarousel(0);   // 回到首页重置轮播位置
    startCarousel();   // 重新开始自动轮播
}
window.showHome = showHome;   // ESM 拆分后供 pc_nav.js（classic script）与内联 onclick 使用
export async function showChat() {
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
        initAssets(); // 模式切换：同步该模式资产（story/haruno 设定不同；未登录时静默失败）
        // 未提交的提交窗口作废：旧模式的 flush/hint 计时器不跨模式触发（防串写历史）
        clearTimeout(S._flushTimer);
        S._flushTimer = null;
        clearTimeout(S._hintTimer);
        S._hintTimer = null;
        messagesEl.innerHTML = "";
        S._hasMore = false;
        await loadHistory();   // 先加载历史（含已保存的开场）
        // 历史为空（haruno 首次进入）：触发开场生成一次并保存为对话内容。
        // 之后进入只走历史加载，不重复开场——与剧情模式行为一致。
        if (CURRENT_MODE === "haruno" && messagesEl.children.length === 0) {
            await openModeOpening();
        }
    }
    try { if (location.hash !== "#chat") history.pushState({chat: true}, "", "#chat"); } catch (e) {}
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

// 轮播图功能入口：剧情模式 / 春日手信 → 各自模式的对话
function enterCarouselAction() {
    CURRENT_MODE = carouselIndex === 0 ? "story" : "haruno";
    showChat();
}

// PC 桌面模式大卡入口（home-modes）：与轮播图入口同语义，直接进入指定模式
function enterMode(mode) {
    if (mode !== "story" && mode !== "haruno") mode = "story";
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
    // 桌面双栏（≥1100px）：默认直接进聊天，首页降级为侧栏「首页」视图
    else if (window.matchMedia && matchMedia("(min-width:1100px)").matches) showChat();
    else showHome();
    initAuth();   // 服务器版：轮播图下登录/用户模块
});
