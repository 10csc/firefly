// 角色卡广场（共创平台 M1）——**外壳**：状态 / DOM / 请求与格式化 / 图片层 / 面板开关 / 窗口钩子
//
// 与「角色卡管理」（#cards-view）是同一层级的两块地：管理页管**本机已有**的卡，
// 广场页管**远端可安装**的卡。入口放在管理页头部（同一个角色包域，不另起导航）。
//
// 2026-10-01（P4-7 拆债）：本文件原来 844 行，把列表/详情/安装卸载全塞在一起，超了
// "无文件 >500 行"的约定。现按内聚拆成三个文件（**纯结构拆分，行为零变化**）：
//   · js/panels/plaza.js        —— 本文件：外壳（常量/状态/DOM/小工具/图片层/面板开关/窗口钩子）
//   · js/panels/plaza_list.js   —— 列表：分类 / 标签 / 卡片 / 分页 / 清筛选 / 去登录
//   · js/panels/plaza_detail.js —— 详情：渲染 / 安装 / 卸载 / 已装清单与配额
// 三者共用同一份 `_pz*` 顶层状态与前缀；bundle 是**单作用域拼接**，所以
// **新加的名字必须继续带 `_pz` 前缀**（重名会静默覆盖，见 ORDER 的顺序说明）。
//
// 四条实现约束（都吃过亏，别改）：
//  1. **图片必须 fetch + URL.createObjectURL**：/plaza/api/asset 要带 Authorization，
//     <img src> 直连拿不到 token（服务器模式跨域、本地模式 file://）；
//  2. **面板关闭即 revoke**（3Mbps 带宽 + 移动端内存）：_pzOnPanelClose 一次清干净；
//  3. **分页不一次拉满**：每页 20、滚动到底再拉下一页（size 上限 50）；
//  4. **不在轮询里拉列表**：本模块没有任何定时器；只有用户打开面板/改筛选/装完/卸完才请求。
//
// 接口真相（app/plaza/api.py 已实现并测试通过，前端只调**相对路径**）：
//   GET  /plaza/api/list      列表（**不含卡正文**，≤64KB）
//   GET  /plaza/api/card      详情
//   GET  /plaza/api/asset     封面/头像字节（ETag/304）
//   GET  /plaza/api/installed 本端已装 + 配额
//   POST /plaza/api/install   {"id","replace"}
//   POST /plaza/api/uninstall {"id"}
import { uiSelectEnhance } from "../ui_select.js";

// ── 常量 ────────────────────────────────────────────
const _PZ_PAGE = 20;                  // 每页条数（后端上限 50；20 是 3Mbps 下的稳态选择）
const _PZ_IMG_MAX = 64;               // 图片缓存上限（够铺满一屏 + 几屏余量）
const _PZ_UTAGS = 14;                 // 标签条最多展示几个

// ── 本页状态（一律 _pz 前缀：bundle 是单作用域，前缀是防撞的唯一有效手段）──
const _pzS = { cat: "", tag: "", q: "", sort: "new", page: 1, total: 0, more: false, loading: false };
let _pzGen = 0;                       // 请求代际：筛选一变就 ++，飞行中的旧响应一律丢弃
let _pzAbort = null;                  // 上一次列表请求的 AbortController（省流量：没人要的结果立刻掐断）
const _pzCards = [];                  // 已加载的卡片（详情用；列表只渲染缩略信息）
const _pzInstalled = new Map();       // id → {name, char_name}
let _pzInstalledUsed = null;          // /installed 的配额用量（非服务器模式为 null）
let _pzInstalledAt = 0;               // 上次拉 /installed 的时刻（本地缓存，避免重复请求）
let _pzBooted = false, _pzIsOpen = false;
let _pzObserver = null;

