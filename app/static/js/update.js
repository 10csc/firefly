// 检查更新（GitHub 优先，失败自动降级 Gitee）与自动更新下载
// 注意：CURRENT_VERSION 是前端版本号单一来源，tools/check_version.py 校验本文件（及 server/frontend 同步副本）
import { escapeHtml } from "./util.js";
import { IS_SERVER } from "./api.js";

// ═══════════════════════════════════════════
// 检查更新（GitHub 优先，失败自动降级 Gitee——国内网络 Gitee 更稳）
// ═══════════════════════════════════════════
const CURRENT_VERSION = "0.9.0";   // 与 android versionName / 安装器 AppVersion 保持一致
// PC 三栏外壳（pc_shell.js，独立 classic script）底部状态栏要显示版本号，
// 它看不到 bundle 作用域，所以暴露一个只读副本（不要在这里写版本，单一来源仍是本文件）。
window.__appVersion = CURRENT_VERSION;
// 设置面板版本号动态显示（单一版本源：CURRENT_VERSION；替代 index.html 硬编码文案）
const curVersionEl = document.getElementById("current-version");
if (curVersionEl) curVersionEl.textContent = "v" + CURRENT_VERSION;
const UPDATE_SOURCES = [
    { api: "https://api.github.com/repos/10csc/firefly/releases/latest", html: "https://github.com/10csc/firefly/releases" },
    { api: "https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest", html: "https://gitee.com/cpt-asymmetry/firefly/releases" },
];
// 公共下载页：APK 主通道走 Gitee，微信/QQ 等不支持 blob 下载的内置浏览器会自动走服务器直连（正确 MIME）
const DOWNLOAD_PAGE_URL = "http://101.200.14.126:8787/download/";
function compareVersions(a, b) {
    const pa = String(a).split(".").map(n => parseInt(n) || 0);
    const pb = String(b).split(".").map(n => parseInt(n) || 0);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const d = (pa[i] || 0) - (pb[i] || 0);
        if (d !== 0) return d;
    }
    return 0;
}
// 资产匹配：PC 装包 exe / 安卓 apk（Gitee 资产名可能带前缀，模糊匹配）
function _matchAsset(assets, re) {
    if (!Array.isArray(assets)) return "";
    for (const a of assets) {
        const n = String(a.name || a.browser_download_url || "");
        if (re.test(n)) return a.browser_download_url || n;
    }
    return "";
}
async function checkUpdate() {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "检查中…";
    if (IS_SERVER) {
        // 服务器模式：检查更新读服务器 version.json（由服务器管理员维护），不走 GitHub/Gitee
        try {
            const resp = await fetch("/version.json", {cache: "no-store"});
            const d = await resp.json();
            const latest = String(d.tag || "").replace(/^v/i, "");
            const cur = String(CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            if (compareVersions(latest, cur) > 0) {
                msg.innerHTML = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(cur)}）<br>新版本由服务器管理员发布`;
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
        } catch (e) {
            msg.textContent = "检查失败（服务器 version.json 不可达）";
        }
        return;
    }
    // 本地模式：优先走本地后端（权威版本源 + 自动下载能力），失败退回纯前端双源检测
    try {
        // /check-update 只注册在 POST_ROUTES（GET 会 404，曾长期被前端双源兜底掩盖）
        const lr = await fetch("/check-update", {method: "POST", cache: "no-store"});
        if (lr.ok) {
            const d = await lr.json();
            if (!d.ok) throw new Error(d.error || "check fail");
            const latest = String(d.tag || "").replace(/^v/i, "");
            const cur = String(d.current || CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            const isAndroid = /Android/i.test(navigator.userAgent) && !/Windows|Mac|Linux/i.test(navigator.userAgent);
            if (compareVersions(latest, cur) > 0) {
                msg.innerHTML = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(cur)}）<br>` +
                    `<button id="auto-update-btn" style="margin-top:6px;padding:4px 12px;border-radius:6px;border:none;background:var(--fg-accent);color:#fff;cursor:pointer">自动更新</button>` +
                    ` ｜ <a href="${escapeHtml(d.html_url || "#")}" target="_blank" rel="noopener" style="color:var(--fg-muted)">发行说明</a>`;
                const btn = document.getElementById("auto-update-btn");
                if (btn) btn.addEventListener("click", () => autoUpdate(isAndroid));
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
            return;
        }
    } catch (e) { /* 降级到前端直连 */ }
    // 前端直连双源（后端接口不可用时）
    for (const src of UPDATE_SOURCES) {
        try {
            const resp = await fetch(src.api, {cache: "no-store"});
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            const data = await resp.json();
            const latest = String(data.tag_name || "").replace(/^v/i, "");
            if (!latest) throw new Error("no tag");
            const isAndroid = /Android/i.test(navigator.userAgent) && !/Windows|Mac|Linux/i.test(navigator.userAgent);
            const exeUrl = _matchAsset(data.assets, /\.exe$/i);
            const apkUrl = _matchAsset(data.assets, /\.apk$/i);
            const dlUrl = isAndroid ? (apkUrl || src.html) : (exeUrl || src.html);
            if (compareVersions(latest, CURRENT_VERSION) > 0) {
                msg.innerHTML = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(CURRENT_VERSION)}）<br>` +
                    `<a href="${escapeHtml(dlUrl || "#")}" target="_blank" rel="noopener" style="color:var(--fg-bright)">下载安装包</a>` +
                    ` ｜ <a href="${escapeHtml(src.html || "#")}" target="_blank" rel="noopener" style="color:var(--fg-muted)">发行说明</a>`;
            } else {
                msg.textContent = `已是最新版本 ${CURRENT_VERSION} ✓`;
            }
            return;
        } catch (e) {
            msg.textContent = "检查失败（网络或仓库不可达）";
        }
    }
}
// 检查更新按钮接线（设置面板版本区；修复前该按钮无任何事件绑定，点击无反应）
const checkUpdateBtn = document.getElementById("check-update-btn");
if (checkUpdateBtn) checkUpdateBtn.addEventListener("click", checkUpdate);

// 自动更新：后端下载安装包 → PC 静默安装并重启；安卓引导系统安装器
async function autoUpdate(isAndroid) {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "下载中…（约 30-60 秒，请勿关闭应用）";
    try {
        const resp = await fetch("/update-download", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({kind: isAndroid ? "apk" : "exe"}),
        });
        const data = await resp.json();
        if (!data.ok) { msg.textContent = "下载失败：" + (data.error || ""); return; }
        if (isAndroid) {
            // WebView 无法直接用 file:// 装 APK：跳系统浏览器打开公共下载页
            // （下载页自动分流：标准浏览器走 Gitee，微信/QQ 等走服务器直连正确 MIME）
            msg.innerHTML = `下载完成 → 请从 <a href="${DOWNLOAD_PAGE_URL}" target="_blank" rel="noopener" style="color:var(--fg-bright)">下载页</a> 下载 APK 安装（系统限制需手动确认；如从 Gitee 页下载变成 .zip，把文件名改回 firefly.apk 即可）`;
            return;
        }
        if (data.installing) {
            msg.textContent = "下载完成，安装程序即将启动…应用会自动关闭，请稍候。";
            setTimeout(() => { location.href = "about:blank"; }, 1500);
        } else {
            msg.innerHTML = `下载完成 → <a href="file://${data.path}" target="_blank" rel="noopener" style="color:var(--fg-bright)">点击运行安装</a>`;
        }
    } catch (e) {
        msg.textContent = "自动更新失败：" + e;
    }
}
