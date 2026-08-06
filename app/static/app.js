// 流萤聊天 App — 前端逻辑

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const SESSION_ID = "firefly-" + Date.now();
let waiting = false;

// 输入检测器：5秒内无新输入则提交
let _pending = [];
let _sendTimer = null;

// 开拓者头像
const TB_AVATARS = { 穹: "/开拓者_穹.png", 星: "/开拓者_星.png" };
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
// 检查更新（GitHub Releases 对比当前版本）
// ═══════════════════════════════════════════
const CURRENT_VERSION = "0.7.0";   // 与 android versionName / 安装器 AppVersion 保持一致
function compareVersions(a, b) {
    const pa = String(a).split(".").map(n => parseInt(n) || 0);
    const pb = String(b).split(".").map(n => parseInt(n) || 0);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const d = (pa[i] || 0) - (pb[i] || 0);
        if (d !== 0) return d;
    }
    return 0;
}
async function checkUpdate() {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "检查中…";
    try {
        const resp = await fetch("https://api.github.com/repos/10csc/firefly/releases/latest", {cache: "no-store"});
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const data = await resp.json();
        const latest = String(data.tag_name || "").replace(/^v/i, "");
        if (!latest) throw new Error("no tag");
        if (compareVersions(latest, CURRENT_VERSION) > 0) {
            msg.innerHTML = `发现新版本 <b style="color:var(--fg-accent)">${latest}</b>（当前 ${CURRENT_VERSION}） → <a href="${data.html_url || "https://github.com/10csc/firefly/releases"}" target="_blank" rel="noopener" style="color:var(--fg-bright)">去下载</a>`;
        } else {
            msg.textContent = `已是最新版本 ${CURRENT_VERSION} ✓`;
        }
    } catch (e) {
        msg.textContent = "检查失败（网络或仓库不可达）";
    }
}
document.getElementById("check-update-btn").addEventListener("click", checkUpdate);

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
        if (btn.dataset.tab === "bubble") renderBubbleGrid();
        if (btn.dataset.tab === "state") loadStateTab();
        if (btn.dataset.tab === "log") loadRequestLog();
        if (btn.dataset.tab === "pipeline") loadPipeline();
    });
});

// ═══════════════════════════════════════════
// 气泡选择（流萤 + 用户各自可选）— 纯 CSS 主题，class 挂在 #messages 容器上
// ═══════════════════════════════════════════
const BUBBLES = [
    { key: "bubble_culture", name: "星体培养皿", cls: "fb-culture" },
    { key: "bubble_rabbit",  name: "逐兔之夏",   cls: "fb-rabbit" },
    { key: "bubble_trotter", name: "补天司命",   cls: "fb-trotter" },
    { key: "bubble_tavern",  name: "孤独的疗愈", cls: "fb-tavern" },
    { key: "bubble_cinema",  name: "大娱乐家",   cls: "fb-cinema" },
    { key: "bubble_warmth",  name: "何枝可依",   cls: "fb-warmth" },
];
let _fireflyBubble = "none";
let _userBubble = "none";

function _applyBubbleCls(key, prefix) {
    const b = BUBBLES.find(x => x.key === key);
    BUBBLES.forEach(x => messagesEl.classList.remove(x.cls));
    if (b) messagesEl.classList.add(b.cls);
    return b ? key : "none";
}

function applyFireflyBubble(key) {
    _fireflyBubble = _applyBubbleCls(key);
    try { localStorage.setItem("firefly-bubble2", _fireflyBubble); } catch(e) {}
}

function applyUserBubble(key) {
    // 用户侧与流萤侧共用主题色（镜像圆角由 CSS 处理），仅记录选择状态
    _userBubble = BUBBLES.some(x => x.key === key) ? key : "none";
    try { localStorage.setItem("user-bubble", _userBubble); } catch(e) {}
}

