// 角色卡广场（共创平台 M1）——**详情 + 安装/卸载 + 已装清单与配额**
//
// 2026-10-01（P4-7）自 js/panels/plaza.js **原样拆出**（纯结构拆分，行为零变化）：
// 详情是覆盖在列表上的一层（#plaza-detail）；安装/卸载改的是**本端**数据根（必须由本端执行），
// 与配额条（#pz-quota）共用 `_pzInstalled`（住在 js/panels/plaza.js 外壳）。
// 新加的名字**必须带 `_pz` 前缀**（bundle 单作用域，重名会静默覆盖）。
import { showToast } from "../util.js";

// ═══ 详情 ═════════════════════════════════════════════
/** 按优先级挑第一个存在的**资源槽位名**（契约 06 §3.3：新形状 display/thumb 优先，老卡的
 *  cover/avatar 兜底）。返回 "" 表示这张卡没有对应图。 */
function _pzPickSlot(c, order) {
    for (const k of order) if (c && c[k]) return k;
    return "";
}

/** asset 文件名 → 槽位名（`sticker-1.webp` → `sticker-1`）；名字取不到时按序号兜底。 */
function _pzStem(name, i) {
    const s = String(name || "").split("/").pop();
    const stem = s.replace(/\.[A-Za-z0-9]+$/, "");
    return stem || ("sticker-" + (i + 1));
}

function _pzSetView(v) {
    if (!_pzRoot) return;
    _pzRoot.classList.toggle("plaza-detail-on", v === "detail");
    if (_pzDetail) _pzDetail.classList.toggle("show", v === "detail");
    if (v === "list" && _pzRoot) _pzRoot.scrollTop = 0;
}

async function _pzOpenDetail(cardId) {
    if (!_pzDetail) return;
    const local = _pzCards.find(c => c.id === cardId) || {id: cardId};
    _pzRenderDetail(local, null);      // 先用列表里已有的信息立刻铺一屏（3Mbps 下别让用户干等）
    _pzSetView("detail");
    const r = await _pzReq("/plaza/api/card?id=" + encodeURIComponent(cardId));
    if (r.ok && r.data && r.data.card) {
        const i = _pzCards.findIndex(c => c.id === cardId);
        if (i >= 0) _pzCards[i] = r.data.card;
        if (!_pzRoot || !_pzRoot.classList.contains("plaza-detail-on")) return;  // 已经退出了
        _pzRenderDetail(r.data.card, null);
    } else if (!r.ok) {
        _pzRenderDetail(local, r.status === 401 ? "广场需要登录后才能查看详情。"
            : (r.status === 404 ? "这张卡已经不在广场上了。"
            : ((r.data && r.data.error) || "详情读取失败，请稍后重试。")));
    }
}

