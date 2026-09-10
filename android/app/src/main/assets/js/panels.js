// 面板族：菜单抽屉 / 各面板开关壳 / 头像选择 / 状态 / 收藏 / 请求记录 / 流程日志 / 用户记忆 / 设定文件 / 手账 / 表情包管理
import { _esc, escapeHtml, showToast, stickerSrc, idbSaveMedia } from "./util.js";
import { IS_SERVER, API_BASE } from "./api.js";
import { CURRENT_MODE } from "./views.js";
import { loadConfig } from "./settings.js";
import { S } from "./state.js";

// 开拓者头像
export const TB_AVATARS = { 穹: "开拓者_穹.png", 星: "开拓者_星.png" };
export let tbChoice = localStorage.getItem("tb_avatar") || "穹";

export function openAvatarPicker() {
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
export function openMenu() {
    menuDrawer.classList.add("open");
    menuOverlay.classList.add("show");
    // 默认 tab 是设定文件（DOM active），无点击事件，需主动加载
    loadCharFiles(); loadJournal(); loadUserMemory();
}
export function closeMenu() {
    menuDrawer.classList.remove("open");
    menuOverlay.classList.remove("show");
}
window.closeMenu = closeMenu;

// ═══════════════════════════════════════════
// 设置面板（首页 ⚙ 打开，API 配置独立于此）
// ═══════════════════════════════════════════
const settingsPanel = document.getElementById("settings-panel");
export function openSettings() {
    settingsPanel.classList.add("show");
    loadConfig();
    try { loadSnapshots(); } catch (e) {}   // 快照列表（chat.js 同作用域函数）
    try { window.btActivate && window.btActivate("mine"); } catch (e) {}
}
export function closeSettings() { settingsPanel.classList.remove("show"); }
window.openSettings = openSettings;
window.closeSettings = closeSettings;

// 反馈面板（首页 ✉ 打开）
const feedbackPanel = document.getElementById("feedback-panel");
function openFeedback() { feedbackPanel.classList.add("show"); }
export function closeFeedback() { feedbackPanel.classList.remove("show"); }
window.openFeedback = openFeedback;
window.closeFeedback = closeFeedback;

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
        if (btn.dataset.tab === "state") loadStateTab();
        if (btn.dataset.tab === "fav") loadFavorites();
        if (btn.dataset.tab === "log") loadRequestLog();
        if (btn.dataset.tab === "pipeline") loadPipeline();
    });
});

// ═══════════════════════════════════════════
// 状态 tab（数值状态系统已下线，接回后再渲染条）
// ═══════════════════════════════════════════

function loadStateTab() {
    const list = document.getElementById("state-list");
    if (list) {
        list.innerHTML = '<div style="color:#8a8a8a;line-height:1.6">状态系统尚未接入。<br>当前流水线：检索 → 分析 → 回复 → 表情包。</div>';
    }
}

// ═══════════════════════════════════════════
// 请求记录
// ═══════════════════════════════════════════
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

