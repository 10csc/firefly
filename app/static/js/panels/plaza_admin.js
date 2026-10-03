// 角色卡广场治理（P4-3）：举报弹层 + 管理员管理台
//
// 位置：与「广场」（panels/plaza.js）、「制卡」（panels/plaza_forge.js）**同一个域**。
//   · 举报弹层 `#pz-modal`：覆盖广场整页的一层（详情页按「举报」入口打开）；
//   · 管理台 `#plaza-admin`：广场面板内的一层，与 #plaza-list / #plaza-detail / #plaza-forge 同级。
// 关闭链路：广场面板关掉 → plaza.js 的 `_pzClose` → `plazaAdminLeave()` 一起收（不另起导航）。
//
// 五条实现约束（与 plaza.js / plaza_forge.js 同源，别改）：
//  1. **前端不猜角色**：管理端点只有 admin 能过，其它账号后端一律 403 —— 这里不缓存
//     "我是不是管理员"，每次进来都按后端答复说话（403 → 人话提示，不装死也不假装成功）。
//  2. **不轮询**：本模块没有任何定时器；只在打开管理台 / 换筛选 / 下架恢复之后各请求一次。
//  3. **能用 textContent 就不用 innerHTML**：举报理由、卡名、审核理由都是用户数据。
//  4. **不用属性选择器找 JS 属性**（docs/错误总结.md #19）：DOM 引用一律走闭包/`_paDom` 变量。
//  5. **前缀 `_pa` 防撞**：bundle 是单作用域，前缀是防撞的唯一有效手段。
//
// 接口真相（app/plaza/api.py 已实现并有 30 项后端测试，前端只调**相对路径**）：
//   POST /plaza/api/report        {id, reason}         → {ok:true, id}
//   GET  /plaza/api/admin/list    ?status=&page=&size= → {ok,total,page,size,items:[…公开字段…,
//                                                          report_count, reports:[{reason,at}], review]}
//   POST /plaza/api/admin/status  {id, status}         → {ok:true, id, status}
import { showToast } from "../util.js";

// ── 常量 ────────────────────────────────────────────
const _PA_REASONS = ["色情低俗", "暴力血腥", "违法违规", "侵犯权益", "越狱注入", "站外引流", "其他"];
const _PA_FILTERS = [["all", "全部"], ["published", "已发布"], ["pending", "待复核"], ["archived", "已归档"]];
const _PA_STATUS = {draft: "草稿", pending: "待复核", published: "已发布",
                    rejected: "已驳回", archived: "已归档"};
const _PA_VERDICT = {pass: "通过", reject: "未通过", manual: "转人工复核"};
const _PA_RISK = {low: "低", medium: "中", high: "高"};
const _PA_PAGE = 20;
const _PA_REASON_MAX = 120;          // 与后端 store.add_report 的 [:120] 对齐

// ── 状态（一律 _pa 前缀）─────────────────────────────
const _paS = {
    booted: false, open: false, busy: false,
    status: "all", page: 1, total: 0, items: [],
    modalSeq: 0, reportId: "", reportName: "", reason: "",
};
const _paDom = {};                   // 只在本模块建立/持有；不做任何 querySelector 查找

// ═══ 小工具 ═══════════════════════════════════════════
function _paEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
}

function _paBtn(cls, text, onClick) {
    const b = _paEl("button", cls, text);
    b.type = "button";
    b.onclick = onClick;
    return b;
}

/** 带鉴权的 JSON 请求。返回 {ok, status, data}——**不抛异常**，由调用方决定文案。 */
async function _paReq(url, opts) {
    try {
        const r = await fetch(url, Object.assign({headers: {"Accept": "application/json"}}, opts || {}));
        const st = r.status;
        let d = null;
        try { d = await r.json(); } catch (e) { d = null; }
        return {ok: st >= 200 && st < 300, status: st, data: d};
    } catch (e) {
        return {ok: false, status: 0, data: null, net: true};
    }
}

function _paPost(url, body) {
    return _paReq(url, {method: "POST", headers: {"Content-Type": "application/json"},
                        body: JSON.stringify(body || {})});
}

/** 错误 → 人话。**403 是"非 admin"的唯一权威口径**（后端只认角色，前端不猜）。 */
function _paWhy(r, fallback) {
    if (r && r.status === 0) return "连不上角色卡广场，请检查网络后重试（你的卡与聊天记录不受影响）。";
    if (r && r.status === 401) return "需要登录后才能使用管理台。";
    if (r && r.status === 403) return "当前账号没有管理权限（如需协助请联系管理员）。";
    if (r && r.status === 404) return "这张卡已不在广场上，刷新列表即可。";
    const e = r && r.data && r.data.error;
    return String(e || fallback || "操作失败，请稍后重试。");
}

