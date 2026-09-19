// 角色卡管理树（数据驱动 · 2026-09-15）
// 数据源 = GET /pack-structure（core/pack_structure.py 的声明式规范）——
// 本模块只按返回的域/来源渲染树：可收起可展开，文件点击**就地展开**编辑（不跳页底）。
// 业务结构由后端规范决定；新域/新槽位出现后，本模块自动跟随，无需改代码。
import { escapeHtml, showToast } from "../util.js";
import { CURRENT_MODE } from "../views.js";

let _treeMode = "";

export async function loadPackTree(mode) {
    _treeMode = mode || CURRENT_MODE;
    const box = document.getElementById("pv-tree");
    if (!box) return;
    try {
        const resp = await fetch(`/pack-structure?mode=${encodeURIComponent(_treeMode)}`);
        const data = await resp.json();
        if (!data.ok) { box.innerHTML = "结构读取失败"; return; }
        _render(box, data);
    } catch (e) {
        box.innerHTML = "结构读取失败（网络）";
    }
}

function _render(box, data) {
    _parkEmbeds();   // 先把嵌入块泊出——innerHTML 清空会销毁树内一切（含事件接线）
    box.innerHTML = "";
    _domainLabels = {};
    for (const d of data.domains) _domainLabels[d.key] = d.label;
    for (const d of data.domains) {
        // ★ 2026-09-19：两个域整体不渲染（用户真机反馈）——
        //   · `assets`（形象资产）：功能早已挪到顶部大图，这个域只剩一句说明，是空壳；
        //   · `danger`（危险区）：官方包不能归档/删除 ⇒ 没有任何危险操作；而原来给官方包
        //     塞的"恢复头像/封面默认"根本不是危险操作（已挪到顶部大图旁）。
        //   两者都该"没有内容就没有这个区"，而不是留一个空的折叠条。
        if (d.key === "assets") continue;
        if (d.key === "danger" && !data.custom) continue;
        const det = document.createElement("details");
        det.className = "pt-domain";
        det.dataset.key = d.key;
        const sum = document.createElement("summary");
        sum.innerHTML = `<b>${escapeHtml(d.label)}</b> <span class="pt-desc">${escapeHtml(d.desc || "")}</span>`;
        det.appendChild(sum);
        for (const s of d.sources) {
            // 节点可能"故意不渲染"（见 _renderSource 的 assets / 官方包 danger 分支）——
            // appendChild(null) 会抛错，所以这里必须判空。
            const node = _renderSource(d, s, data);
            if (node) det.appendChild(node);
        }
        box.appendChild(det);
    }
}

// ── 嵌入块车位 ──
// 树重渲 = #pv-tree innerHTML 清空。被 _moveInto 搬进树的既有区块（用户形象/主动消息/
// 表情包/危险区）若不清空前泊出，会随清空被销毁：二次渲染后这些域全空，
// 且模块级事件接线（防抖保存/上传/称呼保存）随元素死亡。车位 = display:none 的隐藏容器，
// 泊入其中元素存活、getElementById 可达，渲染后再搬回新节点。
const _EMBED_GETTERS = {
    identity: () => document.getElementById("pv-identity"),
    config: () => document.querySelector(".pv-proactive"),
    stickers: () => document.querySelector(".pv-stickers"),
    danger: () => document.getElementById("pv-danger"),
};
let _embedParking = null;
let _domainLabels = {};

function _parkEmbeds() {
    if (!_embedParking) {
        _embedParking = document.createElement("div");
        _embedParking.style.display = "none";
        document.body.appendChild(_embedParking);
    }
    for (const get of Object.values(_EMBED_GETTERS)) {
        const el = get();
        if (el) _embedParking.appendChild(el);
    }
}

