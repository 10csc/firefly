// 角色包面板：表情包管理 + 包详情页（人设文案 / 主动消息 / 专属表情包 / 头像封面）
// 阶段 2.5 自 panels.js 拆出。包详情页的写回一律用 _packViewMode（F-3 代际保护）。

import { API_BASE, IS_SERVER } from "./api.js";
import { S } from "./state.js";
import { escapeHtml, idbSaveMedia, showToast, stickerSrc } from "./util.js";
import { uiSelectEnhance } from "./ui_select.js";
import { loadPackTree } from "./pack_tree.js";
import { TB_AVATARS, tbChoice } from "../panels.js";
import { CURRENT_MODE } from "./views.js";


// （A7c 运行模式切换已删除：本地优先 + 失败自动回落服务器——保留此注记防旧代码复活）

// ═══ 角色编辑页（全屏 #pack-view：封面横幅 + 大头像 + 人设文案编辑 + 自建角色删除）═══
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
// - _packViewMode：本页展示的是哪个包。所有写回一律用它，**不读实时的 CURRENT_MODE** ——
//   原来 A→B 快速切换后，A 页残留的"保存"会把 A 的文案写进 B。
// - _packViewGen：进入本页时的模式代际，await 后模式已切换就丢弃本次结果。
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
    // F-5：无封面/头像的包必须清掉旧 src——否则详情页沿用上一个包的图（"看起来还是流萤"）
    if (coverEl) {
        if (data.assets && data.assets.cover) coverEl.src = data.assets.cover + "?t=" + t;
        else coverEl.removeAttribute("src");
    }
    const avatarEl = document.getElementById("pv-avatar");
    if (avatarEl) {
        if (data.assets && data.assets.avatar) avatarEl.src = data.assets.avatar + "?t=" + t;
        else avatarEl.removeAttribute("src");
    }
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

    // 用户形象区（05）：称呼 + 用户头像，数据来自 /modes（经 __getPresets 桥）
    try {
        const mods = (window.__getPresets && window.__getPresets()) || [];
        const pm = mods.find(m => m.id === mode) || {};
        const nameInp = document.getElementById("pv-user-name");
        if (nameInp) nameInp.value = pm.user_name || "";
        const uav = document.getElementById("pv-user-avatar");
        if (uav) {
            // 预览回落链：包头像 → 全局内置形象（穹/星）——空 src 会显示破图+alt，必须给兜底
            const tbNow = (() => { try { return localStorage.getItem("tb_avatar") || "穹"; } catch (e) { return "穹"; } })();
            uav.src = pm.user_avatar ? pm.user_avatar + "?t=" + t : TB_AVATARS[tbNow];
            document.querySelectorAll(".pv-id-choice").forEach(x => x.classList.toggle("on", !pm.user_avatar && x.dataset.key === tbNow));
        }
        const resetLink = document.getElementById("pv-user-avatar-reset");
        if (resetLink) resetLink.style.display = pm.user_avatar ? "" : "none";
    } catch (e) {}
    try { loadPackTree(mode); } catch (e) {}   // 角色卡管理树（数据驱动，2026-09-15）

    // 角色形象「恢复默认」—— 2026-09-19 从"危险区"挪到顶部大图旁。
    // 理由：它只是撤销用户自己换的图，**不是危险操作**；而官方包又不能删除，
    // 于是"危险区"对官方包既没内容也没存在理由。形象相关的操作现在全在顶部一处。
    for (const [slot, elId, label] of [["avatar", "pv-asset-reset-avatar", "恢复默认头像"],
                                       ["cover", "pv-asset-reset-cover", "恢复默认封面"]]) {
        const a = document.getElementById(elId);
        if (!a) continue;
        a.onclick = async () => {
            if (!confirm(`${label}？（删除你换的图，回落到角色卡自带的）`)) return;
            try {
                await fetch("/pack-asset/delete", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({mode, slot})});
                showToast("已恢复默认");
                loadPackView();
                try { window.__modesReload && window.__modesReload(); } catch (e) {}
            } catch (e) { showToast("操作失败"); }
        };
    }

    // 危险区（2026-09-19 重排）：
    //   · 「恢复头像默认 / 恢复封面默认」**不是危险操作**，已挪到顶部大图旁（.pv-asset-ops）；
    //   · 官方（内置）包不能归档/删除 ⇒ 这个区对它们**完全不渲染**（见 pack_tree.js 的 danger 分支），
    //     所以这里只在自建包时才会有内容。
    const danger = document.getElementById("pv-danger");
    danger.innerHTML = "";
    if (data.custom) {
        // 活跃包的危险区主按钮 = 「归档」（3.5 一级：不删数据，可反悔）。
        // 「彻底删除」**只在首页归档区**（views.js 的 _renderArchivedPacks）——那是二级操作，
        // 需要输入包名确认，且后端强制先打一份快照。放在这里当主按钮会诱导手滑。
        if ((data.state || "active") === "archived") {
            const note = document.createElement("div");
            note.style.cssText = "font-size:0.8em;color:var(--fg-muted)";
            note.textContent = "该包已归档：请在首页「已归档」区选择恢复或彻底删除。";
            danger.appendChild(note);
        } else {
            const archBtn = document.createElement("button");
            archBtn.className = "pv-danger-btn";
            archBtn.type = "button";
            archBtn.textContent = `归档「${data.name}」（数据保留，可随时恢复）`;
            archBtn.onclick = async () => {
                // 动作实现与首页归档区共用（window.__packLifecycle，见 views.js）
                if (!await window.__packLifecycle("archive", data.mode, data.name || data.mode)) return;
                try { window.closePackView(); } catch (e) {}
                await window.__modesReload();   // 重载后会切到合法包（归档包已不在清单里）
            };
            danger.appendChild(archBtn);
        }
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
function _packAssetUpload(slot) {    const inp = document.createElement("input");
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

// ═══ 用户形象区接线（05：每包独立的用户称呼 + 头像）═══
document.getElementById("pv-user-name-save")?.addEventListener("click", async () => {
    const msg = document.getElementById("pv-user-msg");
    const v = document.getElementById("pv-user-name").value.trim();
    try {
        const r = await fetch("/pack-config", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _packViewMode || CURRENT_MODE, user_name: v})});
        const d = await r.json();
        if (d.ok) {
            if (msg) msg.textContent = "已保存（称呼：" + (d.user_name || "默认") + "）";
            try { window.__modesReload && window.__modesReload(); } catch (e) {}   // /modes 携带新称呼
        } else if (msg) msg.textContent = "保存失败：" + (d.error || "");
    } catch (e) { if (msg) msg.textContent = "网络错误"; }
});
document.getElementById("pv-user-avatar-edit")?.addEventListener("click", () => _packAssetUpload("user_avatar"));
document.getElementById("pv-user-avatar-reset")?.addEventListener("click", async () => {
    if (!confirm("恢复默认用户头像？（删除你上传的头像，回落到内置形象）")) return;
    try {
        const r = await fetch("/pack-asset/delete", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _packViewMode || CURRENT_MODE, slot: "user_avatar"})});
        const d = await r.json();
        if (d.ok) { showToast("已恢复默认"); loadPackView(); try { window.__modesReload && window.__modesReload(); } catch (e) {} }
        else showToast(d.error || "操作失败");
    } catch (e) { showToast("网络错误"); }
});

// 用户形象区：内置形象选择（穹/星，无包自定义头像时生效；复用全局 TB 选择存储）
document.querySelectorAll(".pv-id-choice").forEach(el => {
    el.addEventListener("click", () => {
        try {
            localStorage.setItem("tb_avatar", el.dataset.key);
            document.querySelectorAll(".tb-avatar").forEach(x => { x.src = TB_AVATARS[el.dataset.key]; });
        } catch (e) {}
        loadPackView();   // 预览刷新（无包头像时显示内置选择）
    });
});
