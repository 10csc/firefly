// 共享状态与 DOM 引用（原 app.js 头部 + 跨模块可变状态 S）
// 流萤聊天 App — 前端逻辑（统一前端 0.8.0：本地 / 服务器双模式一套代码）

export const messagesEl = document.getElementById("messages");
export const inputEl = document.getElementById("msg-input");
export const sendBtn = document.getElementById("send-btn");
export const SESSION_ID = "firefly-" + Date.now();

// 跨模块共享可变状态（ESM 导入绑定只读，跨模块赋值的 let 统一收口到 S）
export const S = {
    _flushTimer: null,
    _hasMore: false,
    _hiddenEnabled: true,
    _hintTimer: null,
    _lastRenderTs: 0,
    _rendering: false,
    waiting: false,
};