function renderBubbleGrid() {
    const grid = document.getElementById("bubble-grid");
    if (!grid) return;
    grid.innerHTML = "";
    // 流萤气泡
    const ff = document.createElement("div");
    ff.className = "bubble-section";
    ff.innerHTML = '<div class="bubble-label">流萤的聊天气泡</div>' +
        `<div class="bubble-card ${_fireflyBubble === 'none' ? 'selected' : ''}" onclick="pickFireflyBubble('none')">
            <div class="bubble-none">默认</div><span>无气泡</span></div>` +
        BUBBLES.map(b => `
        <div class="bubble-card ${_fireflyBubble === b.key ? 'selected' : ''}" onclick="pickFireflyBubble('${b.key}')">
            <div class="bubble-demo ${b.cls.replace('fb-', 'demo-')}">${b.name}</div>
            <span>${b.name}</span>
        </div>`).join("");
    grid.appendChild(ff);
    // 用户气泡
    const us = document.createElement("div");
    us.className = "bubble-section";
    us.innerHTML = '<div class="bubble-label">我的聊天气泡</div>' +
        `<div class="bubble-card ${_userBubble === 'none' ? 'selected' : ''}" onclick="pickUserBubble('none')">
            <div class="bubble-none">默认</div><span>无气泡</span></div>` +
        BUBBLES.map(b => `
        <div class="bubble-card ${_userBubble === b.key ? 'selected' : ''}" onclick="pickUserBubble('${b.key}')">
            <div class="bubble-demo ${b.cls.replace('fb-', 'demo-')}">${b.name}</div>
            <span>${b.name}</span>
        </div>`).join("");
    grid.appendChild(us);
}

function pickFireflyBubble(key) { applyFireflyBubble(key); renderBubbleGrid(); }
function pickUserBubble(key) { applyUserBubble(key); renderBubbleGrid(); }
window.pickFireflyBubble = pickFireflyBubble;
window.pickUserBubble = pickUserBubble;

// 加载保存的气泡
document.addEventListener("DOMContentLoaded", () => {
    let fb = null, ub = null;
    try { fb = localStorage.getItem("firefly-bubble2"); ub = localStorage.getItem("user-bubble"); } catch(e) {}
    applyFireflyBubble(fb || "none");   // 默认无气泡（旧 key 弃用，避免历史残留）
    applyUserBubble(ub || "none");
});

// ═══════════════════════════════════════════
// 配置管理
// ═══════════════════════════════════════════
async function loadConfig() {
    const ids = {
        a: "analyzer-model-select", r: "retriever-model-select",
        o: "organizer-model-select",
        p: "polisher-model-select",
        re: "retriever-effort-select", ae: "analyzer-effort-select",
        pe: "polisher-effort-select", oe: "organizer-effort-select",
        rt: "retriever-temp-slider", rtv: "retriever-temp-value",
        k: "key-input", m: "config-msg",
    };
    try {
        const resp = await fetch("/config");
        const data = await resp.json();
        const el = {};
        for (const [k, id] of Object.entries(ids)) el[k] = document.getElementById(id);

        if (el.a) el.a.value = data.analyzer_model || "deepseek-v4-flash";
        if (el.r) el.r.value = data.retriever_model || "deepseek-v4-flash";
        if (el.o) el.o.value = data.organizer_model || "deepseek-v4-flash";
        if (el.p) el.p.value = data.polisher_model || "deepseek-v4-flash";
        if (el.re) el.re.value = data.retriever_effort || "none";
        if (el.ae) el.ae.value = data.analyzer_effort || "high";
        if (el.pe) el.pe.value = data.polisher_effort || "high";
        if (el.oe) el.oe.value = data.organizer_effort || "none";
        if (data.retriever_temperature != null && el.rt) {
            el.rt.value = data.retriever_temperature;
            if (el.rtv) el.rtv.textContent = Number(data.retriever_temperature).toFixed(1);
        }
        if (el.m) {
            el.m.textContent = data.has_key
                ? "当前 Key：" + (data.key_prefix || "已设置")
                : "尚未设置 API Key";
        }
        if (el.k) { el.k.placeholder = data.has_key ? "已设置，留空则保留原 Key" : "sk-..."; el.k.value = ""; }
        return data;
    } catch (e) { return {has_key: false}; }
}

