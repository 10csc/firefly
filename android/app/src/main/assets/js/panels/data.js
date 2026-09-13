// 资料面板：状态 / 收藏 / 用户记忆 / 设定文件 / 手账（阶段 2.5 自 panels.js 拆出）
// 只注册监听 + 挂 window.*（菜单 tab 切换在外壳 panels.js 里按 tab 调用这些 loader）。

import { escapeHtml, showToast } from "./util.js";
import { CURRENT_MODE } from "./views.js";

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
            const who = f.who === "user" ? "我" : "流萤";
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

// 用户设定（补充剧情设定）
async function loadCharFiles() {
    const msg = document.getElementById("char-file-msg");
    try {
        const resp = await fetch(`/character-files?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        const byName = {};
        (data.files || []).forEach(f => { byName[f.name] = f.content; });
        const us = document.getElementById("user-setting-editor");
        if (us) us.value = byName["用户设定.md"] || "";   // ?? 为 ES2020（Chrome 80+），安卓 8.0 WebView 解析期 SyntaxError 全站失效，改用 ||（此处语义等价）
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
