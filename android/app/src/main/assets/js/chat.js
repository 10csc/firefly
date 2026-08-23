// 聊天核心：消息渲染 / 打字机 / 长按菜单 / 引用 / 发送 / 历史 / 休息撤回
import { S, SESSION_ID, inputEl, messagesEl, sendBtn } from "./state.js";
import { _toast, escapeHtml, showToast, stickerSrc } from "./util.js";
import { compressImage } from "./imgzip.js";
import { API_BASE, IS_SERVER } from "./api.js";
import { TB_AVATARS, closeMenu, openAvatarPicker, openMenu, openSettings, tbChoice } from "./panels.js";
import { CURRENT_MODE, MODE_NAMES, _modeGen } from "./views.js";
import { _idleOk, _notifyFirefly, checkProactive } from "./proactive.js";

// 消息渲染
// ═══════════════════════════════════════════
// ═══════════════════════════════════════════
// 滚动到底部（rAF 延迟：等 DOM 更新/键盘 resize 后再滚，QQ/微信式自动拉底）
// ═══════════════════════════════════════════
export function scrollToBottom() {
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

/** 按 seq 插序：DOM 顺序 = 记录顺序（不管渲染先后）。
 * 有 seq → 找到第一个 seq 更大的行，插它前面（同 seq 追加末尾）；无 seq → 追加。
 * 修复：多批回复动画交错时表情包"堆积"错位——插序保证显示与记录一致。 */
function _insertRow(row, seq) {
    if (seq === null || seq === undefined) { messagesEl.appendChild(row); return; }
    const rows = messagesEl.querySelectorAll(".msg-row[data-seq]");
    let anchor = null;
    for (const r of rows) {
        const s = parseInt(r.dataset.seq, 10);
        if (!isNaN(s) && s > seq) { anchor = r; break; }
    }
    if (anchor) messagesEl.insertBefore(row, anchor);
    else messagesEl.appendChild(row);
}

function addTextMessage(text, who, prepend = false, seq = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");   // 新消息从下方浮现（历史加载不带动画）
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    if (quote) {
        // 带引用的消息：气泡上方加引用小卡片（QQ 式）
        const col = document.createElement("div");
        col.className = "msg-col";
        col.appendChild(_buildQuotePreview(quote));
        col.appendChild(bubble);
        row.appendChild(col);
    } else {
        row.appendChild(bubble);
    }
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    return row;
}

/** 引用快照 → 气泡上方的小卡片（谁 + 内容摘要） */
function _quoteContentText(q) {
    if (!q) return "";
    if (q.type === "sticker") return "[表情包：" + (q.label || "") + "]";
    if (q.type === "image") return "[图片：" + (q.desc || "（无描述）") + "]";
    if (q.type === "narration") return q.text || "";
    return q.content || "";
}
function _quoteWhoName(q) { return q && q.who === "user" ? "我" : "流萤"; }
function _buildQuotePreview(q) {
    const div = document.createElement("div");
    div.className = "quote-preview";
    const who = document.createElement("span");
    who.className = "qp-who";
    who.textContent = _quoteWhoName(q);
    const txt = document.createElement("span");
    txt.className = "qp-text";
    txt.textContent = _quoteContentText(q);
    div.appendChild(who);
    div.appendChild(txt);
    return div;
}

async function addSticker(stickerPath, who, prepend = false, seq = null, label = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (label) row.dataset.stickerLabel = label;   // 长按菜单需要表情含义
    if (!prepend) row.classList.add("float-in");
    // A2 媒体本地策略：local: 引用 → IndexedDB 取本体；缺失降级占位（服务器不保存图片）
    const src = await stickerSrc(stickerPath, IS_SERVER, API_BASE);
    let img = null;
    if (src) {
        img = document.createElement("img");
        img.className = "sticker-img";
        img.src = src;
        img.dataset.stickerPath = stickerPath;
        // 容错：表情包文件缺失（历史遗留/用户删除）时降级为文字占位，不显示裂图
        img.onerror = () => {
            if (img.dataset.fallback) return;
            img.dataset.fallback = "1";
            const span = document.createElement("span");
            span.className = "sticker-fallback";
            span.textContent = "（表情包已失效）";
            row.replaceChild(span, img);
        };
    } else {
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = "（表情包已失效）";
        img = span;
    }
    if (quote) {
        const col = document.createElement("div");
        col.className = "msg-col";
        col.appendChild(_buildQuotePreview(quote));
        col.appendChild(img);
        row.appendChild(col);
    } else {
        row.appendChild(img);
    }
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    return row;
}

/** A9 图片消息渲染：两端统一走后端 /image?id=&mode= 取字节（服务器版也上传落盘，IndexedDB 图片存储已退役）。
 *  服务器版 <img> 标签带不了 Bearer：经鉴权 fetch 拿字节转 objectURL；本地版同源直接 <img src>。
 *  缺失/失败 → 显示 desc 文字占位。 */
async function addImage(msg, who, prepend = false, seq = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly") + " image-row";
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");
    const imgId = (msg.img_id || "").toString().slice(0, 200);
    const desc = (msg.desc || "").toString().slice(0, 300);
    let src = null;
    if (imgId) {
        const url = "/image?id=" + encodeURIComponent(imgId) + "&mode=" + encodeURIComponent(CURRENT_MODE);
        if (IS_SERVER) {
            try {
                const resp = await fetch(url);
                if (resp.ok) { try { src = URL.createObjectURL(await resp.blob()); } catch (e) {} }
            } catch (e) {}
        } else {
            src = url;
        }
    }
    let content;
    if (src) {
        const img = document.createElement("img");
        img.className = "image-img";
        img.style.cssText = "max-width:min(56vw,320px);max-height:280px;border-radius:12px;display:block";
        img.src = src;
        img.dataset.imgId = imgId;
        img.dataset.desc = desc;
        img.onerror = () => {
            if (img.dataset.fallback) return;
            img.dataset.fallback = "1";
            const span = document.createElement("span");
            span.className = "sticker-fallback";
            span.textContent = desc ? `（图片：${desc}）` : "（图片已失效）";
            row.replaceChild(span, img);
        };
        content = img;
    } else {
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = desc ? `（图片：${desc}）` : "（图片已失效）";
        span.dataset.imgId = imgId;
        span.dataset.desc = desc;
        content = span;
    }
    if (quote) {
        const col = document.createElement("div");
        col.className = "msg-col";
        col.appendChild(_buildQuotePreview(quote));
        col.appendChild(content);
        row.appendChild(col);
    } else {
        row.appendChild(content);
    }
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

// ═══════════════════════════════════════════
// 长按消息菜单（QQ 式）：引用 / 收藏
// ═══════════════════════════════════════════
const _LONG_PRESS_MS = 500;
let _lpTimer = null, _lpRow = null, _lpStart = null;
let _msgMenu = null;
let _quoteTarget = null;   // 引用快照（发送后随消息提交，然后清空）

/** 从消息行提取快照（who/type/content…；seq 只在历史渲染的消息上有） */
function _msgSnapshot(row) {
    const who = row.classList.contains("user") ? "user" : "firefly";
    const snap = {who};
    const seq = parseInt(row.dataset.seq, 10);
    if (!isNaN(seq)) snap.seq = seq;
    if (row.classList.contains("narration-row")) {
        snap.type = "narration";
        const el = row.querySelector(".narration");
        let t = el ? el.textContent : "";
        t = t.replace(/^（|）$/g, "").trim();   // 剥掉旁白自带括号，引用内容更干净
        snap.text = t;
    } else if (row.querySelector(".sticker-img")) {
        snap.type = "sticker";
        snap.label = row.dataset.stickerLabel || "";
        const img = row.querySelector(".sticker-img");
        if (img) snap.path = img.dataset.stickerPath || "";
    } else if (row.querySelector(".image-img")) {
        snap.type = "image";
        const img = row.querySelector(".image-img");
        if (img) {
            snap.img_id = img.dataset.imgId || "";
            snap.desc = img.dataset.desc || "";
        }
    } else {
        snap.type = "text";
        const b = row.querySelector(".bubble");
        snap.content = b ? b.textContent : "";
    }
    return snap;
}

function _snapHasContent(s) {
    if (!s) return false;
    if (s.type === "text") return !!(s.content && s.content.trim());
    if (s.type === "sticker") return !!(s.label || s.path);
    if (s.type === "narration") return !!(s.text && s.text.trim());
    if (s.type === "image") return !!(s.img_id || s.desc);
    return false;
}

function _closeMsgMenu() {
    if (_msgMenu) { _msgMenu.remove(); _msgMenu = null; }
}
window._closeMsgMenu = _closeMsgMenu;

/** PC 侧栏导航用：打开菜单抽屉并切到指定 tab（双栏下抽屉即右栏视图） */
function openMenuTab(tab) {
    openMenu();
    const b = document.querySelector('.menu-tab[data-tab="' + tab + '"]');
    if (b) b.click();
}
window.openMenuTab = openMenuTab;

function _showMsgMenu(row, x, y) {
    const snap = _msgSnapshot(row);
    if (!_snapHasContent(snap)) return;
    _closeMsgMenu();   // 唯一性：先清掉旧菜单，保证同一时刻只有一个「引用/收藏」菜单
    const menu = document.createElement("div");
    menu.id = "msg-long-menu";
    _msgMenu = menu;   // 登记为当前菜单（_closeMsgMenu 靠它清理；漏掉会越积越多）
    const mkBtn = (label, icon, fn) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "mlm-btn";
        b.textContent = icon + " " + label;
        b.addEventListener("click", fn);
        return b;
    };
    menu.appendChild(mkBtn("引用", "📎", () => { _closeMsgMenu(); _setQuote(snap); }));
    menu.appendChild(mkBtn("收藏", "⭐", async () => {
        _closeMsgMenu();
        try {
            const resp = await fetch("/favorite", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({mode: CURRENT_MODE, message: snap}),
            });
            const d = await resp.json();
            showToast(d.ok ? "已收藏（菜单 → 收藏可查看）" : "收藏失败：" + (d.error || ""));
        } catch (e) { showToast("收藏失败，请重试"); }
    }));
    document.body.appendChild(menu);
    // 定位：消息在上半屏 → 菜单放下方；下半屏 → 放上方（QQ 式，且不超出视口）
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    const r = row.getBoundingClientRect();
    const vw = innerWidth, vh = innerHeight;
    let left = Math.min(Math.max(8, x - mw / 2), vw - mw - 8);
    const cy = r.top + r.height / 2;
    let top = cy < vh / 2 ? r.bottom + 10 : r.top - mh - 10;
    top = Math.max(8, Math.min(top, vh - mh - 8));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
}