function _pzRenderDetail(c, errText) {
    _pzDetail.innerHTML = "";
    const banner = document.createElement("div");
    banner.className = "pz-dbanner";
    const back = document.createElement("button");
    back.type = "button";
    back.className = "pz-dback";
    back.textContent = "←";
    back.setAttribute("aria-label", "返回广场列表");
    back.onclick = () => _pzSetView("list");
    banner.appendChild(back);
    // 契约 06 §3.3：**大图只在详情页取**。字段名 → 资源槽位（新形状优先，老卡兜底）：
    //   display（详情图） > cover（老卡的封面） > thumb（实在没有大图就用列表小图顶上）
    const heroSlot = _pzPickSlot(c, ["display", "cover", "thumb"]);
    if (heroSlot) {
        const img = document.createElement("img");
        img.alt = "";
        img.decoding = "async";
        img._pzLive = true;
        img._pzCard = c.id;
        img._pzSlot = heroSlot;
        banner.appendChild(img);
    }

    const body = document.createElement("div");
    body.className = "pz-dbody";
    const au = c.author || {};

    // 标题行：头像 + 角色名 + 卡名 + 作者/体积/下载数
    const head = document.createElement("div");
    head.className = "pz-dhead";
    const av = document.createElement("div");
    av.className = "pz-davatar";
    const ph = document.createElement("span");
    ph.textContent = (c.char_name || c.name || "?").slice(0, 1);
    av.appendChild(ph);
    const avSlot = _pzPickSlot(c, ["avatar", "thumb"]);
    if (avSlot) {
        const aimg = document.createElement("img");
        aimg.alt = "";
        aimg.decoding = "async";
        aimg._pzLive = true;
        aimg._pzCard = c.id;
        aimg._pzSlot = avSlot;
        av.appendChild(aimg);
    }
    const tt = document.createElement("div");
    tt.className = "pz-dtitle";
    const nm = document.createElement("div");
    nm.className = "pz-dname";
    nm.textContent = c.name || c.id;
    const sub = document.createElement("div");
    sub.className = "pz-dsub";
    const parts = [];
    if (c.char_name) parts.push("角色：" + c.char_name);
    if (au.display) parts.push(au.display);
    if (au.official) parts.push("官方");
    sub.textContent = parts.join(" · ");
    const meta = document.createElement("div");
    meta.className = "pz-dmeta";
    const bits = [];
    if (c.category) bits.push(c.category);
    if (c.size_bytes) bits.push(_pzFmtSize(c.size_bytes));
    bits.push(_pzFmtNum(c.downloads) + " 次下载");
    const pub = _pzFmtTime(c.published_at || c.created_at);
    if (pub) bits.push("发布于 " + pub);
    meta.textContent = bits.join(" · ");
    tt.append(nm, sub, meta);
    head.append(av, tt);
    body.appendChild(head);

    if (c.tagline) {
        const tg = document.createElement("div");
        tg.className = "pz-dtagline";
        tg.textContent = c.tagline;
        body.appendChild(tg);
    }
    if (Array.isArray(c.tags) && c.tags.length) {
        const tw = document.createElement("div");
        tw.className = "pz-dtags";
        for (const t of c.tags) {
            const s = document.createElement("span");
            s.className = "pz-tag static";
            s.textContent = "#" + String(t);
            tw.appendChild(s);
        }
        body.appendChild(tw);
    }
    const sec = document.createElement("div");
    sec.className = "pz-sec-title";
    sec.textContent = "简介";
    const desc = document.createElement("div");
    desc.className = "pz-ddesc";
    desc.textContent = c.desc || "（作者没写简介）";
    body.append(sec, desc);

    // 表情包（契约 06 §3.3：只在详情页取，每张 ≤20KB）。列表里完全不碰它们。
    // 响应里的 stickers 两种都吃：`["sticker-1.webp"]`（老）与 `[{file,name,path,label}]`（带标签）。
    const stk = Array.isArray(c.stickers) ? c.stickers.filter(x => !!x) : [];
    if (stk.length) {
        const st = document.createElement("div");
        st.className = "pz-sec-title";
        st.textContent = "表情包（" + stk.length + "）";
        const grid = document.createElement("div");
        grid.className = "pz-dstk";
        stk.forEach((it, i) => {
            const isObj = it && typeof it === "object";
            const file = isObj ? String(it.file || it.name || it.path || "") : String(it);
            const label = isObj ? String(it.label || "") : "";
            const cell = document.createElement("div");
            cell.className = "pz-dstk-cell";
            const im = document.createElement("img");
            im.alt = "";
            im.decoding = "async";
            im._pzLive = true;
            im._pzCard = c.id;
            im._pzSlot = _pzStem(file, i);
            cell.appendChild(im);
            if (label) {
                const lab = document.createElement("div");
                lab.className = "pz-dstk-label";
                lab.textContent = label;        // 用户数据：一律 textContent，绝不当 HTML
                cell.appendChild(lab);
            }
            grid.appendChild(cell);
        });
        body.append(st, grid);
    }

    const foot = document.createElement("div");
    foot.className = "pz-dfoot";
    const hint = document.createElement("div");
    hint.className = "pz-dhint";
    hint.textContent = _pzInstalled.has(c.id)
        ? "这张卡已经装在本机，可以在角色卡管理里直接使用。"
        : "安装后出现在角色卡列表里，与自建卡一样使用。";

    const actions = document.createElement("div");
    actions.className = "pz-dactions";
    const isIn = _pzInstalled.has(c.id);
    const insBtn = document.createElement("button");
    insBtn.type = "button";
    insBtn.className = "pz-btn primary";
    insBtn.textContent = isIn ? "覆盖安装" : "安装";
    insBtn.onclick = () => _pzInstall(c.id, c.name || c.id, isIn, insBtn);
    actions.appendChild(insBtn);
    if (isIn) {
        const un = document.createElement("button");
        un.type = "button";
        un.className = "pz-btn danger";
        un.textContent = "卸载";
        un.onclick = () => _pzUninstall(c.id, c.name || c.id);
        actions.appendChild(un);
        const ok = document.createElement("span");
        ok.className = "pz-dok";
        ok.textContent = "已安装";
        actions.appendChild(ok);
    }
    // ★「以此为模板改编」（2026-10-01 用户：「如果用户要在我的基础上改呢？」）——
    //   把这张广场卡载入制卡页，**生成一张新 id 的卡**（绝不覆盖原卡）。装载在 plaza_forge_local.js。
    const adapt = document.createElement("button");
    adapt.type = "button";
    adapt.className = "pz-btn";
    adapt.id = "pz-adapt-btn";
    adapt.textContent = "以此为模板改编";
    adapt.title = "把这张卡的内容载入制卡页，改完发布成你自己的新卡（不会改动原卡）";
    adapt.onclick = () => {
        if (window.plazaAdaptFromPlaza) window.plazaAdaptFromPlaza(c.id);
        else showToast("这个版本还不支持改编");
    };
    actions.appendChild(adapt);
    // 举报入口（P4-3）：治理动作放在详情页动作行最后，**弱化样式**（红色语义留给卸载）。
    // 真正的弹层与请求在 panels/plaza_admin.js（同一域的另一层）；这里只负责给入口。
    const rep = document.createElement("button");
    rep.type = "button";
    rep.className = "pz-btn pz-report-btn";
    rep.textContent = "举报";
    rep.title = "这张卡有问题？选个理由告诉管理员";
    rep.onclick = () => {
        try { window.plazaReportOpen && window.plazaReportOpen(c.id, c.name || c.id); } catch (e) {}
    };
    actions.appendChild(rep);
    if (errText) {
        const e = document.createElement("div");
        e.className = "pz-derr";
        e.textContent = errText;
        body.appendChild(e);
    }
    foot.append(hint, actions);
    body.appendChild(foot);
    _pzDetail.append(banner, body);
    _pzBust(_pzDetail);
}

