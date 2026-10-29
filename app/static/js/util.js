// 通用小工具：toast / HTML 转义（从各节抠出合并）
/** 轻提示（全站唯一实现）：顶部安全区下方居中，3 秒自动消失。
 *  level: info（默认）| success | error —— 只换左侧色条，位置与尺寸不变。
 *  ⚠ 不要再新增第二套 toast：历史上 showToast 与 _toast 共用同一元素却各用一套
 *  显隐机制（opacity vs inline display），谁先创建另一套就完全不可见。 */
let toastTimer = null;
export function showToast(msg, level) {
    let el = document.getElementById("app-toast");
    if (!el) {
        el = document.createElement("div");
        el.id = "app-toast";
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.remove("success", "error");
    if (level === "success" || level === "error") el.classList.add(level);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}
export function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

/** 自绘确认弹层 —— 替代原生 confirm/alert/prompt（WebView 里原生弹窗不可主题化）。
 *  opts: { title, body, confirmText, cancelText, danger, input, placeholder, validate, validateMsg }
 *  返回 Promise：普通确认 → true/false；opts.input → 输入的字符串 / null（取消）。
 *  确认按钮在 input 模式下由 validate 决定是否放行；输入为空时直接拦截。 */
// 同时可能叠着多个确认弹层（例如连点两次删除）。返回键要关的是最上面那个，
// 所以按打开顺序记栈，而不是 querySelector 抓第一个。
let _modalStack = [];
window.__closeAppModal = function () {
    const top = _modalStack[_modalStack.length - 1];
    if (!top) return false;
    try { top.cancel(); } catch (e) {}
    return true;
};

export function uiConfirm(opts) {
    const o = opts || {};
    return new Promise((resolve) => {
        const mask = document.createElement("div");
        mask.className = "app-modal-mask";
        const box = document.createElement("div");
        box.className = "app-modal" + (o.danger ? " danger" : "");
        box.setAttribute("role", "dialog");
        box.setAttribute("aria-modal", "true");

        const title = document.createElement("div");
        title.className = "app-modal-title";
        title.textContent = o.title || "请确认";
        const body = document.createElement("div");
        body.className = "app-modal-body";
        body.textContent = o.body || "";
        box.append(title, body);

        let inputEl = null;
        if (o.input) {
            inputEl = document.createElement("input");
            inputEl.type = "text";
            inputEl.className = "app-modal-input";
            inputEl.placeholder = o.placeholder || "";
            box.appendChild(inputEl);
        }
        const msg = document.createElement("div");
        msg.className = "app-modal-msg";
        box.appendChild(msg);

        const row = document.createElement("div");
        row.className = "app-modal-actions";
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "app-modal-btn";
        cancelBtn.textContent = o.cancelText || "取消";
        const okBtn = document.createElement("button");
        okBtn.type = "button";
        okBtn.className = "app-modal-btn primary" + (o.danger ? " danger" : "");
        okBtn.textContent = o.confirmText || "确定";
        row.append(cancelBtn, okBtn);
        box.appendChild(row);
        mask.appendChild(box);
        document.body.appendChild(mask);

        const entry = { cancel: null };
        const done = (val) => {
            document.removeEventListener("keydown", onKey, true);
            mask.remove();
            const i = _modalStack.indexOf(entry);
            if (i >= 0) _modalStack.splice(i, 1);
            resolve(val);
        };
        const cancelValue = () => (o.input ? null : false);
        entry.cancel = () => done(cancelValue());
        _modalStack.push(entry);
        function onKey(e) {
            if (e.key === "Escape") { e.preventDefault(); done(cancelValue()); }
            else if (e.key === "Enter" && (!inputEl || document.activeElement === inputEl)) {
                e.preventDefault();
                okBtn.click();
            }
        }
        document.addEventListener("keydown", onKey, true);
        cancelBtn.addEventListener("click", () => done(cancelValue()));
        mask.addEventListener("click", (e) => { if (e.target === mask) done(cancelValue()); });
        okBtn.addEventListener("click", () => {
            if (!inputEl) { done(true); return; }
            const v = inputEl.value.trim();
            if (!v) { msg.textContent = "请输入内容"; return; }
            if (typeof o.validate === "function" && !o.validate(v)) {
                msg.textContent = o.validateMsg || "输入不匹配";
                return;
            }
            done(v);
        });
        setTimeout(() => { (inputEl || cancelBtn).focus(); }, 30);
    });
}

/** 把网络/后端的原始技术错误翻成用户能看懂的一句话。
 *  认不出、或本来就是中文业务文案时返回空串 —— 调用方据此决定原样展示。
 *  原始串仍进控制台（诊断包会带），界面只放翻译结果。 */
export function friendlyError(raw) {
    const s = String(raw || "").trim();
    if (!s) return "";
    if (/HTTPError|HTTP Error|Failed to fetch|NetworkError|Load failed|net::ERR_|ECONNREFUSED|TimeoutError|timed out/i.test(s)) {
        const m = s.match(/\b(4\d{2}|5\d{2})\b/);
        if (m) {
            return (m[1][0] === "5" ? "服务器暂时出了故障" : "服务器没有找到要的东西")
                + "（" + m[1] + "），稍后再试";
        }
        return "网络或服务器没响应，稍后再试";
    }
    return "";
}
export function _esc(s) {
    return String(s == null ? "" : s)
        .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

// ═══ 本机 API Key 单点读取（服务器模式）═══
// 正式存储：firefly_providers（W1 多供应商，key 在 active 供应商的 api_key 字段）；
// 遗留兜底：firefly_api_key（旧版直接存此处 + W1 后的兼容镜像字段）。
// providers 优先是为了防「兼容镜像字段被覆写为空、但主存储还有 key」的偶发丢失
// （用户反馈：服务器模式偶发每轮对话要求重新设置 Key）。
export function getLocalApiKey() {
    try {
        const list = JSON.parse(localStorage.getItem("firefly_providers") || "[]");
        const arr = Array.isArray(list) ? list : [];
        const active = localStorage.getItem("firefly_active_provider") || "";
        const p = arr.find(x => x && x.id === active) || arr[0] || null;
        if (p && p.api_key) return p.api_key;
    } catch (e) { /* 解析失败走 legacy 兜底 */ }
    try { return localStorage.getItem("firefly_api_key") || ""; } catch (e) { return ""; }
}

// ═══ 媒体本地存储（A2：服务器只传输不保存图片；本体存 WebView IndexedDB）═══
// 键 = 内容 sha256（服务器返回的 local:<sha256>.<ext> 引用）；Blob 存取。
let _idbPromise = null;
function _idb() {
    if (!_idbPromise) {
        _idbPromise = new Promise((res, rej) => {
            try {
                if (!window.indexedDB) { rej(new Error("IndexedDB 不可用")); return; }
                const req = indexedDB.open("firefly_media", 1);
                req.onupgradeneeded = () => {
                    const db = req.result;
                    if (!db.objectStoreNames.contains("media")) db.createObjectStore("media");
                };
                req.onsuccess = () => res(req.result);
                req.onerror = () => rej(req.error);
            } catch (e) { rej(e); }
        });
    }
    return _idbPromise;
}

export async function idbSaveMedia(key, blob) {
    try {
        const db = await _idb();
        return await new Promise((res, rej) => {
            const tx = db.transaction("media", "readwrite");
            tx.objectStore("media").put(blob, key);
            tx.oncomplete = () => res(true);
            tx.onerror = () => rej(tx.error);
        });
    } catch (e) { return false; }
}

export async function idbGetMedia(key) {
    try {
        const db = await _idb();
        return await new Promise((res) => {
            const tx = db.transaction("media", "readonly");
            const rq = tx.objectStore("media").get(key);
            rq.onsuccess = () => res(rq.result || null);
            rq.onerror = () => res(null);
        });
    } catch (e) { return null; }
}

/** 表情包图片源解析：local: 引用 → IndexedDB dataURL（无图返回 null=占位）；否则服务器资产 URL。 */
export async function stickerSrc(file, isServer, apiBase) {
    if (!file) return null;
    if (String(file).startsWith("local:")) {
        const blob = await idbGetMedia(String(file).slice(6));
        if (!blob) return null;
        try { return URL.createObjectURL(blob); } catch (e) { return null; }
    }
    return (isServer ? apiBase : "") + "/assets/" + encodeURI(String(file));
}
