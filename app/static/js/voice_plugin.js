// ═══════════════════════════════════════════
// 语音插件页（首页 →「语音插件」）
//   GET  /voice/plugin              → 状态（5 态 + 缺什么 + 下载进度）
//   POST /voice/plugin {action,...} → install/cancel/enable/disable/uninstall/rescan/set_mood/set_repo
// 设计与边界见 docs/工具/tts.md。本页只做展示与派发，不做任何模型逻辑。
// ═══════════════════════════════════════════
let _vpTimer = null;

const _VP_LABEL = {
    downloading: "⏳ 下载中",
    not_installed: "⬇ 未安装",
    installed_disabled: "⏸ 已安装 · 未启用",
    unavailable: "⚠ 已启用 · 不可用",
    ready: "✅ 可用",
};

function _vpEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
        {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function _vpMB(n) {
    return (n / 1048576).toFixed(1) + " MB";
}

async function _vpFetchState() {
    try {
        const r = await fetch("/voice/plugin");
        return await r.json();
    } catch (e) {
        return {id: "unavailable", label: "无法连接后端", reason: String(e)};
    }
}

async function _vpAct(action, extra) {
    try {
        const r = await fetch("/voice/plugin", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(Object.assign({action: action}, extra || {})),
        });
        const d = await r.json();
        if (d && d.ok === false) showToast("操作失败：" + (d.error || "未知原因"));
        return d;
    } catch (e) {
        showToast("操作失败：无法连接后端");
        return null;
    }
}

function _vpRender(st) {
    const box = document.getElementById("vv-body");
    if (!box) return;
    const dl = st.download || {};
    const moodRows = Object.keys(st.moods || {}).map(k => {
        const on = (st.mood === k);
        return `<button class="vv-mood${on ? " on" : ""}" type="button"
             onclick="voiceSetMood('${_vpEsc(k)}')">${_vpEsc(st.moods[k])}</button>`;
    }).join(" ");

    // 操作按钮：按状态给出可用的那几个
    const btns = [];
    if (st.id === "downloading") {
        btns.push(`<button class="vv-btn" onclick="voiceAct('cancel')">取消下载</button>`);
    } else {
        if (st.repo_configured) {
            btns.push(`<button class="vv-btn primary" onclick="voiceAct('install')">${st.models_found ? "重新下载" : "下载模型"}</button>`);
        }
        btns.push(`<button class="vv-btn" onclick="voiceAct('rescan')">重新扫描</button>`);
        const vc = st.voice_cache || {};
        if (vc.files) {
            btns.push(`<button class="vv-btn danger" onclick="voiceAct('purge_voice')">清除语音缓存（${vc.files} 条）</button>`);
        }
        if (st.models_found) {
            btns.push(st.enabled
                ? `<button class="vv-btn" onclick="voiceAct('disable')">停用</button>`
                : `<button class="vv-btn primary" onclick="voiceAct('enable')">启用</button>`);
            btns.push(`<button class="vv-btn danger" onclick="voiceAct('uninstall')">卸载</button>`);
        }
    }

    // 下载进度：带 sha256 对账状态（2026-09-18）。
    //   verify=on  → 拿到了仓库清单，每个文件下完都会比对 sha256（坏文件不会落盘）
    //   verify=off → 拿不到清单（自建镜像无清单端点等），降级为只比大小
    const _vfy = dl.verify === "on"
        ? `· 已校验 ${dl.verified || 0} 个`
        : (dl.verify === "off" ? "· 未校验（仓库无清单）" : "");
    const _pct = Math.max(0, Math.min(100, Number(dl.percent) || 0));   // 纵深防御：后端若给出越界值也不显示
    const dlBar = dl.running ? `
      <div class="vv-dl">
        <div class="vv-dl-bar"><i style="width:${_pct}%"></i></div>
        <div class="vv-dl-txt">${_vpEsc(dl.file || "")} · ${_pct}%
          ${dl.total ? "（" + _vpMB(dl.done) + " / " + _vpMB(dl.total) + "）" : ""}
          · 已用 ${dl.elapsed || 0}s ${_vfy}</div>
      </div>` : (dl.error ? `<div class="vv-err">上次下载：${_vpEsc(dl.error)}</div>`
        : (dl.verified ? `<div class="vv-hint">上次下载已通过 sha256 校验（${dl.verified} 个文件）</div>` : ""));

    const missing = (st.missing_files || []).length ? `
      <div class="vv-err">缺 ${st.missing_files.length} 个文件：${_vpEsc(st.missing_files.join("、"))}</div>` : "";

    const repoBox = st.repo_configured ? "" : `
      <div class="vv-field">
        <div class="vv-hint">模型仓库地址（含 <code>{file}</code> 占位；留空则无法下载）</div>
        <input id="vv-repo" type="text" placeholder="https://www.modelscope.cn/api/v1/models/&lt;ns&gt;/&lt;name&gt;/repo?Revision=master&amp;FilePath={file}">
        <button class="vv-btn" onclick="voiceAct('set_repo',{url:document.getElementById('vv-repo').value})">保存地址</button>
      </div>`;

    box.innerHTML = `
      <div class="vv-card vv-state vv-state-${_vpEsc(st.id)}">
        <div class="vv-state-label">${_vpEsc(_VP_LABEL[st.id] || st.label || st.id)}</div>
        ${st.reason ? `<div class="vv-reason">${_vpEsc(st.reason)}</div>` : ""}
      </div>

      ${dlBar}${missing}

      <div class="vv-card">
        <div class="vv-h">语气</div>
        <div class="vv-moods">${moodRows}</div>
        <div class="vv-hint">作用范围：长按消息 →「转语音」时使用所选语气</div>
      </div>

      <div class="vv-card">
        <div class="vv-h">模型</div>
        <div class="vv-kv"><span>状态</span><b>${st.models_found ? "已就位" : "未安装"}</b></div>
        <div class="vv-kv"><span>体积</span><b>${st.models_found ? _vpMB(st.model_bytes) : "—"}</b></div>
        <div class="vv-kv"><span>目录</span><code>${_vpEsc(st.models_dir || (st.search_dirs || [])[0] || "")}</code></div>
        ${st.engine && st.engine.ready ? `<div class="vv-kv"><span>引擎</span><b>已装载</b></div>` : ""}
        ${(st.voice_cache && st.voice_cache.files)
            ? `<div class="vv-kv"><span>已生成语音</span><b>${st.voice_cache.files} 条 · ${_vpMB(st.voice_cache.bytes)}</b></div>`
            : ""}
      </div>

      <div class="vv-actions">${btns.join(" ")}</div>
      ${repoBox}

      <div class="vv-foot">
        语音模型来自第三方整合包；<b>语音原声版权归米哈游所有</b>，仅供个人二次创作使用，
        禁止售卖与再分发。模型仅存放在本机，不会上传到服务器。
      </div>`;
}