const retrieverTempSlider = document.getElementById("retriever-temp-slider");
const retrieverTempVal = document.getElementById("retriever-temp-value");
if (retrieverTempSlider) {
    retrieverTempSlider.addEventListener("input", () => {
        if (retrieverTempVal) retrieverTempVal.textContent = Number(retrieverTempSlider.value).toFixed(1);
    });
}

async function checkKey() {
    // 不自动弹出配置页：默认进入聊天页，无 key 时发消息会引导（_doSend 内处理）
    try {
        await loadConfig();
    } catch (e) { /* 服务未就绪，静默 */ }
}

document.getElementById("key-save").addEventListener("click", async () => {
    const k = document.getElementById("key-input").value.trim();
    const am = document.getElementById("analyzer-model-select").value;
    const rm = document.getElementById("retriever-model-select").value;
    const om = document.getElementById("organizer-model-select").value;
    const pm = document.getElementById("polisher-model-select").value;
    const re = document.getElementById("retriever-effort-select").value;
    const ae = document.getElementById("analyzer-effort-select").value;
    const pe = document.getElementById("polisher-effort-select").value;
    const oe = document.getElementById("organizer-effort-select").value;
    const rtemp = parseFloat(retrieverTempSlider.value) || 0.0;
    const msg = document.getElementById("config-msg");
    const payload = {
        analyzer_model: am, retriever_model: rm, organizer_model: om, polisher_model: pm,
        retriever_effort: re, analyzer_effort: ae, polisher_effort: pe, organizer_effort: oe,
        retriever_temperature: rtemp,
    };
    if (k) payload.api_key = k;
    msg.textContent = "保存中…";
    try {
        const resp = await fetch("/set-config", { method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload) });
        const data = await resp.json();
        msg.textContent = data.ok ? "已保存" : "保存失败";
    } catch (e) { msg.textContent = "网络错误"; }
});

// ═══════════════════════════════════════════
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
        img.src = "/流萤_头像.png";
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
    img.src = "/assets/" + stickerPath;
    row.appendChild(img);
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { messagesEl.appendChild(row); scrollToBottom(); }
    return row;
}

