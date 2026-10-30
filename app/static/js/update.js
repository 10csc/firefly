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
const CURRENT_VERSION = "0.9.2";   // 与 android versionName / 安装器 AppVersion 保持一致
// PC 三栏外壳（pc_shell.js，独立 classic script）底部状态栏要显示版本号，
// 它看不到 bundle 作用域，所以暴露一个只读副本（不要在这里写版本，单一来源仍是本文件）。
window.__appVersion = CURRENT_VERSION;
// 设置面板版本号动态显示（单一版本源：CURRENT_VERSION；替代 index.html 硬编码文案）
const curVersionEl = document.getElementById("current-version");
if (curVersionEl) curVersionEl.textContent = "v" + CURRENT_VERSION;
const aboutSummaryEl = document.getElementById("about-summary");
if (aboutSummaryEl) aboutSummaryEl.textContent = "v" + CURRENT_VERSION;
const UPDATE_SOURCES = [
    { api: "https://api.github.com/repos/10csc/firefly/releases/latest", html: "https://github.com/10csc/firefly/releases" },
    { api: "https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest", html: "https://gitee.com/cpt-asymmetry/firefly/releases" },
];
// 公共下载页：APK 主通道走 Gitee，微信/QQ 等不支持 blob 下载的内置浏览器会自动走服务器直连（正确 MIME）
const DOWNLOAD_PAGE_URL = "https://101.200.14.126/download/";
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
    // ★ 2026-10-30 修：旧实现是 `/Android/ && !/Windows|Mac|Linux/` ——
    //   安卓 WebView 的 UA 必然含 "Linux"（形如 `(Linux; Android 14; PJE110)`），
    //   后半段把安卓自己也排除掉了，函数恒为 false：
    //   于是自动更新在手机上一直走 PC 分支，下载完只给个 file:// 链接，
    //   壳的 installApk 从来没被调用过。UA 含 Android 即移动端，无需再排除桌面系统。
    return /Android/i.test(navigator.userAgent);
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
        // 2026-10-22 安全缓解（P8-5）原为无条件引导下载页；2026-10-30 改为按热更实际状态分流：
        //   canAuto = 补丁已生效 + 验签可用 ⇒ 直接给「立即更新」（后端下包、校验 sha256、
        //   安卓交系统安装器；PC 静默运行安装器）。
        //   否则维持原提示 —— 那时旧更新链仍有 MITM 风险，引导重启或下载页。
        if (opts.canAuto) {
            msg.innerHTML = head + `<br>` +
                `<button type="button" id="update-now-btn" style="margin-top:10px;padding:10px 20px;border:0;` +
                `border-radius:10px;background:var(--fg-accent);color:#0b0c12;font-size:15px;font-weight:600;` +
                `cursor:pointer">立即更新到 ${escapeHtml(latest)}</button>` +
                `<div style="color:var(--fg-muted);margin-top:6px">下载完成后会交给系统安装器，聊天数据保留</div>` + notes;
            const btn = document.getElementById("update-now-btn");
            if (btn) {
                btn.addEventListener("click", () => {
                    btn.disabled = true;
                    btn.textContent = "正在下载…";
                    autoUpdate(opts.isAndroid);
                });
            }
            return;
        }
        msg.innerHTML = head + `<br>` +
            `<span style="color:var(--fg-muted)">安全修复待重启：请完全退出并重新打开 App 后，再前往下载页手动更新。</span>` +
            ` <a href="${DOWNLOAD_PAGE_URL}" target="_blank" rel="noopener" style="color:var(--fg-bright)">前往下载页</a>` + notes;
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
            msg.textContent = "检查更新失败：暂时连不上更新服务，请稍后重试";
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
                // 自动更新是否放行，取决于热更层的真实状态（P8-5 的条件化，2026-10-30）：
                // 补丁已生效（restart_pending 为假）且验签可用时，旧更新链的 MITM 风险已消除。
                let canAuto = false;
                try {
                    const hs = await fetch("/hotupdate/status", {cache: "no-store"})
                        .then(r => (r.ok ? r.json() : null));
                    canAuto = !!hs && hs.verify_ok !== false && !hs.restart_pending;
                } catch (e) { canAuto = false; }
                _renderFound(msg, latest, cur, {
                    auto: true, canAuto, isAndroid,
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
        msg.textContent = "检查更新失败：暂时连不上更新服务，请稍后重试";
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
    // 2026-10-22 安全缓解（P8-5）原本在这里**无条件 return**，把整条自动更新链封死；
    // 2026-10-30 改为**按热更实际状态判断**：只有补丁尚未生效（restart_pending）
    // 或验签不可用（verify_ok===false）时才拦住 —— 那时旧更新链确实还有 MITM 风险；
    // 补丁已生效后继续拦着，只是白让用户多走一趟下载页。
    // 拿不到状态时按"拦住"处理（安全侧默认）。
    try {
        const hs = await fetch("/hotupdate/status", {cache: "no-store"})
            .then(r => (r.ok ? r.json() : null));
        if (!hs || hs.verify_ok === false || hs.restart_pending) {
            msg.textContent = "安全修复需要重启 App 后再生效；请完全退出并重新打开，或前往下载页手动安装。";
            return;
        }
    } catch (e) {
        msg.textContent = "安全修复需要重启 App 后再生效；请完全退出并重新打开，或前往下载页手动安装。";
        return;
    }
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
        console.warn("[update] 自动更新失败", e);   // 原始异常落日志，不拼进用户可见串
        msg.textContent = "自动更新没成功；请稍后重试，或前往下载页手动安装最新版";
    }
}