function _cancelLongPress() {
    if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
    _lpRow = null; _lpStart = null;
}

// 长按（触屏/鼠标按住）与 PC 右键都弹菜单
messagesEl.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const row = e.target.closest(".msg-row");
    if (!row || e.target.closest(".msg-avatar")) return;          // 头像长按留给形象切换
    if (row.querySelector(".typing-bubble")) return;              // 加载占位不可长按
    _cancelLongPress();
    _lpRow = row;
    _lpStart = {x: e.clientX, y: e.clientY};
    _lpTimer = setTimeout(() => {
        _lpTimer = null;
        if (_lpRow && _lpRow.isConnected) {
            try { if (navigator.vibrate) navigator.vibrate(20); } catch (e) {}   // 触觉反馈
            _showMsgMenu(_lpRow, _lpStart.x, _lpStart.y);
        }
    }, _LONG_PRESS_MS);
});
window.addEventListener("pointermove", (e) => {
    if (_lpTimer && _lpStart) {
        const dx = e.clientX - _lpStart.x, dy = e.clientY - _lpStart.y;
        if (dx * dx + dy * dy > 64) _cancelLongPress();   // 移动超 8px = 滚动/滑动，取消长按
    }
}, {passive: true});
window.addEventListener("pointerup", _cancelLongPress);
window.addEventListener("pointercancel", _cancelLongPress);
messagesEl.addEventListener("scroll", _closeMsgMenu, {passive: true});
// 点击菜单外任意处关闭
document.addEventListener("pointerdown", (e) => {
    if (_msgMenu && !e.target.closest("#msg-long-menu")) _closeMsgMenu();
}, {capture: true});
// PC 右键同样弹菜单
messagesEl.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".msg-row");
    if (!row) return;
    e.preventDefault();
    _showMsgMenu(row, e.clientX, e.clientY);
});