async function renderVoiceView() {
    const box = document.getElementById("vv-body");
    if (box && !box.innerHTML.trim()) box.innerHTML = `<div class="vv-loading">读取中…</div>`;
    const st = await _vpFetchState();
    _vpRender(st);
    // 下载中 → 自动轮询刷新进度
    if (_vpTimer) { clearTimeout(_vpTimer); _vpTimer = null; }
    if (st.id === "downloading") {
        _vpTimer = setTimeout(renderVoiceView, 1200);
    }
}

function openVoiceView() {
    homeView.classList.remove("show");
    const cv = document.getElementById("cards-view");
    if (cv) cv.classList.remove("show");
    const fv = document.getElementById("fix-view");
    if (fv) fv.classList.remove("show");
    const v = document.getElementById("voice-view");
    if (!v) return;
    v.classList.add("show");
    try { if (location.hash !== "#voice") history.pushState({voice: true}, "", "#voice"); } catch (e) {}
    renderVoiceView();
}
window.openVoiceView = openVoiceView;

function closeVoiceView() {
    const v = document.getElementById("voice-view");
    if (v) v.classList.remove("show");
    if (_vpTimer) { clearTimeout(_vpTimer); _vpTimer = null; }
    showHome();
    try { if (location.hash === "#voice") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closeVoiceView = closeVoiceView;

async function voiceAct(action, extra) {
    if (action === "uninstall" && !confirm("确定卸载语音插件？\n\n会删除本机的模型文件（约 872 MB）。\n已生成的语音不受影响（那些随「清理历史」处理）。")) return;
    if (action === "purge_voice" && !confirm("清除所有已生成的语音？\n\n只删音频文件 —— 模型、语气库、聊天记录都不受影响。\n清除后重新点「转语音」会用当前前端重新合成（旧音频可能带有已修复的问题）。")) return;
    await _vpAct(action, extra);
    renderVoiceView();
}
window.voiceAct = voiceAct;

async function voiceSetMood(mood) {
    await _vpAct("set_mood", {mood: mood});
    // 同步长按菜单用的默认语气（chat.js 的 _voiceMood）
    if (window.setVoiceMood) window.setVoiceMood(mood);
    renderVoiceView();
}
window.voiceSetMood = voiceSetMood;

// ═══════════════════════════════════════════
// 消息下面的「语音条」
//   参考微信「语音转文字」的形态：语音生成后**贴在消息气泡下方**（不是弹播放器）。
//   数据来源：/voice/plugin 的 voice_cache.seqs = {mode: {seq: 时长}}。
//   · 生成成功后立刻贴上去
//   · 历史渲染后再扫一遍贴回（刷新/翻页后仍在）
//   · 点它播放 / 再点暂停（播放区在条上，长按菜单不再承担"播放"职责）
// ═══════════════════════════════════════════
let _voiceSeqMap = null;        // {mode: {seq: dur}}
let _voiceAudio = null;
let _voicePlayingBar = null;

async function voiceSyncCache() {
    try {
        const st = await (await fetch("/voice/plugin")).json();
        _voiceSeqMap = (st && st.voice_cache && st.voice_cache.seqs) || {};
    } catch (e) {
        _voiceSeqMap = null;
    }
    return _voiceSeqMap;
}
window.voiceSyncCache = voiceSyncCache;

/** 该 seq 是否已有语音（同步查本地缓存表） */
function voiceHas(seq) {
    const m = _voiceSeqMap && _voiceSeqMap[CURRENT_MODE];
    return !!(m && Object.prototype.hasOwnProperty.call(m, String(seq)));
}
window.voiceHas = voiceHas;

function _voiceStopPlay() {
    if (_voiceAudio) { try { _voiceAudio.pause(); } catch (e) {} _voiceAudio = null; }
    if (_voicePlayingBar) {
        _voicePlayingBar.classList.remove("playing");
        const i = _voicePlayingBar.querySelector(".vb-ico");
        if (i) i.textContent = "🔊";
        _voicePlayingBar = null;
    }
}

function _voicePlayBar(bar, seq) {
    // 再点 = 暂停
    if (_voicePlayingBar === bar) { _voiceStopPlay(); return; }
    _voiceStopPlay();
    const a = new Audio("/voice-file?mode=" + encodeURIComponent(CURRENT_MODE)
                        + "&name=v" + seq + ".wav");
    _voiceAudio = a;
    _voicePlayingBar = bar;
    bar.classList.add("playing");
    const ico = bar.querySelector(".vb-ico");
    if (ico) ico.textContent = "⏸";
    const stop = () => { if (_voicePlayingBar === bar) _voiceStopPlay(); };
    a.addEventListener("ended", stop);
    a.addEventListener("error", () => { stop(); showToast("播放失败"); });
    a.play().catch(() => { stop(); showToast("播放失败（再点一次试试）"); });
}

/** 把语音条贴到某条消息下面（幂等）。返回条元素。
 *
 *  ★ 语音条是 `.msg-row` 的**直接子元素**，靠 `.msg-row.has-voice{flex-wrap:wrap}`
 *    换到第二行 —— 这样**头像仍与气泡底部对齐**（微信/QQ 的形态）。
 *    早先把条塞进 `.msg-col` 时，列变高 → `align-items:flex-end` 把头像拉到语音条底，
 *    头像会比气泡低一截（真机截图发现）。
 */
function voiceAttachBar(row, seq, dur) {
    if (!row || seq === null || seq === undefined) return null;
    const exist = row.querySelector(".voice-bar");
    if (exist) {
        if (dur) {
            const d = exist.querySelector(".vb-dur");
            if (d) d.textContent = Math.round(dur) + "″";
        }
        return exist;
    }
    if (!row.querySelector(".bubble")) return null;   // 没气泡的（表情包/旁白）不贴
    row.classList.add("has-voice");
    // 外层 .voice-row 占满一行（强制换行 → 头像仍与气泡底部对齐），
    // 内层 .voice-bar 保持内容宽度（早先直接给条 flex-basis:100% 会把它拉成整行宽）
    const wrap = document.createElement("div");
    wrap.className = "voice-row";
    const bar = document.createElement("div");
    bar.className = "voice-bar";
    bar.dataset.seq = String(seq);
    bar.title = "点击播放";
    bar.innerHTML = `<span class="vb-ico">🔊</span><span class="vb-dur">`
        + (dur ? Math.round(dur) + "″" : "…") + `</span>`;
    bar.addEventListener("click", (e) => { e.stopPropagation(); _voicePlayBar(bar, seq); });
    wrap.appendChild(bar);
    row.appendChild(wrap);
    return bar;
}
window.voiceAttachBar = voiceAttachBar;

/** 历史渲染后扫一遍，把已有语音的条贴回去 */
function voiceRestoreBars() {
    const m = _voiceSeqMap && _voiceSeqMap[CURRENT_MODE];
    if (!m) return 0;
    const root = (typeof messagesEl !== "undefined" && messagesEl) ? messagesEl : document;
    let n = 0;
    root.querySelectorAll(".msg-row[data-seq]").forEach((row) => {
        const s = parseInt(row.dataset.seq, 10);
        if (isNaN(s)) return;
        const k = String(s);
        if (Object.prototype.hasOwnProperty.call(m, k) && voiceAttachBar(row, s, m[k])) n++;
    });
    return n;
}
window.voiceRestoreBars = voiceRestoreBars;

/** 首次进聊天时：拉一次缓存表再贴 */
async function voiceInitBars() {
    await voiceSyncCache();
    voiceRestoreBars();
}
window.voiceInitBars = voiceInitBars;

/** 渲染后自动贴（首次会先拉一次缓存表；之后直接用内存里的表） */
let _voiceCacheSynced = false;
async function voiceRestoreBarsAuto() {
    try {
        if (!_voiceCacheSynced) {
            await voiceSyncCache();
            _voiceCacheSynced = true;
        }
        voiceRestoreBars();
    } catch (e) { /* 语音条失败绝不影响消息渲染 */ }
}
window.voiceRestoreBarsAuto = voiceRestoreBarsAuto;
