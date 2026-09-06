/* ═══════════════════════════════════════════
   PC 双栏左侧导航（≥1100px 才激活）
   - petite-vue 按需加载：移动/窄屏设备永远不下载框架，旧 WebView 零风险
   - 导航动作复用 app.js 已有全局函数（showChat/showHome/openMenuTab/openFixView…）
   ═══════════════════════════════════════════ */
(function () {
    var MQ = "(min-width:1100px)";

    function desktop() {
        return window.matchMedia && matchMedia(MQ).matches;
    }

    function mount() {
        if (!window.PetiteVue || window.__pcNavMounted) return;
        window.__pcNavMounted = true;
        PetiteVue.createApp({
            current: "chat",
            items: [
                { key: "chat",    icon: "💬", label: "聊天" },
                { key: "home",    icon: "🏠", label: "首页" },
                { key: "char",    icon: "📖", label: "设定文件" },
                { key: "pack",    icon: "🎭", label: "角色包" },
                { key: "sticker", icon: "😊", label: "表情包" },
                { key: "fav",     icon: "⭐", label: "收藏" },
                { key: "fix",     icon: "🛠", label: "指出问题" },
            ],
            diagItems: [
                { key: "log",     icon: "📋", label: "请求记录" },
                { key: "pipeline",icon: "🧪", label: "流程日志" },
            ],
            go: function (item) {
                this.current = item.key;
                if (item.key === "chat") { showChat(); }
                else if (item.key === "home") { showHome(); }
                else if (item.key === "fix") { openFixView(); }
                else { openMenuTab(item.key); }   // 抽屉 tab → 右栏视图
            },
            goSettings: function () {
                this.current = "settings";
                openSettings();
            },
            switchTheme: function () { toggleTheme(); },
        }).mount("#pc-sidebar");
    }

    function loadFramework() {
        if (window.__pcNavLoading) return;
        window.__pcNavLoading = true;
        var s = document.createElement("script");
        s.src = "vendor/petite-vue.iife.js";
        s.onload = mount;
        document.head.appendChild(s);
    }

    function maybeInit() {
        if (desktop()) loadFramework();
    }

    // 视口跨过断点时补挂载（浏览器窗口拖大场景）
    if (window.matchMedia) {
        var mq = matchMedia(MQ);
        var onChange = function (e) { if (e.matches) maybeInit(); };
        if (mq.addEventListener) mq.addEventListener("change", onChange);
        else if (mq.addListener) mq.addListener(onChange);   // 旧 WebView 兜底
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", maybeInit);
    } else {
        maybeInit();
    }
})();