// ═══ 举报弹层 ═════════════════════════════════════════
function _paReportOpen(cardId, cardName) {
    const wrap = document.getElementById("pz-modal");
    if (!wrap) return;
    _paS.reportId = String(cardId || "");
    _paS.reportName = String(cardName || cardId || "");
    _paS.reason = "";
    const seq = ++_paS.modalSeq;
    wrap.textContent = "";
    wrap.classList.add("show");

    const box = _paEl("div", "pz-modal-box");
    box.appendChild(_paEl("div", "pz-modal-title", "举报「" + _paS.reportName + "」"));
    box.appendChild(_paEl("div", "pz-modal-note",
        "选一个理由（可补一句说明）。同一张卡只记一次；举报会交到管理员手里复核，不会直接删卡。"));

    // 提交按钮先建出来（理由 chip 的 onclick 要改它的禁用态）
    const cancel = _paBtn("pz-btn", "取消", () => _paReportClose());
    const ok = _paBtn("pz-btn primary", "提交举报", null);
    ok.id = "pzr-ok";
    ok.disabled = true;                     // ★ 不预选默认理由：理由错了等于白举报
    ok.title = "先选一个举报理由";

    const chips = _paEl("div", "pzr-reasons");
    const chipEls = [];
    for (const r of _PA_REASONS) {
        const btn = _paBtn("pzr-reason", r, () => {
            _paS.reason = r;
            for (const x of chipEls) x.classList.toggle("on", x.textContent === r);
            ok.disabled = false;
            ok.title = "";
        });
        chipEls.push(btn);
        chips.appendChild(btn);
    }
    const note = document.createElement("input");
    note.type = "text";
    note.id = "pzr-note";
    note.autocomplete = "off";
    note.maxLength = _PA_REASON_MAX;
    note.placeholder = "补一句说明（可留空）";

    const err = _paEl("div", "pz-modal-err", "");
    const acts = _paEl("div", "pz-modal-acts");
    acts.append(cancel, ok);
    box.append(chips, note, err, acts);
    wrap.appendChild(box);

    ok.onclick = async () => {
        if (_paS.busy || !_paS.reason) return;
        const extra = String(note.value || "").trim();
        const reason = (extra ? (_paS.reason + "：" + extra) : _paS.reason).slice(0, _PA_REASON_MAX);
        _paS.busy = true;
        ok.disabled = true;
        cancel.disabled = true;
        ok.textContent = "提交中…";
        const r = await _paPost("/plaza/api/report", {id: _paS.reportId, reason: reason});
        _paS.busy = false;
        if (seq !== _paS.modalSeq) return;          // 弹层已被关掉/重开：不再动这层 UI
        ok.disabled = false;
        cancel.disabled = false;
        ok.textContent = "提交举报";
        if (!r.ok) {
            err.textContent = _paWhy(r, "举报没能提交，请稍后重试。");
            err.style.display = "block";
            return;
        }
        showToast("已收到，我们会尽快处理");
        _paReportDone();                             // 结果留在弹层里（toast 会消失，这个不会）
    };
    try { note.focus(); } catch (e) {}
}

/** 提交成功后的结果态：把弹层换成一句明确的答复（用户不必猜"到底提交上没有"）。 */
function _paReportDone() {
    const wrap = document.getElementById("pz-modal");
    if (!wrap) return;
    wrap.textContent = "";
    const box = _paEl("div", "pz-modal-box");
    const okBox = _paEl("div", "pzr-ok", "已收到，我们会尽快处理。");
    okBox.id = "pzr-done";
    okBox.appendChild(_paEl("div", "pzr-ok-sub",
        "举报已记在「" + _paS.reportName + "」名下，管理员复核时能看到。感谢你帮忙看着广场。"));
    const acts = _paEl("div", "pz-modal-acts");
    acts.appendChild(_paBtn("pz-btn primary", "知道了", () => _paReportClose()));
    box.append(okBox, acts);
    wrap.appendChild(box);
}

function _paReportClose() {
    _paS.modalSeq++;
    const wrap = document.getElementById("pz-modal");
    if (!wrap) return;
    wrap.classList.remove("show");
    wrap.textContent = "";
}

