// 视图切换：首页 / 聊天 / 轮播 / 主题 / 界面缩放 / 粒子与降级开关
import { S, messagesEl } from "./state.js";
import { initAuth, showAuthModule, IS_SERVER } from "./api.js";
import { showToast } from "./util.js";
import { closeMenu, openSettings } from "./panels.js";
import { renderMessages, scrollToBottom } from "./chat_render.js";
import { resetBatchWindow, _clearQuote } from "./chat.js";
import { loadHistory } from "./chat_history.js";
import { openFixView } from "./fix.js";
import { initAssets } from "./relay.js";
import { uiSelectEnhance } from "./ui_select.js";

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

// 模式（仅两类）：story=故事/剧情模式；haruno=剧本模式（有旁白与环境描写）
// 角色预设化：模式清单来自后端注册表（GET /modes），前端不写死包列表
export let CURRENT_MODE = "story";
let _lastMode = null;   // 上次进入聊天时的模式（切换时重载历史）
export let _modeGen = 0;       // 模式代际：切换时递增，飞行中的异步渲染/历史加载任务作废丢弃
export const MODE_NAMES = { story: "剧情模式", haruno: "剧本模式" };   // 默认两内置；loadModes 后按注册表刷新
export const PRESET_MODES = [];   // /modes 清单（id/name/presentation/desc/tagline/cover/avatar/has_opening）
// 归档包清单（3.5）：归档 = 不进 PRESET_MODES（不在 /modes 的 modes 里），但数据与清单条目都在。
// 单独存一份是为了让首页能画出"已归档"区块——否则归档就等于把包弄丢：看不见、恢复不了。
export const ARCHIVED_PACKS = [];
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

export function modeName(id) { return MODE_NAMES[id] || id; }
export function currentPreset() {
    return PRESET_MODES.find(m => m.id === CURRENT_MODE) || PRESET_MODES[0] || null;
}

/** 当前角色卡的**角色名**（角色卡化铁律：框架任何地方都不许写角色名字面量）。
 *  取不到返回空串 —— 调用方自行决定降级（名字行不渲染 / 文案省略称呼），
 *  **不要**回落到某个具体角色名，否则自建卡会显示别人的名字。 */
export function charName() {
    const p = currentPreset();
    return ((p && (p.char_name || p.name)) || "").toString().trim();
}

/** 当前角色卡的**用户称呼**（同上：取不到返回空串，不回落字面量）。 */
export function userName() {
    const p = currentPreset();
    return ((p && p.user_name) || "").toString().trim();
}
export function setCurrentMode(mode) {   // ESM 导出只读绑定，外部经此切换
    _applyMode(mode);
}

// 拉取预设包清单并渲染模式卡片（轮播 + PC 大卡）；失败兜底两内置包（与后端兜底一致）
export async function loadModes() {
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
export function applyTextTemplates() {
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
export function applyModeBranding() {
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
