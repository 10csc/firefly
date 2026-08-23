// 主动性轮询与后台主动消息
import { S, SESSION_ID, inputEl, sendBtn } from "./state.js";
import { _inflight, renderMessages } from "./chat.js";
import { CURRENT_MODE, _modeGen, appView } from "./views.js";

// ═══════════════════════════════════════════
// 主动性轮询 — 流萤在合适的时候主动找开拓者说话
// ═══════════════════════════════════════════
// 轮询纪律（避免冲突）：
// - 仅在聊天页可见且空闲时检查（不等待回复、不在打字、距离上次回复 > 2 分钟）
// - 服务端门控保证频率（主动式=轮次+概率；概率式=时间静默+前端概率），不通过则零成本返回空
// - 空闲判定（概率式硬性）：无输入、无提交、无处理中（S.waiting/pending/输入框非空）
const _PROACTIVE_INTERVAL = 10 * 1000;   // 轮询周期 10s
const _PROACTIVE_QUIET = 2 * 60 * 1000;  // 主动式：回复渲染后 2 分钟内不检查
const _PROB_QUIET = 10 * 60 * 1000;      // 概率式：距上次渲染 10 分钟内不检查（与服务端静默阈值一致）

function _chatVisible() {
    return appView && appView.style.display !== "none";
}

// 空闲判定：无输入 / 无请求在飞 / 无思考锁 / 无渲染动画
export function _idleOk() {
    if (S.waiting) return false;
    if (S._rendering) return false;         // 主动消息渲染动画中
    if (_inflight > 0) return false;      // 发送请求在飞（等待回复）
    if (inputEl && inputEl.value.trim()) return false;
    return _chatVisible();
}

// 渲染主动消息：先锁定输入（思考 2~5s 模拟"想了想/想起什么"），期间禁止用户输入防竞态
async function _renderProactiveWithThink(data) {
    S._lastRenderTs = Date.now();
    S.waiting = true;
    inputEl.disabled = true; sendBtn.disabled = true;
    const statusEl = document.querySelector("#header .status");
    const defaultStatus = "会找到的，属于我的梦...";   // 固定简介（防快照污染）
    if (statusEl) statusEl.textContent = "对方正在输入...";
    const thinkMs = 2000 + Math.floor(Math.random() * 3000);
    await new Promise(r => setTimeout(r, thinkMs));
    if (statusEl) statusEl.textContent = defaultStatus;
    S.waiting = false;
    inputEl.disabled = false; sendBtn.disabled = false;
    renderMessages(data.messages, "firefly", data);
}

export async function checkProactive() {
    if (!_idleOk()) return;
    const gen = _modeGen;   // 捕获发起时的模式代际
    try {
        // 轮询探测：不改任何前端状态（大部分概率未中，闪状态是错的）
        const resp = await fetch("/proactive-status", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃旧模式主动消息（已写盘原模式）
        if (data.proactive && data.messages && data.messages.length) {
            // 确定要回复：才锁输入框 + 显示状态 + 思考延迟 + 渲染（全程锁防乱序）
            S._lastRenderTs = Date.now();
            S.waiting = true;
            inputEl.disabled = true; sendBtn.disabled = true;
            const statusEl = document.querySelector("#header .status");
            const defaultStatus = statusEl ? statusEl.textContent : "";
            if (statusEl) statusEl.textContent = "对方正在输入...";
            const thinkMs = 2000 + Math.floor(Math.random() * 3000);
            await new Promise(r => setTimeout(r, thinkMs));
            if (statusEl) statusEl.textContent = defaultStatus;
            S.waiting = false;
            inputEl.disabled = false; sendBtn.disabled = false;
            renderMessages(data.messages, "firefly", data);
            _notifyFirefly(data.messages);   // 后台触发的主动消息 → 状态栏通知（桥判断前台与否）
        }
        // 无消息：前端状态完全不动
    } catch (e) {
        // 网络异常：无状态变更，无需恢复（静默等下一轮）
    }
}
// ═══════════════════════════════════════════
// 服务器版后台主动（KeepAliveService 定时触发）
// ═══════════════════════════════════════════

export function _notifyFirefly(messages) {
    // 消息渲染后提醒：FireflyJs 桥仅 App 不在前台时发状态栏通知（前台不打扰，
    // 复刻本地版 _notify_reply_if_background 语义）
    try {
        if (window.FireflyJs && window.FireflyJs.notify && Array.isArray(messages)) {
            const texts = messages.filter(m => m && m.type === "text" && m.content)
                                  .map(m => m.content);
            if (texts.length) window.FireflyJs.notify("流萤 · AI", texts.join("\n").slice(0, 200));
        }
    } catch (e) {}
}

window.__serverProactive = async function () {
    // KeepAliveService 后台触发：hidden 开关判断 + 主动式/概率式门控在服务端，
    // relay 代发由页面 relay 引擎完成（本函数跑在页面，Key 可直达）
    if (!S._hiddenEnabled) return;
    await checkProactive();
};

setInterval(checkProactive, _PROACTIVE_INTERVAL);
