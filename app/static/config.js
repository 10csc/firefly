// 运行模式（统一前端 0.8.0）：本文件在 index.html 中先于 app.js 加载。
// - 本地（PC 浏览器 / 安卓内置引擎 127.0.0.1:8765 由本静态目录提供服务）：local
// - 服务器网页（server/frontend/config.js）：定义 FIREFLY_SERVER_BASE → server
// - 安卓服务器模式（file:// 加载）：由壳拦截 config.js 请求动态注入两个字段
window.FIREFLY_MODE = "local";
