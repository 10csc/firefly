// 角色卡制作（共创平台 M2）：站内填表制卡 → 存草稿 → 提交 AI 审核 → 发布到广场
//
// 位置：与「广场」「角色卡管理」同一个域（角色卡）。广场页头部进「✦ 制作角色卡」，
// 制卡页是**广场面板内的一层**（#plaza-forge 绝对铺满），所以返回键只回广场列表，
// 关广场时这一层跟着收掉——不另起导航，也不新增 id 给 pc.css 去适配。
//
// 2026-10-01（P4-7b 拆债 / P4-7c 契约 06）：本文件原来 1242 行（**全仓最大的前端文件**），
// 先按内聚拆开，再按冻结契约 06《制卡自由度与图片规格》把"固定几栏"升级成**自由编辑器**。
// 五个文件（bundle 是单作用域拼接，顺序见 ORDER；**新加的名字必须带 `_pf` 前缀**，重名会静默覆盖）：
//   · js/panels/plaza_forge.js        —— 本文件：外壳（常量/状态/DOM/小工具/提示区/骨架与表单骨架/面板开关）
//   · js/panels/plaza_forge_files.js  —— 文本层：固定三件 + 1..6 份知识库（可增删改名）+ 开场白条目（_pff*）
//   · js/panels/plaza_forge_images.js —— 图片层：头像/缩略图 thumb/详情图/≤8 表情包，实时 KB 与上限（_pfi*）
//   · js/panels/plaza_forge_form.js   —— 数据层：元数据/标签 + payload 读写校验（_pfRead/_pfValidate/_pfSetForm）
//   · js/panels/plaza_forge_review.js —— 生命周期：草稿箱 + 存草稿 + 审核闸门与弹窗 + 发布/下架/删除

// 六条实现约束（都吃过亏，别改）：
//  1. **API Key 零持久化**：只在点「提交审核」那一刻从输入框读一次，发出去后立刻清空
//     输入框与内存变量；**绝不**进 localStorage/sessionStorage/console/日志/URL。
//  2. **审核后改卡必须重审**：后端发布时比对审核摘要，前端 `_pfS.dirty` 同步这个事实——
//     改完立刻显式禁用发布并亮出「需重新提交审核」，不让用户提交后才发现。
//  3. **图片压缩后才进 payload**（契约 06 §3.3）：先在本地压到槽位上限再进 payload——
//     头像 ≤30KB / 列表缩略图 thumb ≤12KB / 详情图 ≤300KB / 表情包 ≤20KB×≤8；
//     **压不下去就拒收**（不硬塞），UI 实时显示每张的 KB 与上限。
//  4. **不轮询**：本模块没有任何定时器。只在打开面板 / 存草稿 / 审核完 / 发布完 / 删除后
//     请求一次；列表只拉摘要（GET /plaza/api/drafts），草稿正文只在「继续编辑」时按 id 拉。
//  5. **不用属性选择器找 JS 属性**（见 docs/错误总结.md #19）：DOM 全部由本模块建立，
//     引用一律走闭包变量，不做 `querySelector("[_pfX]")` 这类查询。
//  6. **能用 textContent 就不用 innerHTML**：文案里全是用户数据（卡名/简介/审核理由）。
//
// 接口真相（app/plaza/api.py 已实现并测试通过，前端只调**相对路径**）：
//   POST /plaza/api/draft        {card, display?}      → {ok, draft:{id,status,updated_at}}
//   GET  /plaza/api/drafts                             → {ok, items:[…摘要…]}
//   GET  /plaza/api/draft?id=X                         → {ok, draft:{meta, card}}
//   POST /plaza/api/draft/delete {id}                  → {ok}
//   POST /plaza/api/review       {sk, card, display?}  → {ok, review:{verdict,risk,reasons…}}
//   POST /plaza/api/publish      {id, replace?}        → {ok, card:{…广场投影…}}
//   POST /plaza/api/unpublish    {id}                  → {ok}
import { showToast } from "../util.js";
import { uiSelectEnhance } from "../ui_select.js";

