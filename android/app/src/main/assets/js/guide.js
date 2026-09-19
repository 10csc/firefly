// 使用引导（基础 + 深度）
import { escapeHtml } from "./util.js";
import { closeFeedback, closeMenu, closeSettings, openMenu, openSettings } from "./panels.js";
import { openFixView } from "./fix.js";
import { showChat, showHome } from "./views.js";

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
