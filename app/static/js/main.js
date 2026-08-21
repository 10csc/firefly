// 启动编排：合并原文件两套启动路径（DOMContentLoaded 在 views.js）
import "./util.js";
import "./proactive.js";
import "./guide.js";
import { IS_SERVER } from "./api.js";
import { checkKey } from "./panels.js";
import { loadHistory } from "./chat.js";
import { loadFixStatus } from "./fix.js";
import { CURRENT_MODE } from "./views.js";
import { initAssets, startRelay } from "./relay.js";

// ═══════════════════════════════════════════
// 起床检查
// ═══════════════════════════════════════════
async function checkWake() {
    try {
        const resp = await fetch(`/wake-status?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (data.interrupted) {
            document.getElementById("wake-overlay").style.display = "flex";
            document.getElementById("wake-text").textContent = "流萤正在起床，记忆还在整理中…";
        }
    } catch(e) {}
}

// ═══════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════
checkWake();
checkKey().then(() => { loadHistory(); });
initAssets();   // 服务器模式：已有 token 时立即资产本地化（未登录静默失败，登录后 initAuth 会再触发）
loadFixStatus();   // 设定纠错助手：恢复多轮对齐/待确认方案（本地与服务器模式都可用）
if (IS_SERVER) startRelay();   // relay 引擎仅服务器模式（本地为 direct 直发）