// ── DOM（顺序同 index.html）────────────────────────
const _pzRoot = document.getElementById("plaza-view");
const _pzListPane = document.getElementById("plaza-list");
const _pzDetail = document.getElementById("plaza-detail");
const _pzSearch = document.getElementById("pz-search");
const _pzCats = document.getElementById("pz-cats");
const _pzTags = document.getElementById("pz-tags");
const _pzSort = document.getElementById("pz-sort");
const _pzQuota = document.getElementById("pz-quota");
const _pzGrid = document.getElementById("plaza-grid");
const _pzMsg = document.getElementById("plaza-msg");
const _pzMore = document.getElementById("plaza-more");
const _pzSentinel = document.getElementById("plaza-sentinel");

// ── 图片缓存（fetch + objectURL；本会话内复用，面板关闭时全部 revoke）──
// key = 请求 URL；value = {pr: Promise, url: objectURL, el: 最后挂过的元素}
const _pzImg = new Map();

// ═══ 小工具 ═══════════════════════════════════════════
function _pzFmtSize(n) {
    const b = Number(n) || 0;
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(b < 10 * 1024 ? 1 : 0) + " KB";
    return (b / 1024 / 1024).toFixed(1) + " MB";
}

function _pzFmtNum(n) {
    const v = Number(n) || 0;
    return v >= 10000 ? (v / 10000).toFixed(1) + " 万" : String(v);
}

function _pzFmtTime(sec) {
    // 后端给的是**秒级** Unix 时间戳（store.py 的 published_at / created_at）
    const t = Number(sec) || 0;
    if (!t) return "";
    const d = new Date(t * 1000);
    if (isNaN(d.getTime())) return "";
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
        "-" + String(d.getDate()).padStart(2, "0");
}

function _pzQuery(params) {
    const p = new URLSearchParams();
    for (const k in params) {
        const v = params[k];
        if (v === undefined || v === null || v === "") continue;
        p.set(k, String(v));
    }
    const s = p.toString();
    return "/plaza/api/list" + (s ? ("?" + s) : "");
}

/** 带鉴权的 JSON 请求。返回 {ok, status, data}——**不抛异常**，由调用方决定文案。 */
async function _pzReq(url, opts) {
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

// ── 提示区：永远用 textContent 写文案（内容含用户数据，绝不给 innerHTML 留口子）──
function _pzSetMsg(text, retry) {
    if (!_pzMsg) return;
    _pzMsg.innerHTML = "";
    _pzMsg.style.display = text ? "flex" : "none";
    if (!text) return;
    const span = document.createElement("div");
    span.className = "pz-msg-txt";
    span.textContent = text;
    _pzMsg.appendChild(span);
    if (retry) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "pz-btn";
        b.textContent = retry.label;
        b.onclick = retry.onClick;
        _pzMsg.appendChild(b);
    }
}

// ═══ 图片：取字节 → objectURL → 懒加载挂载 → 离开视野释放 ═══
function _pzAssetGet(cardId, slot) {
    const url = "/plaza/api/asset?id=" + encodeURIComponent(cardId) + "&slot=" + encodeURIComponent(slot || "cover");
    let e = _pzImg.get(url);
    if (!e) {
        e = {url: "", el: null};
        e.pr = (async () => {
            try {
                const r = await fetch(url, {headers: {"Accept": "image/*"}});
                if (!r.ok) return null;
                const blob = await r.blob();
                if (!blob || !blob.size) return null;
                e.url = URL.createObjectURL(blob);
                return e.url;
            } catch (err) { return null; }
        })();
        _pzImg.set(url, e);
        _pzImgTrim();
    }
    return e;
}

/** 缓存上限：超出后把**当前没挂在任何元素上**的条目 revoke 掉（在用的绝不回收）。
 *  注意：revoke 之后 URL 就废了，必须把条目从 Map 里删掉，否则下次会拿到死链。 */
function _pzImgTrim() {
    if (_pzImg.size <= _PZ_IMG_MAX) return;
    for (const [k, e] of _pzImg) {
        if (_pzImg.size <= _PZ_IMG_MAX) break;
        if (e.el) continue;
        if (e.url) { try { URL.revokeObjectURL(e.url); } catch (err) {} }
        e.url = "";
        _pzImg.delete(k);
    }
}

