// 聊天核心：长按菜单 / 引用 / 发送四阶段 / 提交窗口状态机 / 数据备份导入导出
import { S, SESSION_ID, inputEl, messagesEl, sendBtn } from "./state.js";
import { _toast, escapeHtml, showToast } from "./util.js";
import { IS_SERVER } from "./api.js";
import { openMenu, openSettings } from "./panels.js";
import { addTextMessage, _quoteContentText, _quoteWhoName } from "./chat_render.js";
import { CURRENT_MODE, MODE_NAMES, _modeGen } from "./views.js";
import { _idleOk, _notifyFirefly, checkProactive } from "./proactive.js";

// ═══════════════════════════════════════════
// 长按消息菜单（QQ 式）：引用 / 收藏
// ═══════════════════════════════════════════
const _LONG_PRESS_MS = 500;
let _lpTimer = null, _lpRow = null, _lpStart = null;
let _msgMenu = null;
export let _quoteTarget = null;   // 引用快照（发送后随消息提交，然后清空）

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
export function _clearQuote() {
    _quoteTarget = null;
    const bar = document.getElementById("quote-bar");
    if (bar) bar.style.display = "none";
}
document.getElementById("quote-cancel")?.addEventListener("click", _clearQuote);

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

// ══ 数据快照（保存全部角色到服务器；每账号留最近 5 份，换机可恢复）══
function _fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
}

async function createSnapshot() {
    _toast("正在打包快照…");
    try {
        const resp = await fetch("/snapshot/create", { method: "POST" });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast(data.pushed ? "快照已保存到服务器（" + _fmtSize(data.size) + "）"
                               : "快照已保存到本机（" + _fmtSize(data.size) + "）"
                                 + (data.push_error ? "；" + data.push_error : ""));
            loadSnapshots();
        } else {
            _toast("快照失败：" + (data.error || ""));
        }
    } catch (e) { _toast("快照失败，请检查网络"); }
}

async function loadSnapshots() {
    const list = document.getElementById("snapshot-list");
    if (!list) return;
    try {
        const resp = await fetch("/snapshot/list");
        const data = await resp.json();
        const items = data.snapshots || [];
        if (!items.length) {
            list.innerHTML = '<div style="color:var(--fg-muted);font-size:0.75em">还没有快照，点「保存快照」存一份</div>';
            return;
        }
        list.innerHTML = items.map(s => `
        <div class="snapshot-row" data-name="${escapeHtml(s.name)}" style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.75em">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.name)}">${escapeHtml(s.time || s.name)}</span>
            <span style="color:var(--fg-muted);flex-shrink:0">${_fmtSize(s.size)}</span>
            <button type="button" class="sn-restore">恢复</button>
            <button type="button" class="sn-download">下载</button>
            <button type="button" class="sn-del">删除</button>
        </div>`).join("");
        list.querySelectorAll(".snapshot-row").forEach(row => {
            const name = row.dataset.name;
            row.querySelector(".sn-restore").addEventListener("click", () => restoreSnapshot(name));
            row.querySelector(".sn-del").addEventListener("click", () => deleteSnapshot(name));
            row.querySelector(".sn-download").addEventListener("click", () => {
                if (!IS_SERVER) {
                    window.location.href = `/snapshot/download?name=${encodeURIComponent(name)}`;
                } else {
                    fetch(`/snapshot/download?name=${encodeURIComponent(name)}`)
                        .then(r => r.blob()).then(blob => {
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement("a");
                            a.href = url; a.download = name;
                            document.body.appendChild(a); a.click();
                            setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
                        }).catch(() => _toast("下载失败"));
                }
            });
        });
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;font-size:0.75em">快照列表加载失败</div>';
    }
}

async function restoreSnapshot(name) {
    if (!confirm(`用快照「${name}」覆盖全部角色与聊天记录？\n恢复前会自动保存当前状态的快照。\n\n确定恢复吗？`)) return;
    _toast("正在恢复快照…");
    try {
        const resp = await fetch("/snapshot/restore", {
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

async function deleteSnapshot(name) {
    if (!confirm(`删除快照「${name}」？此操作不可撤销。`)) return;
    try {
        const resp = await fetch("/snapshot/delete", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast("快照已删除");
            loadSnapshots();
        } else {
            _toast("删除失败：" + (data.error || ""));
        }
    } catch (e) { _toast("删除失败，请检查网络"); }
}

document.getElementById("snapshot-create-btn")?.addEventListener("click", createSnapshot);
document.getElementById("snapshot-list") && loadSnapshots();


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
export let _mediaBusy = false;          // 媒体选择中（图片相册/表情面板）：暂停提交（长期等待）

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

export function _batchArmSubmit() {
    _lastMsgTs = Date.now();
    _pendingBatch++;
}

/** 媒体选择结束后：重新计时 5 秒（不增加批计数——未产生新消息）。 */
export function _batchRefreshTimer() {
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

export async function _chatSend(msgs) {
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
                // 与后端 polisher.DEGRADED_TEXT 保持一致（降级话术单一来源，2026-09-04 合并）
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
