// 逐文件 AI 辅助抽屉（阶段 E）：✨ 图标 → 与 AI 对话 → diff 提案 → 应用才写盘
// AI 永不直接写盘；提案经后端静态校验 + 备份 + 原子写（见 modules/pack_assist.py 边界表）。
import { escapeHtml, showToast } from "../util.js";
import { CURRENT_MODE } from "../views.js";

let _av = { mode: "", file: "", history: [], pending: null };

export function openAssist(mode, file, label) {
    _av = { mode: mode || CURRENT_MODE, file, history: [], pending: null };
    const v = document.getElementById("assist-view");
    if (!v) return;
    document.getElementById("av-title").textContent = `✨ AI 辅助 · ${label || file}`;
    document.getElementById("av-chat").innerHTML =
        `<div class="fix-hist">说说想把这个文件改成什么样，AI 会给出具体修改提案（应用前一定会给你看 diff）。</div>`;
    document.getElementById("av-proposal").style.display = "none";
    document.getElementById("av-input").value = "";
    v.style.display = "flex";
    setTimeout(() => document.getElementById("av-input").focus(), 200);
}
window.__openAssist = openAssist;

export function closeAssist() {
    const v = document.getElementById("assist-view");
    if (v) v.style.display = "none";
}
window.closeAssist = closeAssist;

function _avMsg(who, text) {
    const chat = document.getElementById("av-chat");
    chat.insertAdjacentHTML("beforeend",
        `<div class="fix-msg ${who === "user" ? "me" : "ai"}"><div class="fix-who">${who === "user" ? "我" : "AI"}</div><div class="fix-text">${escapeHtml(text)}</div></div>`);
    chat.scrollTop = chat.scrollHeight;
}

function _renderProposal(changes) {
    const box = document.getElementById("av-proposal");
    box.style.display = "block";
    box.innerHTML = `<div class="fix-proposal-title"><span>修改提案（尚未生效）</span><span class="fix-op-tag">待确认</span></div>`
        + changes.map(c => `<div class="fix-change"><div class="fix-change-head">
            <span class="fix-op-tag ${c.op === "append" ? "add" : "fix"}">${c.op === "append" ? "补充" : "纠正"}</span></div>
            ${c.op === "replace" ? `<div class="fix-diff-old">− ${escapeHtml(c.old || "")}</div>` : ""}
            <div class="fix-diff-new">+ ${escapeHtml(c.new || "")}</div>
            <div class="fix-reason">${escapeHtml(c.reason || "")}</div></div>`).join("")
        + `<div class="fix-proposal-actions">
             <button class="hb-btn" type="button" id="av-discard">放弃</button>
             <button class="hb-btn primary" type="button" id="av-apply">应用修改</button>
           </div>`;
    document.getElementById("av-discard").onclick = () => {
        _av.pending = null;
        box.style.display = "none";
        _avMsg("ai", "已放弃这次提案。继续说想怎么改就行。");
    };
    document.getElementById("av-apply").onclick = _applyPending;
}

async function _applyPending() {
    if (!_av.pending) return;
    const changes = _av.pending;
    try {
        const r = await fetch("/pack-assist/apply", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _av.mode, file: _av.file, changes})});
        const d = await r.json();
        if (d.ok) {
            showToast("已应用（写入前有自动备份）");
            document.getElementById("av-proposal").style.display = "none";
            _av.pending = null;
            _avMsg("ai", "已写入并生效。还要改别的地方吗？");
            // 让背后的编辑页刷新（详情页/知识库列表/记忆区各自的重载函数都在）
            try { window.loadPackView && window.loadPackView(); } catch (e) {}
        } else {
            _avMsg("ai", "校验没通过：" + (d.error || ""));
        }
    } catch (e) { showToast("网络错误"); }
}

async function _avSend() {
    const inp = document.getElementById("av-input");
    const text = (inp.value || "").trim();
    if (!text) return;
    inp.value = "";
    _avMsg("user", text);
    _av.history.push({who: "user", text});
    _avMsg("ai", "（正在想…）");
    try {
        const r = await fetch("/pack-assist", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _av.mode, file: _av.file, message: text, history: _av.history})});
        const d = await r.json();
        // 摘掉"正在想"
        const chat = document.getElementById("av-chat");
        const last = chat.querySelectorAll(".fix-msg.ai");
        if (last.length) last[last.length - 1].remove();
        if (d.need_key) { _avMsg("ai", "需要先去 ⚙ 设置里填 API Key。"); return; }
        if (!d.ok) { _avMsg("ai", "出了点问题：" + (d.error || "")); return; }
        _av.history.push({who: "ai", text: d.reply});
        _avMsg("ai", (d.searched ? "🌐 已联网检索官方资料\n\n" : "") + d.reply);
        if (Array.isArray(d.changes) && d.changes.length) {
            _av.pending = d.changes;
            _renderProposal(d.changes);
        }
    } catch (e) {
        const chat = document.getElementById("av-chat");
        const last = chat.querySelectorAll(".fix-msg.ai");
        if (last.length) last[last.length - 1].remove();
        _avMsg("ai", "网络错误，稍后再试");
    }
}

document.getElementById("av-send")?.addEventListener("click", _avSend);
document.getElementById("av-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); _avSend(); }
});
document.getElementById("av-back")?.addEventListener("click", closeAssist);