// ═══ 管理台骨架 ═══════════════════════════════════════
function _paBuild() {
    const view = document.getElementById("plaza-admin");
    if (!view) return false;
    view.textContent = "";

    const head = _paEl("div", "cv-head");
    const back = _paBtn("cv-back", "←", () => closePlazaAdmin());
    back.setAttribute("aria-label", "返回广场列表");
    const title = _paEl("div", "cv-title", "广场管理台");
    const refresh = _paBtn("pza-refresh", "刷新", () => _paLoad(true));
    refresh.id = "pza-refresh-btn";
    head.append(back, title, refresh);

    const bar = _paEl("div", "pza-bar");
    const filters = _paEl("div", "pz-chips");
    filters.id = "pza-filters";
    for (const [v, t] of _PA_FILTERS) {
        const b = _paBtn("pz-chip", t, () => {
            if (_paS.status === v) return;
            _paS.status = v;
            _paRenderFilters();
            _paLoad(true);
        });
        b._paVal = v;
        filters.appendChild(b);
    }
    bar.appendChild(filters);

    const msg = _paEl("div", "pz-msg");
    msg.id = "pza-msg";
    const list = _paEl("div", "pza-list");
    list.id = "pza-list";
    const more = _paEl("div", "pz-more", "加载更多");
    more.id = "pza-more";
    more.style.display = "none";
    more.onclick = () => { if (!_paS.busy && _paS.items.length < _paS.total) _paLoad(false); };

    view.append(head, bar, msg, list, more);
    _paDom.view = view; _paDom.filters = filters; _paDom.msg = msg;
    _paDom.list = list; _paDom.more = more;
    _paRenderFilters();
    return true;
}

function _paRenderFilters() {
    const box = _paDom.filters;
    if (!box) return;
    // 引用走自己建的按钮上的 JS 属性（**不是**属性选择器，见错误总结 #19）
    for (const b of box.children) b.classList.toggle("on", b._paVal === _paS.status);
}

function _paSetMsg(text, retry) {
    const el = _paDom.msg;
    if (!el) return;
    el.textContent = "";
    el.style.display = text ? "flex" : "none";
    if (!text) return;
    el.appendChild(_paEl("div", "pz-msg-txt", text));
    if (retry) el.appendChild(_paBtn("pz-btn", retry.label, retry.onClick));
}

function _paUpdateMore() {
    const more = _paDom.more;
    if (!more) return;
    if (_paS.items.length < _paS.total) {
        more.style.display = "";
        more.textContent = "加载更多（已显示 " + _paS.items.length + " / " + _paS.total + "）";
        more.disabled = !!_paS.busy;
    } else {
        more.style.display = "none";
    }
}

// ═══ 管理台列表 ═══════════════════════════════════════
async function _paLoad(reset) {
    if (!_paDom.list) return;
    if (reset) {
        _paS.page = 1;
        _paS.items = [];
        _paDom.list.textContent = "";
    }
    if (_paS.busy) return;
    _paS.busy = true;
    if (!_paS.items.length) _paSetMsg("正在读取管理数据…", null);

    const q = "?status=" + encodeURIComponent(_paS.status)
        + "&page=" + encodeURIComponent(String(_paS.page))
        + "&size=" + encodeURIComponent(String(_PA_PAGE));
    const r = await _paReq("/plaza/api/admin/list" + q);
    _paS.busy = false;

    if (!r.ok) {
        _paUpdateMore();
        _paSetMsg(_paWhy(r, "管理数据读取失败，请稍后重试。"),
                  r.status === 0 ? {label: "重试", onClick: () => _paLoad(true)} : null);
        return;
    }
    const d = r.data || {};
    const items = Array.isArray(d.items) ? d.items : [];
    _paS.total = Number(d.total) || 0;
    for (const c of items) {
        if (c && c.id && !_paS.items.some(x => x.id === c.id)) _paS.items.push(c);
    }
    const frag = document.createDocumentFragment();
    for (const c of items) frag.appendChild(_paRow(c));
    _paDom.list.appendChild(frag);
    _paS.page++;
    _paSetMsg(_paS.items.length ? "" : "这个状态下没有卡片。", null);
    _paUpdateMore();
}

function _paFmtTime(sec) {
    const t = Number(sec) || 0;
    if (!t) return "";
    const d = new Date(t * 1000);
    if (isNaN(d.getTime())) return "";
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}