function _renderSource(domain, s, data) {
    const t = s.type;
    if (t === "slot" || t === "file") return _fileNode(s, data);
    if (t === "dir") return _kbNode(s);
    if (t === "sys_prompt") return _sysPromptNode(s);
    if (t === "ref") return _refNode(s);
    // ★ 2026-09-19：`assets` 节点不再渲染 —— 它只剩一句"去顶部大图换图"的说明，
    //   留一个空折叠区纯属噪音（用户："形象自从在上面改就不要在下面放个空的"）。
    //   角色形象的所有操作（换封面/换头像/恢复默认）现在都在顶部大图区。
    if (t === "assets") return null;
    // ★ 2026-09-19：危险区**只对自建包**渲染。
    //   · 官方（内置）包不能归档、不能删除 ⇒ 里面没有任何"危险"操作；
    //   · 原来给官方包塞的"恢复头像/封面默认"根本不是危险操作，已挪到顶部大图旁。
    //   两个理由都指向同一结论：官方包不该有这个区（用户："官方角色卡删不了就别放危险区"）。
    if (t === "danger") return (data && data.custom) ? _moveInto(t) : null;
    if (t === "identity" || t === "config" || t === "stickers") return _moveInto(t);
    const ph = document.createElement("div");
    ph.className = "pt-src";
    return ph;
}

// 控件型来源：把既有工作区块搬入树节点（不重写功能，只换归属；元素来自车位或初始位置）
function _moveInto(type) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src pt-embed";
    const get = _EMBED_GETTERS[type];
    const el = get ? get() : null;
    if (el) wrap.appendChild(el);
    return wrap;
}

// 引用型来源：只读跳转行——交叉文件的唯一编辑点在目标域，不重复放编辑器（避免两处改同一文件）
function _refNode(s) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src";
    const row = document.createElement("div");
    row.className = "pt-row pt-ref";
    const target = _domainLabels[s.target] || s.target || "";
    row.title = s.note || "";
    row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span><span class="pt-tag">见「${escapeHtml(target)}」→</span>`;
    row.addEventListener("click", () => {
        const dom = document.querySelector(`#pv-tree .pt-domain[data-key="${s.target}"]`);
        if (dom) { dom.open = true; dom.scrollIntoView({block: "start", behavior: "smooth"}); }
    });
    wrap.appendChild(row);
    return wrap;
}

// ── 文件节点（slot/file）：行 + 就地展开编辑器 ──
const _STATE_LABEL = {baseline: "", inherited: "", customized: "（已修改）"};

function _fileNode(s, data) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src pt-file";
    const row = document.createElement("div");
    row.className = "pt-row";
    const shared = (s.used_by || []).length > 1
        ? `<span class="pt-tag pt-shared" title="本文件被多个阶段读取：${escapeHtml(s.used_by.join("、"))}">共用</span>` : "";
    const feed = s.feeds ? `<span class="pt-tag" title="本文件的输出流向">→ ${escapeHtml(s.feeds)}</span>` : "";
    const ai = s.assistable === false ? "" : `<span class="assist-ico" title="AI 辅助修改本文件">✨</span>`;
    // 包未附带的文件（如 sticker 包无 opening.json）：灰态只展示，不提供编辑入口
    if (s.exists === false && !s.user_copy) {
        row.classList.add("pt-row-missing");
        row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span><span class="pt-tag">本包未附带</span>`;
        wrap.appendChild(row);
        return wrap;
    }
    row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span>
        ${s.state && _STATE_LABEL[s.state] ? `<span class="pt-tag">${_STATE_LABEL[s.state]}</span>` : ""}
        ${!s.state && s.user_copy ? `<span class="pt-tag">已改</span>` : ""}
        ${shared}${feed}${ai}`;
    const ed = document.createElement("div");
    ed.className = "pt-editor";
    ed.style.display = "none";
    wrap.append(row, ed);
    row.addEventListener("click", () => _toggleFile(ed, s));
    const ico = row.querySelector(".assist-ico");
    if (ico) ico.addEventListener("click", e => {
        e.stopPropagation();
        const file = s.type === "slot" ? s.file : s.path;
        if (window.__openAssist) window.__openAssist(_treeMode, file, s.label);
    });
    return wrap;
}

async function _loadFileContent(s) {
    if (s.type === "slot") {
        const resp = await fetch(`/pack-files?mode=${encodeURIComponent(_treeMode)}`);
        const data = await resp.json();
        const f = (data.files || []).find(x => x.name === s.file);
        return f ? (f.content || "") : "";
    }
    // file 类型统一走 /pack-file（白名单 = 结构规范声明的 file 路径）——
    // 修复：此前硬编码 /pack-memory，opening.json 的读写会落在出厂记忆上
    const resp = await fetch(`/pack-file?mode=${encodeURIComponent(_treeMode)}&path=${encodeURIComponent(s.path)}`);
    const data = await resp.json();
    return data.content || "";
}