// ═══ 安装 / 卸载 ══════════════════════════════════════
async function _pzInstall(cardId, name, replace, btn) {
    // 覆盖安装会先备份旧卡（后端 _backup_existing），这里把话说清楚再动手
    if (replace && !confirm("覆盖安装「" + name + "」？\n" +
            "同 id 的角色卡会被卡里的内容替换（覆盖前后端会自动留一份备份）。")) return;
    const old = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "安装中…"; }
    const r = await _pzReq("/plaza/api/install", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: cardId, replace: !!replace}),
    });
    if (btn) { btn.disabled = false; btn.textContent = old; }
    if (r.ok) {
        const info = (r.data && r.data.card) || {};
        showToast(info.upgraded ? ("已覆盖安装「" + (info.name || name) + "」（旧版已备份）")
                                : ("已安装「" + (info.name || name) + "」"));
        _pzInstalled.set(cardId, {id: cardId, name: info.name || name, char_name: info.char_name || ""});
        _pzInstalledAt = 0;                       // 配额用量变了，下次打开面板重新拉
        try { window.__modesReload && window.__modesReload(); } catch (e) {}   // 新卡进角色卡列表
        _pzRefreshInstalled(true);
        _pzOpenDetail(cardId);                    // 重渲染详情（按钮从「安装」变「已安装/卸载」）
        return;
    }
    // 后端文案已经是人话（"已安装同名…请选择覆盖" / "最多安装 5 张…"），直接用
    const why = (r.data && r.data.error) || (r.status === 401 ? "请先登录后再安装。" : "安装失败，请稍后重试。");
    if (r.status === 401) { showToast(why); _pzGotoLogin(); return; }
    if (/覆盖/.test(why) && !replace) {
        if (confirm(why + "\n\n现在就覆盖安装吗？")) _pzInstall(cardId, name, true, btn);
        return;
    }
    showToast(why);
}