// ── 引用：设置引用条（输入框上方） ──
function _setQuote(snap) {
    _quoteTarget = snap;
    const bar = document.getElementById("quote-bar");
    if (!bar) return;
    const whoEl = document.getElementById("quote-who");
    const txtEl = document.getElementById("quote-text");
    if (whoEl) whoEl.textContent = "引用 " + _quoteWhoName(snap);
    if (txtEl) txtEl.textContent = _quoteContentText(snap);
    bar.style.display = "flex";
    inputEl.focus();
}
function _clearQuote() {
    _quoteTarget = null;
    const bar = document.getElementById("quote-bar");
    if (bar) bar.style.display = "none";
}
document.getElementById("quote-cancel")?.addEventListener("click", _clearQuote);

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

export function renderMessages(messages, who, data) {
    if (!messages || messages.length === 0) return;
    S._lastRenderTs = Date.now();   // 渲染时间戳（供主动性轮询门控；原二次包装已合并进来）
    const gen = _modeGen;   // 捕获渲染启动时的模式代际
    S._rendering = true;   // 渲染动画开始：防主动轮询中途插入乱序
    // 时间标注：取第一条消息的时间，放居中分割线
    const ts = messages[0].time ? messages[0].time.slice(11, 16) : new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    addTimeDivider(ts);
    // 逐条消息加载：先显示三圆点占位，再替换为真实内容（消息含文本与表情包）
    // 加载时长按字数 0.7~1.5s（表情包按最短 0.7s）；消息之间留 0.5s 空白模拟游戏节奏
    let seq = 0;
    const showNext = () => {
        if (gen !== _modeGen) { S._rendering = false; return; }   // 模式已切换：丢弃剩余动画
        if (seq >= messages.length) {
            S._rendering = false;   // 渲染动画完成
            return;
        }
        const msg = messages[seq++];
        const chars = (msg.content || msg.text || "").length;
        const loadMs = Math.min(1500, Math.max(700, 700 + chars * 25));
        const typingRow = addTypingBubble(who);
        setTimeout(() => {
            if (gen !== _modeGen) { typingRow.remove(); S._rendering = false; return; }
            typingRow.remove();
            if (msg.type === "sticker") addSticker(msg.path, who);
            else if (msg.type === "narration") addNarration(msg.text, msg.style);
            else if (msg.type === "image") addImage(msg, who);
            else addTextMessage(msg.content, who);
            setTimeout(showNext, 500);   // 消息间隔：0.5s 空白
        }, loadMs);
    };
    showNext();
}

