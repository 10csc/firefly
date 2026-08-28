// 增量同步 UI（/sync/now）：进度条 / 冲突警告 toast / 10 分钟节流 / 回前台补同步
import { showToast } from "./util.js";
import { IS_SERVER } from "./api.js";
import { CURRENT_MODE } from "./views.js";

/** A1 增量同步（W4 编排端点 /sync/now）：登录后把本地文字数据与云端账号双向合并。
 *  window.autoSyncNow(force)：仅本地版 + 已登录（/auth/state 确认）才发起；
 *  force=false 时 10 分钟节流（localStorage 时间戳），force=true（手动「立即同步」）跳过节流。
 *  进行中显示非阻塞状态条「正在同步…」；完成 toast 计数；冲突橙色警告；失败 toast 原因。
 *  挂接点：登录态确认（api.js initAuth）/ 进聊天页与模式切换（views.js showChat）/ 回前台（下方 visibilitychange）。 */
const _SYNC_THROTTLE_MS = 10 * 60 * 1000;
let _syncing = false;
let _syncPill = null;
let _warnToastTimer = null;

function _syncPillShow(text) {
    if (!_syncPill) {
        _syncPill = document.createElement("div");
        _syncPill.style.cssText = "position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:998;background:rgba(28,30,46,.96);color:#e8e0d0;border:1px solid rgba(255,196,107,.45);border-radius:10px;padding:7px 14px;font-size:0.75em;box-shadow:0 6px 22px rgba(0,0,0,.45);pointer-events:none;display:none";
        document.body.appendChild(_syncPill);
    }
    _syncPill.textContent = text;
    _syncPill.style.display = "block";
}
function _syncPillHide() { if (_syncPill) _syncPill.style.display = "none"; }

/** 橙色警告 toast（样式沿用 _toast 浮层，边框改橙）：冲突备份等需要用户注意但非阻断的提醒 */
function _warnToast(msg) {
    let t = document.getElementById("app-warn-toast");
    if (!t) {
        t = document.createElement("div");
        t.id = "app-warn-toast";
        t.style.cssText = "position:fixed;top:56px;left:50%;transform:translateX(-50%);z-index:999;background:rgba(46,34,22,.97);color:#ffd9b0;border:1px solid rgba(255,150,80,.65);border-radius:10px;padding:9px 16px;font-size:0.8em;max-width:86vw;text-align:center;box-shadow:0 6px 22px rgba(0,0,0,.45);pointer-events:none;display:none";
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(_warnToastTimer);
    _warnToastTimer = setTimeout(() => { t.style.display = "none"; }, 4000);
}

window.autoSyncNow = async function (force) {
    if (IS_SERVER) return;   // 服务器版数据天然在云端，无需同步
    if (_syncing) return;    // 并发护栏：上一次同步还在飞
    if (!force) {
        let last = 0;
        try { last = parseInt(localStorage.getItem("firefly_last_sync") || "0", 10) || 0; } catch (e) {}
        if (Date.now() - last < _SYNC_THROTTLE_MS) return;   // 节流期内静默跳过
    }
    try {
        const st = await (await fetch("/auth/state")).json();
        if (!st.logged_in || st.offline_ok === false) return;   // 未登录/登录已过期：静默跳过
    } catch (e) { return; }   // 本地后端未就绪：静默
    _syncing = true;
    try { localStorage.setItem("firefly_last_sync", String(Date.now())); } catch (e) {}
    const msg = document.getElementById("sync-now-msg");
    if (msg) msg.textContent = "同步中…";
    _syncPillShow("正在同步…");
    try {
        const r = await fetch("/sync/now", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: CURRENT_MODE}),
        });
        const d = await r.json().catch(() => ({}));
        if (d.ok) {
            const text = `同步完成：上传 ${(d.uploaded || []).length} · 下载 ${(d.downloaded || []).length} · 合并 ${(d.merged || []).length}`;
            const conflicts = d.conflicts || 0;
            if (msg) msg.textContent = "✓ " + text + (conflicts ? `，冲突 ${conflicts}（见 .sync_backups）` : "");
            showToast(text);
            if (conflicts > 0) _warnToast(`${conflicts} 处冲突已各自备份`);
        } else {
            const err = "同步未完成：" + (d.error || "请先登录账号");
            if (msg) msg.textContent = err;
            showToast(err);
        }
    } catch (e) {
        if (msg) msg.textContent = "网络错误，稍后再试";
        showToast("同步失败：网络错误，稍后再试");
    } finally {
        _syncPillHide();
        _syncing = false;
    }
};
window.syncNow = function () { window.autoSyncNow(true); };   // 手动「立即同步」：强制不节流

// 回前台补一次同步（节流期内自动跳过，不打扰）
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") window.autoSyncNow();
});
