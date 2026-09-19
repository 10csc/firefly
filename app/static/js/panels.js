// 面板族：菜单抽屉 / 各面板开关壳 / 头像选择 / 状态 / 收藏 / 请求记录 / 流程日志 / 用户记忆 / 设定文件 / 手账 / 表情包管理（阶段 2.5 拆分后 = 面板外壳）
import { _esc, escapeHtml, showToast, stickerSrc, idbSaveMedia } from "./util.js";
import { IS_SERVER, API_BASE } from "./api.js";
import { CURRENT_MODE } from "./views.js";
import { loadConfig } from "./settings.js";
import { S } from "./state.js";

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
    // （2026-09-18：loadCharFiles 已随「用户设定」编辑器一起去掉——它属角色卡管理域）
    loadJournal(); loadUserMemory(); loadArchive();
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
    try { loadSnapshots(); } catch (e) {}   // 快照列表（chat.js 同作用域函数）
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
