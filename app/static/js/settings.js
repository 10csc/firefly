// 设置面板配置：供应商与模型管理 / 主动性预设 / 配置加载（loadConfig）与自动保存（_scheduleAutoSave）
import { S } from "./state.js";
import { getLocalApiKey } from "./util.js";
import { IS_SERVER, applyApiSource } from "./api.js";

// ═══════════════════════════════════════════
// 配置管理
// ═══════════════════════════════════════════
// 配置管理（设置页分组：账号与连接 / 主动消息 / 模型与速度 / 外观 / 数据与系统）
// A8 多供应商：_providers 列表 + _activeId（本地存后端 config.json；服务器版存浏览器 localStorage）
let _providers = [];
let _activeId = "deepseek";
const PROVIDERS_LS_KEY = "firefly_providers";
const ACTIVE_PROVIDER_LS_KEY = "firefly_active_provider";
const _MODEL_INPUT_IDS = ["retriever-model-input", "analyzer-model-input", "polisher-model-input", "organizer-model-input"];

const _PROACTIVE_PRESETS = {
    less:   { hard: 8, soft: 0.25 },
    medium: { hard: 6, soft: 0.35 },
    often:  { hard: 4, soft: 0.50 },
};
let _configLoaded = false;
let _saveTimer = null;

function _$(id) { return document.getElementById(id); }

function _proactivePresetName(hard, soft) {
    if (hard <= 4 && soft >= 0.45) return "often";
    if (hard >= 8) return "less";
    return "medium";
}

function _activeProviderObject() {
    return _providers.find(p => p.id === _activeId) || _providers[0] || null;
}

function _providersLS() {
    try { return JSON.parse(localStorage.getItem(PROVIDERS_LS_KEY) || "[]"); } catch (e) { return []; }
}

function _setProvidersLS(list, active) {
    try {
        localStorage.setItem(PROVIDERS_LS_KEY, JSON.stringify(list));
        localStorage.setItem(ACTIVE_PROVIDER_LS_KEY, active);
    } catch (e) {}
    // 兼容旧字段：relay.js / fetch 包装器仍读 firefly_api_key / firefly_api_base
    try {
        const p = list.find(x => x.id === active);
        if (p) {
            localStorage.setItem("firefly_api_key", p.api_key || "");
            localStorage.setItem("firefly_api_base", p.base_url || "");
        }
    } catch (e) {}
}

async function _getProviderModels(provider) {
    // 本地版：后端带 Key 转发；服务器版：浏览器直连（Key 在本机）
    if (!provider.base_url || !/^https?:\/\//.test(provider.base_url)) {
        throw new Error("接口地址必须是 http(s) 开头");
    }
    if (!provider.api_key) throw new Error("请先填写该供应商的 API Key");
    let resp;
    if (IS_SERVER) {
        resp = await fetch(provider.base_url.replace(/\/+$/, "") + "/models", {
            headers: { "Authorization": "Bearer " + provider.api_key },
        });
    } else {
        resp = await fetch("/models?provider=" + encodeURIComponent(provider.id));
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok && !IS_SERVER) throw new Error(data.error || "获取失败");
    if (IS_SERVER && !resp.ok) {
        // 直连失败（CORS/网络）→ 让后端代查（服务器转发自带 Key 的请求）
        throw new Error("直连获取失败（" + (data.error && data.error.message ? data.error.message : resp.status) + "）");
    }
    const models = IS_SERVER ? (data.data || []).map(m => m.id).filter(Boolean)
                             : (data.models || []);
    if (!models.length) throw new Error(IS_SERVER ? "供应商未返回模型清单" : "未返回模型清单");
    return models;
}

function _renderProviderSelect() {
    const sel = _$("provider-select");
    if (!sel) return;
    sel.innerHTML = "";
    for (const p of _providers) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name + ((p.api_key || p._has_key) ? "" : "（未填 Key）");
        sel.appendChild(opt);
    }
    if (_providers.length) sel.value = _activeId;
}

function _renderModelSuggest() {
    const dl = _$("model-suggest");
    const p = _activeProviderObject();
    if (!dl) return;
    const models = (p && p.models && p.models.length) ? p.models
        : ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"];
    dl.innerHTML = "";
    for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m;
        dl.appendChild(opt);
    }
}

