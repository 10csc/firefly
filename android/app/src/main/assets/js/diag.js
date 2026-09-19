// 诊断包导出（本地导出，不经服务器）—— 见 app/api/diag.py 与 docs/未完成事项清单.md P1-3
//
// 用户 2026-09-19 的要求：导出成文件/压缩包，**并且导出后给一个跳转 QQ 的选项**
//（"不然怕用户在 QQ 里找不到文件"）。
//
// 三件事：
//   ① 导出：隐藏链接触发下载，保留 Content-Disposition 的文件名
//      （window.open 在部分 WebView 会被拦，项目里已有先例，见 chat.js 导出 zip 的写法）；
//   ② 导出后**就地**给下一步：打开 QQ 群 / 复制群号；
//   ③ 跳 QQ 用系统 scheme `mqqapi://`。安卓壳的 shouldOverrideUrlLoading 会把非内部 URL
//      交给系统 ACTION_VIEW（MainActivity.kt:477），所以这里 location.href 即可；
//      PC 浏览器上没装 QQ 时不会报错，只是没反应 —— 所以**同时**给"复制群号"兜底。
import { API_BASE, IS_SERVER } from "./api.js";
import { showToast } from "./util.js";

const QQ_GROUP = "1097936258";
// QQ 群卡片 scheme：card_type=group + uin=<群号>，QQ 各版本都能接住
const QQ_SCHEME = `mqqapi://card/show_pslcard?src_type=internal&version=1&uin=${QQ_GROUP}&card_type=group&source=qrcode`;

function _msg(text, ok) {
    const el = document.getElementById("diag-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = ok ? "var(--fg-accent)" : "var(--fg-muted)";
}

/** 触发下载：诊断包由本地后端即时生成，落在用户设备上，服务端不接触。 */
export function exportDiagnostics() {
    if (IS_SERVER) {
        // 服务器版没有这个端点（后端 403）；界面也不该显示按钮，这里只做兜底提示
        _msg("服务器版不提供诊断包（诊断包只在你自己设备上生成）");
        return;
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
    _msg("已开始导出到本机下载目录 …");
    // 导出后**立刻**把"下一步"摆出来（用户此刻最容易找不到文件）
    const box = document.getElementById("diag-next");
    if (box) box.hidden = false;
    try { showToast("诊断包已导出，点「打开 QQ 群」发出去即可"); } catch (e) {}
}

/** 跳转 QQ 群（安卓壳交给系统；PC 浏览器没装 QQ 就无反应 → 提示用复制） */
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
    _msg("已尝试打开 QQ · 群号 " + QQ_GROUP + "（没跳转就点「复制群号」）");
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
