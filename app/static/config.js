// 运行模式（统一前端 0.8.0）：本文件在 index.html 中先于 app.js 加载。
// - 本地（PC 浏览器 / 安卓内置引擎 127.0.0.1:8765 由本静态目录提供服务）：local
// - 服务器网页（server/frontend/config.js）：定义 FIREFLY_SERVER_BASE → server
// - 安卓服务器模式（file:// 加载）：由壳拦截 config.js 请求动态注入两个字段
window.FIREFLY_MODE = "local";
// 注：原先这里还有 window.FIREFLY_SERVER_WEB（PC「打开服务器版」按钮用）。
// 2026-10-01 架构纠正：服务器**不再对外提供 App 前端**（只做后台 —— 客户端 API + 角色卡平台），
// 该按钮与入口卡片已删，本字段随之删除。
// 另注：安卓壳的服务器地址读的是打包进 APK 的 `assets/config.js`（源自 server/frontend/config.js
// 的 FIREFLY_SERVER_BASE，见 MainActivity.loadServerBase），**与本文件无关**。
