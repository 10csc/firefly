// 角色卡广场（共创平台 M1）——**列表**：分类 / 标签 / 卡片 / 分页 / 清筛选 / 去登录
//
// 2026-10-01（P4-7）自 js/panels/plaza.js **原样拆出**（纯结构拆分，行为零变化）：
// 顶层状态（`_pzS`/`_pzCards`/`_pzGen`/`_pzAbort`）与 DOM 引用仍住在外壳 js/panels/plaza.js，
// 这里只放行为；新加的名字**必须带 `_pz` 前缀**（bundle 单作用域，重名会静默覆盖）。
// 四条实现约束见 js/panels/plaza.js 头部（图片走 objectURL / 不轮询 / 分页 ≤50 / 关闭即 revoke）。
import { showToast } from "../util.js";

// ═══ 列表 ═════════════════════════════════════════════
function _pzRenderCats() {
    if (!_pzCats) return;
    _pzCats.innerHTML = "";
    const list = ["全部"].concat(_pzS.cats || []);
    for (const c of list) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "pz-chip" + (((c === "全部" && !_pzS.cat) || c === _pzS.cat) ? " on" : "");
        b.textContent = c;
        b.onclick = () => {
            _pzS.cat = (c === "全部") ? "" : c;
            _pzRenderCats();
            _pzLoad(true);
        };
        _pzCats.appendChild(b);
    }
}

/** 标签条：只从**本次已加载**的卡片里收集（不额外请求），点一下即按该标签精确过滤。 */
function _pzRenderTags() {
    if (!_pzTags) return;
    const seen = new Set();
    const order = [];
    for (const c of _pzCards) {
        for (const t of (Array.isArray(c.tags) ? c.tags : [])) {
            const s = String(t || "").trim();
            if (!s || seen.has(s)) continue;
            seen.add(s);
            order.push(s);
        }
    }
    const list = order.slice(0, _PZ_UTAGS);
    if (_pzS.tag && list.indexOf(_pzS.tag) < 0) list.unshift(_pzS.tag);
    if (!list.length) { _pzTags.innerHTML = ""; return; }
    _pzTags.innerHTML = "";
    for (const t of list) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "pz-tag" + (t === _pzS.tag ? " on" : "");
        b.textContent = "#" + t;
        b.onclick = () => {
            _pzS.tag = (t === _pzS.tag) ? "" : t;   // 再点一次取消
            _pzRenderTags();
            _pzLoad(true);
        };
        _pzTags.appendChild(b);
    }
}

function _pzCardEl(c) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "pz-card";
    el.onclick = () => _pzOpenDetail(c.id);

    // 封面：有图才建 img（避免 404 图标的抖动）；加载前用首字占位。
    // ★契约 06 §3.3：**列表只发几 KB 的 thumb**（老卡没有 thumb 时才退到 cover）——
    //   display/avatar/表情包一律不在这里请求，那是详情页的事。
    const cov = document.createElement("div");
    cov.className = "pz-cover";
    const ph = document.createElement("span");
    ph.className = "pz-cov-ph";
    ph.textContent = (c.char_name || c.name || "?").slice(0, 1);
    cov.appendChild(ph);
    const covSlot = c.thumb ? "thumb" : (c.cover ? "cover" : "");
    if (covSlot) {
        const img = document.createElement("img");
        img.alt = "";
        img.decoding = "async";
        img._pzLive = true;
        img._pzCard = c.id;
        img._pzSlot = covSlot;
        cov.appendChild(img);
    }

    const badges = document.createElement("div");
    badges.className = "pz-badges";
    const au = c.author || {};
    if (au.official) {
        const off = document.createElement("span");
        off.className = "pz-badge-off";
        off.textContent = "官方";
        badges.appendChild(off);
    }
    if (_pzInstalled.has(c.id)) {
        const ins = document.createElement("span");
        ins.className = "pz-badge-in";
        ins.textContent = "已安装";
        badges.appendChild(ins);
    }
    if (badges.children.length) cov.appendChild(badges);

    const body = document.createElement("div");
    body.className = "pz-cbody";
    const nm = document.createElement("div");
    nm.className = "pz-cname";
    nm.textContent = c.name || c.id;
    const ch = document.createElement("div");
    ch.className = "pz-cchar";
    ch.textContent = c.char_name || "";
    const ds = document.createElement("div");
    ds.className = "pz-cdesc";
    ds.textContent = c.desc || c.tagline || "（作者没写简介）";
    const ft = document.createElement("div");
    ft.className = "pz-cfoot";
    const cat = document.createElement("span");
    cat.className = "pz-cat";
    cat.textContent = c.category || "其他";
    const dl = document.createElement("span");
    dl.className = "pz-dl";
    dl.textContent = _pzFmtNum(c.downloads) + " 次下载";
    ft.append(cat, dl);
    body.append(nm, ch, ds, ft);

    el.append(cov, body);
    return el;
}

function _pzAppendItems(items) {
    if (!_pzGrid) return;
    const frag = document.createDocumentFragment();
    for (const c of items) frag.appendChild(_pzCardEl(c));
    _pzGrid.appendChild(frag);
}