async function _toggleFile(ed, s) {
    if (ed.style.display === "none") {
        if (!ed.dataset.loaded) {
            ed.innerHTML = `<div class="pt-editing">${escapeHtml(s.type === "slot" ? s.file : s.path)}</div>
                <textarea class="pt-ta"></textarea>
                <div class="btn-row" style="display:flex;gap:8px;margin-top:6px">
                    <button class="pt-save" type="button" style="flex:1">保存</button>
                    <button class="pt-revert" type="button" style="flex:1">恢复默认</button>
                    <button class="pt-close" type="button" style="flex:1">收起</button>
                </div><div class="pt-msg" style="font-size:0.72em;color:var(--fg-muted);margin-top:4px"></div>`;
            const content = await _loadFileContent(s);
            ed.querySelector(".pt-ta").value = content;
            ed.dataset.loaded = "1";
            ed.querySelector(".pt-save").onclick = () => _saveFile(ed, s);
            ed.querySelector(".pt-revert").onclick = () => _revertFile(ed, s);
            ed.querySelector(".pt-close").onclick = () => { ed.style.display = "none"; };
        }
        ed.style.display = "block";
    } else {
        ed.style.display = "none";
    }
}

async function _saveFile(ed, s) {
    const msg = ed.querySelector(".pt-msg");
    const content = ed.querySelector(".pt-ta").value;
    if (!content.trim()) { msg.textContent = "内容不能为空"; return; }
    msg.textContent = "保存中…";
    const isSlot = s.type === "slot";
    try {
        const resp = await fetch(isSlot ? "/character-file-update" : "/pack-file/update", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(isSlot
                ? {mode: _treeMode, filename: s.file, content}
                : {mode: _treeMode, path: s.path, content}),
        });
        const d = await resp.json();
        msg.textContent = d.ok ? "已保存（下轮对话生效）" : "保存失败：" + (d.error || "");
    } catch (e) { msg.textContent = "网络错误"; }
}

async function _revertFile(ed, s) {
    if (!confirm("恢复默认？（删除你的修改）")) return;
    const msg = ed.querySelector(".pt-msg");
    const isSlot = s.type === "slot";
    try {
        // file 类型走 /pack-file/delete 删用户副本——修复：此前向 /pack-memory/update 发空串，
        // 后端拒绝空内容，「恢复默认」永远失败
        const resp = await fetch(isSlot ? "/character-file/delete" : "/pack-file/delete", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(isSlot ? {mode: _treeMode, filename: s.file} : {mode: _treeMode, path: s.path}),
        });
        const d = await resp.json();
        if (d.ok) { showToast("已恢复默认"); ed.style.display = "none"; loadPackTree(_treeMode); }
        else msg.textContent = d.error || "操作失败";
    } catch (e) { msg.textContent = "网络错误"; }
}

// ── 知识库目录节点：组内文件列表 + 就地展开 ──
const _KB_GROUPS = {world: "世界观", factions: "势力", story: "主线剧情", character: "角色个人", dialogues: "对话"};