function _updateKeyGuide() {
    const box = _$("provider-guide");
    if (!box) return;
    const p = _activeProviderObject();
    if (!p) { box.innerHTML = ""; return; }
    const isDp = /deepseek\.com/.test(p.base_url || "");
    box.innerHTML = isDp
        ? `① 浏览器打开 <b>platform.deepseek.com</b>，注册并登录<br>② 左侧「API Keys」→ 创建，复制 <b>sk-</b> 开头的 Key<br>③ 粘贴到「${p.name}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`
        : `① 打开供应商控制台（<b>${p.base_url.replace(/\/+$/, "")}</b> 所在站点主页）创建 API Key<br>② 粘贴到「${p.name}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`;
}

function _openProviderForm(provider) {
    const form = _$("provider-form");
    if (!form) return;
    form.style.display = "block";
    _$("provider-name").value = provider ? provider.name : "";
    _$("provider-base").value = provider ? provider.base_url : "https://";
    _$("provider-key").value = "";
    _$("provider-key").placeholder = provider && provider.api_key ? "已设置，留空保留" : "sk-...";
    _$("provider-form-msg").textContent = "";
    form.dataset.editing = provider ? provider.id : "";
    _$("provider-delete").style.display = provider ? "" : "none";
}

function _closeProviderForm() {
    const form = _$("provider-form");
    if (form) form.style.display = "none";
}

async function _fetchAndFillModels() {
    const msg = _$("provider-form-msg");
    const id = _$("provider-form").dataset.editing;
    const name = _$("provider-name").value.trim();
    const base = _$("provider-base").value.trim().replace(/\/+$/, "");
    const key = _$("provider-key").value.trim();
    const cur = _providers.find(p => p.id === id) || {};
    if (!/^https?:\/\//.test(base)) { msg.textContent = "接口地址必须是 http(s) 开头"; return; }
    msg.textContent = "获取中…";
    try {
        const models = await _getProviderModels({ id: id || cur.id || "custom", base_url: base, api_key: key || cur.api_key });
        const p = _providers.find(x => x.id === id);
        if (p) { p.models = models; _renderModelSuggest(); msg.textContent = "已获取 " + models.length + " 个模型，记得点「保存供应商」"; }
        else { msg.textContent = "模型清单：" + models.slice(0, 5).join("、") + (models.length > 5 ? " 等" + models.length + " 个" : "") + "（保存供应商后生效）"; }
    } catch (e) { msg.textContent = "获取失败：" + e.message; }
}

async function _saveProviderForm() {
    const msg = _$("provider-form-msg");
    const name = _$("provider-name").value.trim();
    const base = _$("provider-base").value.trim().replace(/\/+$/, "");
    const key = _$("provider-key").value.trim();
    const editing = _$("provider-form").dataset.editing;
    if (!name) { msg.textContent = "请填写名称"; return; }
    if (!/^https?:\/\//.test(base)) { msg.textContent = "接口地址必须是 http(s) 开头"; return; }
    const existing = editing && _providers.find(p => p.id === editing);
    if (existing) {
        existing.name = name; existing.base_url = base;
        if (key) existing.api_key = key;
    } else {
        const id = "p" + Date.now().toString(36);
        _providers.push({ id, name, base_url: base, api_key: key,
                          models: [], caps: {} });
        _activeId = id;
    }
    _renderProviderSelect();
    _renderModelSuggest();
    _updateKeyGuide();
    updateSettingsSummaries();
    await saveConfigNow(true);
    _closeProviderForm();
}

function _deleteProviderForm() {
    const editing = _$("provider-form").dataset.editing;
    if (!editing || _providers.length <= 1) { _$("provider-form-msg").textContent = "至少保留一个供应商"; return; }
    if (!confirm("删除供应商「" + editing + "」？")) return;
    _providers = _providers.filter(p => p.id !== editing);
    if (_activeId === editing) _activeId = _providers[0].id;
    _renderProviderSelect();
    _renderModelSuggest();
    _updateKeyGuide();
    updateSettingsSummaries();
    saveConfigNow(true);
    _closeProviderForm();
}

function _applyProactivePreset(name) {
    const p = _PROACTIVE_PRESETS[name] || _PROACTIVE_PRESETS.medium;
    _$("proactive-hard-slider").value = p.hard;
    _$("proactive-soft-slider").value = Math.round(p.soft * 100);
    _$("proactive-hard-value").textContent = p.hard;
    _$("proactive-soft-value").textContent = Math.round(p.soft * 100) + "%";
    updateSettingsSummaries();
}

function updateSettingsSummaries() {
    const ps = _$("proactive-summary");
    if (ps) {
        const on = _$("proactive-enabled").checked;
        const preset = _$("proactive-preset");
        ps.textContent = on ? ("开启 · " + (preset && preset.selectedOptions[0] ? preset.selectedOptions[0].textContent : "偶尔")) : "已关闭";
    }
    const ms = _$("model-summary");
    if (ms) {
        const p = _activeProviderObject();
        const pm = _$("polisher-model-input") ? _$("polisher-model-input").value.trim() : "";
        ms.textContent = (p ? p.name : "DeepSeek") + " · " + (pm || "Flash");
    }
}

function _buildSettingsPayload() {
    return {
        analyzer_model: _$("analyzer-model-input").value.trim(),
        retriever_model: _$("retriever-model-input").value.trim(),
        organizer_model: _$("organizer-model-input").value.trim(),
        polisher_model: _$("polisher-model-input").value.trim(),
        retriever_effort: _$("retriever-effort-select").value,
        analyzer_effort: _$("analyzer-effort-select").value,
        polisher_effort: _$("polisher-effort-select").value,
        organizer_effort: _$("organizer-effort-select").value,
        retriever_temperature: parseFloat(_$("retriever-temp-slider").value) || 0,
        proactive_enabled: _$("proactive-enabled").checked,
        proactive_hard: parseInt(_$("proactive-hard-slider").value) || 6,
        proactive_soft: (parseInt(_$("proactive-soft-slider").value) || 35) / 100,
        prob_reply_enabled: _$("prob-reply-enabled").checked,
        prob_reply_value: (parseInt(_$("prob-reply-slider").value) || 10) / 100,
        hidden_reply_enabled: _$("hidden-reply-enabled").checked,
    };
}

async function _postSettings(payload, msg) {
    const resp = await fetch("/set-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        msg.textContent = "保存失败：" + (data.error || "请稍后再试");
        return false;
    }
    S._hiddenEnabled = payload.hidden_reply_enabled !== false;
    msg.textContent = "已保存 ✓";
    clearTimeout(msg._timer);
    msg._timer = setTimeout(() => { msg.textContent = ""; }, 2500);
    return true;
}

async function saveConfigNow(explicit) {
    const msg = _$("config-msg");
    const srcSel = _$("api-source-select");
    const src = srcSel ? srcSel.value : "own";
    const keyInput = _$("key-input");
    const k = keyInput ? keyInput.value.trim() : "";
    const p0 = _activeProviderObject();
    if (p0 && k) p0.api_key = k;   // Key 输入框 → 当前激活供应商
    if (IS_SERVER) {
        try { localStorage.setItem("firefly_api_source", src); } catch (e) {}
        if (src !== "proxy") _setProvidersLS(_providers, _activeId);
        if (keyInput) keyInput.value = "";
    }
    const payload = _buildSettingsPayload();
    if (!IS_SERVER) {
        // 本地版：供应商列表 + 激活项随配置落盘（Key 在 providers 内）
        payload.providers = _providers;
        payload.active_provider = _activeId;
        if (k) payload.api_key = k;   // 后端写入激活供应商
    }
    if (explicit) msg.textContent = "保存中…";
    const ok = await _postSettings(payload, msg);
    if (ok && keyInput) keyInput.value = "";
}

function _scheduleAutoSave() {
    if (!_configLoaded) return;
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        const msg = _$("config-msg");
        if (msg) msg.textContent = "自动保存中…";
        saveConfigNow(false);
    }, 400);
}

