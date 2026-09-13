// 角色包面板：表情包管理 + 包详情页（人设文案 / 主动消息 / 专属表情包 / 头像封面）
// 阶段 2.5 自 panels.js 拆出。包详情页的写回一律用 _packViewMode（F-3 代际保护）。

import { API_BASE, IS_SERVER } from "./api.js";
import { S } from "./state.js";
import { escapeHtml, idbSaveMedia, showToast, stickerSrc } from "./util.js";
import { CURRENT_MODE } from "./views.js";

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

// F-3（2026-09-13）包详情页的两个捕获量：
// - _packViewMode：本页展示的是哪个包。所有写回（保存文案 / 换资产 / 恢复默认 / 加表情包 /
//   主动消息配置）一律用它，**不读实时的 CURRENT_MODE** —— 原来 A→B 快速切换后，
//   A 页残留的"保存"会把 A 的文案写进 B（请求发出时 CURRENT_MODE 已经是 B）。
// - _packViewGen：进入本页时的模式代际。await 之后一旦模式已切换就丢弃本次结果，
//   不把 A 的数据渲染进 B 的页面（范式与 chat_history.loadHistory 一致）。
let _packViewMode = "";
let _packViewGen = -1;

async function loadPackView() {
    const mode = CURRENT_MODE;      // 捕获：本次渲染/写回都属于这个包
    const gen = _modeGen;
    _packViewMode = mode;
    _packViewGen = gen;
    let data;
    try {
        const resp = await fetch(`/pack-files?mode=${encodeURIComponent(mode)}`);
        data = await resp.json();
    } catch (e) { showToast("加载失败（网络）"); return; }
    if (gen !== _modeGen) return;   // 模式已切换：丢弃本次结果，防止 A 的内容渲染进 B 的页面
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

    _loadPackStickers(mode);

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
                    body: JSON.stringify({mode, slot})});
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
                    body: JSON.stringify({mode, filename: f.name, content})});
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
                    body: JSON.stringify({mode, filename: f.name})});
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
            // F-3：写回"本页展示的包"，不用实时 CURRENT_MODE（切换后定时器仍会烧到新包上）
            mode: _packViewMode || CURRENT_MODE,
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
async function _loadPackStickers(mode = _packViewMode || CURRENT_MODE) {
    const gen = _modeGen;
    const grid = document.getElementById("pv-stk-grid");
    if (!grid) return;
    grid.innerHTML = "";
    let items = [];
    try {
        const resp = await fetch("/stickers");
        const data = await resp.json();
        items = Array.isArray(data.stickers) ? data.stickers : [];
    } catch (e) { return; }
    if (gen !== _modeGen) return;   // 模式已切换：不把 A 的表情包渲染进 B 的格子
    const usable = items.filter(s => !s.pack || s.pack === mode);
    for (const s of usable) {
        if (gen !== _modeGen) return;
        const cell = document.createElement("div");
        cell.className = "pv-stk-item" + (s.enabled ? "" : " off");
        const img = document.createElement("img");
        try {
            const url = await stickerSrc(s.file, IS_SERVER, API_BASE);
            if (url) img.src = url;
        } catch (e) {}
        if (s.pack === mode) {
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
                _loadPackStickers(mode);
            } catch (e) {}
        };
        ops.appendChild(tgl);
        if (s.pack === mode && s.editable) {
            const del = document.createElement("button");
            del.textContent = "×";
            del.title = "删除（仅专属）";
            del.onclick = async () => {
                if (!confirm(`删除表情包「${s.label}」？`)) return;
                try {
                    await fetch("/sticker-delete", {method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({id: s.id})});
                    _loadPackStickers(mode);
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
    fd.append("mode", _packViewMode || CURRENT_MODE);   // 归属"本页展示的包"（F-3）
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
            _loadPackStickers(_packViewMode || CURRENT_MODE);
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
            fd.append("mode", _packViewMode || CURRENT_MODE);   // 本页展示的包（F-3）
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
