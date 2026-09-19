// 热更新前端（见 docs/热更新规范.md 与 热更新/02_实现契约.md §五/§六/§七）
//
// 前端在这条链路上有四件不可省的事：
//   ① 上报「忙/闲」——后端据此决定什么时候允许 reload（规范 §5.2.1：绝不打断用户）
//   ② 首帧渲染完成后上报 boot-ok —— 这是"启动成功判据"，坏补丁靠它才敢确认（§5.5）
//   ③ reload 前把草稿存 sessionStorage —— 一个会吞掉用户正在打的字的"静默修复"比不修还糟（§5.2.2）
//   ④ 把运行版本三元组露给用户，并给一个手动回滚的出口（信任问题，不只是合规）
//
// 写成自包含 IIFE：bundle 是单作用域拼接，不污染其它模块的名字。
(function () {
    "use strict";

    var POLL_MS = 10000;          // 本地请求，代价可忽略；10s 内完成"立即生效"
    var DRAFT_KEY = "firefly_hu_draft";
    var _busy = false;
    var _busyWhy = "";
    var _last = null;

    function $(id) { return document.getElementById(id); }

    function post(path, obj) {
        try {
            return fetch(path, {
                method: "POST", cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(obj || {}),
            });
        } catch (e) { return Promise.resolve(null); }
    }

    // ── ① 忙/闲上报 ────────────────────────────────
    function setBusy(busy, why) {
        if (busy === _busy && why === _busyWhy) return;
        _busy = busy; _busyWhy = why;
        post("/hotupdate/activity", { busy: busy, why: why });
    }

    function computeBusy() {
        // 输入框有内容 = 用户正在打字（后端还会叠加"模型下载中/正在应用"两个自有判据）
        var inp = $("msg-input");
        if (inp && inp.value && inp.value.trim()) return "typing";
        if (typeof _voicePlaying !== "undefined" && _voicePlaying) return "playing";
        return "";
    }

    function watchActivity() {
        var inp = $("msg-input");
        if (inp) {
            ["input", "focus", "compositionstart"].forEach(function (ev) {
                inp.addEventListener(ev, function () { setBusy(!!computeBusy(), computeBusy()); });
            });
            ["blur", "compositionend"].forEach(function (ev) {
                inp.addEventListener(ev, function () {
                    var w = computeBusy();
                    setBusy(!!w, w);
                });
            });
        }
        setInterval(function () {
            var w = computeBusy();
            setBusy(!!w, w);
        }, 3000);
    }

    // ── ③ 草稿保留 ──────────────────────────────────
    function saveDraft() {
        try {
            var inp = $("msg-input");
            var msgs = $("messages");
            sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
                text: inp ? inp.value : "",
                scroll: msgs ? msgs.scrollTop : 0,
                at: Date.now(),
            }));
        } catch (e) { /* 存不下也不能拦着 reload */ }
    }

    function restoreDraft() {
        try {
            var raw = sessionStorage.getItem(DRAFT_KEY);
            if (!raw) return;
            sessionStorage.removeItem(DRAFT_KEY);
            var d = JSON.parse(raw);
            if (Date.now() - (d.at || 0) > 60000) return;   // 太久的草稿不要（可能是上次崩的）
            var inp = $("msg-input");
            if (inp && d.text) {
                inp.value = d.text;
                if (typeof setBusy === "function") setBusy(true, "typing");
            }
            var msgs = $("messages");
            if (msgs && d.scroll) msgs.scrollTop = d.scroll;
        } catch (e) { /* 恢复失败无所谓 */ }
    }

    // ── ④ 状态展示 + 操作 ───────────────────────────
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function render(st) {
        var box = $("hotupdate-box");
        if (!box || !st) return;
        var run = st.running || {};
        var lines = [];
        var ver = esc(run.base_version || "") +
            (run.hot_serial ? ' <b style="color:var(--fg-accent)">hot.' + run.hot_serial + "</b>" : "");
        lines.push('<div>运行版本：' + ver +
            (run.patch_hash ? ' <span style="opacity:.6">' + esc(run.patch_hash) + "</span>" : "") +
            "</div>");
        if (!st.enabled) {
            lines.push('<div style="opacity:.75">热更新已关闭（只保留安全吊销）</div>');
        } else if (st.available) {
            lines.push('<div>可更新：修复 ' + st.available.serial + " · " +
                esc(st.available.note || "") + "</div>");
        } else if (st.overlay_files) {
            lines.push('<div style="opacity:.75">已应用 ' + st.overlay_files + " 个文件</div>");
        } else {
            lines.push('<div style="opacity:.6">没有待安装的修复</div>');
        }
        if (st.rolled_back_reason) {
            lines.push('<div style="color:#e0a05c">上次修复已回退：' + esc(st.rolled_back_reason) + "</div>");
        }
        if (st.last_error) {
            lines.push('<div style="opacity:.7">' + esc(st.last_error) + "</div>");
        }
        box.innerHTML = lines.join("");
        var rb = $("hotupdate-rollback-btn");
        if (rb) rb.style.display = st.applied_serial ? "" : "none";
        var ap = $("hotupdate-apply-btn");
        if (ap) ap.style.display = (st.enabled && st.available) ? "" : "none";
        var tg = $("hotupdate-toggle");
        if (tg) tg.checked = !!st.enabled;
    }

    function poll() {
        fetch("/hotupdate/status", { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (st) {
                _last = st;
                render(st);
                // ★ 生效：后端说可以刷新了，而且此刻确实空闲 → 存草稿后刷新
                if (st.reload_pending && st.idle) {
                    saveDraft();
                    location.reload();
                }
            })
            .catch(function () { /* 后端在重启/不可用：下一轮再试 */ });
    }

    function wire() {
        var tg = $("hotupdate-toggle");
        if (tg) {
            tg.addEventListener("change", function () {
                post("/hotupdate/action", { action: "set_enabled", enabled: tg.checked })
                    .then(poll);
            });
        }
        var ck = $("hotupdate-check-btn");
        if (ck) {
            ck.addEventListener("click", function () {
                var m = $("hotupdate-msg");
                if (m) m.textContent = "检查中…";
                post("/hotupdate/action", { action: "check" }).then(function (r) {
                    return r ? r.json() : null;
                }).then(function (d) {
                    if (m) m.textContent = (d && d.ok) ? "已检查" : ("检查失败：" + ((d && d.error) || "网络不可达"));
                    poll();
                }).catch(function () { if (m) m.textContent = "检查失败"; });
            });
        }
        var ap = $("hotupdate-apply-btn");
        if (ap) {
            ap.addEventListener("click", function () {
                var m = $("hotupdate-msg");
                if (m) m.textContent = "下载并安装中…";
                post("/hotupdate/action", { action: "apply" }).then(function (r) {
                    return r ? r.json() : null;
                }).then(function (d) {
                    if (m) m.textContent = (d && d.ok) ? "已安装，即将生效" : ("安装失败：" + ((d && d.error) || ""));
                    poll();
                }).catch(function () { if (m) m.textContent = "安装失败"; });
            });
        }
        var rb = $("hotupdate-rollback-btn");
        if (rb) {
            rb.addEventListener("click", function () {
                if (!confirm("回退到当前安装包的版本？")) return;
                post("/hotupdate/action", { action: "rollback" }).then(poll);
            });
        }
    }

    function boot() {
        restoreDraft();
        wire();
        // ★ 启动成功判据：首帧渲染完成（这里就是）→ 告诉后端"补丁是好的"
        post("/hotupdate/boot-ok", {});
        poll();
        setInterval(poll, POLL_MS);
        watchActivity();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