export async function loadConfig() {
    const ids = {
        a: "analyzer-model-input", r: "retriever-model-input",
        o: "organizer-model-input", p: "polisher-model-input",
        re: "retriever-effort-select", ae: "analyzer-effort-select",
        pe: "polisher-effort-select", oe: "organizer-effort-select",
        rt: "retriever-temp-slider", rtv: "retriever-temp-value",
        k: "key-input", m: "config-msg",
        pe_: "proactive-enabled", ph: "proactive-hard-slider",
        phv: "proactive-hard-value", ps: "proactive-soft-slider",
        psv: "proactive-soft-value",
        pr_: "prob-reply-enabled", pr: "prob-reply-slider",
        prv: "prob-reply-value",
        hr_: "hidden-reply-enabled",
    };
    try {
        const resp = await fetch("/config");
        const data = await resp.json();
        const el = {};
        for (const [k, id] of Object.entries(ids)) el[k] = document.getElementById(id);

        const normEffort = v => (v === "low" ? "low" : v);
        if (el.a) el.a.value = data.analyzer_model || "deepseek-v4-flash-vision-exp";
        if (el.r) el.r.value = data.retriever_model || "deepseek-v4-flash-vision-exp";
        if (el.o) el.o.value = data.organizer_model || "deepseek-v4-flash-vision-exp";
        if (el.p) el.p.value = data.polisher_model || "deepseek-v4-flash-vision-exp";
        if (el.re) el.re.value = normEffort(data.retriever_effort || "none");
        if (el.ae) el.ae.value = normEffort(data.analyzer_effort || "high");
        if (el.pe) el.pe.value = normEffort(data.polisher_effort || "high");
        if (el.oe) el.oe.value = normEffort(data.organizer_effort || "none");

        const hard = data.proactive_hard != null ? data.proactive_hard : 6;
        const soft = data.proactive_soft != null ? data.proactive_soft : 0.35;
        const prob = data.prob_reply_value != null ? data.prob_reply_value : 0.10;
        if (el.pe_) el.pe_.checked = data.proactive_enabled !== false;
        if (el.ph) {
            el.ph.value = hard;
            if (el.phv) el.phv.textContent = hard;
        }
        if (el.ps) {
            el.ps.value = Math.round(soft * 100);
            if (el.psv) el.psv.textContent = Math.round(soft * 100) + "%";
        }
        if (el.pr_) el.pr_.checked = data.prob_reply_enabled !== false;
        if (el.pr) {
            el.pr.value = Math.round(prob * 100);
            if (el.prv) el.prv.textContent = Math.round(prob * 100) + "%";
        }
        if (el.hr_) el.hr_.checked = data.hidden_reply_enabled !== false;
        S._hiddenEnabled = data.hidden_reply_enabled !== false;
        const pp = _$("proactive-preset");
        if (pp) pp.value = _proactivePresetName(hard, soft);

        // 供应商（A8）：本地版走后端 /config；服务器版供应商配置在浏览器 localStorage
        if (IS_SERVER) {
            _providers = _providersLS();
            _activeId = (() => { try { return localStorage.getItem(ACTIVE_PROVIDER_LS_KEY) || ""; } catch (e) { return ""; } })();
            if (!_providers.length) {
                // 服务器模式首次初始化：继承 legacy 字段（firefly_api_key/api_base——
                // 旧版/升级前直接存这里），避免升级后首个会话把已有 Key 当“未设置”丢一次
                const legacyKey = getLocalApiKey();
                const legacyBase = (() => { try { return localStorage.getItem("firefly_api_base") || ""; } catch (e) { return ""; } })();
                _providers = [{ id: "deepseek", name: "DeepSeek",
                                base_url: legacyBase || "https://api.deepseek.com/v1",
                                api_key: legacyKey || "", models: [], caps: {} }];
                _activeId = "deepseek";
                _setProvidersLS(_providers, _activeId);
            }
            const p0 = _providers.find(p => p.id === _activeId) || _providers[0];
            if (p0) _activeId = p0.id;
        } else {
            _providers = (data.providers || []).map(p => ({
                id: p.id, name: p.name, base_url: p.base_url,
                api_key: "", models: p.models || [], caps: p.caps || {},
                _has_key: !!p.has_key, _key_prefix: p.key_prefix || "",
            }));
            _activeId = data.active_provider || (_providers[0] && _providers[0].id) || "deepseek";
        }

        const srcSel = _$("api-source-select");
        const srcField = _$("api-source-field");
        const localSrc = (() => { try { return localStorage.getItem("firefly_api_source") || "own"; } catch (e) { return "own"; } })();
        if (srcSel) {
            srcSel.value = localSrc === "proxy" ? "proxy" : "own";
            applyApiSource(IS_SERVER && localSrc === "proxy");
        }
        if (srcField) srcField.style.display = IS_SERVER ? "" : "none";
        // A5：增量同步（本地版登录后可用；服务器版数据天然在云端）
        const syncNowField = _$("sync-now-field");
        if (syncNowField) syncNowField.style.display = IS_SERVER ? "none" : "";

        if (data.retriever_temperature != null && el.rt) {
            el.rt.value = data.retriever_temperature;
            if (el.rtv) el.rtv.textContent = Number(data.retriever_temperature).toFixed(1);
        }

        if (el.m) {
            const keyLabel = _$("key-label");
            const pA = _activeProviderObject();
            const providerLabel = pA ? pA.name : "DeepSeek";
            if (IS_SERVER) {
                const localKey = getLocalApiKey();
                el.m.textContent = localKey ? "Key 已设置（仅存本机浏览器）" : "尚未设置 API Key（不会上传服务器）";
                if (keyLabel) keyLabel.textContent = "API Key（存于本机浏览器，不会上传服务器）";
            } else {
                el.m.textContent = pA && pA._has_key
                    ? "Key 已设置（" + (pA._key_prefix || "仅本机") + "）"
                    : "尚未设置 API Key";
                if (keyLabel) keyLabel.textContent = "API Key（存本机配置文件，仅本机使用）";
            }
        }
        if (el.k) {
            if (IS_SERVER) {
                const localKey = getLocalApiKey();
                el.k.placeholder = localKey ? "已设置，留空则保留" : "sk-...";
            } else {
                const pA = _activeProviderObject();
                el.k.placeholder = pA && pA._has_key ? "已设置，留空则保留原 Key" : "sk-...";
            }
            el.k.value = "";
        }

        // 供应商 UI（选择器 / 模型建议 / 获取 Key 教程）
        _renderProviderSelect();
        _renderModelSuggest();
        _updateKeyGuide();

        const hiddenField = _$("hidden-reply-field");
        if (hiddenField) hiddenField.style.display = window.androidWakeLock ? "" : "none";

        const exitRow = _$("app-exit-row");
        if (exitRow && data.platform === "pc") {
            exitRow.style.display = "";
            const exitBtn = _$("app-exit-btn");
            if (exitBtn) exitBtn.onclick = () => {
                if (!confirm("确定退出流萤吗？聊天数据已实时保存，下次启动继续。")) return;
                exitBtn.disabled = true;
                exitBtn.textContent = "正在退出…";
                fetch("/shutdown", {method: "GET"}).catch(() => {});
            };
        }

        _configLoaded = true;
        updateSettingsSummaries();
        return data;
    } catch (e) { return {has_key: false}; }
}