function _kbNode(s) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src";
    const head = document.createElement("div");
    head.className = "pt-row";
    const feed = s.feeds ? `<span class="pt-tag" title="本目录内容的输出流向">→ ${escapeHtml(s.feeds)}</span>` : "";
    head.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span> <span class="pt-tag">${s.count || 0} 个文件</span>${feed}`;
    const body = document.createElement("div");
    body.className = "pt-kb";
    body.style.display = "none";
    wrap.append(head, body);
    head.addEventListener("click", () => _toggleKB(body, head));
    return wrap;
}

async function _toggleKB(body, head) {
    if (body.style.display === "none") {
        if (!body.dataset.loaded) {
            const resp = await fetch(`/pack-knowledge?mode=${encodeURIComponent(_treeMode)}`);
            const data = await resp.json();
            const files = (data.ok && data.files) || [];
            const groups = {};
            for (const f of files) (groups[f.path.split("/")[0]] = groups[f.path.split("/")[0]] || []).push(f);
            let html = "";
            for (const top of Object.keys(groups)) {
                html += `<div class="pt-kb-grp">${escapeHtml(_KB_GROUPS[top] || top)}/</div>`;
                for (const f of groups[top]) {
                    html += `<div class="pt-row pt-kb-file" data-path="${escapeHtml(f.path)}">
                        <span class="pt-name" style="font-size:0.78em">${escapeHtml(f.path.split("/").slice(1).join("/"))}</span>
                        ${f.user_copy ? '<span class="pt-tag">已改</span>' : ""}
                        <span class="assist-ico" data-ai="${escapeHtml(f.path)}" title="AI 辅助修改本文件">✨</span>
                    </div>`;
                }
            }
            body.innerHTML = html || `<div class="pt-note">还没有知识文件</div>`;
            body.dataset.loaded = "1";
            body.querySelectorAll(".pt-kb-file").forEach(row => {
                row.addEventListener("click", () => _toggleKBFile(row));
            });
            body.querySelectorAll(".assist-ico").forEach(ico => {
                ico.addEventListener("click", e => {
                    e.stopPropagation();
                    if (window.__openAssist) window.__openAssist(_treeMode, "knowledge/" + ico.dataset.ai, ico.dataset.ai);
                });
            });
        }
        body.style.display = "block";
    } else {
        body.style.display = "none";
    }
}

async function _toggleKBFile(row) {
    let ed = row.nextElementSibling;
    if (ed && ed.classList && ed.classList.contains("pt-editor")) {
        ed.style.display = ed.style.display === "none" ? "block" : "none";
        return;
    }
    ed = document.createElement("div");
    ed.className = "pt-editor";
    const path = row.dataset.path;
    ed.innerHTML = `<div class="pt-editing">${escapeHtml(path)}</div>
        <textarea class="pt-ta"></textarea>
        <div class="btn-row" style="display:flex;gap:8px;margin-top:6px">
            <button class="pt-save" type="button" style="flex:1">保存</button>
            <button class="pt-revert" type="button" style="flex:1">恢复默认</button>
            <button class="pt-close" type="button" style="flex:1">收起</button>
        </div><div class="pt-msg" style="font-size:0.72em;color:var(--fg-muted);margin-top:4px"></div>`;
    row.after(ed);
    try {
        const resp = await fetch(`/pack-knowledge/file?mode=${encodeURIComponent(_treeMode)}&path=${encodeURIComponent(path)}`);
        const data = await resp.json();
        ed.querySelector(".pt-ta").value = data.ok ? (data.content || "") : "";
    } catch (e) { ed.querySelector(".pt-ta").value = ""; }
    ed.querySelector(".pt-save").onclick = async () => {
        const msg = ed.querySelector(".pt-msg");
        const content = ed.querySelector(".pt-ta").value;
        if (!content.trim()) { msg.textContent = "内容不能为空"; return; }
        const resp = await fetch("/pack-knowledge/update", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _treeMode, path, content})});
        const d = await resp.json();
        msg.textContent = d.ok ? "已保存（下轮对话生效）" : "保存失败：" + (d.error || "");
    };
    ed.querySelector(".pt-revert").onclick = async () => {
        if (!confirm("恢复默认？（删除你的修改）")) return;
        const resp = await fetch("/pack-knowledge/delete", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _treeMode, path})});
        const d = await resp.json();
        if (d.ok) { showToast("已恢复默认"); ed.remove(); row.remove(); }
        else ed.querySelector(".pt-msg").textContent = d.error || "操作失败";
    };
    ed.querySelector(".pt-close").onclick = () => { ed.style.display = "none"; };
}

// ── 系统提示词节点（只读）──
function _sysPromptNode(s) {
    const det = document.createElement("details");
    det.className = "pt-src pt-sysprompt";
    const sum = document.createElement("summary");
    sum.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span> <span class="pt-tag">代码内置 · 只读</span>`;
    const pre = document.createElement("pre");
    pre.className = "pt-pre";
    pre.textContent = s.text || "";
    det.append(sum, pre);
    return det;
}
