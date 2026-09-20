// 诊断包导出（本地导出，不经服务器）—— 见 app/api/diag.py 与 docs/未完成事项清单.md P1-3
//
// 用户 2026-09-19 的要求：导出成文件/压缩包，**并且导出后给一个跳转 QQ 的选项**
//（"不然怕用户在 QQ 里找不到文件"）。
//
// 三件事：
//   ① 导出：**安卓壳优先**（FireflyJs.sendDiagnostics：壳自己取包 → 写缓存 →
//      ACTION_SEND 系统分享面板，附件已带好）；没有桥才退回"隐藏链接下载"；
//      （window.open 在部分 WebView 会被拦，项目里已有先例，见 chat.js 导出 zip 的写法）；
//   ② 导出后**就地**给下一步：打开 QQ（用户自己发给开发者）/ 复制群号兜底；
//   ③ 跳 QQ 用系统 scheme `mqqapi://`。安卓壳的 shouldOverrideUrlLoading 会把非内部 URL
//      交给系统 ACTION_VIEW（MainActivity.kt:477），所以这里 location.href 即可；
//      PC 浏览器上没装 QQ 时不会报错，只是没反应 —— 所以**同时**给"复制群号"兜底。
import { API_BASE, IS_SERVER } from "./api.js";
import { showToast } from "./util.js";

const QQ_GROUP = "1097936258";   // 仅作"复制群号"兜底（用户找不到开发者时用）
// ★ 直接启动 QQ 主界面（不指定群、不指定联系人）：用户自己决定发给谁。
//   2026-09-19 用户明确要求："直接跳转QQ就行，这样用户会自己发我"。
const QQ_SCHEME = "mqq://";

/** 壳的回执出口（Kotlin 里 diagResult() 会调它）——成功/失败都要让用户看见。 */
window.__diagResult = function (text, ok) {
    _msg(text, ok);
    try { showToast(String(text)); } catch (e) {}
};

function _msg(text, ok) {
    const el = document.getElementById("diag-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = ok ? "var(--fg-accent)" : "var(--fg-muted)";
}

function _shell() {
    // 安卓壳注入的桥：有它就能"带附件直接分享"（比让用户自己找文件靠谱得多）
    try {
        return (window.FireflyJs && typeof window.FireflyJs.sendDiagnostics === "function")
            ? window.FireflyJs : null;
    } catch (e) { return null; }
}

/** 导出诊断包：有壳桥就**带附件分享**（QQ 在分享面板里），否则退回下载到本机。 */
export function exportDiagnostics() {
    if (IS_SERVER) {
        // 服务器版没有这个端点（后端 403）；界面也不该显示按钮，这里只做兜底提示
        _msg("服务器版不提供诊断包（诊断包只在你自己设备上生成）");
        return;
    }
    const sh = _shell();
    if (sh) {
        let ok = false;
        try {
            // 把页面 origin 传进壳：桥线程不能读 WebView（会抛），所以由这边告诉它地址
            ok = sh.sendDiagnostics(location.origin || "");
        } catch (e) { ok = false; }
        if (ok) {
            _msg("正在生成诊断包 …（稍后会弹出分享面板）");
            return;
        }
        _msg("壳内分享不可用，改为导出到本机（下面还有打开 QQ）");
    }
    try {
        const a = document.createElement("a");
        a.href = API_BASE + "/export-diagnostics";
        a.style.display = "none";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { try { a.remove(); } catch (e) {} }, 0);
    } catch (e) {
        _msg("导出失败：" + (e && e.message ? e.message : e));
        return;
    }
    _msg("已保存到本机下载目录，正在打开 QQ —— 把它发给我即可");
    try { showToast("诊断包已导出，发给我即可"); } catch (e) {}
    openQQGroup();          // 按钮就叫"导出并发送"，PC 上发送=打开 QQ（文件在本机，得自己拖进去）
}

/** 打开 QQ（安卓壳交给系统；PC 没装 QQ 就无反应 → 提示用复制群号兜底） */
export function openQQGroup() {
    try {
        // 用隐藏 iframe 触发 scheme：比 location.href 更不容易把当前页顶掉
        const f = document.createElement("iframe");
        f.style.display = "none";
        f.src = QQ_SCHEME;
        document.body.appendChild(f);
        setTimeout(() => { try { f.remove(); } catch (e) {} }, 1500);
    } catch (e) {
        try { window.location.href = QQ_SCHEME; } catch (e2) {}
    }
    _msg("已尝试打开 QQ —— 把刚导出的 zip 发给我即可（没跳转就点「复制群号」）");
}

/** 复制群号（兜底：PC 上没装 QQ、或用户想自己在 QQ 里搜） */
export async function copyQQGroup() {
    try {
        await navigator.clipboard.writeText(QQ_GROUP);
        _msg("群号已复制：" + QQ_GROUP, true);
    } catch (e) {
        // 老 WebView / 非安全上下文没有 clipboard API → 退回选中提示
        _msg("复制失败，请手动记下群号：" + QQ_GROUP);
    }
}

window.exportDiagnostics = exportDiagnostics;
window.openQQGroup = openQQGroup;
window.copyQQGroup = copyQQGroup;


/** 一键清理已导出的诊断包。
 *  安卓：壳真删（缓存 + Download 目录 + DownloadManager 记录），返回人话摘要；
 *  PC：文件在浏览器下载目录，应用删不到 —— 如实告知，不假装成功。 */
export function clearDiagnostics() {
    const sh = _shell();
    if (sh) {
        let msg = "";
        try {
            msg = String(sh.clearDiagnostics());
        } catch (e) {
            msg = "清理失败：" + (e && e.message ? e.message : e);
        }
        if (!msg) msg = "清理完成";
        _msg(msg, true);
        try { showToast(msg); } catch (e) {}   // 小字容易看不见 → 再浮层提示一次
        return;
    }
    const tip = "PC 上诊断包在浏览器下载目录里（应用删不到），请到那里删除";
    _msg(tip);
    try { showToast(tip); } catch (e) {}
}

window.clearDiagnostics = clearDiagnostics;
