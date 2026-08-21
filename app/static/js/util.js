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