function _pzSetLoading(on) {
    _pzS.loading = !!on;
    if (_pzMore) {
        _pzMore.textContent = on ? "加载中…" : "加载更多";
        _pzMore.disabled = !!on;
    }
}

async function _pzLoad(reset) {
    if (!_pzGrid) return;
    if (reset) {
        _pzGen++;
        _pzS.page = 1;
        _pzCards.length = 0;
        _pzGrid.innerHTML = "";
        if (_pzRoot) _pzRoot.scrollTop = 0;
    }
    if (_pzS.loading && !reset) return;   // 只有"追加一页"才给在飞的那次让路
    // ★ 2026-10-01（P4-7c 真浏览器抓到）：`reset` 请求**一律接管**。原来这里是无条件 `if (loading) return`，
    //   于是"清空了 #plaza-grid → 因为有人在飞就早退"会把列表永久留在空白/旧内容上：
    //   在飞的那次又会被 `_pzGen` 判为过期而丢弃，`_pzSetLoading(false)` 永远不会执行。
    //   治理动作（下架/恢复）和切换筛选都走 reset，必须让最后一次说了算。
    _pzSetLoading(true);
    if (!_pzCards.length) _pzSetMsg("正在读取广场…", null);

    const gen = _pzGen;
    if (_pzAbort) { try { _pzAbort.abort(); } catch (e) {} }
    const ac = (typeof AbortController === "function") ? new AbortController() : null;
    _pzAbort = ac;
    const url = _pzQuery({category: _pzS.cat, tag: _pzS.tag, q: _pzS.q,
                          sort: _pzS.sort, page: _pzS.page, size: _PZ_PAGE});
    // ★ cache: "no-store"：列表是**治理动作的镜子**（下架/恢复后必须立刻变），
    //   走浏览器缓存会拿到旧 payload（2026-10-01 真浏览器抓到：下架后 list 仍是旧两卡，
    //   而后端直查已经只剩一张）。图片走各自的 objectURL 缓存，不受这里影响。
    const r = await _pzReq(url, Object.assign({cache: "no-store"},
                                              ac ? {signal: ac.signal} : null));
    if (gen !== _pzGen) return;               // 用户已换筛选：这次结果作废（也不动 UI）

    if (!r.ok) {
        _pzSetLoading(false);
        if (r.status === 401) {
            _pzS.more = false;
            _pzSetMsg("广场需要登录后才能浏览。", {label: "去登录", onClick: _pzGotoLogin});
        } else if (r.status === 0) {
            _pzS.more = false;
            _pzSetMsg("连不上角色卡广场，请检查网络后重试（你的卡与聊天记录不受影响）。", {label: "重试", onClick: () => _pzLoad(!_pzCards.length)});
        } else {
            _pzS.more = false;
            const why = (r.data && r.data.error) || ("广场服务返回 " + r.status);
            _pzSetMsg(why, {label: "重试", onClick: () => _pzLoad(!_pzCards.length)});
        }
        if (_pzMore) _pzMore.style.display = "none";
        return;
    }

    const d = r.data || {};
    const items = Array.isArray(d.items) ? d.items : [];
    if (Array.isArray(d.categories) && d.categories.length) {
        const same = _pzS.cats && _pzS.cats.length === d.categories.length &&
            _pzS.cats.every((x, i) => x === d.categories[i]);
        _pzS.cats = d.categories;
        if (!same || !_pzCats.children.length) _pzRenderCats();
    }
    _pzS.total = Number(d.total) || 0;
    for (const c of items) {
        if (c && c.id && !_pzCards.some(x => x.id === c.id)) _pzCards.push(c);
    }
    _pzAppendItems(items);
    _pzBust(_pzGrid);
    _pzS.more = (_pzS.page * _PZ_PAGE) < _pzS.total && items.length > 0;
    _pzS.page++;
    _pzSetLoading(false);
    _pzRenderTags();
    _pzUpdateMore();

    if (!_pzCards.length) {
        const isFiltered = !!(_pzS.q || _pzS.cat || _pzS.tag);
        _pzSetMsg(isFiltered ? "没有找到符合条件的角色卡，换个条件试试。"
                             : "广场上还没有公开的角色卡。",
                  isFiltered ? {label: "清除筛选", onClick: _pzClearFilter} : null);
    } else {
        _pzSetMsg("", null);
    }
}

function _pzUpdateMore() {
    if (!_pzMore) return;
    if (_pzS.more) {
        _pzMore.style.display = "";
        _pzMore.textContent = "加载更多（已显示 " + _pzCards.length + " / " + _pzS.total + "）";
        _pzMore.disabled = !!_pzS.loading;
    } else {
        _pzMore.style.display = "none";
    }
}

function _pzClearFilter() {
    _pzS.cat = "";
    _pzS.tag = "";
    _pzS.q = "";
    if (_pzSearch) _pzSearch.value = "";
    _pzRenderCats();
    _pzRenderTags();
    _pzLoad(true);
}

function _pzGotoLogin() {
    _pzClose();
    try { window.showHome && window.showHome(); } catch (e) {}
    try { window.showAuthModule && window.showAuthModule(); } catch (e) {}
    try { window.toggleAuthForms && window.toggleAuthForms(); } catch (e) {}
    showToast("请先登录，再逛角色卡广场");
}
