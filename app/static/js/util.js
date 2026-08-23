// 通用小工具：toast / HTML 转义（从各节抠出合并）
// 轻提示（3s 自动消失）
let toastTimer = null;
export function showToast(msg) {
    let el = document.getElementById("app-toast");
    if (!el) {
        el = document.createElement("div");
        el.id = "app-toast";
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}
export function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
/** 轻量 toast（页面顶部浮层，3 秒消失；不打断输入） */
export function _toast(msg) {
    let t = document.getElementById("app-toast");
    if (!t) {
        t = document.createElement("div");
        t.id = "app-toast";
        t.style.cssText = "position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:999;background:rgba(28,30,46,.96);color:#e8e0d0;border:1px solid rgba(255,196,107,.45);border-radius:10px;padding:9px 16px;font-size:0.8em;max-width:86vw;text-align:center;box-shadow:0 6px 22px rgba(0,0,0,.45);display:none;pointer-events:none";
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.style.display = "none"; }, 3200);
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