async function loadRequestLog() {
    const list = document.getElementById("log-list");
    const countEl = document.getElementById("log-count");
    if (!list) return;
    try {
        const resp = await fetch("/requests");
        const data = await resp.json();
        if (countEl) {
            const totalCost = (data.requests || []).reduce((s, r) => s + (Number(r.cost_cny) || 0), 0);
            countEl.textContent = totalCost > 0
                ? `共 ${data.count} 次请求 · 累计约 ¥${totalCost.toFixed(3)}`
                : `共 ${data.count} 次请求`;
        }
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
// 用户记忆（= memory.md，休息时自动整理的过往摘要）/ 用户设定（补充设定）
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
    fd.append("mode", CURRENT_MODE);   // 归属当前包（阶段6）
    try {
        const resp = await fetch("/add-sticker", { method: "POST", body: fd });
        const data = await resp.json();
        if (data.ok) {
            // A2 媒体本地策略（服务器版）：图片本体存本机 IndexedDB（key=内容哈希），
            // 服务器只保留文字元数据（label/category/哈希）；上传后立即本地化
            if (data.local && data.file && file instanceof Blob) {
                await idbSaveMedia(String(data.file).slice("local:".length), file);
                msg.textContent = "已添加：" + data.label + "（图片仅存本机）";
            } else {
                msg.textContent = "已添加：" + data.label;
            }
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
        // A2：缩略图异步解析（local: 引用 → IndexedDB；无图显示占位块）
        const rows = await Promise.all(stickers.map(async s => {
            const src = await stickerSrc(s.file, IS_SERVER, API_BASE);
            const thumb = src
                ? `<img class="stk-thumb" src="${escapeHtml(src)}" loading="lazy" onerror="this.style.opacity=0.2">`
                : `<div class="stk-thumb" style="display:flex;align-items:center;justify-content:center;opacity:0.35;font-size:0.6em">无图</div>`;
            return `
        <div class="sticker-row" data-id="${escapeHtml(s.id)}">
            <div class="stk-head">
                ${thumb}
                <button class="stk-toggle ${s.enabled ? "on" : ""}" data-on="${s.enabled ? "1" : ""}" ${(s.editable || s.is_default) ? "" : "disabled"}>${s.enabled ? "启用中" : "已停用"}</button>
            </div>${s.pack ? `<div style="font-size:0.62em;color:var(--fg-accent);margin-top:2px">专属：${escapeHtml(s.pack)}</div>` : ""}
            <div class="stk-main">
                <select class="stk-cat-sel" ${(s.editable || s.is_default) ? "" : "disabled"}>
                    <option value="可爱" ${s.category==="可爱"?"selected":""}>可爱</option>
                    <option value="帅气" ${s.category==="帅气"?"selected":""}>帅气</option>
                </select>
                <input class="stk-label-input" type="text" value="${escapeHtml(s.label)}" maxlength="120" ${(s.editable || s.is_default) ? "" : "readonly"}>
                <div class="stk-actions">
                    <button class="stk-save" disabled>保存</button>
                    <button class="stk-del" ${(s.is_default || !s.editable) ? "disabled" : ""}>删</button>
                </div>
            </div>
        </div>`;
        }));
        list.innerHTML = rows.join("");
        list.querySelectorAll(".sticker-row").forEach(row => {
            const id = row.dataset.id;
            const inp = row.querySelector(".stk-label-input");
            const cat = row.querySelector(".stk-cat-sel");
            const save = row.querySelector(".stk-save");
            const del = row.querySelector(".stk-del");
            const toggle = row.querySelector(".stk-toggle");
            const origLabel = inp.value;
            const origCat = cat.value;

            function checkChanged() {
                save.disabled = (inp.value.trim() === origLabel && cat.value === origCat) || (!inp.value.trim() && !cat.value);
            }
            inp.addEventListener("input", checkChanged);
            cat.addEventListener("change", checkChanged);

            toggle.addEventListener("click", async () => {
                const next = toggle.dataset.on !== "1";
                toggle.disabled = true;
                try {
                    const r = await fetch("/sticker-update", {
                        method:"POST",
                        headers:{"Content-Type":"application/json"},
                        body:JSON.stringify({id, enabled: next}),
                    });
                    const d = await r.json();
                    if (d.ok) {
                        toggle.dataset.on = next ? "1" : "";
                        toggle.classList.toggle("on", next);
                        toggle.textContent = next ? "启用中" : "已停用";
                        msg.textContent = next ? "已启用：" + d.label : "已停用：" + d.label;
                    }
                } catch(e) {}
                toggle.disabled = false;
            });

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
// 运行模式切换（A7c 已删除）：本地优先 + 后端失败自动回落服务器——无手动切换入口。
// 保留此注释防止旧代码/测试引用复活。
// ═══════════════════════════════════════════

// ═══════════════════════════════════════════
// 角色编辑页（全屏 #pack-view：封面横幅 + 大头像 + 人设文案编辑 + 自建角色删除）
// ═══════════════════════════════════════════
const _PACK_PROMPT_LABELS = {
    "core.md": "核心设定（身份/经历/价值观）",
    "identity.md": "人际关系与认知边界",
    "sms_samples.md": "短信风格示例",
    "prompts/polisher.md": "回复器人设（核心文案）",
    "prompts/analyzer_extra.md": "分析器·剧本事实核查补充",
    "prompts/organizer_sticker.md": "组织器·表情包调度提示词",
    "prompts/organizer_narration.md": "组织器·旁白生成提示词",
    "prompts/proactive_context.md": "主动消息·情境文案",
    "prompts/env_suffix.md": "环境句·世界后缀",
};

async function loadPackView() {
    let data;
    try {
        const resp = await fetch(`/pack-files?mode=${encodeURIComponent(CURRENT_MODE)}`);
        data = await resp.json();
    } catch (e) { showToast("加载失败（网络）"); return; }
    if (!data || !data.files) return;

    const presLabel = {sticker: "短信+表情包", narration: "短信+旁白", none: "纯短信"}[data.presentation] || data.presentation;
    const t = Date.now();
    const coverEl = document.getElementById("pv-cover");
    if (coverEl && data.assets && data.assets.cover) coverEl.src = data.assets.cover + "?t=" + t;
    const avatarEl = document.getElementById("pv-avatar");
    if (avatarEl && data.assets && data.assets.avatar) avatarEl.src = data.assets.avatar + "?t=" + t;
    document.getElementById("pv-name").textContent = "";
    document.getElementById("pv-scene").textContent = data.name || data.mode;
    document.getElementById("pv-pres").textContent = presLabel;
    document.getElementById("pv-tagline").textContent = "";
    // 角色名/签名从注册表取（经 views.js 的 window 桥——bundle 拼接后 import() 会产生第二份模块实例）
    try {
        const mods = (window.__getPresets && window.__getPresets()) || [];
        const p = mods.find(m => m.id === data.mode) || {};
        document.getElementById("pv-name").textContent = p.char_name || data.name || data.mode;
        document.getElementById("pv-tagline").textContent = p.tagline || "";
    } catch (e) {}
    // 封面/头像编辑按钮绑定
    document.getElementById("pv-cover-edit").onclick = () => _packAssetUpload("cover");
    document.getElementById("pv-avatar-edit").onclick = () => _packAssetUpload("avatar");

    // 主动消息区填值
    const pro = data.proactive || {};
    const setChk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    setChk("pv-pro-enabled", pro.enabled);
    setChk("pv-pro-prob", pro.pro_enabled);
    setChk("pv-pro-hidden", pro.hidden_enabled);
    // 后台主动消息的客户端门控跟随当前角色卡的「隐藏式」开关（服务端不含该判断）
    S._hiddenEnabled = !!pro.hidden_enabled;
    const hardEl = document.getElementById("pv-pro-hard");
    const softEl = document.getElementById("pv-pro-soft");
    if (hardEl) { hardEl.value = pro.hard ?? 6; document.getElementById("pv-pro-hard-v").textContent = hardEl.value; }
    if (softEl) { softEl.value = Math.round((pro.soft ?? 0.35) * 100); document.getElementById("pv-pro-soft-v").textContent = softEl.value + "%"; }

    _loadPackStickers();

    // 危险区：自建角色可删除；非自建显示恢复资产默认入口
    const danger = document.getElementById("pv-danger");
    danger.innerHTML = "";
    const tools = document.createElement("div");
    tools.style.cssText = "margin-top:16px;font-size:0.75em;color:var(--fg-muted);display:flex;gap:14px";
    for (const [slot, label] of [["avatar", "恢复头像默认"], ["cover", "恢复封面默认"]]) {
        const a = document.createElement("a");
        a.textContent = label;
        a.style.cssText = "cursor:pointer;text-decoration:underline";
        a.onclick = async () => {
            if (!confirm(`${label}？（删除你的修改）`)) return;
            try {
                await fetch("/pack-asset/delete", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({mode: CURRENT_MODE, slot})});
                showToast("已恢复默认");
                loadPackView();
                try { window.__modesReload && window.__modesReload(); } catch (e) {}
            } catch (e) { showToast("操作失败"); }
        };
        tools.appendChild(a);
    }
    danger.appendChild(tools);
    if (data.custom) {
        const delBtn = document.createElement("button");
        delBtn.className = "pv-danger-btn";
        delBtn.type = "button";
        delBtn.textContent = `删除角色「${data.name}」（含人设与聊天记录）`;
        delBtn.onclick = async () => {
            if (!confirm(`确定删除角色「${data.name}」？\n人设、记忆、聊天记录会一起删除，不可恢复。`)) return;
            if (!confirm("再确认一次：删除后无法恢复。确定删除？")) return;
            try {
                const r = await fetch("/pack-delete", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({id: data.mode})});
                const d = await r.json();
                if (d.ok) {
                    showToast("已删除");
                    await window.__modesReload();
                    try { window.closePackView(); } catch (e) {}
                } else {
                    showToast("删除失败：" + (d.error || ""));
                }
            } catch (e) { showToast("网络错误"); }
        };
        danger.appendChild(delBtn);
    }

    // 人设文案（可折叠编辑器：核心三件 + 用户设定 + 提示词六段，全部放权可编辑）
    const promptsBox = document.getElementById("pv-prompts");
    promptsBox.innerHTML = "";
    for (const f of data.files) {
        const det = document.createElement("details");
        const sum = document.createElement("summary");
        sum.textContent = (_PACK_PROMPT_LABELS[f.name] || f.name) + (f.customized ? "（已修改）" : "");
        const ta = document.createElement("textarea");
        ta.value = f.content || "";
        const btnRow = document.createElement("div");
        btnRow.className = "btn-row";
        btnRow.style.marginTop = "6px";
        const saveBtn = document.createElement("button");
        saveBtn.type = "button"; saveBtn.textContent = "保存";
        saveBtn.onclick = async () => {
            const content = ta.value;
            if (!content.trim()) { showToast("内容不能为空（要恢复默认请用下方小字）"); return; }
            try {
                const r = await fetch("/character-file-update", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({mode: CURRENT_MODE, filename: f.name, content})});
                const d = await r.json();
                showToast(d.ok ? "已保存（下轮对话生效）" : ("保存失败：" + (d.error || "")));
                if (d.ok) sum.textContent = (_PACK_PROMPT_LABELS[f.name] || f.name) + "（已修改）";
            } catch (e) { showToast("网络错误"); }
        };
        const rstBtn = document.createElement("button");
        rstBtn.type = "button"; rstBtn.textContent = "恢复默认";
        rstBtn.onclick = async () => {
            if (!confirm("恢复该文案为默认？（删除你的修改）")) return;
            try {
                await fetch("/character-file/delete", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({mode: CURRENT_MODE, filename: f.name})});
                showToast("已恢复默认");
                loadPackView();
            } catch (e) { showToast("操作失败"); }
        };
        btnRow.append(saveBtn, rstBtn);
        det.append(sum, ta, btnRow);
        promptsBox.appendChild(det);
    }
}
window.loadPackView = loadPackView;

// 主动消息区：400ms 防抖自动保存（与设置页同风格）
let _proSaveTimer = null;
function _proScheduleSave() {
    clearTimeout(_proSaveTimer);
    _proSaveTimer = setTimeout(async () => {
        const msg = document.getElementById("pv-pro-msg");
        const payload = {
            mode: CURRENT_MODE,
            enabled: document.getElementById("pv-pro-enabled").checked,
            hard: parseInt(document.getElementById("pv-pro-hard").value) || 6,
            soft: (parseInt(document.getElementById("pv-pro-soft").value) || 35) / 100,
            prob_enabled: document.getElementById("pv-pro-prob").checked,
            hidden_enabled: document.getElementById("pv-pro-hidden").checked,
        };
        try {
            const r = await fetch("/pack-config", {method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)});
            const d = await r.json();
            if (msg) msg.textContent = d.ok ? "已保存" : ("保存失败：" + (d.error || ""));
            if (d.ok) S._hiddenEnabled = payload.hidden_enabled;   // 后台主动门控即时跟随
        } catch (e) { if (msg) msg.textContent = "保存失败（网络）"; }
    }, 400);
}
for (const id of ["pv-pro-enabled", "pv-pro-prob", "pv-pro-hidden"]) {
    document.getElementById(id)?.addEventListener("change", _proScheduleSave);
}
document.getElementById("pv-pro-hard")?.addEventListener("input", () => {
    const el = document.getElementById("pv-pro-hard");
    document.getElementById("pv-pro-hard-v").textContent = el.value;
    _proScheduleSave();
});
document.getElementById("pv-pro-soft")?.addEventListener("input", () => {
    const el = document.getElementById("pv-pro-soft");
    document.getElementById("pv-pro-soft-v").textContent = el.value + "%";
    _proScheduleSave();
});

// ═══════════════════════════════════════════
// 详情页表情包区（本包可用 = 全局共享 + 本包专属；专属可增删启停）
// ═══════════════════════════════════════════
async function _loadPackStickers() {
    const grid = document.getElementById("pv-stk-grid");
    if (!grid) return;
    grid.innerHTML = "";
    let items = [];
    try {
        const resp = await fetch("/stickers");
        const data = await resp.json();
        items = Array.isArray(data.stickers) ? data.stickers : [];
    } catch (e) { return; }
    const usable = items.filter(s => !s.pack || s.pack === CURRENT_MODE);
    for (const s of usable) {
        const cell = document.createElement("div");
        cell.className = "pv-stk-item" + (s.enabled ? "" : " off");
        const img = document.createElement("img");
        try {
            const url = await stickerSrc(s.file, IS_SERVER, API_BASE);
            if (url) img.src = url;
        } catch (e) {}
        if (s.pack === CURRENT_MODE) {
            const pk = document.createElement("span");
            pk.className = "pk";
            pk.textContent = "专属";
            cell.appendChild(pk);
        }
        const lb = document.createElement("div");
        lb.className = "lb";
        lb.textContent = s.label || "";
        const ops = document.createElement("div");
        ops.className = "ops";
        const tgl = document.createElement("button");
        tgl.textContent = s.enabled ? "◐" : "○";
        tgl.title = s.enabled ? "停用" : "启用";
        tgl.onclick = async () => {
            try {
                await fetch("/sticker-update", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({id: s.id, enabled: !s.enabled})});
                _loadPackStickers();
            } catch (e) {}
        };
        ops.appendChild(tgl);
        if (s.pack === CURRENT_MODE && s.editable) {
            const del = document.createElement("button");
            del.textContent = "×";
            del.title = "删除（仅专属）";
            del.onclick = async () => {
                if (!confirm(`删除表情包「${s.label}」？`)) return;
                try {
                    await fetch("/sticker-delete", {method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({id: s.id})});
                    _loadPackStickers();
                } catch (e) {}
            };
            ops.appendChild(del);
        }
        cell.append(img, lb, ops);
        grid.appendChild(cell);
    }
    if (!usable.length) {
        const empty = document.createElement("div");
        empty.style.cssText = "color:var(--fg-muted);font-size:0.78em;grid-column:1/-1";
        empty.textContent = "还没有表情包——点下方按钮给这个角色添加第一张。";
        grid.appendChild(empty);
    }
}