// 消息渲染后记录时间（renderMessages 内调用；0.9.0 起已合并进函数本体，保留此注释防回归）

// ═══════════════════════════════════════════
// 发送消息 — 四阶段模型：输入 → 发送 → 提交 → 回复
//   输入：打字（内容只在输入框，不触发队列）
//   发送：Enter / 发送按钮 / 点表情 → 消息**立即 POST 后端**（不等 5 秒，切后台不丢）
//   提交：后端 5 秒滑动窗口合并（后端控制；/chat/hint 重置窗口、/chat/flush 提前结束）
//   回复：流萤回复渲染
// 关键：发送 ≠ 提交。后端窗口合并连续消息；输入框未发送的内容永不提交（绝不自动发送）。
// ═══════════════════════════════════════════
export let _inflight = 0;        // 在飞请求数（WakeLock 引用计数：全部完成才释放）
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
    timeout: "回复超时了，稍后再试一次吧",
    cooldown: "上游服务波动中，休息一下再试试",
    quota_exhausted: "今日服务器托管额度已用完，可在设置中切换为自带 Key 模式",
    unknown: "出了点问题，请稍后再试",
};


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

/** 本地备份管理（备份存后端 backups/ 目录；云端账号备份已下线，改由 /sync/now 增量同步）。
 *  列表 / 新建 / 恢复 / 删除 / 下载；恢复与删除沿用 confirm 二次确认。 */
function _fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
}

async function loadBackups() {
    const list = document.getElementById("backup-list");
    if (!list) return;
    try {
        const resp = await fetch("/backups");
        const data = await resp.json();
        const items = (data.backups || []).filter(b => !b.mode || b.mode === CURRENT_MODE);
        if (!items.length) {
            list.innerHTML = '<div style="color:var(--fg-muted);font-size:0.75em">还没有备份，点「新建备份」存一份</div>';
            return;
        }
        list.innerHTML = items.map(b => `
        <div class="backup-row" data-name="${escapeHtml(b.name)}" style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.75em">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(b.name)}">${escapeHtml(b.time || b.name)}</span>
            <span style="color:var(--fg-muted);flex-shrink:0">${_fmtSize(b.size)}</span>
            <button type="button" class="bk-restore">恢复</button>
            <button type="button" class="bk-del">删除</button>
            <button type="button" class="bk-download">下载</button>
        </div>`).join("");
        list.querySelectorAll(".backup-row").forEach(row => {
            const name = row.dataset.name;
            row.querySelector(".bk-restore").addEventListener("click", () => restoreBackup(name));
            row.querySelector(".bk-del").addEventListener("click", () => deleteBackup(name));
            row.querySelector(".bk-download").addEventListener("click", () => downloadBackup(name));
        });
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;font-size:0.75em">备份列表加载失败</div>';
    }
}
window.loadBackups = loadBackups;

async function createBackup() {
    _toast("正在创建备份…");
    try {
        const resp = await fetch("/backup/create", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: CURRENT_MODE}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast("备份已创建（" + _fmtSize(data.size) + "）");
            loadBackups();
        } else {
            _toast("备份失败：" + (data.error || ""));
        }
    } catch (e) { _toast("备份失败，请检查网络"); }
}
window.createBackup = createBackup;

async function restoreBackup(name) {
    if (!confirm(`用备份「${name}」覆盖当前「${MODE_NAMES[CURRENT_MODE] || CURRENT_MODE}」的全部数据？\n\n确定恢复吗？`)) return;
    _toast("正在恢复备份…");
    try {
        const resp = await fetch("/backup/restore", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast("恢复成功，正在重新加载…");
            setTimeout(() => location.reload(), 800);
        } else {
            _toast("恢复失败：" + (data.error || ""));
        }
    } catch (e) { _toast("恢复失败，请检查网络"); }
}

async function deleteBackup(name) {
    if (!confirm(`删除备份「${name}」？此操作不可撤销。`)) return;
    try {
        const resp = await fetch("/backup/delete", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast("备份已删除");
            loadBackups();
        } else {
            _toast("删除失败：" + (data.error || ""));
        }
    } catch (e) { _toast("删除失败，请检查网络"); }
}

