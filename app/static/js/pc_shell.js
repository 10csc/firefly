/* ═══════════════════════════════════════════════════════════════════
   PC 适配层（≥1100px）—— 见 docs/设计/PC端重构-IA方案.md

   ★ 本文件在 PC 档（≥1100px）负责四件事，全部只在 desktop() 分支里跑：
     ① 底部状态栏（**只有信息、没有命令**：版本/热更/同步/公告未读）；
     ② 侧边栏高亮（读各 view 的 class，不改视图状态）；
     ③ 联系人列（角色卡=联系人，点一下即切卡，复用既有 enterMode）；
     ④ 交互壳：聊天页右缘菜单把手（pcMenuToggle）+ 卡详情页内抽屉（pcOpenPackDrawer）。
   窄屏（<1100px）一行都不执行；结构在 index.html 只**追加**，样式在 pc.css 且只在
   @media(min-width:1100px) 内生效 ⇒ 手机端逐字节不受影响。

   为什么这么克制（2026-09-19 第一版被否的教训）：第一版在这里造了一整套 PC 导航
   （左侧图标栏 + 第二列目录 + 顶栏 + 右侧上下文栏），把**手机 ☰ 菜单页里的东西**
   （收藏/表情包/设定文件/请求记录/流程日志）抄成了全局入口 ⇒ 同一命令出现 2~3 条路径
   （微软 UX 指南：Present each command on only one tab）。现在：层级与命名全部沿用手机，
   PC 只改"呈现"，且卡外/卡内严格分层（卡内命令只出现在聊天页头/菜单页）。

   ★ 约束：不碰 bundle 内部，只用 window 上的既有入口；任何一步失败都不许影响页面。
   ═══════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    var MQ = '(min-width:1100px)';
    var POLL_MS = 30000;
    var PC = { hot: null, notice: null, auth: null, booted: false, contactSig: '', drawerReturn: '' };

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
        ]).then(function () { renderStatus(); renderContacts(); markNav(); },
                function () { renderStatus(); renderContacts(); markNav(); });
    }

    /* ── 三栏外壳（骨架，2026-10-02；IA 见 docs/设计/PC端重构-IA方案.md）──────────
       中列 = **角色卡（联系人）**：点一下即切卡（复用手机端实际那条 `enterMode()`
       = `_switchMode` + `showChat`，见 views.js），解决旧审计 A6"PC 切包比手机还绕"。
       侧边栏只做**卡外**入口（写死在 index.html 的 onclick，这里只负责高亮当前所在那一栏），
       **不新增任何业务逻辑**。
       ⚠ 全部只在 desktop() 分支里执行 ⇒ 手机端一行都不跑。 */

    function presets() {
        try { return (window.__getPresets && window.__getPresets()) || []; } catch (e) { return []; }
    }

    // 「模式标记」标签与角色卡库列表（views.js renderCardsList）**同一套映射**，
    // 保证联系人列的标记写着与卡库一致的词（复用 m.presentation，不新造字段）。
    var PRES_LABEL = { sticker: '短信+表情包', narration: '短信+旁白', none: '纯短信' };

    function renderContacts() {
        var host = $('pc-contact-list');
        if (!host || !desktop()) return;
        var list = presets();
        var cur = '';
        try { cur = (window.__getCurrentMode && window.__getCurrentMode()) || ''; } catch (e) {}
        // 幂等：签名没变就不重建 DOM（避免每次点击都重排、抢焦点）。
        // 签名带上 presentation/name：换卡或改模式标记时也能重画。
        var sig = cur + '#' + list.map(function (m) {
            return [m.id, m.char_name || m.name || '', m.name || '', m.presentation || '', m.avatar || ''].join('|');
        }).join(',');
        if (sig === PC.contactSig) return;
        PC.contactSig = sig;
        host.textContent = '';
        if (!list.length) {
            var empty = document.createElement('div');
            empty.className = 'pc-contact-sub';
            empty.textContent = '还没有角色卡';
            host.appendChild(empty);
            return;
        }
        list.forEach(function (m) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'pc-contact' + (m.id === cur ? ' active' : '');
            b.title = m.name ? (m.name + (m.char_name ? ' · ' + m.char_name : '')) : (m.id || '');
            if (m.avatar) {
                var img = document.createElement('img');
                img.alt = '';
                img.src = m.avatar + '?t=' + Date.now();
                b.appendChild(img);
            } else {
                var ph = document.createElement('span');
                ph.className = 'pc-avatar-ph';
                ph.textContent = String(m.char_name || m.name || '?').slice(0, 1);
                b.appendChild(ph);
            }
            var main = document.createElement('div');
            main.className = 'pc-contact-main';
            // 角色名（主行，卡片主角是角色，与首页大卡/卡库列表口径一致）
            var nm = document.createElement('div');
            nm.className = 'pc-contact-name';
            nm.textContent = m.char_name || m.name || m.id;
            main.appendChild(nm);
            // 卡名 / 模式名（副行）
            if (m.name) {
                var sub = document.createElement('div');
                sub.className = 'pc-contact-sub';
                sub.textContent = m.name;
                main.appendChild(sub);
            }
            // 模式标记（复用 m.presentation；取不到就不渲染，不凭空造）
            var tagTxt = PRES_LABEL[m.presentation] || m.presentation || '';
            if (tagTxt) {
                var tag = document.createElement('span');
                tag.className = 'pc-contact-mode';
                tag.textContent = tagTxt;
                main.appendChild(tag);
            }
            b.appendChild(main);
            b.addEventListener('click', function () {
                // 点一下即切卡：复用**手机端实际用的那条**路径 enterMode()
                // （views.js：enterMode = _switchMode(校验+持久化) + showChat），不另造切换逻辑。
                try {
                    if (window.enterMode) window.enterMode(m.id);
                    else {
                        if (window.__setCurrentMode) window.__setCurrentMode(m.id);
                        if (window.showChat) window.showChat();
                    }
                } catch (e) {}
                PC.contactSig = '';        // 强制重画（高亮换人）
                renderContacts();
                markNav();
            });
            host.appendChild(b);
        });
    }

    /** 侧边栏高亮：当前显示的是哪一栏（只读 `classList.contains('show')`，不改视图状态）。 */
    function markNav() {
        if (!desktop()) return;
        [['home', 'home-view'], ['cards', 'cards-view'], ['plaza', 'plaza-view'],
         ['voice', 'voice-view'], ['settings', 'settings-panel'],
         ['notice', 'notice-panel'], ['feedback', 'feedback-panel']].forEach(function (p) {
            var btn = document.querySelector('.pc-nav-btn[data-pc="' + p[0] + '"]');
            var v = $(p[1]);
            if (!btn || !v) return;
            btn.classList.toggle('active', v.classList.contains('show'));
        });
    }

    /* ── 聊天页右缘的菜单把手（2026-10-02）────────────────────────────
       把手只调**既有** `openMenu()/closeMenu()`；`#menu-drawer` 打开时由这里给 <html> 挂
       `pc-menu-open`，pc.css 用它让聊天区**让位**（不遮输入区）并把把手贴到菜单左缘。
       用 MutationObserver 观察抽屉的 class，而不是自己维护开关状态 —— 单一事实来源在抽屉上。

       ⚠ 2026-10-02 修真实 bug：这个把手原先**直接**调 `window.openMenu()`，既没做存在性
       判断，也依赖 bundle 顶层声明已执行完（bundle.js:961 的 `function openMenu()` 在 classic
       script 里确实会挂到 window —— 所以 `window.openMenu` **是存在的**；但若脚本尚未执行到
       该顶层函数声明，此刻 `window.openMenu` 仍为 undefined，顺序不稳）。于是这个把手在边界
       时机会「展」不开。
       正确做法（不碰 bundle.js / panels.js）：`openMenuCompat` 先做函数类型判断，命中才调
       `window.openMenu`；否则回退 `.click()` 既有的 `#menu-btn` —— 它的 click 已绑定 openMenu
       （bundle.js:959），且程序化 `.click()` 对 `display:none` 元素照样派发（PC 档 `#menu-btn`
       被 pc.css 隐藏）。关闭仍走 `window.closeMenu`（bundle.js:972）。 */
    function syncMenuOpen() {
        if (!desktop()) return;
        var d = $('menu-drawer');
        var open = !!(d && d.classList.contains('open'));
        document.documentElement.classList.toggle('pc-menu-open', open);
        var h = $('pc-menu-handle');
        if (h) h.setAttribute('aria-expanded', open ? 'true' : 'false');
        PC.menuOpen = open;
    }

    /** 打开菜单：优先既有 `window.openMenu`（先做函数类型判断，避免顶层声明未执行完时的 undefined 时序），
     *  否则回退点击既有 `#menu-btn`（其 click 绑定了 openMenu）。 */
    function openMenuCompat() {
        if (typeof window.openMenu === 'function') { window.openMenu(); return; }
        var b = $('menu-btn');
        if (b && b.click) b.click();
    }

    window.pcMenuToggle = function () {
        if (!desktop()) return;                    // 窄屏：把手不存在，直接不做事
        try {
            var d = $('menu-drawer');
            var open = !!(d && d.classList.contains('open'));
            if (open) { if (window.closeMenu) window.closeMenu(); }
            else { openMenuCompat(); }
        } catch (e) {}
        setTimeout(syncMenuOpen, 30);
    };

    function watchMenu() {
        var d = $('menu-drawer');
        if (!d || typeof MutationObserver !== 'function') return;
        new MutationObserver(syncMenuOpen).observe(d, { attributes: true, attributeFilter: ['class'] });
        syncMenuOpen();
    }

    /* ── 卡详情 = 聊天页内右侧抽屉（T5，2026-10-02）───────────────────────
       复用**同一个** `#pack-view`（不新增第二套呈现，避免"抽屉 + 整页"并存）：
       PC 档由 pc.css 把 `#pack-view` 从整页覆盖改成受限宽抽屉（右缘、留出聊天列），
       窄屏一行不改（仍是整页）。这里只做三件事，且全在 desktop() 分支里：
         ① 菜单页的「卡详情」入口（`pcOpenPackDrawer`，见 index.html 的 .menu-actions）；
         ② 打开时给 <html> 挂 `pc-drawer-open` ⇒ pc.css 里聊天列 `right: --pc-drawer-w` 让位；
         ③ 从聊天页打开的这一路，关闭后回到聊天页（`closePackView` 默认回首页，见 views.js）。
       ⚠ 单一事实来源在 `#pack-view.show` 上（用 MutationObserver 观察，不自己维护开关）。 */
    window.pcOpenPackDrawer = function () {
        if (!desktop()) return;                    // 窄屏：入口不显形；即便被调也不做事
        PC.drawerReturn = 'chat';                  // 记住"从聊天页进来的"，关闭后回聊天页
        try { if (window.closeMenu) window.closeMenu(); } catch (e) {}   // 菜单与抽屉不同时占右缘
        try {
            var mode = (window.__getCurrentMode && window.__getCurrentMode()) || '';
            if (window.openPackView) window.openPackView(mode || undefined);
        } catch (e) {}
        setTimeout(syncDrawerOpen, 30);
    };

    function syncDrawerOpen() {
        if (!desktop()) return;
        var v = $('pack-view');
        document.documentElement.classList.toggle('pc-drawer-open', !!(v && v.classList.contains('show')));
    }

    function watchDrawer() {
        var v = $('pack-view');
        if (!v || typeof MutationObserver !== 'function') return;
        new MutationObserver(function () {
            if (!desktop()) return;
            var open = v.classList.contains('show');
            document.documentElement.classList.toggle('pc-drawer-open', open);
            if (!open && PC.drawerReturn === 'chat') {
                PC.drawerReturn = '';              // 只回一次，避免影响后续正常导航
                try { if (window.showChat) window.showChat(); } catch (e) {}
            }
        }).observe(v, { attributes: true, attributeFilter: ['class'] });
        syncDrawerOpen();
    }

    /** 预设包清单是 bundle 里 loadModes() 异步填进 PRESET_MODES 的；pc_shell 在
        DOMContentLoaded 就 renderContacts()，可能比它早 ⇒ 联系人列会停在"还没有角色卡"。
        观察 #home-modes（loadModes→renderModeCards 会重灌它的子节点）在清单到位时重画。 */
    function watchPresets() {
        var hm = $('home-modes');
        if (!hm || typeof MutationObserver !== 'function') return;
        new MutationObserver(function () { renderContacts(); })
            .observe(hm, { childList: true });
    }

    function boot() {
        if (PC.booted || !desktop()) return;
        PC.booted = true;
        refresh();
        watchMenu();
        watchDrawer();
        watchPresets();
        // 兜底的两拍重画（清单可能在观察器挂上之前就已到位）
        setTimeout(function () { renderContacts(); markNav(); }, 800);
        setTimeout(function () { renderContacts(); markNav(); }, 2500);
        setInterval(function () { if (desktop()) refresh(); }, POLL_MS);
        // 视图切换没有统一事件：捕获阶段的一次点击 + 微延时同步"高亮 + 联系人"（不轮询）
        document.addEventListener('click', function () {
            setTimeout(function () { renderContacts(); markNav(); }, 60);
        }, true);
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
