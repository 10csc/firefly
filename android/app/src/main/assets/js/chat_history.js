// 聊天历史：历史加载（滚动翻页）/ 休息 / 清除 / 撤回
import { S, SESSION_ID, messagesEl } from "./state.js";
import { closeMenu } from "./panels.js";
import { addImage, addNarration, addSticker, addTextMessage, addTimeDivider } from "./chat_render.js";
import { CURRENT_MODE, _modeGen } from "./views.js";

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