function _pzReleaseEl(el) {
    if (!el || !el._pzImgUrl) return;
    const e = _pzImg.get(el._pzImgUrl);
    if (e && e.el === el) e.el = null;
    el._pzImgUrl = "";
}

function _pzAttach(el, e) {
    if (!el._pzLive || !e.url) return;
    el._pzImgUrl = e.url;      // el 记的是"我自己拿的是哪条缓存"，回收时按这条定位
    el.src = e.url;
    if (!e.el) e.el = el;      // e 记的是"当前哪个元素在用我"（防误回收）
}

async function _pzLoadImg(el, cardId, slot) {
    const e = _pzAssetGet(cardId, slot);
    if (e.url) { _pzAttach(el, e); return; }
    const url = await e.pr;
    if (!el._pzLive) { _pzReleaseEl(el); return; }   // 元素已被回收：不留悬挂的 objectURL
    if (!url || !e.url) { el.style.visibility = "hidden"; return; }
    _pzAttach(el, e);
}

function _pzReleaseAll() {
    for (const [, e] of _pzImg) {
        if (e.url) { try { URL.revokeObjectURL(e.url); } catch (err) {} }
        e.url = "";
        e.el = null;
    }
    _pzImg.clear();
}

// ═══ 面板开关 ═════════════════════════════════════════
function _pzInit() {
    if (_pzBooted) return;
    _pzBooted = true;
    if (_pzSearch) {
        let t = null;
        _pzSearch.addEventListener("input", () => {
            clearTimeout(t);
            t = setTimeout(() => {
                const v = (_pzSearch.value || "").trim();
                if (v === _pzS.q) return;
                _pzS.q = v;
                _pzLoad(true);                        // 搜索必然是"重来一页"，不做追加
            }, 350);                                  // 3Mbps：别每敲一个字打一次请求
        });
        // 回车立刻搜（不用等 350ms）
        _pzSearch.addEventListener("keydown", (e) => {
            if (e.key !== "Enter") return;
            clearTimeout(t);
            _pzS.q = (_pzSearch.value || "").trim();
            _pzLoad(true);
        });
    }
    if (_pzSort) {
        _pzSort.addEventListener("change", () => {
            _pzS.sort = _pzSort.value || "new";
            _pzLoad(true);
        });
        try { uiSelectEnhance(_pzSort.parentElement || document); } catch (e) {}
    }
    if (_pzMore) {
        _pzMore.onclick = () => { if (_pzS.more && !_pzS.loading) _pzLoad(false); };
    }
    // 触底自动加载：只观察哨兵，不轮询（约束 4）
    try {
        if (_pzSentinel && typeof IntersectionObserver === "function") {
            _pzObserver = new IntersectionObserver((entries) => {
                for (const en of entries) {
                    if (en.isIntersecting && _pzIsOpen && _pzS.more && !_pzS.loading) _pzLoad(false);
                }
            }, {root: _pzRoot || null, rootMargin: "240px"});
            _pzObserver.observe(_pzSentinel);
        }
    } catch (e) {}
    // 图片回收：卡片滚出视野就 revoke（滚回来时对象 URL 还在缓存里就直接命中，不用重下）
    if (_pzGrid && typeof IntersectionObserver === "function") {
        const io = new IntersectionObserver((entries) => {
            for (const en of entries) {
                const img = en.target;
                if (!en.isIntersecting) { _pzReleaseEl(img); continue; }
                if (!img.getAttribute("src")) _pzLoadImg(img, img._pzCard, img._pzSlot);
            }
        }, {root: _pzRoot || null, rootMargin: "320px"});   // 提前 320px 取图：3Mbps 下滚到才取会白屏
        _pzGrid._pzIO = io;
    }
}