// ── 常量：与 app/plaza/card_format.py / drafts.py / store.py 逐字对齐（改后端要同步改这里）──
const _PF_CATS = ["陪伴", "剧情", "日常", "战斗", "治愈", "搞笑", "其他"];
const _PF_LIMITS = {name: 40, char_name: 20, user_name: 20, desc: 120, tagline: 40,
                    // ★ 标签个数 ← 后端 `card_format.MAX_TAGS`（2026-10-02：5 → **20**；单标签
                    //   长度 ← `MAX_TAG_LEN`=12）。改后端要同步改这里。
                    tags: 20, tag: 12, display: 40};
/** 草稿份数上限 ← 后端 `drafts.DRAFT_MAX`（2026-10-02：10 → **50**；**管理员不限**）。 */
const _PF_DRAFT_MAX = 50;
const _PF_DRAFT_MAX_ADMIN = Infinity;
// 图片上限搬去了 plaza_forge_images.js（_PFI_SPEC/_PFI_STK，契约 06 §3.3 的单一出处）
// ★ 体积上限**取值来源 = 后端 `app/plaza/card_format.py`**（改后端必须同步改这里，且**只写这一处**）：
//   · `MAX_TOTAL_BYTES` = 10MB（解压后总量）；管理员 `ADMIN_MAX_TOTAL_BYTES` = 64MB
//   · `MAX_TEXT_TOTAL_BYTES` = 8MB（单卡**文字总量**，UTF-8 字节）；**管理员不限**
//   · `MAX_FILES` = **500**（character/ 下文件数）；知识库份数与单文件大小 2026-10-01 起都不再限
const _PF_DATA_MAX = 10 * 1024 * 1024;        // ← card_format.MAX_TOTAL_BYTES
const _PF_DATA_MAX_ADMIN = 64 * 1024 * 1024;  // ← card_format.ADMIN_MAX_TOTAL_BYTES
const _PF_TEXT_TOTAL_MAX = 8 * 1024 * 1024;   // ← card_format.MAX_TEXT_TOTAL_BYTES
/** character/ 下文件数上限（← card_format.MAX_FILES=**500**，2026-10-01 由 24 放宽：
 *  用户实测被"卡内文本文件有 33 个，超过单卡上限 24 个"直接挡住 ⇒ 它只当**安全天花板**用，不是配额）。 */
const _PF_FILES_MAX = 500;
/** 文字总量上限：**管理员不限**（后端 `parse_card_zip(is_admin=True)` 把 max_text_total 置 None）。 */
function _pfTextTotalMax() { return _pfIsAdminSync() ? Infinity : _PF_TEXT_TOTAL_MAX; }
/** 整卡体积上限：管理员 64MB（后端是"抬高上限"而不是真无界——那是 DoS 防护，不是配额）。 */
function _pfDataMax() { return _pfIsAdminSync() ? _PF_DATA_MAX_ADMIN : _PF_DATA_MAX; }
/** 同步读"是不是管理员"的**缓存**（`_pfIsAdmin()` 在建制卡页骨架时就会把它填好）。
 *  未知（还没问到）时按**普通账号**处理：宁可严一点，也绝不因为"没问到"而放宽。 */
function _pfIsAdminSync() { return _pfAdminCache === true; }
/** 文本的**真实字节数**（UTF-8；中文 1 字 3 字节 —— 后端按字节算，前端不能按 `.length` 算）。 */
function _pfTextBytes(s) {
    const t = String(s == null ? "" : s);
    if (!t) return 0;
    try { return new Blob([t]).size; } catch (e) { return t.length; }
}
/** 一份 payload 里的**文字总量**（files[].text + opening 两条目）。 */
function _pfTextTotal(card) {
    let n = 0;
    for (const f of (Array.isArray((card || {}).files) ? card.files : [])) {
        n += _pfTextBytes((f || {}).text);
    }
    const op = (card || {}).opening || {};
    for (const s of (op.first_messages || [])) n += _pfTextBytes(s);
    for (const s of (op.narrations || [])) n += _pfTextBytes(s);
    return n;
}
const _PF_ID_RE = /^[a-z0-9_-]{1,32}$/;      // core/preset_parse.py:_PRESET_ID_RE
const _PF_PRESENTATION = [["sticker", "短信 + 表情包"], ["narration", "短信 + 旁白"],
                         ["none", "纯短信"]];
const _PF_STATUS = {draft: "草稿", reviewed: "已通过审核", rejected: "审核未通过",
                    published: "已发布"};
const _PF_VERDICT = {pass: "通过", reject: "未通过", manual: "转人工复核"};

