// 运行模式（统一前端 0.8.0）：本文件在 index.html 中先于 app.js 加载。
// - 本地（PC 浏览器 / 安卓内置引擎 127.0.0.1:8765 由本静态目录提供服务）：local
// - 服务器网页（server/frontend/config.js）：定义 FIREFLY_SERVER_BASE → server
// - 安卓服务器模式（file:// 加载）：由壳拦截 config.js 请求动态注入两个字段
window.FIREFLY_MODE = "local";
// PC 服务器版入口单点（首页「服务器云端版」卡片 → 新标签打开；安卓壳 loadServerBase
// 也从本文件首 URL 解析服务器地址，两者必须一致——改这里时同步注意）
window.FIREFLY_SERVER_WEB = "http://101.200.14.126:8787";