// 详情页表情包添加（自动归属当前包）
document.getElementById("pv-stk-add")?.addEventListener("click", () => {
    const f = document.getElementById("pv-stk-addform");
    if (f) f.style.display = f.style.display === "none" ? "block" : "none";
});
document.getElementById("pv-stk-submit")?.addEventListener("click", async () => {
    const file = document.getElementById("pv-stk-file").files[0];
    const category = document.getElementById("pv-stk-category").value;
    const label = document.getElementById("pv-stk-label").value.trim();
    const msg = document.getElementById("pv-stk-msg");
    if (!file) { msg.textContent = "请先选择图片"; return; }
    if (!label) { msg.textContent = "请填写含义描述"; return; }
    const fd = new FormData();
    fd.append("file", file);
    fd.append("category", category);
    fd.append("label", label);
    fd.append("mode", CURRENT_MODE);   // 归属当前包
    try {
        const resp = await fetch("/add-sticker", {method: "POST", body: fd});
        const data = await resp.json();
        if (data.ok) {
            if (data.local && data.file && file instanceof Blob) {
                try { await idbSaveMedia(String(data.file).slice("local:".length), file); } catch (e) {}
            }
            msg.textContent = "已添加";
            document.getElementById("pv-stk-label").value = "";
            document.getElementById("pv-stk-file").value = "";
            _loadPackStickers();
        } else {
            msg.textContent = "失败：" + (data.error || "");
        }
    } catch (e) { msg.textContent = "网络错误"; }
});

// 头像/封面上传（复用图片压缩，选文件后上传为包资产）
function _packAssetUpload(slot) {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "image/png,image/jpeg,image/webp";
    inp.onchange = async () => {
        const f = inp.files && inp.files[0];
        if (!f) return;
        if (f.size > 5 * 1024 * 1024) { showToast("图片过大（上限 5MB）"); return; }
        try {
            const fd = new FormData();
            fd.append("mode", CURRENT_MODE);
            fd.append("slot", slot);
            fd.append("file", f, f.name || (slot + ".png"));
            const r = await fetch("/pack-asset", {method: "POST", body: fd});
            const d = await r.json();
            if (d.ok) {
                showToast("已替换");
                loadPackView();
                // 封面/头像换了：模式卡片与品牌标识刷新（重拉注册表资产 URL）
                try { window.__modesReload && window.__modesReload(); } catch (e) {}
            } else {
                showToast("上传失败：" + (d.error || ""));
            }
        } catch (e) { showToast("网络错误"); }
    };
    inp.click();
}