// ── 状态（一律 _pf 前缀：bundle 是单作用域，前缀是防撞的唯一有效手段）──
// 文本与图片的编辑态分别住在 _pffS / _pfiS（各自模块），这里只放跨模块共用的部分。
const _pfS = {
    booted: false, open: false, busy: false, drafting: false,
    id: "", draftStatus: "", tags: [],
    verdict: "", dirty: true,     // dirty=true = 当前卡体与"已通过的审核结论"不一致
    drafts: [], draftsLoaded: false, draftsErr: "",
};
let _pfSeq = 0;                   // 表单代际：切草稿/新建时 ++，迟到的异步回填一律丢弃
let _pfModalSeq = 0;              // 审核弹窗代际：关掉之后迟到的响应不再动 UI

// ── DOM 引用（全部在 _pfInit 里按 id 现取；index.html 里写死了骨架）──
const _pfEl = {};
const _pf = (id) => document.getElementById(id);

// ═══ 小工具 ═══════════════════════════════════════════
function _pfFmtSize(n) {
    const b = Number(n) || 0;
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(b < 10 * 1024 ? 1 : 0) + " KB";
    return (b / 1024 / 1024).toFixed(1) + " MB";
}

function _pfFmtTime(sec) {
    // 后端给**秒级** Unix 时间戳（drafts.py:updated_at），与 plaza.js 同一口径
    const t = Number(sec) || 0;
    if (!t) return "—";
    const d = new Date(t * 1000);
    if (isNaN(d.getTime())) return "—";
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
        + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

/** 名称 → 建议 id（小写字母/数字/下划线/短横线，≤32）。中文等非 ASCII 一律剔除。 */
function _pfSlug(name) {
    let s = String(name || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
    s = s.replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
    return s;
}

function _pfEl2(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
}

/** 带鉴权的 JSON 请求。返回 {ok, status, data}——**不抛异常**，由调用方决定文案。 */
async function _pfReq(url, opts) {
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

function _pfPost(url, body) {
    return _pfReq(url, {method: "POST", headers: {"Content-Type": "application/json"},
                        body: JSON.stringify(body || {})});
}

/** 错误 → 人话 + 一个可选按钮（返回 null 表示"没什么可说的"）。 */
function _pfWhy(r, fallback) {
    if (r && r.status === 0) return "连不上制卡服务，请检查网络后重试（草稿不受影响）。";
    if (r && r.status === 401) return "需要登录后才能制作与发布。";
    const e = r && r.data && r.data.error;
    return String(e || fallback || "操作失败，请稍后重试。");
}

/** 当前账号是不是管理员（只用于"要不要显示官方勾选 / 用哪套体积上限"；**权威判定在服务端**）。
 *  取值顺序（2026-10-01：本地模式也要能认出管理员）：
 *    ① 服务器/平台：`/auth/me` 直接给 `role`（`server/app.py`）；
 *    ② 本地版：`/auth/state` 回带 `role`（serverops 加的一行）；
 *    ③ 兜底：登录时可能留在 `localStorage.firefly_role` 的痕迹。
 *  都拿不到 ⇒ false（**失败即按普通账号**：宁可不显示勾选、不越权，也不放宽上限）。 */
let _pfAdminCache = null;
async function _pfIsAdmin() {
    if (_pfAdminCache !== null) return _pfAdminCache;
    const read = (d) => String((d && d.role) || "") === "admin";
    for (const url of ["/auth/me", "/auth/state"]) {
        try {
            const r = await fetch(url, {headers: {"Accept": "application/json"}, cache: "no-store"});
            if (!r.ok) continue;
            const d = await r.json();
            if (d && (d.role !== undefined || d.email || d.logged_in)) {
                _pfAdminCache = read(d);
                return _pfAdminCache;
            }
        } catch (e) { /* 换下一个来源 */ }
    }
    let yes = false;
    try { yes = String(localStorage.getItem("firefly_role") || "") === "admin"; } catch (e) {}
    _pfAdminCache = yes;
    return yes;
}
/** 发布时要不要带 `official: true`（服务端会再核一次角色；非管理员带了也白带）。 */
function _pfOfficialWanted() {
    try { return !!(_pfEl.offBox && _pfEl.offBox.checked && _pfEl.offWrap
                    && _pfEl.offWrap.style.display !== "none"); } catch (e) { return false; }
}

// ═══ 提示区 ═══════════════════════════════════════════
function _pfMsg(text, retry) {
    const el = _pfEl.msg;
    if (!el) return;
    el.textContent = "";
    el.style.display = text ? "flex" : "none";
    if (!text) return;
    el.appendChild(_pfEl2("div", "pz-msg-txt", text));
    if (retry === true) {
        const b = _pfEl2("button", "pz-btn", "重试");
        b.type = "button";
        b.onclick = () => { _pfMsg("", false); _pfLoadDrafts(true); };
        el.appendChild(b);
    } else if (retry && retry.label) {
        const b = _pfEl2("button", "pz-btn", retry.label);
        b.type = "button";
        b.onclick = retry.onClick;
        el.appendChild(b);
    }
}

function _pfGotoLogin() {
    closePlazaForge();
    try { window.closePlaza && window.closePlaza(); } catch (e) {}
    try { window.showHome && window.showHome(); } catch (e) {}
    try { window.showAuthModule && window.showAuthModule(); } catch (e) {}
    try { window.toggleAuthForms && window.toggleAuthForms(); } catch (e) {}
    showToast("请先登录，再制作与发布角色卡");
}

// ═══ 骨架：一次建好，之后只更新文字与禁用态 ═══════════
function _pfBuild() {
    // ⚠ 只清 `#pf-body`（表单/草稿箱的容器），**绝不**清 `#plaza-forge` 本身：
    //   #pf-review / #pf-modal 是它的另外两个子节点，清父节点会把弹窗一起删掉
    //   ——2026-10-01 真浏览器抓到：点「提交审核」毫无反应，弹窗节点根本不存在。
    const view = _pf("pf-body");
    if (!view) return false;
    view.textContent = "";
    const cols = _pfEl2("div", "pfc-cols");
    view.appendChild(cols);

    // ── 左列：编辑表单 ──────────────────────────────
    const pane = _pfEl2("div", "pfc-pane");
    const head = _pfEl2("div", "cv-head");
    const back = _pfEl2("button", "cv-back", "←");
    back.type = "button";
    back.setAttribute("aria-label", "返回广场列表");
    back.onclick = () => closePlazaForge();
    const title = _pfEl2("div", "cv-title", "制作角色卡");
    // 「载入已有卡」（2026-10-01 用户点名要）：把用户**自己的角色卡**载进这张表，
    // 之后走同样的三步（存草稿 → 提交审核 → 发布）。平台页的「我的卡」就是这个页面。
    const localBtn = _pfEl2("button", "pf-new-btn", "载入已有卡");
    localBtn.type = "button";
    localBtn.id = "pf-local-btn";
    localBtn.title = "把你自己已有的角色卡载入这张表，检查后发布到广场";
    localBtn.onclick = () => {
        if (window.plazaPublishLocalPick) window.plazaPublishLocalPick();
    };
    const newBtn = _pfEl2("button", "pf-new-btn", "新建");
    newBtn.type = "button";
    newBtn.title = "清空表单，从头做一张新卡";
    newBtn.onclick = _pfNew;
    head.append(back, title, localBtn, newBtn);

    const msg = _pfEl2("div", "pz-msg");
    msg.id = "pf-msg";
    const form = _pfEl2("div", "pf-form");
    form.id = "pf-form";
    cols.append(pane);
    pane.append(head, msg, form);

    // ── 右列：草稿箱 ────────────────────────────────
    const box = _pfEl2("div", "pfc-box");
    box.id = "pf-drafts-box";
    cols.appendChild(box);

    _pfEl.view = view; _pfEl.pane = pane; _pfEl.form = form; _pfEl.msg = msg;
    _pfEl.draftsBox = box; _pfEl.head = head;
    _pfBuildForm();
    _pfRenderDrafts();
    return true;
}

function _pfField(label, node, tip) {
    const f = _pfEl2("div", "field");
    const lab = _pfEl2("div", "pf-label", label);
    f.append(lab, node);
    if (tip) f.appendChild(_pfEl2("div", "field-tip", tip));
    return f;
}

function _pfInput(id, ph, maxlen, type) {
    const i = document.createElement("input");
    i.id = id;
    i.type = type || "text";
    i.placeholder = ph || "";
    i.autocomplete = "off";
    if (maxlen) i.maxLength = maxlen;
    return i;
}

/** 计数条：`<div class="pf-count"><span>已写 N / M</span></div>` */
function _pfCountBox() {
    const w = _pfEl2("div", "pf-count");
    const t = _pfEl2("span", "", "");
    w.appendChild(t);
    return {el: w, txt: t};
}

function _pfBuildForm() {
    const form = _pfEl.form;
    // ⚠ 变量名不许叫 L / _PF_LIMITS 之类去遮罩外层常量：本文件是 bundle 单作用域的一部分，
    //   `const L = ...` 会把同名引用打进 TDZ（参照 docs/错误总结.md #15 的同类事故）。
    const lim = _PF_LIMITS;

    // ① 基本信息
    form.appendChild(_pfEl2("div", "pf-sec", "① 基本信息"));
    const nameIn = _pfInput("pf-name", "卡名（如：夏日烟火）", lim.name);
    const idIn = _pfInput("pf-id", "自动生成，可改（小写字母/数字/_/-）", 32);
    const idTip = _pfEl2("div", "pf-idtip", "未填写卡名时 id 会留空。");
    const idWrap = _pfEl2("div", "pf-idrow");
    idWrap.append(idIn, idTip);
    form.append(_pfField("卡名 *", nameIn, "列表与详情页显示的卡片名（≤" + lim.name + " 字）"),
                _pfField("卡片 id *", idWrap, "角色卡在本机与广场的唯一标识（≤32 位）"),
                // 二级字段：同行并排
                _pfEl2("div", "pf-pair"));
    const pair = form.lastChild;
    pair.append(
        _pfField("角色名 *", _pfInput("pf-char", "角色叫什么", lim.char_name)),
        _pfField("称呼 *", _pfInput("pf-user", "角色怎么称呼你", lim.user_name)));
    form.appendChild(_pfEl2("div", "pf-pair"));
    const pair2 = form.lastChild;
    const catSel = document.createElement("select");
    catSel.id = "pf-cat";
    for (const c of _PF_CATS) {
        const o = document.createElement("option");
        o.value = c; o.textContent = c;
        catSel.appendChild(o);
    }
    const presSel = document.createElement("select");
    presSel.id = "pf-presentation";
    for (const [v, t] of _PF_PRESENTATION) {
        const o = document.createElement("option");
        o.value = v; o.textContent = t;
        presSel.appendChild(o);
    }
    pair2.append(_pfField("分类", catSel, "未知分类后端会回退「其他」"),
                 _pfField("呈现方式", presSel, "决定对话里怎么显示"));
    const descIn = _pfInput("pf-desc", "一句话说明这张卡是做什么的", lim.desc);
    const descBox = _pfCountBox();
    form.append(_pfField("简介 *", descIn, "广场卡片上显示（≤" + lim.desc + " 字）"), descBox.el);
    const tagIn = _pfInput("pf-tagline", "标题下一行小字（可空）", lim.tagline);
    const tagBox = _pfCountBox();
    form.append(_pfField("副标题", tagIn, "可空，≤" + lim.tagline + " 字"), tagBox.el);

    // ② 标签（可增删，≤20 —— 与后端 `MAX_TAGS` 同口径）
    form.appendChild(_pfEl2("div", "pf-sec", "② 标签"));
    const tagWrap = _pfEl2("div", "pf-tags");
    const tagList = _pfEl2("div", "pf-taglist");
    tagList.id = "pf-taglist";
    const tagEntry = _pfEl2("div", "pf-tagentry");
    tagEntry.append(tagIn2(), _pfEl2("div", "pf-tagtip", "回车/逗号添加，点 × 删除（最多 "
                                     + lim.tags + " 个，每个 ≤" + lim.tag + " 字）"));
    function tagIn2() {
        const w = _pfEl2("div", "pf-tagin");
        _pfEl.tagInput = _pfInput("pf-tag", "输入标签后回车", lim.tag);
        const add = _pfEl2("button", "pf-btn small", "添加");
        add.type = "button";
        add.onclick = () => _pfTagsAdd(_pfEl.tagInput.value, true);
        w.append(_pfEl.tagInput, add);
        return w;
    }
    tagWrap.append(tagList, tagEntry);
    form.append(_pfField("标签", tagWrap, null));

    // ③ 文本文件 + ④ 开场白（plaza_forge_files.js）
    //    固定三件（core/identity/sms_samples）+ 1..6 份 knowledge/<名字>.md（可增删改名）+ 开场白条目。
    _pffMount(form);

    // ⑤ 图片（plaza_forge_images.js）
    //    头像 ≤30KB / 列表缩略图 thumb ≤12KB / 详情图 ≤300KB / 表情包 ≤20KB×≤8；实时 KB 与上限。
    _pfiMount(form);

    // ⑥ 署名 + 提交
    form.appendChild(_pfEl2("div", "pf-sec", "⑥ 署名与提交"));
    const dispIn = _pfInput("pf-display", "留空 = 邮箱打码展示（可空）", lim.display);
    form.append(_pfField("作者展示名", dispIn, "广场上作者一栏显示这个名字（不加链接）"));

    const status = _pfEl2("div", "pf-status");
    status.id = "pf-status";
    const acts = _pfEl2("div", "pf-actions");
    const bSave = _pfEl2("button", "pz-btn", "存草稿");
    bSave.type = "button";
    bSave.id = "pf-save-btn";
    bSave.onclick = () => _pfSave();
    const bRev = _pfEl2("button", "pz-btn", "提交审核");
    bRev.type = "button";
    bRev.id = "pf-review-btn";
    bRev.onclick = () => _pfReviewOpen();
    const bPub = _pfEl2("button", "pz-btn primary", "发布到广场");
    bPub.type = "button";
    bPub.id = "pf-publish-btn";
    bPub.disabled = true;
    bPub.onclick = () => _pfPublish();
    const bUnp = _pfEl2("button", "pz-btn danger", "下架");
    bUnp.type = "button";
    bUnp.id = "pf-unpublish-btn";
    bUnp.onclick = () => _pfUnpublish();
    acts.append(bSave, bRev, bPub, bUnp);
    // ★「标记为官方」（2026-10-01 用户要：管理员要有发布官方卡的入口）——
    //   只有 `role == admin` 才**显示**（服务端也会拒，前端只是不给入口）；默认**不勾**。
    //   role 来源：服务器/平台 `/auth/me`（server/app.py 的 /auth/me 带 role）；本地版拿不到就隐藏。
    const offWrap = _pfEl2("label", "pf-official");
    offWrap.id = "pf-official-wrap";
    offWrap.style.display = "none";
    const offBox = document.createElement("input");
    offBox.type = "checkbox";
    offBox.id = "pf-official";
    offBox.checked = false;
    offWrap.append(offBox, document.createTextNode(" 标记为官方（官方卡在广场带「官方」徽标）"));
    offWrap.title = "只有管理员能发布官方卡；勾选后这张卡在广场上会显示官方徽标";
    // ★ 管理员免审说明（非管理员整块隐藏）：管理员的「提交审核」会被隐藏，这里说清为什么
    const adminNote = _pfEl2("div", "pf-admin-note",
        "管理员免审：存一次草稿后可直接「发布到广场」（服务端的审核闸对管理员豁免）。");
    adminNote.id = "pf-admin-note";
    adminNote.style.display = "none";
    _pfIsAdmin().then((yes) => {
        offWrap.style.display = yes ? "flex" : "none";
        if (typeof _pfSyncBtns === "function") _pfSyncBtns();   // 角色一确定就重算按钮与说明
    });
    form.append(status, offWrap, adminNote, acts);

    _pfEl.name = nameIn; _pfEl.id = idIn; _pfEl.idTip = idTip;
    _pfEl.char = _pf("pf-char"); _pfEl.user = _pf("pf-user");
    _pfEl.cat = catSel; _pfEl.pres = presSel;
    _pfEl.desc = descIn; _pfEl.descCount = descBox;
    _pfEl.tagline = tagIn; _pfEl.taglineCount = tagBox;
    _pfEl.tagList = tagList;
    _pfEl.display = dispIn;
    _pfEl.status = status; _pfEl.bSave = bSave; _pfEl.bRev = bRev;
    _pfEl.bPub = bPub; _pfEl.bUnp = bUnp; _pfEl.offWrap = offWrap; _pfEl.offBox = offBox;
    _pfEl.adminNote = adminNote;

    // ── 接线：任何一次输入都让"审核结论"过期（除非程序回填）──
    const watch = (el, counter) => {
        if (!el) return;
        const upd = () => {
            if (counter) _pfCount(counter, el.value.length, _PF_LIMITS[counter.key] || 0);
            _pfMarkDirty();
        };
        el.addEventListener("input", upd);
    };
    _pfEl.descCount.key = "desc";
    _pfEl.taglineCount.key = "tagline";
    watch(descIn, descBox); watch(tagIn, tagBox);
    // 文本文件（固定三件/知识库/开场白）与图片的输入接线在各自模块里（_pff* / _pfi*），
    // 那边每次改动都调 _pfMarkDirty()，语义与这里一致：**任何改动都让审核结论作废**。
    for (const el of [_pfEl.char, _pfEl.user, catSel, presSel, dispIn]) {
        if (!el) continue;
        el.addEventListener("input", () => _pfMarkDirty());
        el.addEventListener("change", () => _pfMarkDirty());
    }
    // id：用户没手动改过就跟着卡名走（改动过就不再被覆盖——已发布的 id 不能被悄悄改掉）
    let idTouched = false;
    idIn.addEventListener("input", () => { idTouched = true; _pfSyncId(); _pfMarkDirty(); });
    _pfEl.idTouched = () => idTouched;
    _pfEl.setIdTouched = (v) => { idTouched = !!v; };
    nameIn.addEventListener("input", () => {
        if (!idTouched) idIn.value = _pfSlug(nameIn.value);
        _pfSyncId();
        _pfMarkDirty();
    });
    _pfEl.tagInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === "," || e.key === "，") {
            e.preventDefault();
            _pfTagsAdd(_pfEl.tagInput.value, true);
        }
    });
    _pfEl.tagInput.addEventListener("blur", () => _pfTagsAdd(_pfEl.tagInput.value, false));
    try { uiSelectEnhance(form); } catch (e) {}
    _pfCountAll();
}

