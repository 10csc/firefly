// 检查更新（★ 2026-09-24 改为**服务器主导**，见 docs/版本更新规范.md）
// 注意：CURRENT_VERSION 是前端版本号单一来源，tools/check_version.py 校验本文件（及 server/frontend 同步副本）
import { escapeHtml } from "./util.js";
import { IS_SERVER } from "./api.js";

// ═══════════════════════════════════════════
// 检查更新（主通道 = 服务器 /update-manifest；GitHub/Gitee 仅作最后兜底）
//
// 为什么改（旧实现的两个硬伤）：
//   1. GitHub 国内不稳 —— 用户侧超时/被墙，检测时好时坏；
//   2. "成功即返回"吞掉更新 —— 旧代码按 GitHub→Gitee 顺序，**先成功者胜**。
//      GitHub 停留在 v0.8.1 时直接 return，Gitee 上的 v0.9.0 被静默丢弃，
//      0.8.1 客户端于是永远显示「已是最新版本」。
//      （实测：Gitee 有 0.9.0、GitHub 忘发 → 大量用户收不到更新提示。）
// 现在：服务器统一管理版本号与下载直链，客户端只认一个端点；
//      兜底阶段也改为**按版本号取最高**，而非先到先得。
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
function _isAndroid() {
    return /Android/i.test(navigator.userAgent) && !/Windows|Mac|Linux/i.test(navigator.userAgent);
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
// 从服务器清单挑本平台的下载直链；未随发（url 空）→ 回退下载页
function _pickFromManifest(m, isAndroid) {
    const a = (m && m.assets && (isAndroid ? m.assets.apk : m.assets.exe)) || {};
    return String(a.url || "").trim();
}
// 渲染「发现新版本」——三种可下载形态（服务器自动下载 / 清单直链 / 下载页）
function _renderFound(msg, latest, cur, opts) {
    const notes = opts.notes ? ` ｜ <a href="${escapeHtml(opts.notes)}" target="_blank" rel="noopener" style="color:var(--fg-muted)">发行说明</a>` : "";
    const head = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(cur)}）`;
    if (opts.auto) {
        msg.innerHTML = head + `<br>` +
            `<button id="auto-update-btn" style="margin-top:6px;padding:4px 12px;border-radius:6px;border:none;background:var(--fg-accent);color:#fff;cursor:pointer">自动更新</button>` +
            notes;
        const btn = document.getElementById("auto-update-btn");
        if (btn) btn.addEventListener("click", () => autoUpdate(opts.isAndroid));
        return;
    }
    if (opts.directUrl) {
        msg.innerHTML = head + `<br>` +
            `<a href="${escapeHtml(opts.directUrl)}" target="_blank" rel="noopener" style="color:var(--fg-bright)">下载安装包</a>` + notes;
        return;
    }
    msg.innerHTML = head + `<br>` +
        `<a href="${DOWNLOAD_PAGE_URL}" target="_blank" rel="noopener" style="color:var(--fg-bright)">前往下载页</a>` + notes;
}
async function checkUpdate() {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "检查中…";
    const isAndroid = _isAndroid();
    if (IS_SERVER) {
        // 服务器模式：问本进程的 /update-manifest（服务器管理员维护 update.json）
        try {
            const resp = await fetch("/update-manifest", {cache: "no-store"});
            const m = await resp.json();
            if (!m.ok) throw new Error(m.error || "no manifest");
            const latest = String(m.tag || "").replace(/^v/i, "");
            const cur = String(CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            if (compareVersions(latest, cur) > 0) {
                _renderFound(msg, latest, cur, {
                    notes: m.notes_url,
                    directUrl: _pickFromManifest(m, isAndroid),
                });
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
        } catch (e) {
            msg.textContent = "检查失败（服务器更新清单不可达）";
        }
        return;
    }
    // 本地模式：优先走本地后端（后端会去问服务器清单，权威版本源 + 自动下载能力），
    // 失败退回纯前端双源检测（取版本最高者）
    try {
        // /check-update 只注册在 POST_ROUTES（GET 会 404，曾长期被前端双源兜底掩盖）
        const lr = await fetch("/check-update", {method: "POST", cache: "no-store"});
        if (lr.ok) {
            const d = await lr.json();
            if (!d.ok) throw new Error(d.error || "check fail");
            const latest = String(d.tag || "").replace(/^v/i, "");
            const cur = String(d.current || CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            if (compareVersions(latest, cur) > 0) {
                _renderFound(msg, latest, cur, {
                    auto: true, isAndroid,
                    notes: d.notes_url || d.html_url,
                });
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
            return;
        }
    } catch (e) { /* 降级到前端直连 */ }
    // 前端直连双源（后端接口不可用时）——★ 取**版本最高**者，不再"先成功者胜"
    let best = null;
    for (const src of UPDATE_SOURCES) {
        try {
            const resp = await fetch(src.api, {cache: "no-store"});
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            const data = await resp.json();
            const latest = String(data.tag_name || "").replace(/^v/i, "");
            if (!latest) throw new Error("no tag");
            if (!best || compareVersions(latest, best.latest) > 0) {
                best = { latest, data, src };
            }
        } catch (e) { /* 单源失败继续看下一个 */ }
    }
    if (!best) {
        msg.textContent = "检查失败（网络或仓库不可达）";
        return;
    }
    const { latest, data, src } = best;
    if (compareVersions(latest, CURRENT_VERSION) > 0) {
        const exeUrl = _matchAsset(data.assets, /\.exe$/i);
        const apkUrl = _matchAsset(data.assets, /\.apk$/i);
        _renderFound(msg, latest, CURRENT_VERSION, {
            notes: src.html,
            directUrl: isAndroid ? (apkUrl || "") : (exeUrl || ""),
        });
    } else {
        msg.textContent = `已是最新版本 ${CURRENT_VERSION} ✓`;
    }
}
// 检查更新按钮接线（设置面板版本区；修复前该按钮无任何事件绑定，点击无反应）
const checkUpdateBtn = document.getElementById("check-update-btn");
if (checkUpdateBtn) checkUpdateBtn.addEventListener("click", checkUpdate);

// 自动更新：后端下载安装包 → PC 静默安装并重启；安卓交**系统安装器**（APP 内装完，不用再下第二遍）
//
// ★ 2026-09-25 修掉的那半：旧实现在安卓上"下载完 85MB 之后，提示用户去下载页再下一次"
//   —— 等于一个包下两遍、一次都不装。现在由壳（FireflyJs.installApk）把**已经校验过 sha256**
//   的包交给系统安装器；没有壳（PC 浏览器 / 服务器模式）才退回旧路径。
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
            // 安卓：后端已下好并校验 → 让壳交系统安装器。
            // 壳返回空串 = 已经拉起安装器；非空 = 拒绝/失败原因（如实显示，不假装成功）
            let err = "";
            try {
                const sh = (window.FireflyJs
                    && typeof window.FireflyJs.installApk === "function")
                    ? window.FireflyJs : null;
                if (sh) err = String(sh.installApk(data.name || "") || "");
                else err = "（当前环境不是应用内，无法自动安装）";
            } catch (e) {
                err = (e && e.message) ? e.message : String(e);
            }
            if (err) {
                msg.textContent = "下载完成，但自动安装未启动：" + err
                    + " ｜ 也可前往下载页手动安装";
                return;
            }
            msg.textContent = "下载完成 → 请在系统弹窗里点「安装」（覆盖安装，聊天数据保留）";
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
