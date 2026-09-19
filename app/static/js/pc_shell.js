/* ═══════════════════════════════════════════════════════════════════
   PC 适配层（≥1100px）—— 见 docs/设计/PC端前端重构.md

   ★ 这个文件现在**只剩一件事**：把全局信息填进底部状态栏。

   为什么变得这么小：第一版在这里造了一整套 PC 导航（左侧图标栏 + 第二列目录 +
   顶栏 + 右侧上下文栏），把**手机 ☰ 菜单页里的东西**（收藏/表情包/设定文件/请求记录/
   流程日志）抄成了全局入口。后果是三重的（用户 2026-09-19 当场指出）：
     · 首页在展示"菜单页的内容"——那菜单页还留着干什么；
     · 同一个命令出现 2~3 条路径（微软 UX 指南明说：Present each command on only one tab；
       重复入口会让人"找到一条就不再找另一条"）；
     · 左边多出一栏谁也不明白它是干嘛的。
   现在：**层级与命名全部沿用手机**（全局顶栏 / 首页 / 聊天页 / 聊天页 ☰ 菜单），
   PC 只改"呈现"（菜单停靠到右侧、首页轮播改多列网格——都在 pc.css 里）。
   所以这里只剩状态栏，外加一个"记住用户有没有手动关掉状态栏"的位置。

   ★ 约束：不碰 bundle 内部，只用 window 上的既有入口；任何一步失败都不许影响页面。
   ═══════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    var MQ = '(min-width:1100px)';
    var POLL_MS = 30000;
    var PC = { hot: null, notice: null, auth: null, booted: false };

    function desktop() { return !!(window.matchMedia && matchMedia(MQ).matches); }
    function $(id) { return document.getElementById(id); }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
        });
    }
    function jget(url) {
        return fetch(url, { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .catch(function () { return null; });
    }

    // ── 底部状态栏：**信息**，不含任何命令 ──────────────
    function renderStatus() {
        var el = $('pc-statusbar');
        if (!el) return;
        var ver = window.__appVersion || '';
        var hot = PC.hot || {};
        var hotTxt = hot.enabled === false ? '自动修复已关'
            : (hot.applied_serial ? ('已应用修复 #' + hot.applied_serial) : '无修复');
        var lastSync = 0;
        try { lastSync = parseInt(localStorage.getItem('firefly_last_sync') || '0', 10) || 0; } catch (e) {}
        var syncTxt = '未登录', syncCls = '';
        if (PC.auth && PC.auth.logged_in) {
            if (lastSync) {
                var mins = Math.floor((Date.now() - lastSync) / 60000);
                syncTxt = mins < 1 ? '刚同步'
                    : (mins < 60 ? mins + ' 分钟前同步' : Math.floor(mins / 60) + ' 小时前同步');
                syncCls = mins > 24 * 60 ? 'warn' : 'ok';
            } else { syncTxt = '待同步'; syncCls = 'warn'; }
        }
        var unread = (PC.notice && PC.notice.unread) || 0;
        el.innerHTML =
            '<span><span class="psb-dot ' + (hot.applied_serial ? 'ok' : '') + '"></span>v'
            + esc(ver) + '</span>'
            + '<span><span class="psb-dot '
            + (hot.enabled === false ? 'warn' : (hot.applied_serial ? 'ok' : '')) + '"></span>'
            + esc(hotTxt) + '</span>'
            + '<span><span class="psb-dot ' + syncCls + '"></span>' + esc(syncTxt) + '</span>'
            + '<span class="psb-right"><span class="psb-dot ' + (unread ? 'warn' : '') + '"></span>'
            + (unread ? ('公告 ' + unread + ' 条未读') : '公告已读') + '</span>';
    }

    function refresh() {
        if (!desktop()) return;
        // 公告未读：读 notice.js 落下的本地缓存（不额外联网）
        try {
            var raw = localStorage.getItem('firefly_notice_cache');
            PC.notice = raw ? JSON.parse(raw) : null;
        } catch (e) { PC.notice = null; }
        return Promise.all([
            jget('/hotupdate/status').then(function (d) { if (d) PC.hot = d; }),
            jget('/auth/state').then(function (d) { if (d) PC.auth = d; }),
        ]).then(renderStatus, renderStatus);
    }

    function boot() {
        if (PC.booted || !desktop()) return;
        PC.booted = true;
        refresh();
        setInterval(function () { if (desktop()) refresh(); }, POLL_MS);
    }

    if (window.matchMedia) {
        var mq = matchMedia(MQ);
        var onChange = function (e) { if (e.matches) boot(); };
        if (mq.addEventListener) mq.addEventListener('change', onChange);
        else if (mq.addListener) mq.addListener(onChange);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
    window.__pcShell = PC;      // 排障用
})();