export async function checkKey() {
    try { await loadConfig(); } catch (e) { /* 服务未就绪，静默 */ }
}

(function initSetGroups() {
    document.querySelectorAll("#settings-panel .set-head").forEach(head => {
        head.addEventListener("click", () => {
            const group = head.closest(".set-group");
            const willOpen = !group.classList.contains("open");
            document.querySelectorAll("#settings-panel .set-group").forEach(g => g.classList.remove("open"));
            if (willOpen) group.classList.add("open");
        });
    });
})();

_$("retriever-temp-slider")?.addEventListener("input", () => {
    _$("retriever-temp-value").textContent = Number(_$("retriever-temp-slider").value).toFixed(1);
});
_$("proactive-hard-slider")?.addEventListener("input", () => {
    _$("proactive-hard-value").textContent = _$("proactive-hard-slider").value;
});
_$("proactive-soft-slider")?.addEventListener("input", () => {
    _$("proactive-soft-value").textContent = _$("proactive-soft-slider").value + "%";
});
_$("prob-reply-slider")?.addEventListener("input", () => {
    _$("prob-reply-value").textContent = _$("prob-reply-slider").value + "%";
});

_$("proactive-preset")?.addEventListener("change", () => {
    _applyProactivePreset(_$("proactive-preset").value);
    _scheduleAutoSave();
});