// ═══ 面板开关 ═════════════════════════════════════════
function _pfInit() {
    // ⚠ 2026-10-01（真浏览器验证抓到）：这里原来写 `if (_pfS.booted) return false;`，
    // 把"**已经初始化过**"当成了失败 —— 于是**制卡页第二次打开静默无反应**
    // （关闭后再点入口，`openPlazaForge()` 直接 return，界面不动、也没有任何报错）。
    // 已初始化是**成功**状态，直接放行；顺带保留表单里未提交的内容（不再 `_pfNew()` 清空）。
    if (_pfS.booted) return true;
    if (!_pf("plaza-forge") || !_pf("pf-body")) return false;
    if (!_pfBuild()) return false;
    _pfS.booted = true;
    _pfNew();
    return true;
}

function openPlazaForge() {
    if (!_pfInit()) return;
    const view = _pf("plaza-forge");
    if (!view) return;
    // 广场面板可能还没开（比如深链）：带上它，否则这一层无所依附
    const root = _pf("plaza-view");
    if (root && !root.classList.contains("show")) {
        try { window.openPlaza && window.openPlaza(); } catch (e) {}
    }
    if (root) root.classList.add("plaza-forge-on");
    view.classList.add("show");
    _pfS.open = true;
    _pfLoadDrafts(false);         // 打开时加载一次（打开制卡页/存草稿/审核完才拉，不轮询）
}

function closePlazaForge() {
    _pfS.open = false;
    _pfModalClose();
    const view = _pf("plaza-forge");
    if (view) view.classList.remove("show");
    const root = _pf("plaza-view");
    if (root) root.classList.remove("plaza-forge-on");
    const rev = _pf("pf-review");
    if (rev) { rev.classList.remove("show"); rev.textContent = ""; }
    // 制卡页可能改过广场内容（发布 / 下架）→ 回列表时必须拿到新数据。
    // 否则用户视角是"发布成功了，但广场里看不到"（真浏览器验证抓到的洞）。
    try { window.plazaReloadList && window.plazaReloadList(); } catch (e) {}
}

/** 广场面板关闭 → 制卡层跟着收（否则下次打开广场会直接看见制卡页）。
 *  由 panels/plaza.js 的 _pzClose 调用（同一域的收尾，见该文件注释）。 */
function plazaForgeLeave() {
    _pfiS.seq++;                  // 作废飞行中的图片压缩（代际在图片层：连续选图只留最后一次）
    _pfModalClose();
    if (!_pfS.open) return;
    closePlazaForge();
}

window.openPlazaForge = openPlazaForge;
window.closePlazaForge = closePlazaForge;
window.plazaForgeLeave = plazaForgeLeave;