function _paRow(c) {
    const row = _paEl("div", "pza-row");
    const st = String(c.status || "published");
    const archived = st === "archived";

    const top = _paEl("div", "pza-row-top");
    top.appendChild(_paEl("div", "pza-name", c.name || c.id));
    top.appendChild(_paEl("span", "pza-badge " + st, _PA_STATUS[st] || st));

    const meta = [];
    meta.push("id " + String(c.id || ""));
    if (c.category) meta.push(String(c.category));
    if (c.char_name) meta.push("角色：" + String(c.char_name));
    meta.push("下载 " + (Number(c.downloads) || 0));
    const pub = _paFmtTime(c.published_at || c.created_at);
    if (pub) meta.push("发布于 " + pub);

    // 举报数 + 理由（管理员一眼看到"为什么被举报"）
    const n = Number(c.report_count) || 0;
    const rep = _paEl("div", "pza-reports" + (n ? " hot" : ""),
                      n ? ("举报 " + n + " 次") : "暂无举报");
    const reasons = (Array.isArray(c.reports) ? c.reports : [])
        .map(x => String((x && x.reason) || "").trim()).filter(Boolean);
    if (reasons.length) {
        rep.appendChild(_paEl("span", "pza-reasons", " · " + reasons.slice(-3).join(" / ")));
    }

    // 审核结论
    const rev = c.review || {};
    const v = String(rev.verdict || "");
    const bits = [];
    if (v) bits.push(_PA_VERDICT[v] || v);
    if (rev.risk) bits.push("风险 " + (_PA_RISK[rev.risk] || rev.risk));
    if (rev.model) bits.push("模型 " + String(rev.model));
    const review = _paEl("div", "pza-review", "审核：" + (bits.length ? bits.join(" · ") : "未提交"));

    const acts = _paEl("div", "pza-acts");
    const arch = _paBtn("pz-btn small pza-arch", "下架", () => _paAct(c, "archived", arch));
    arch.disabled = archived;
    arch.title = archived ? "这张卡已经是已归档状态" : "从公开广场撤下（内容与举报记录都留着）";
    const rest = _paBtn("pz-btn small" + (archived ? " primary" : "") + " pza-restore", "恢复",
                        () => _paAct(c, "published", rest));
    rest.disabled = !archived;
    rest.title = archived ? "重新放回公开广场" : "只有已归档/未公开的卡需要恢复";
    acts.append(arch, rest);

    row.append(top, _paEl("div", "pza-meta", meta.join(" · ")), rep, review, acts);
    return row;
}

async function _paAct(c, status, btn) {
    if (_paS.busy) return;
    const label = status === "archived" ? "下架" : "恢复";
    if (status === "archived"
        && !confirm("下架「" + (c.name || c.id) + "」？\n公开广场上不再显示这张卡；内容与举报记录都留着，可随时恢复。")) {
        return;
    }
    const old = btn ? btn.textContent : label;
    if (btn) { btn.disabled = true; btn.textContent = label + "中…"; }
    _paS.busy = true;
    const r = await _paPost("/plaza/api/admin/status", {id: c.id, status: status});
    _paS.busy = false;
    if (btn) { btn.disabled = false; btn.textContent = old; }
    if (!r.ok) {
        showToast(_paWhy(r, label + "失败，请稍后重试。"));
        return;
    }
    showToast((status === "archived" ? "已下架「" : "已恢复「") + (c.name || c.id) + "」");
    // ★ 公开广场列表必须跟着变：否则用户回列表还看得见刚下架的卡（"操作成功但世界没变"）。
    try { window.plazaReloadList && window.plazaReloadList(); } catch (e) {}
    _paLoad(true);
}

// ═══ 面板开关 ═════════════════════════════════════════
function _paInit() {
    if (_paS.booted) return !!_paDom.view;      // 已初始化是**成功**状态，直接放行
    if (!document.getElementById("plaza-admin")) return false;
    if (!_paBuild()) return false;
    _paS.booted = true;
    return true;
}

function openPlazaAdmin() {
    if (!_paInit()) return;
    try { window.closePlazaForge && window.closePlazaForge(); } catch (e) {}
    const root = document.getElementById("plaza-view");
    if (root && !root.classList.contains("show")) {
        try { window.openPlaza && window.openPlaza(); } catch (e) {}   // 深链进来时带上广场面板
    }
    if (root) root.classList.add("plaza-admin-on");
    if (_paDom.view) _paDom.view.classList.add("show");
    _paS.open = true;
    _paRenderFilters();
    _paLoad(true);
}

function closePlazaAdmin() {
    _paS.open = false;
    _paReportClose();
    if (_paDom.view) _paDom.view.classList.remove("show");
    const root = document.getElementById("plaza-view");
    if (root) root.classList.remove("plaza-admin-on");
    // 管理台改的就是广场那一份数据（下架/恢复）→ 回列表要拿到新结果
    try { window.plazaReloadList && window.plazaReloadList(); } catch (e) {}
}

/** 广场面板关闭 → 治理层跟着收（否则下次打开广场会直接落在管理台上）。
 *  由 panels/plaza.js 的 _pzClose 调用（同一域的收尾，见该文件注释）。 */
function plazaAdminLeave() {
    _paReportClose();
    if (!_paS.open) return;
    closePlazaAdmin();
}

window.openPlazaAdmin = openPlazaAdmin;
window.closePlazaAdmin = closePlazaAdmin;
window.plazaAdminLeave = plazaAdminLeave;
window.plazaReportOpen = _paReportOpen;