/** 新挂上来的卡片图交给观察器；首次打开时也在装完观察器后调一次。
 *
 * ⚠ 2026-10-01（真浏览器验证 P4-2 抓到）：这里原来写的是
 * `querySelectorAll("img[_pzLive]")`，而 `_pzLive` 是**在 JS 上设的属性**
 * （`img._pzLive = true`），不是 HTML 属性 —— CSS 属性选择器永远选不中元素，
 * 于是**一张封面都不会被观察、也不会被加载**（列表里只有占位字）。
 * 假 DOM 测试看不出来（它只断言"没有直接写 src"），只有真浏览器能抓到。
 * 现在改为取全部 img、再按 JS 属性过滤。 */
function _pzBust(root) {
    if (!root || !_pzGrid) return;
    const io = _pzGrid._pzIO;
    root.querySelectorAll("img").forEach(img => {
        if (!img._pzLive) return;
        if (img.getAttribute("src")) return;
        if (io) io.observe(img);
        else _pzLoadImg(img, img._pzCard, img._pzSlot);   // 无 IntersectionObserver：直接取
    });
}

function _pzOnPanelClose() {
    // ★ 关闭面板 = 这一屏的图片一律释放（约束 2：不带 objectURL 离开）
    _pzReleaseAll();
    if (_pzAbort) { try { _pzAbort.abort(); } catch (e) {} _pzAbort = null; }
    _pzGen++;            // 作废所有飞行中的响应（回来时重新拉）
    _pzS.loading = false;
}

function _pzOpen() {
    if (!_pzRoot) return;
    _pzInit();
    _pzRoot.classList.add("show");
    _pzSetView("list");
    _pzIsOpen = true;
    if (_pzSort) _pzSort.value = _pzS.sort;
    _pzRenderCats();
    _pzRenderTags();
    _pzRenderQuota();
    // 每次打开刷新一次列表/已装清单（**只在这里**发生，本模块没有任何定时器）
    _pzLoad(true);
    _pzRefreshInstalled(false);    // 30 秒内复用缓存，不重复打
}

function _pzClose() {
    _pzIsOpen = false;
    // 制卡层（panels/plaza_forge.js）是广场面板内的一层：广场关掉，它必须跟着收，
    // 否则下次打开广场会直接落在制卡页上（同一域的收尾，不是额外导航）。
    try { window.plazaForgeLeave && window.plazaForgeLeave(); } catch (e) {}
    // 治理层（panels/plaza_admin.js：管理台 + 举报弹层）同属广场这一个域：广场收，它们也必须收，
    // 否则下次打开广场会直接落在管理台上（理由同上一行的制卡层）。
    try { window.plazaAdminLeave && window.plazaAdminLeave(); } catch (e) {}
    if (!_pzRoot) return;
    _pzRoot.classList.remove("show");
    _pzRoot.classList.remove("plaza-detail-on");
    if (_pzDetail) { _pzDetail.classList.remove("show"); _pzDetail.innerHTML = ""; }
    _pzOnPanelClose();
    if (_pzSearch) _pzSearch.value = _pzS.q;   // 保留筛选词，回来时接着用
}

function _pzBack() {
    // 详情页内的返回由按钮自己处理；面板级返回一律关面板（与角色卡管理页同语义）
    _pzClose();
    try { window.openCardsView ? window.openCardsView() : window.showHome(); } catch (e) {}
}

// 内联 onclick 需要（index.html 的返回键）
window.openPlaza = _pzOpen;
window.closePlaza = _pzClose;
window.plazaBack = _pzBack;
// 跨模块刷新钩子（2026-10-01）：制卡页发布/下架后回到列表时，**列表必须是新的**。
// 真浏览器验证抓到过这个洞：发布成功 → 返回广场 → 列表还是旧的（看不到刚发布的那张卡），
// 因为 `_pzLoad` 只在搜索/筛选/安装等动作里触发。只在广场页确实打开时才拉，避免无谓请求。
window.plazaReloadList = () => {
    try {
        if (_pzRoot && _pzRoot.classList.contains("show")) _pzLoad(true);
    } catch (e) { /* 刷新失败不影响关闭流程 */ }
};