async function _pzUninstall(cardId, name) {
    if (!confirm("卸载「" + name + "」？\n角色卡会从本机列表里移除（可随时从广场重新安装）。")) return;
    const r = await _pzReq("/plaza/api/uninstall", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: cardId}),
    });
    if (!r.ok) {
        showToast((r.data && r.data.error) || "卸载失败，请稍后重试。");
        return;
    }
    showToast("已卸载「" + name + "」");
    _pzInstalled.delete(cardId);
    _pzInstalledAt = 0;
    try { window.__modesReload && window.__modesReload(); } catch (e) {}
    _pzRefreshInstalled(true);
    _pzLoad(true);                                // 列表上的「已安装」角标要跟着变
    _pzSetView("list");
}

// ═══ 已安装 + 配额 ════════════════════════════════════
async function _pzRefreshInstalled(force) {
    const fresh = _pzInstalledAt && (Date.now() - _pzInstalledAt < 30000);
    if (!force && fresh) { _pzRenderQuota(); return; }
    const r = await _pzReq("/plaza/api/installed");
    if (!_pzIsOpen) return;          // 面板已关：别往一张看不见的面板上写
    if (!r.ok || !r.data) {
        // 未登录时这里是 401：列表本身会给出「去登录」提示，配额条就不重复刷屏了
        _pzRenderQuota();
        return;
    }
    const d = r.data;
    _pzInstalled.clear();
    for (const it of (Array.isArray(d.items) ? d.items : [])) {
        if (it && it.id) _pzInstalled.set(it.id, it);
    }
    _pzInstalledUsed = d;
    _pzInstalledAt = Date.now();
    _pzRenderQuota();
}

function _pzRenderQuota() {
    if (!_pzQuota) return;
    _pzQuota.innerHTML = "";
    const d = _pzInstalledUsed;
    if (!d || d.server_mode === false) {
        // 本地模式不计配额：**不显示** 0/5 那种假进度条，只说明安装位置
        const s = document.createElement("span");
        s.className = "pz-quota-lite";
        s.textContent = "已安装 " + _pzInstalled.size + " 张广场角色卡（保存在本机）";
        _pzQuota.appendChild(s);
        return;
    }
    // 配额上限：**`null`/缺失 = 不限**（管理员账号由服务端这么返回，2026-10-01 用户拍板）。
    // ⚠ 不能写成 `Number(d.max_cards) || 0` —— `Number(null) === 0`，那会把"不限"显示成「0/0 张」。
    const _cap = (v) => (v === null || v === undefined) ? null : (Number(v) || 0);
    const maxC = _cap(d.max_cards);
    const maxB = _cap(d.max_bytes);
    const usedC = Number(d.used_cards) || 0;
    const usedB = Number(d.used_bytes) || 0;
    const bar = document.createElement("div");
    bar.className = "pz-quota-bars";
    for (const [used, max, unit] of [[usedC, maxC, "张"], [usedB, maxB, ""]]) {
        const row = document.createElement("div");
        row.className = "pz-quota-row";
        const t = document.createElement("span");
        t.className = "pz-quota-t";
        if (unit) {
            t.textContent = (max === null) ? ("卡片 " + used + " 张（不限）")
                                           : ("卡片 " + used + " / " + max + " 张");
        } else {
            t.textContent = (max === null) ? ("容量 " + _pzFmtSize(used) + "（不限）")
                                           : ("容量 " + _pzFmtSize(used) + " / " + _pzFmtSize(max));
        }
        const track = document.createElement("div");
        track.className = "pz-quota-track";
        const fill = document.createElement("i");
        // 不限 ⇒ 没有"进度"可言（恒 0 宽度），只显示用量文字
        const pct = (max !== null && max > 0) ? Math.min(100, Math.round(used * 100 / max)) : 0;
        fill.style.width = pct + "%";
        if (pct >= 90) fill.className = "warn";     // 快满了：暖红提示，别等失败才知道
        track.appendChild(fill);
        row.append(t, track);
        bar.appendChild(row);
    }
    _pzQuota.appendChild(bar);
    if (_pzInstalled.size) {
        const names = [];
        _pzInstalled.forEach(v => names.push(v.char_name || v.name || v.id));
        const lst = document.createElement("div");
        lst.className = "pz-quota-list";
        lst.textContent = "已装：" + names.join("、");
        _pzQuota.appendChild(lst);
    }
}
