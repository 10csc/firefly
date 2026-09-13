// 调试面板：请求记录 / 流水线日志（阶段 2.5 自 panels.js 拆出）

import { _esc } from "./util.js";
import { CURRENT_MODE } from "./views.js";

// ═══════════════════════════════════════════
// 请求记录
// ═══════════════════════════════════════════

async function loadRequestLog() {
    const list = document.getElementById("log-list");
    const countEl = document.getElementById("log-count");
    if (!list) return;
    try {
        const resp = await fetch("/requests");
        const data = await resp.json();
        if (countEl) {
            const totalCost = (data.requests || []).reduce((s, r) => s + (Number(r.cost_cny) || 0), 0);
            countEl.textContent = totalCost > 0
                ? `共 ${data.count} 次请求 · 累计约 ¥${totalCost.toFixed(3)}`
                : `共 ${data.count} 次请求`;
        }
        const rows = (data.requests || []).slice().reverse();
        if (rows.length === 0) {
            list.innerHTML = '<div style="color:#8a8a8a;padding:10px">暂无记录</div>';
            return;
        }
        list.innerHTML = rows.map(r => `
        <div style="display:flex;align-items:center;gap:4px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.8em;color:#c8d0e0">
            <span style="flex-shrink:0;width:50px;color:#8a8a8a">${r.time || "?"}</span>
            <span style="flex-shrink:0;width:64px">${r.module}</span>
            <span style="flex-shrink:0;width:52px">${r.model || "?"}</span>
            <span style="flex-shrink:0;width:22px;text-align:center">${r.success ? '<span style="color:#6c8">✓</span>' : '<span style="color:#c66">✗</span>'}</span>
            <span style="flex:1;text-align:right">${r.total_tokens || 0}</span>
            <span style="flex-shrink:0;width:70px;text-align:right;color:#8a8a8a">¥${(r.cost_cny || 0).toFixed(6)}</span>
        </div>`).join("");
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;padding:10px">加载失败</div>';
    }
}
window.loadRequestLog = loadRequestLog;

// ═══════════════════════════════════════════
// 流程日志：每轮各阶段的输入/输出/思考过程
// ═══════════════════════════════════════════

function _stageBlock(title, elapsed, fields) {
    const rows = fields
        .filter(([, v]) => v != null && String(v).trim() !== "")
        .map(([k, v]) => {
            const body = _esc(typeof v === "string" ? v : JSON.stringify(v, null, 1));
            if (k === "思考过程") {
                return `<details style="margin:2px 0"><summary style="cursor:pointer;color:#8a8a8a">思考过程（点开）</summary><pre style="white-space:pre-wrap;word-break:break-all;color:#8a8a8a;margin:4px 0;font-size:0.95em">${body}</pre></details>`;
            }
            return `<div style="margin:2px 0"><span style="color:#8a8a8a">${k}:</span> <span style="white-space:pre-wrap;word-break:break-all">${body}</span></div>`;
        }).join("");
    return `<details open style="margin:4px 0;padding:4px 8px;background:rgba(255,255,255,0.03);border-radius:6px">
        <summary style="cursor:pointer;color:#c8d0e0">${title}${elapsed != null ? ` <span style="color:#8a8a8a;font-size:0.85em">${elapsed}s</span>` : ""}</summary>
        <div style="padding:4px 0 2px">${rows}</div></details>`;
}

async function loadPipeline() {
    const list = document.getElementById("pipeline-list");
    const countEl = document.getElementById("pipeline-count");
    if (!list) return;
    try {
        const resp = await fetch(`/pipeline?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (countEl) countEl.textContent = `最近 ${data.count} 轮`;
        const rows = (data.pipeline || []).slice().reverse();
        if (rows.length === 0) {
            list.innerHTML = '<div style="color:#8a8a8a;padding:10px">暂无记录（本次启动后还没聊过）</div>';
            return;
        }
        list.innerHTML = rows.map(p => {
            let inner = "";
            if (p.error) {
                inner = `<div style="color:#c66;padding:4px 0">流水线异常: ${_esc(p.error)}</div>`;
            } else {
                const a = p.analyzer || {}, o = p.organizer || {}, po = p.polisher || {}, rt = p.retriever || {};
                inner =
                    _stageBlock("⓪ 知识检索", rt.elapsed, [
                        ["摘要", rt.knowledge],
                    ]) +
                    _stageBlock("① 分析器", a.elapsed, [
                        ["意图", a.intent],
                        ["事实核查", (a.fact_check || []).length ? a.fact_check : ""],
                        ["摘要", a.summary],
                        ["原始输出", a.raw_json],
                        ["思考过程", a.reasoning],
                    ]) +
                    _stageBlock("② 回复器", po.elapsed, [
                        ["原始输出", po.raw],
                        ["思考过程", po.reasoning],
                    ]) +
                    _stageBlock("③ 工具调度（表情包）", o.elapsed, [
                        ["选图", o.sticker_label || "（不发）"],
                        ["原始输出", o.raw],
                        ["思考过程", o.reasoning],
                    ]);
            }
            return `<div style="margin-bottom:14px;padding:8px;border:1px solid rgba(255,255,255,0.08);border-radius:8px;font-size:0.8em;color:#c8d0e0">
                <div style="margin-bottom:4px"><span style="color:#8a8a8a">${p.time || "?"}</span> 开拓者: <span style="color:#e0d5c1">${_esc(p.user_input)}</span>${p.hint ? ` <span style="color:#8a8a8a">(hint:${p.hint})</span>` : ""}</div>
                ${inner}
            </div>`;
        }).join("");
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;padding:10px">加载失败</div>';
    }
}
window.loadPipeline = loadPipeline;

// ═══════════════════════════════════════════
// 用户记忆（= memory.md，休息时自动整理的过往摘要）/ 用户设定（补充设定）
// ═══════════════════════════════════════════
