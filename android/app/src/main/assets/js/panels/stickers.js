// 表情包管理（菜单抽屉「表情包」页签：添加 / 映射表 / 启停 / 删除）
// 阶段 B（2026-09-15）自 panels/packs.js 拆出：这段属菜单域，与角色包详情页无关。
import { API_BASE, IS_SERVER } from "../api.js";
import { escapeHtml, idbSaveMedia, stickerSrc } from "../util.js";
import { uiSelectEnhance } from "../ui_select.js";
import { CURRENT_MODE } from "../views.js";

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
        try { uiSelectEnhance(list); } catch (e) {}   // 动态生成的分类下拉也走自绘（守卫幂等）
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
