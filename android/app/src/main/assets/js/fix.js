// 设定纠错助手视图
import { SESSION_ID, sendBtn } from "./state.js";
import { escapeHtml, showToast } from "./util.js";
import { openSettings } from "./panels.js";
import { MODE_NAMES, appView, homeView, showHome } from "./views.js";

// ═══════════════════════════════════════════
// 设定纠错助手（对齐 → 开始修改 → diff 审批 → 应用/回滚）
// ═══════════════════════════════════════════
const FIX_FILE_LABELS = {
    "core.md": "核心设定", "identity.md": "关系与习惯", "sms_samples.md": "短信风格",
    "用户设定.md": "用户补充设定", "memory.md": "过往摘要", "手账.md": "流萤手账",
};
let FIX_MODE = "story";   // 首页卡片选择的模式；进入聊天后跟随最近使用模式
let _fixBusy = false;


function fixModeLabel(mode) { return MODE_NAMES[mode] || mode; }

export function openFixView() {
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

export async function loadFixStatus(force) {
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
            // A3：LLM 错误分类 → 人话提示（与聊天页 ERROR_TIPS 同口径）
            const FIX_ERROR_TIPS = {
                key_invalid: "API Key 无效或已过期，请到设置中检查",
                no_balance: "API 余额不足，请充值后再试",
                rate_limit: "请求太频繁，稍等一会儿再试试",
                network: "网络不通，请检查网络后重试",
                server_error: "服务端暂时出错，请稍后再试",
                relay_timeout: "代发超时，请检查网络后重试",
                timeout: "回复超时了，稍后再试一次吧",
                cooldown: "上游服务波动中，休息一下再试试",
                quota_exhausted: "今日服务器托管额度已用完，可切换为自带 Key 模式",
                unknown: "出了点问题，请稍后再试",
            };
            showToast(data.error_code
                ? (FIX_ERROR_TIPS[data.error_code] || FIX_ERROR_TIPS.unknown)
                : (data.error || "分析失败，请稍后再试"));
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