/** 下载备份 zip（沿用导出通道 GET /export-data 的浏览器下载；带 name 时后端给对应备份包）。 */
async function downloadBackup(name) {
    const qs = `mode=${encodeURIComponent(CURRENT_MODE)}` + (name ? `&name=${encodeURIComponent(name)}` : "");
    if (!IS_SERVER) {
        window.location.href = `/export-data?${qs}`;
        _toast("正在下载备份…");
        return;
    }
    try {
        const resp = await fetch(`/export-data?${qs}`);
        if (!resp.ok) { _toast("下载失败，请稍后再试"); return; }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = name || `firefly-backup-${CURRENT_MODE}.zip`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
        _toast("已开始下载备份");
    } catch (e) { _toast("下载失败，请检查网络"); }
}

/** 等待回复期间轮询流水线阶段（检索→分析→回复→表情包），把"对方正在输入…"换成具体阶段。
 *  仅在拿到 stage 时替换文本；请求结束由 _chatSend 的 finally 清除。
 *  A3：消费后端 waited 字段——等待偏久（>60s）提示"上游有点慢"，不再像卡死。 */
let _waitedWarned = false;
function _pollStage(statusEl) {
    clearInterval(_stageTimer);
    _waitedWarned = false;
    _stageTimer = setInterval(async () => {
        if (_inflight <= 0) { clearInterval(_stageTimer); _stageTimer = null; return; }
        try {
            const r = await fetch(`/chat-stage?sid=${encodeURIComponent(SESSION_ID)}&mode=${encodeURIComponent(CURRENT_MODE)}`);
            const d = await r.json();
            if (d.stage && d.label && _inflight > 0 && statusEl) statusEl.textContent = d.label;
            if (d.waited != null && d.waited > 60 && !_waitedWarned && _inflight > 0 && statusEl) {
                _waitedWarned = true;
                statusEl.textContent = "上游有点忙，正在努力回复…";
            }
        } catch (e) { /* 网络抖动静默，状态保持"对方正在输入" */ }
    }, 2000);
}

// ═══════════════════════════════════════════
// 提交窗口状态机（0.8.1 简化，两判定 + 上限）
// 提交流程：消息入队（气泡上屏+写盘，不丢）→ 等待提交。
// 提交条件（两个都满足）：
//   ① 输入框为空（没在打下一句）
//   ② 距最新一条消息 ≥ 5 秒（_lastMsgTs 每次发送重置，以最新消息为准）
// 打字中（输入框有内容）→ 持续 hint 续期后端窗口（2s 节流），流萤继续等，绝不提交；
// 合并上限：单批连续消息 ≥ _MAX_BATCH_MSGS 条 → 立即提交（即使输入框有内容）。
// 与后端 _CHAT_WINDOW_MAX_MSGS=10 一致。
// ═══════════════════════════════════════════
const _MAX_BATCH_MSGS = 10;
let _lastMsgTs = 0;              // 最新一条已发送消息的时间戳
let _pendingBatch = 0;           // 本批已入队未提交条数
let _mediaBusy = false;          // 媒体选择中（图片相册/表情面板）：暂停提交（长期等待）

// 常驻检查器（页面级）：每 1 秒拍一次，只在有 pending 批时判定。
// 简化语义：
//   _pendingBatch === 0 → 无事可做，跳过；
//   _mediaBusy（相册/表情面板打开）→ 与打字中同构：持续 hint 续期后端窗口，
//     不提交（后端窗口是 5 秒滑动的，不续期照样到期——前端暂停 flush 不够，必须续期）；
//   输入框有内容（打字）→ 续期后端窗口，不提交；
//   距最新消息 ≥ 5 秒 → 提交（flush）。
let _checkTimer = setInterval(() => {
    if (_pendingBatch <= 0) return;
    if (_pendingBatch >= _MAX_BATCH_MSGS) { _batchCommit(); return; }   // 合并上限 10 条
    if (_mediaBusy) { _sendHint(); return; }                            // 媒体选择中：续期窗口+不提交
    if (inputEl && inputEl.value.trim()) { _sendHint(); return; }       // 打字中：续期窗口
    if (Date.now() - _lastMsgTs < 5000) return;                         // 距最新消息 <5 秒：继续等
    _batchCommit();
}, 1000);

function _batchArmSubmit() {
    _lastMsgTs = Date.now();
    _pendingBatch++;
}

/** 媒体选择结束后：重新计时 5 秒（不增加批计数——未产生新消息）。 */
function _batchRefreshTimer() {
    _lastMsgTs = Date.now();
}

function _batchCommit() {
    _pendingBatch = 0; _lastMsgTs = 0;
    _sendFlush();
}