function addNarration(text, style, prepend = false, seq = null) {
    // 视觉小说式旁白：scene=居中小字（环境/事件），action=居中括号（动作）
    const row = document.createElement("div");
    row.className = "msg-row narration-row";
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");
    const el = document.createElement("div");
    el.className = "narration " + (style === "scene" ? "narration-scene" : "narration-action");
    if (style === "action") el.textContent = "（" + text + "）";
    else el.textContent = text;
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
    // 时间标注：取第一条消息的时间，放居中分割线
    const ts = messages[0].time ? messages[0].time.slice(11, 16) : new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    addTimeDivider(ts);
    // 逐条消息加载：先显示三圆点占位，再替换为真实内容（消息含文本与表情包）
    // 加载时长按字数 0.7~1.5s（表情包按最短 0.7s）；消息之间留 0.5s 空白模拟游戏节奏
    let seq = 0;
    const showNext = () => {
        if (seq >= messages.length) return;
        const msg = messages[seq++];
        const chars = (msg.content || msg.text || "").length;
        const loadMs = Math.min(1500, Math.max(700, 700 + chars * 25));
        const typingRow = addTypingBubble(who);
        setTimeout(() => {
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
    if (icon) icon.src = light ? "/assets/theme_moon.png" : "/assets/theme_sun.png";
}
window.toggleTheme = toggleTheme;
(function applyTheme() {
    let t = "dark";
    try { t = localStorage.getItem("theme") || "dark"; } catch (e) {}
    if (t === "light") {
        document.body.classList.add("theme-light");
        const icon = document.getElementById("theme-icon");
        if (icon) icon.src = "/assets/theme_moon.png";
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
const MODE_NAMES = { story: "剧情模式", haruno: "春日手信" };

function showHome() {
    homeView.classList.add("show");
    appView.style.display = "none";     // 首页独立视图：真正隐藏聊天页（避免半透明透视）
    closeMenu();
    stopCarousel();
}
async function showChat() {
    homeView.classList.remove("show");
    appView.style.display = "flex";     // 恢复聊天页
    startCarousel();
    scrollToBottom();
    // 顶部显示当前模式名
    const modeTag = document.getElementById("chat-mode-tag");
    if (modeTag) modeTag.textContent = MODE_NAMES[CURRENT_MODE] || CURRENT_MODE;
    // 模式可能已切换：清空并重载当前模式历史（story/haruno 数据隔离）
    if (_lastMode !== CURRENT_MODE) {
        _lastMode = CURRENT_MODE;
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
    try {
        const resp = await fetch("/open-mode", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ mode: CURRENT_MODE }),
        });
        const data = await resp.json();
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
    carouselTrack.style.transform = `translateX(-${carouselIndex * 100}%)`;
    [...carouselDots.children].forEach((d, di) => d.classList.toggle("active", di === carouselIndex));
}
function startCarousel() {
    stopCarousel();
    carouselTimer = setInterval(() => goCarousel(carouselIndex + 1), 10000);
}
function stopCarousel() { if (carouselTimer) { clearInterval(carouselTimer); carouselTimer = null; } }
// 触摸滑动/点击：只绑定轮播图图片区（carouselTrack），其余区域不触发
let touchX = null;
carouselTrack.addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, {passive: true});
carouselTrack.addEventListener("touchend", (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 40) goCarousel(carouselIndex + (dx < 0 ? 1 : -1));
    else enterCarouselAction();   // 触摸点击轮播图 → 按功能入口进入
    touchX = null;
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
    carouselTrack.style.transform = `translateX(calc(-${carouselIndex * 100}% + ${dx}px))`;
});
window.addEventListener("mouseup", (e) => {
    if (!dragState) return;
    const dx = dragState.curX - dragState.startX;
    const moved = dragState.moved;
    dragState = null;
    carouselTrack.style.transform = `translateX(-${carouselIndex * 100}%)`;
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
    if (location.hash === "#chat") showChat();
    else showHome();
});

// ═══════════════════════════════════════════
// 发送消息 — 四阶段模型：输入 → 发送 → 提交 → 回复
//   输入：打字（内容只在输入框，不触发队列）
//   发送：Enter / 发送按钮 / 点表情 → 内容入队 _pending（可合并），重置提交计时
//   提交：5s 窗口结束 → _doSend 把整个队列发给后端
//   回复：流萤回复渲染
// 关键：发送 ≠ 提交。按 Enter 只是入队，5s 窗口内可继续发送合并；
//      输入框未发送的内容永不提交（绝不自动发送）。
// ═══════════════════════════════════════════
async function _doSend() {
    if (!_pending.length || waiting) return;
    waiting = true;
    clearTimeout(_sendTimer);
    inputEl.disabled = true; sendBtn.disabled = true;
    const statusEl = document.querySelector("#header .status");
    const defaultStatus = statusEl.textContent;
    statusEl.textContent = "对方正在输入...";
    const msgs = _pending.slice();   // 混合数组：字符串=文字，{type:"sticker"}=表情
    _pending = [];
    try {
        const resp = await fetch("/chat", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ messages: msgs, session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        statusEl.textContent = defaultStatus;
        if (data.need_key) openSettings();
        else if (data.messages) renderMessages(data.messages, "firefly", data);
        else if (data.reply) addTextMessage(data.reply, "firefly");
    } catch (e) {
        statusEl.textContent = defaultStatus;
        addTextMessage("嗯…信号不太好，等会儿再试试？", "firefly");
    }
    waiting = false;
    inputEl.disabled = false; sendBtn.disabled = false;
    inputEl.focus();
}

async function send() {
    const text = inputEl.value.trim();
    if (!text || waiting) return;
    inputEl.value = "";
    addTextMessage(text, "user");
    _pending.push({type: "text", content: text});   // 统一消息对象类型
    inputEl.focus();
    clearTimeout(_sendTimer);
    _sendTimer = setTimeout(_doSend, 5000);
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
            const resp = await fetch("/stickers");
            const data = await resp.json();
            const list = data.stickers || [];
            stickerGrid.innerHTML = list.map(s =>
                `<img src="/assets/${s.file}" alt="${s.label}" data-label="${s.label}" data-file="${s.file}">`).join("");
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

/** 发送表情包：作为一条消息入队并重新计时（与文字同一队列，不碰输入框内容） */
function sendStickerMessage(label, file) {
    if (waiting) return;
    if (file) addSticker(file, "user");   // 本地立即渲染表情图
    _pending.push({type: "sticker", label, file});
    inputEl.focus();
    clearTimeout(_sendTimer);
    _sendTimer = setTimeout(_doSend, 5000);   // 表情入队 → 触发重新计时
}

sendBtn.addEventListener("click", send);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
// 提交窗口控制（四阶段：发送≠提交）：
// - 队列有内容且输入框非空（未发送）→ 禁止提交，等开拓者说完（可见性感知）
// - 输入框清空 → 重新 5 秒提交计时
inputEl.addEventListener("input", () => {
    if (!_pending.length) return;
    clearTimeout(_sendTimer);
    if (inputEl.value.trim()) {
        _sendTimer = null;   // 有未发送内容：禁止提交（流萤在等开拓者）
    } else {
        _sendTimer = setTimeout(_doSend, 5000);   // 清空：重新 5 秒后提交
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
        list.innerHTML = '<div style="color:#8a8a8a;line-height:1.6">状态系统尚未接入。<br>当前流水线：分析 → 回复 → 表情包。</div>';
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
        if (countEl) countEl.textContent = `共 ${data.count} 次请求`;
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
        if (us) us.value = byName["用户设定.md"] ?? "";
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
        <div class="sticker-row" data-id="${s.id}">
            <div class="stk-top">
                <img class="stk-thumb" src="/assets/${s.file}" loading="lazy" onerror="this.style.opacity=0.2">
                <select class="stk-cat-sel">
                    <option value="可爱" ${s.category==="可爱"?"selected":""}>可爱</option>
                    <option value="帅气" ${s.category==="帅气"?"selected":""}>帅气</option>
                </select>
                <button class="stk-save" disabled>保存</button>
                <button class="stk-del" ${s.is_default ? "disabled" : ""}>删</button>
            </div>
            <input class="stk-label-input" type="text" value="${s.label}" maxlength="20">
        </div>`).join("");
        list.querySelectorAll(".sticker-row").forEach(row => {
            const id = row.dataset.id;
            const inp = row.querySelector(".stk-label-input");
            const cat = row.querySelector(".stk-cat-sel");
            const save = row.querySelector(".stk-save");
            const del = row.querySelector(".stk-del");
            const origLabel = inp.value;
            const origCat = cat.value;

            function checkChanged() {
                save.disabled = (inp.value.trim() === origLabel && cat.value === origCat) || (!inp.value.trim() && !cat.value);
            }
            inp.addEventListener("input", checkChanged);
            cat.addEventListener("change", checkChanged);

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
    const url = beforeSeq ? `/history?limit=150&before_seq=${beforeSeq}&mode=${CURRENT_MODE}` : `/history?limit=150&mode=${CURRENT_MODE}`;
    _lastWho = null;
    try {
        const resp = await fetch(url);
        const data = await resp.json();
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
// 启动
// ═══════════════════════════════════════════
checkWake();
checkKey().then(() => { loadHistory(); });
