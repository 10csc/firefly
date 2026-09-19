// 资料面板：状态 / 收藏 / 用户记忆 / 设定文件 / 手账（阶段 2.5 自 panels.js 拆出）
// 只注册监听 + 挂 window.*（菜单 tab 切换在外壳 panels.js 里按 tab 调用这些 loader）。

import { escapeHtml, showToast } from "./util.js";
import { CURRENT_MODE, charName } from "./views.js";
import { uiSelectEnhance } from "../ui_select.js";

function loadStateTab() {
    const list = document.getElementById("state-list");
    if (list) {
        list.innerHTML = '<div style="color:#8a8a8a;line-height:1.6">状态系统尚未接入。<br>当前流水线：检索 → 分析 → 回复 → 表情包。</div>';
    }
}

// ═══════════════════════════════════════════
// 收藏（长按消息 → 收藏；菜单 → 收藏 查看）
// ═══════════════════════════════════════════
async function loadFavorites() {
    const list = document.getElementById("fav-list");
    const count = document.getElementById("fav-count");
    if (!list) return;
    try {
        const resp = await fetch(`/favorites?mode=${encodeURIComponent(CURRENT_MODE)}`);
        const data = await resp.json();
        const items = Array.isArray(data.items) ? data.items : [];
        if (count) count.textContent = `收藏 ${items.length} 条`;
        if (!items.length) {
            list.innerHTML = `<div class="fav-empty">还没有收藏。<br>在聊天页长按一条消息，点「收藏」即可保存到这里。</div>`;
            return;
        }
        list.innerHTML = items.map(f => {
            const body = f.type === "sticker"
                ? "[表情包：" + escapeHtml(f.label || "") + "]"
                : f.type === "narration"
                    ? escapeHtml(f.text || "")
                    : escapeHtml(f.content || "");
            const who = f.who === "user" ? "我" : escapeHtml(charName());
            return `<div class="fav-item">
                <div class="fav-head"><span class="fav-who">${who}</span><span class="fav-time">${escapeHtml((f.time || "").slice(5, 16))}</span></div>
                <div class="fav-body">${body}</div>
                <button class="fav-del" type="button" data-id="${escapeHtml(String(f.id))}">删除</button>
            </div>`;
        }).join("");
        list.querySelectorAll(".fav-del").forEach(btn => {
            btn.addEventListener("click", async () => {
                try {
                    const resp = await fetch("/favorites/delete", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({mode: CURRENT_MODE, id: btn.dataset.id}),
                    });
                    const d = await resp.json();
                    if (d.ok) loadFavorites();
                    else showToast("删除失败：" + (d.error || ""));
                } catch (e) { showToast("删除失败，请重试"); }
            });
        });
    } catch (e) {
        if (count) count.textContent = "读取失败";
        list.innerHTML = `<div class="fav-empty">收藏读取失败（服务器模式需先登录；本地后端未就绪时也会这样）</div>`;
    }
}
window.loadFavorites = loadFavorites;

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

// ── 历史对话存档（整理后的原文；只进检索器）────────────────────────
// 用户 2026-09-18 口径：整理搬走的那段**原文**要看得见、改得动，
// 也能手动让它「AI 压缩」进「用户记忆」。存/读都走 /archive 与 /memory-action。
//
// （顺带说明：「用户设定」编辑器已从本页移除 —— 它是**用户手写的设定**、不是聊天产物，
//   与「清除历史会清空本页」的语义冲突；现统一在「角色卡管理 → 人设与口吻」编辑。）
async function loadArchive(month) {
    const sel = document.getElementById("archive-month");
    const editor = document.getElementById("archive-editor");
    const msg = document.getElementById("archive-msg");
    if (!editor) return;
    const m = month || (sel && sel.value) || "";
    try {
        const url = `/archive?mode=${encodeURIComponent(CURRENT_MODE)}` + (m ? `&month=${encodeURIComponent(m)}` : "");
        const data = await (await fetch(url)).json();
        if (sel) {
            const months = data.months || [];
            const keep = months.includes(m) ? m : (months[0] || "");
            sel.innerHTML = months.map(x => `<option value="${x}">${x}</option>`).join("");
            sel.value = keep;
            // 自绘下拉：options 被重写后要让它重读（否则弹层还是旧的）
            try {
                if (sel._uiSelectSync) sel._uiSelectSync();
                else uiSelectEnhance(document.getElementById("tab-char"));
            } catch (e) {}
        }
        editor.value = data.content || "";
        if (msg) msg.textContent = data.content ? `${data.content.length} 字` : "（还没有存档）";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}

async function _archiveAction(action, extra) {
    const msg = document.getElementById("archive-msg");
    if (msg) msg.textContent = action === "compress" ? "压缩中…（要调一次模型，稍等）" : "保存中…";
    try {
        const resp = await fetch("/memory-action", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(Object.assign({action: action, mode: CURRENT_MODE}, extra || {})),
        });
        const data = await resp.json();
        if (!data.ok) { if (msg) msg.textContent = "失败：" + (data.error || "未知"); return; }
        if (action === "compress") {
            if (msg) msg.textContent = `✓ 已压缩进「用户记忆」（新增 ${data.added || 0} 条，头部 ${data.head_chars || 0} 字）`;
            if (typeof loadUserMemory === "function") loadUserMemory();   // 摘要变了，顺手刷新上面那块
        } else if (msg) {
            msg.textContent = "✓ 已保存（检索器下次就用改后的原文）";
        }
    } catch (e) { if (msg) msg.textContent = "网络错误"; }
}

(function _wireArchive() {
    const sel = document.getElementById("archive-month");
    const save = document.getElementById("archive-save");
    const reload = document.getElementById("archive-reload");
    const comp = document.getElementById("archive-compress");
    if (save) save.addEventListener("click", () => {
        const editor = document.getElementById("archive-editor");
        _archiveAction("save_archive", {
            month: (sel && sel.value) || "",
            content: editor ? editor.value : "",
        });
    });
    if (reload) reload.addEventListener("click", () => loadArchive());
    if (sel) sel.addEventListener("change", () => loadArchive(sel.value));
    if (comp) comp.addEventListener("click", () => {
        const month = (sel && sel.value) || "";
        if (!month) { const m = document.getElementById("archive-msg"); if (m) m.textContent = "还没有存档可压缩"; return; }
        if (!confirm(`把 ${month} 的存档原文压缩进「用户记忆」？\n（原文会保留，可以反复压；压缩会调用一次模型）`)) return;
        _archiveAction("compress", {month: month});
    });
})();
window.loadArchive = loadArchive;

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