/** 模式切换/离开聊天页时调用：提交批立即作废（旧模式消息不跨模式提交）。timer 常驻不清。 */
export function resetBatchWindow() {
    _pendingBatch = 0; _lastMsgTs = 0; _mediaBusy = false;
}

/** 打字中：重置后端合并窗口（流萤继续等开拓者说完）。
 * 输入框仍有内容 → 持续定时重置（前端在且输入框有残留 = 用户在打字 → 永不提交）。 */
function _sendHint() {
    fetch("/chat/hint", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
    }).catch(() => {});
    // 输入框仍有残留（用户还在打字/未清空）→ 继续定时重置窗口（2s 节流）
    if (inputEl && inputEl.value.trim()) {
        clearTimeout(S._hintTimer);
        S._hintTimer = setTimeout(_sendHint, 2000);
    }
}

/** 提交窗口到期：立即结束后端合并窗口（前台加速；切后台冻结不触发，后端窗口兜底）。
 * 状态显示时机：只有提交（进入核心流水线）才显示"对方正在输入"，窗口等待期不显示。 */
function _sendFlush() {
    if (inputEl) inputEl.placeholder = "说点什么…";   // 提交窗口结束：还原补话提示（3.6）
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
 *  状态显示不在此处设置——窗口等待期不显示"正在输入"，由 _sendFlush（提交）触发。
 *  超时护栏：/chat 挂 4 分钟 AbortController，上游（LLM 端点）卡住时
 *  自动放弃等待并提示，避免页面无限挂起"对方正在输入…"（历史上游 5xx 重试
 *  + 单阶段 120s 超时可拖 15 分钟以上，会话锁全线阻塞）。 */
const _CHAT_FETCH_TIMEOUT = 4 * 60 * 1000;   // 4 分钟（覆盖 4 阶段最长流水线）

async function _chatSend(msgs) {
    _inflight++;
    const statusEl = document.querySelector("#header .status");
    // 修复：不用"请求开始时的快照"恢复（快照可能已被 _sendFlush / 阶段轮询污染成
    // "对方正在输入"/"正在理解你的话…"），统一恢复为流萤的个人简介默认文案。
    const defaultStatus = "会找到的，属于我的梦...";
    const gen = _modeGen;   // 捕获发起时的模式代际
    // 后台保活（安卓 WebView JS Bridge）：回复流程（检索→分析→回复→调度）期间
    // 持 CPU/WiFi 锁，用户切后台/锁屏也能完成回复；引用计数归零才释放。
    // 浏览器端（PC/服务器版）无 androidWakeLock，此段安全跳过
    if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.acquire(); }
    _pollStage(statusEl);   // 启动阶段进度轮询（回复到达后 finally 清除）
    const abortCtrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const abortTimer = abortCtrl ? setTimeout(() => abortCtrl.abort(), _CHAT_FETCH_TIMEOUT) : null;
    try {
        const fetchOpts = {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ messages: msgs, session_id: SESSION_ID, mode: CURRENT_MODE }),
        };
        // 旧 WebView 无 AbortController：不带 signal 字段，行为与之前一致
        if (abortCtrl) fetchOpts.signal = abortCtrl.signal;
        const resp = await fetch("/chat", fetchOpts);
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
            if (abortCtrl && e && e.name === "AbortError") {
                // 上游卡住被超时中止：消息已即时写盘不丢，提示用户稍后再试
                addTextMessage("嗯…上游有点忙，我先不打扰了，过会儿再试试？", "firefly");
            } else {
                addTextMessage("嗯…信号不太好，等会儿再试试？", "firefly");
            }
        }
    } finally {
        if (abortTimer) clearTimeout(abortTimer);
        if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.release(); }
        _inflight--;
        if (_inflight === 0) {
            // 请求完成（主请求带回回复 / 全部副请求结束）：批提交作废（timer 常驻不清）
            _pendingBatch = 0; _lastMsgTs = 0;
            clearTimeout(S._hintTimer); S._hintTimer = null;
            clearTimeout(S._flushTimer); S._flushTimer = null;   // 兼容旧引用
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
    if (!text || S.waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    inputEl.value = "";
    const q = _quoteTarget;
    addTextMessage(text, "user", false, null, q);
    inputEl.focus();
    // 0.8.1：进入提交窗口状态机（两判定+10条上限；窗口到期由 _batchCommit 触发 flush）
    clearTimeout(S._hintTimer);
    inputEl.placeholder = "还可以继续说…";   // 3.6：提交窗口内提示"还能补话"
    const msg = {type: "text", content: text};
    if (q) msg.quote = q;   // 引用随消息提交（后端写盘 + LLM 上下文）
    _chatSend([msg]);   // 统一消息对象类型，立即发送
    _batchArmSubmit();
    _clearQuote();
}

// ═══════════════════════════════════════════
// 发图片（A9）：🖼 按钮 → 选图 → 压缩 → 上传落盘 → 描述 → 发送 image 消息
// （图片链路统一：本地版/服务器版都走 /upload-image；服务器版请求由 fetch 包装器带登录 Bearer）
// ═══════════════════════════════════════════
const imageBtn = document.getElementById("image-btn");
const imageFileInput = document.getElementById("image-file-input");
if (imageBtn && imageFileInput) {
    // 点击 🖼：进入媒体选择状态（暂停提交计时——相册选择是长期操作，
    // 否则第一条消息会在选图中途被 5 秒窗口提前提交）
    imageBtn.addEventListener("click", () => {
        _mediaBusy = true;
        imageFileInput.value = "";
        imageFileInput.click();
    });
    imageFileInput.addEventListener("change", async () => {
        // 选择结束（无论成功/取消/失败）：恢复计时（重新 5 秒）
        const endMedia = (arm = false) => {
            _mediaBusy = false;
            if (arm) {   // 成功发送：进入新批计数
                _batchArmSubmit();
            } else {     // 取消/失败：重新计时但不计入批数
                _batchRefreshTimer();
            }
        };
        // PC/浏览器取消选图不触发 change（只发 cancel）→ _mediaBusy 永远为真、批次永不提交。
        // 补 cancel 监听：取消/重开选择器都恢复计时（Kimi 复审 #2；安卓 WebView 取消同理）。
        imageFileInput.addEventListener("cancel", () => { endMedia(false); });
        const f = imageFileInput.files && imageFileInput.files[0];
        imageFileInput.value = "";
        if (!f || S.waiting) { endMedia(false); return; }
        if (!/^image\/(png|jpe?g|webp|gif)$/.test(f.type || "")) { _toast("仅支持 png/jpg/webp/gif 图片"); endMedia(false); return; }
        if (f.size > 10 * 1024 * 1024) { _toast("图片过大（上限 10MB）"); endMedia(false); return; }
        // 发送前压缩（imgzip.js）：GIF/小图原样；任何失败降级原图不阻塞发送
        const {blob, ext} = await compressImage(f);
        let imgId = "", desc = "";
        try {
            const fd = new FormData();
            fd.append("file", blob, "upload." + ext);   // 压缩产物是匿名 Blob：补文件名让后端拿到正确扩展名
            fd.append("mode", CURRENT_MODE);
            const resp = await fetch("/upload-image", {method: "POST", body: fd});
            const data = await resp.json();
            if (!data.ok) { _toast("图片上传失败：" + (data.error || "")); endMedia(false); return; }   // 配额满等错误直接透传后端文案
            imgId = data.img_id || "";
            desc = data.desc || "";
            if (data.need_desc) desc = "";
        } catch (e) { _toast("网络错误，图片未发送"); endMedia(false); return; }
        if (!desc) {
            // 描述缺失（vision 不支持/失败）：让用户填一句（可为空 → [图片] 占位）
            desc = (window.prompt("流萤还没有识图能力，这幅图是什么？（可留空）", "") || "").trim().slice(0, 300);
        }
        const q = _quoteTarget;
        addImage({img_id: imgId, desc}, "user");
        const msg = {type: "image", img_id: imgId};
        if (desc) msg.desc = desc;
        if (q) msg.quote = q;
        _chatSend([msg]);
        endMedia(true);   // 0.8.1：图片与文字同批合并（两判定+10条上限）
        _clearQuote();
    });
}

// ═══════════════════════════════════════════
// 表情包面板（输入框内 😊 按钮）
// ═══════════════════════════════════════════
const stickerPanel = document.getElementById("sticker-panel");
const stickerGrid = document.getElementById("sticker-grid");
const stickerBtn = document.getElementById("sticker-btn");

// 表情面板打开/关闭的提交计时管理（媒体选择中暂停提交）
function _stickerPanelClose() {
    stickerPanel.classList.remove("show");
    if (_mediaBusy) { _mediaBusy = false; _batchRefreshTimer(); }   // 重新计时
}

stickerBtn.addEventListener("click", async () => {
    if (stickerPanel.classList.contains("show")) {
        _stickerPanelClose();
        return;
    }
    _mediaBusy = true;   // 面板打开期间暂停提交（长期等待用户选择）
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
                    _mediaBusy = false;   // 选中即结束媒体状态（sendStickerMessage 内 _batchArmSubmit 重新计时）
                    _stickerPanelClose();
                    sendStickerMessage(img.dataset.label, img.dataset.file);
                });
            });
        } catch (e) { /* 静默 */ }
    }
});
// 点击聊天区关闭表情面板
messagesEl.addEventListener("click", _stickerPanelClose);
document.getElementById("sticker-panel-close").addEventListener("click", _stickerPanelClose);