["retriever-model-input", "analyzer-model-input", "polisher-model-input", "organizer-model-input",
 "retriever-effort-select", "analyzer-effort-select", "polisher-effort-select", "organizer-effort-select",
 "retriever-temp-slider", "proactive-enabled", "proactive-hard-slider", "proactive-soft-slider",
 "prob-reply-enabled", "prob-reply-slider", "hidden-reply-enabled"].forEach(id => {
    const el = _$(id);
    if (el) el.addEventListener("change", () => { updateSettingsSummaries(); _scheduleAutoSave(); });
});

// A8 供应商 UI 接线
_$("provider-select")?.addEventListener("change", () => {
    const sel = _$("provider-select");
    if (sel && sel.value) {
        _activeId = sel.value;
        _renderModelSuggest();
        _updateKeyGuide();
        updateSettingsSummaries();
        _scheduleAutoSave();
    }
});
_$("provider-add")?.addEventListener("click", () => _openProviderForm(null));
_$("provider-edit")?.addEventListener("click", () => {
    const p = _activeProviderObject();
    if (p) _openProviderForm(p);
});
_$("provider-save")?.addEventListener("click", () => _saveProviderForm());
_$("provider-delete")?.addEventListener("click", () => _deleteProviderForm());
_$("provider-cancel")?.addEventListener("click", () => _closeProviderForm());
_$("provider-fetch-models")?.addEventListener("click", () => _fetchAndFillModels());

const apiSourceSel = _$("api-source-select");
if (apiSourceSel) {
    apiSourceSel.addEventListener("change", () => applyApiSource(apiSourceSel.value === "proxy"));
}

_$("key-save")?.addEventListener("click", () => saveConfigNow(true));
