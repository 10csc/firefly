// 服务器版 relay 引擎与资产本地化
import { API_BASE, IS_SERVER, _serverFetch } from "./api.js";
import { CURRENT_MODE } from "./views.js";

// ═══════════════════════════════════════
// 后端代理（relay）— 服务器不持有用户 Key 的完整链路
// ═══════════════════════════════════════
// 服务器流水线的 LLM 请求在服务器入队（资产用 __CORE__ 等占位符表示），
// 本页 1s 轮询取件 → 占位符填充（本地资产）→ 用户 Key 直连 api_base 代发 → 回传。
// 资产（知识库/核心设定/身份/短信样本，~500KB）下载后缓存 localStorage（5MB 上限绰绰有余）。
let _assets = { knowledge: "", core: "", identity: "", sms_samples: "" };
let _relayBusy = false;

function _assetStoreKey(name) { return "firefly_asset_" + name + "_" + CURRENT_MODE; }

export async function initAssets() {
    // 资产本地化：清单指纹对比 → 差异下载 → 缓存。登录后/模式切换时调用。
    if (!IS_SERVER) return;   // 本地模式：知识库/设定由本地后端直接注入，无需本地化
    const mode = CURRENT_MODE;
    try {
        const idxResp = await fetch("/assets/index?mode=" + mode);
        const idx = await idxResp.json();
        const local = (() => { try { return JSON.parse(localStorage.getItem("firefly_assets_idx") || "{}"); } catch (e) { return {}; } })();
        const assetGroups = {
            knowledge: idx.knowledge, core: idx.character.core,
            identity: idx.character.identity, sms_samples: idx.character.sms_samples,
        };
        for (const [name, info] of Object.entries(assetGroups || {})) {
            const cacheKey = name + ":" + mode;
            const ver = (info && info.version) || "0";
            if ((local[cacheKey] || "") !== ver && ver !== "0") {
                try {
                    const rawResp = await fetch("/assets/raw?name=" + name + "&mode=" + mode);
                    const raw = await rawResp.json();
                    if (raw.content) {
                        localStorage.setItem(_assetStoreKey(name), raw.content);
                        local[cacheKey] = ver;
                    }
                } catch (e) { /* 单项失败不阻塞其余资产 */ }
            }
        }
        try { localStorage.setItem("firefly_assets_idx", JSON.stringify(local)); } catch (e) {}
    } catch (e) {
        // 同步失败（未登录/网络）：用已有缓存兜底（首次无缓存时占位符以空串填充，模型仍可聊天）
    }
    // 无论同步成败，从缓存装配当前模式资产
    for (const name of ["knowledge", "core", "identity", "sms_samples"]) {
        try { _assets[name] = localStorage.getItem(_assetStoreKey(name)) || ""; } catch (e) { _assets[name] = ""; }
    }
}

function fillPlaceholders(payload) {
    const msgs = payload && payload.messages;
    if (!Array.isArray(msgs)) return;
    for (const m of msgs) {
        if (typeof m.content === "string" && m.content.indexOf("__") >= 0) {
            m.content = m.content
                .replaceAll("__CORE__", _assets.core || "")
                .replaceAll("__IDENTITY__", _assets.identity || "")
                .replaceAll("__SMS_SAMPLES__", _assets.sms_samples || "")
                .replaceAll("__KNOWLEDGE__", _assets.knowledge || "");
        }
    }
}

async function relayTick() {
    // 1s 轮询取件。用原始 fetch 手动带头：避开包装器的 401 toast（未登录/过期时静默）。
    if (_relayBusy) return;
    const token = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
    if (!token) return;   // 未登录：服务器不会入队
    let pending = null;
    try {
        const resp = await _serverFetch(API_BASE + "/relay/pending", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
            body: "{}",
        });
        if (resp.status !== 200) return;
        pending = await resp.json();
    } catch (e) { return; }
    if (!pending || !pending.pending) return;
    _relayBusy = true;
    try {
        const apiBase = pending.api_base || "https://api.deepseek.com/v1";
        const key = (() => { try { return localStorage.getItem("firefly_api_key") || ""; } catch (e) { return ""; } })();
        if (!key) throw new Error("no key");
        fillPlaceholders(pending.payload);
        // 中转降级：服务器用本请求 X-API-Key 头代发（Key 内存即弃不落盘），
        // call_id 必须匹配服务器队列中真实 pending 项（非开放代理），
        // 服务器回传时已唤醒流水线（带状态码做错误分类），前端无需再调 /relay/result
        const proxyFallback = async () => {
            const p = await fetch("/relay/proxy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ call_id: pending.call_id, payload: pending.payload }),
            });
            const pd = await p.json();
            if (!pd.ok || !pd.response) throw new Error(pd.error || "proxy failed");
        };
        let ds;
        try {
            // 用户 Key 直连代发（DeepSeek 官方端点支持浏览器 CORS）
            ds = await _serverFetch(apiBase + "/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json", "Authorization": "Bearer " + key },
                body: JSON.stringify(pending.payload),
            });
        } catch (e1) {
            // 直连失败（如 OpenCode Go 端点不支持 CORS）→ 中转降级
            await proxyFallback();
            _relayBusy = false;
            return;
        }
        let respData = null;
        if (ds.ok) {
            try { respData = await ds.json(); }
            catch (e2) {
                // 200 但响应体异常 → 降级重试
                await proxyFallback();
                _relayBusy = false;
                return;
            }
        } else {
            // API 错误响应（401 Key 无效 / 402 余额不足 / 429 限流…）：
            // 照常回传（带状态码），服务器转成分类错误唤醒流水线 → 前端人话提示
            try { respData = await ds.json(); } catch (e3) { respData = {}; }
        }
        await _serverFetch(API_BASE + "/relay/result", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
            body: JSON.stringify({ call_id: pending.call_id, response: respData, status: ds.status }),
        });
    } catch (e) {
        // 直连与中转都失败：不回传 → 服务器侧 120s 超时，流水线自动降级话术
    }
    _relayBusy = false;
}
// 由 main.js 在全部模块求值后调用（直接顶层 setInterval 会在 api↔relay 循环导入中撞 TDZ）
export function startRelay() { setInterval(relayTick, 1000); }   // relay 引擎仅服务器模式（本地为 direct 直发）