/** 发送表情包：作为一条消息立即发送（与文字同一窗口合并，不碰输入框内容） */
function sendStickerMessage(label, file) {
    if (S.waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    const q = _quoteTarget;
    if (file) addSticker(file, "user", false, null, label, q);   // 本地立即渲染表情图
    inputEl.focus();
    clearTimeout(S._hintTimer);
    const msg = {type: "sticker", label, file};
    if (q) msg.quote = q;
    _chatSend([msg]);
    _batchArmSubmit();   // 0.8.1：表情与文字同批合并（两判定+10条上限）
    _clearQuote();
}

sendBtn.addEventListener("click", send);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
// 提交窗口控制（0.8.1 简化，两判定+上限，见 _batchArmSubmit 注释）：
// - 输入框有内容（打字中）→ 暂停提交 + hint 续期后端窗口（流萤继续等）
// - 输入框清空 → 由 1s 检查器按「距最新消息 5 秒」判定提交
inputEl.addEventListener("input", () => {
    clearTimeout(S._hintTimer);
    if (inputEl.value.trim()) {
        inputEl.placeholder = "说点什么…";   // 开始打字即还原提示（3.6）
        _sendHint();   // 立即续期一次 + 2s 节流循环
    } else {
        // 清空输入框：hint 停止（_sendHint 循环见框空即止），交给检查器按 5s 判定
        clearTimeout(S._hintTimer);
        S._hintTimer = null;
    }
});

// ═══════════════════════════════════════════
// 休息 / 清除 / 撤回
// ═══════════════════════════════════════════
const restOverlay = document.getElementById("rest-overlay");
// 休息结果展示后 3 秒自动收起（修复：原来先隐藏遮罩再写文案，用户看不见结果）
function _showRestResult(txt) {
    document.getElementById("rest-text").textContent = txt;
    setTimeout(() => { restOverlay.style.display = "none"; }, 3000);
}
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
        // 0.8.1：文案用真实变化——added/resolved 为 LLM 解析数组（可能为空但头部/手账已更新）
        let msg = "";
        if (data.ok) {
            if (data.skipped) {
                msg = "流萤已休息。这边没有新的对话内容需要整理，下次聊完再叫我吧。";
            } else {
                const parts = [];
                if (data.added) parts.push(`新增记忆 ${data.added} 条`);
                if (data.resolved) parts.push(`解决 ${data.resolved} 条`);
                if (!parts.length && data.head_changed) parts.push("记忆已更新");
                msg = `流萤已休息。${parts.join("，") || "记忆已更新"}。下次见。`;
            }
        } else {
            msg = "整理出了点问题：" + (data.error || "未知");
        }
        _showRestResult(msg);
    } catch (e) {
        _showRestResult("信号不好，等会儿再试。");
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
// 历史加载
// ═══════════════════════════════════════════
let _loading = false;
let _lastWho = null;
function renderHistoryMessage(m, prepend=false) {
    const ts = m.time ? m.time.slice(11,16) : null;
    // 发送方变化时插入时间分割线
    if (m.who !== _lastWho && ts) {
        addTimeDivider(ts);
        _lastWho = m.who;
    }
    if (m.type==="sticker") addSticker(m.path, m.who, prepend, m.seq, m.label, m.quote);
    else if (m.type==="narration") addNarration(m.text, m.style, prepend, m.seq);
    else if (m.type==="image") addImage(m, m.who, prepend, m.seq, m.quote);
    else addTextMessage(m.content, m.who, prepend, m.seq, m.quote);
}
export async function loadHistory(beforeSeq=null) {
    if (_loading) return; _loading = true;
    const gen = _modeGen;   // 捕获发起时的模式代际
    const url = beforeSeq ? `/history?limit=150&before_seq=${beforeSeq}&mode=${CURRENT_MODE}` : `/history?limit=150&mode=${CURRENT_MODE}`;
    _lastWho = null;
    try {
        const resp = await fetch(url);
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃旧模式历史，防止渲染进新模式界面
        if (!data.messages || data.messages.length===0) { S._hasMore=false; return; }
        if (!beforeSeq) { data.messages.forEach(m=>renderHistoryMessage(m,false)); messagesEl.scrollTop=messagesEl.scrollHeight; undoBtn.disabled=false; }
        else { const ph=messagesEl.scrollHeight, ps=messagesEl.scrollTop; data.messages.slice().reverse().forEach(m=>renderHistoryMessage(m,true)); messagesEl.scrollTop=ps+(messagesEl.scrollHeight-ph); }
        S._hasMore = !!data.has_more;
    } catch(e) {} finally { _loading=false; }
}
messagesEl.addEventListener("scroll", () => {
    if (messagesEl.scrollTop===0 && S._hasMore && !_loading) {
        const first = messagesEl.firstChild;
        const seq = first ? parseInt(first.dataset.seq) : null;
        if (seq) loadHistory(seq);
    }
});
