// 角色卡制作（共创平台 M2）——**生命周期**：草稿箱 + 存草稿 + 审核闸门与弹窗 + 发布/下架/删除
//
// 2026-10-01（P4-7b）自 js/panels/plaza_forge.js **原样拆出**（纯结构拆分，行为零变化）：
// 这一层管"存了之后怎么走"：草稿箱列表 → 继续编辑/发布 → 提交 AI 审核 → 发布到广场 / 下架 / 删除。
// 审核闸门（_pfMarkDirty/_pfSyncBtns）是本层的地基：**任何一次改动都让审核结论作废**，
// 表单层的输入事件只是调它，不复制它的判断（约束 2）。
// 顶层状态（`_pfS`/`_pfSeq`/`_pfModalSeq`/`_pfEl`）与 DOM 引用仍住在外壳 js/panels/plaza_forge.js；
// 新加的名字**必须带 `_pf` 前缀**（bundle 单作用域，重名会静默覆盖）。
// ⚠ 审核弹窗**只在这一刻读一次用户自己的 API Key**，随即清空、不落任何存储（约束 1）。
import { showToast } from "../util.js";

// ═══ 审核状态与按钮闸门 ═══════════════════════════════
function _pfMarkDirty() {
    if (!_pfS.dirty) {
        _pfS.dirty = true;
        _pfS.verdict = "";
        _pfStatus("卡片已改动——审核结论作废，请重新提交审核后再发布。", "warn");
    }
    _pfSyncId();
    _pfSyncBtns();
}

function _pfStatus(text, kind) {
    if (!_pfEl.status) return;
    _pfEl.status.className = "pf-status" + (kind ? " " + kind : "");
    _pfEl.status.textContent = text || "";
}

function _pfSyncBtns() {
    // ★ 管理员免审（2026-10-01 用户实测：管理员也被要求"提交审核"才能发布 ✗）。
    //   服务端 `drafts.publish_draft` 对 admin 本就跳过 verdict 闸 ⇒ 前端同步放行：
    //   admin 只要**存过一次草稿**（拿到 id）就能直接发布；非 admin 仍必须"审核通过 + 未改动"。
    const admin = (typeof _pfIsAdminSync === "function") && _pfIsAdminSync();
    const canPublish = admin ? !!_pfS.id : (_pfS.verdict === "pass" && !_pfS.dirty);
    if (_pfEl.bPub) {
        _pfEl.bPub.disabled = _pfS.busy || !canPublish;
        _pfEl.bPub.title = canPublish ? ""
            : (admin ? "先「存草稿」拿到卡片 id，然后就能直接发布（管理员免审）"
                     : "需要先提交审核并通过（通过后 30 秒内发布最稳妥）");
    }
    // 管理员：**隐藏「提交审核」**（他不该被强制走审核），并显示"免审"说明
    if (_pfEl.bRev) _pfEl.bRev.style.display = admin ? "none" : "";
    if (_pfEl.adminNote) _pfEl.adminNote.style.display = admin ? "block" : "none";
    // ★「下架」只对**已发布**的卡显示（2026-10-01 用户截图：新卡也显示「下架」✗）；
    //   其余状态（草稿/待审/未通过）没有"下架"这回事，显示"未发布"更诚实。
    if (_pfEl.bUnp) {
        const published = String(_pfS.draftStatus || "") === "published";
        _pfEl.bUnp.style.display = published ? "" : "none";
        _pfEl.bUnp.disabled = _pfS.busy || !_pfS.id;
    }
    for (const b of [_pfEl.bSave, _pfEl.bRev]) if (b) b.disabled = _pfS.busy;
}

function _pfSyncId() {
    const e = _pf("pf-id");
    if (!e || !_pfEl.idTip) return;
    const v = String(e.value || "").trim();
    const ok = _PF_ID_RE.test(v);
    _pfEl.idTip.className = "pf-idtip " + (ok ? "ok" : (v ? "bad" : ""));
    _pfEl.idTip.textContent = ok ? ("id 合法：" + v)
        : (v ? "id 不合法：只能用 1-32 位小写字母/数字/下划线/短横线" : "id 不能为空");
    return ok;
}

// ═══ 草稿箱 ═══════════════════════════════════════════
async function _pfLoadDrafts(force) {
    if (_pfS.draftsLoaded && !force) { _pfRenderDrafts(); return; }
    _pfRenderDrafts("loading");
    const r = await _pfReq("/plaza/api/drafts");
    if (!r.ok) {
        _pfS.drafts = [];
        _pfS.draftsLoaded = false;
        _pfS.draftsErr = _pfWhy(r, "草稿箱读取失败，请稍后重试。");
        _pfRenderDrafts("error");
        return;
    }
    const d = r.data || {};
    _pfS.drafts = Array.isArray(d.items) ? d.items : [];
    _pfS.draftsLoaded = true;
    _pfS.draftsErr = "";
    _pfRenderDrafts();
}

function _pfRenderDrafts(mode) {
    const box = _pfEl.draftsBox;
    if (!box) return;
    box.textContent = "";
    box.appendChild(_pfEl2("div", "pf-box-title", "我的草稿箱"));
    // ★ 草稿份数：后端 `DRAFT_MAX`（2026-10-02：10 → 50），**管理员不限** ⇒ 文案随之切换
    const sub = _pfEl2("div", "pf-box-sub",
                       "最多保留 " + _PF_DRAFT_MAX + " 份；每份都占服务器空间（3Mbps），不用的记得删。");
    box.appendChild(sub);
    _pfIsAdmin().then((yes) => {
        if (yes && sub.isConnected) {
            sub.textContent = "草稿份数不限（管理员）；每份都占服务器空间（3Mbps），不用的记得删。";
        }
    });
    if (mode === "loading") {
        box.appendChild(_pfEl2("div", "pf-drafts-msg", "正在读取草稿…"));
        return;
    }
    if (mode === "error") {
        const w = _pfEl2("div", "pf-drafts-msg bad", _pfS.draftsErr || "读取失败");
        box.appendChild(w);
        const b = _pfEl2("button", "pz-btn", "重试");
        b.type = "button";
        b.onclick = () => _pfLoadDrafts(true);
        box.appendChild(b);
        if (_pfS.draftsErr && _pfS.draftsErr.indexOf("登录") >= 0) {
            const g = _pfEl2("button", "pz-btn", "去登录");
            g.type = "button";
            g.onclick = _pfGotoLogin;
            box.appendChild(g);
        }
        return;
    }
    if (!_pfS.drafts.length) {
        box.appendChild(_pfEl2("div", "pf-drafts-msg", "还没有草稿。填完左边的表，点「存草稿」就会出现在这里。"));
        return;
    }
    for (const it of _pfS.drafts) {
        const row = _pfEl2("div", "pf-draft");
        const top = _pfEl2("div", "pf-draft-top");
        top.appendChild(_pfEl2("div", "pf-draft-name", it.name || it.id));
        const st = String(it.status || "draft");
        const badge = _pfEl2("span", "pf-badge " + st, _PF_STATUS[st] || st);
        top.appendChild(badge);
        const meta = _pfEl2("div", "pf-draft-meta",
            "id " + it.id + " · " + _pfFmtTime(it.updated_at) + " · " + _pfFmtSize(it.size_bytes)
            + (it.review_verdict ? (" · 审核：" + (_PF_VERDICT[it.review_verdict] || it.review_verdict)) : ""));
        const acts = _pfEl2("div", "pf-draft-acts");
        const edit = _pfEl2("button", "pz-btn small", "继续编辑");
        edit.type = "button";
        edit.onclick = () => _pfLoadOne(it.id, edit);
        const pub = _pfEl2("button", "pz-btn small primary", "发布");
        pub.type = "button";
        pub.disabled = st !== "reviewed";
        pub.title = st === "reviewed" ? "" : "需要先通过审核";
        pub.onclick = () => _pfPublish(it.id, pub);
        const del = _pfEl2("button", "pz-btn small danger", "删除");
        del.type = "button";
        del.onclick = () => _pfDelete(it);
        const unp = _pfEl2("button", "pz-btn small", "下架");
        unp.type = "button";
        unp.onclick = () => _pfUnpublish(it.id, unp);
        acts.append(edit, pub);
        // ★「下架」只对**已发布**的卡有意义（2026-10-01 用户截图：一张新卡也显示「下架」✗）
        if (st === "published") acts.appendChild(unp);
        acts.appendChild(del);
        row.append(top, meta, acts);
        box.appendChild(row);
    }
}

async function _pfLoadOne(id, btn) {
    if (_pfS.busy) return;
    const old = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "读取中…"; }
    const r = await _pfReq("/plaza/api/draft?id=" + encodeURIComponent(id));
    if (btn) { btn.disabled = false; btn.textContent = old; }
    if (!r.ok) {
        if (r.status === 401) { _pfGotoLogin(); return; }
        _pfMsg(_pfWhy(r, "草稿读取失败，请稍后重试。"), false);
        return;
    }
    const d = (r.data && r.data.draft) || {};
    _pfSetForm(d.card || {}, d.meta || {});
    _pfMsg("", false);
    const meta = d.meta || {};
    const verdict = (meta.review && meta.review.verdict) || "";
    if (verdict === "pass") {
        _pfS.verdict = "pass";
        _pfS.dirty = false;                 // 刚拉回来的草稿就是"审核时那一份"
        _pfStatus("这份草稿已通过审核，可以直接发布。", "ok");
    } else if (verdict) {
        _pfS.verdict = "";
        _pfS.dirty = true;
        _pfStatus("上次审核结论：" + (_PF_VERDICT[verdict] || verdict) + "。改好后需要重新提交审核。", "warn");
    } else {
        _pfS.verdict = "";
        _pfS.dirty = true;
        _pfStatus("草稿已载入，还没提交过审核。", "warn");
    }
    _pfSyncBtns();
    // ★ 草稿回读也把**审核覆盖范围**摆出来（不是只在刚审完那一次显示）——老结论没有这些字段就不显示
    if (meta.review && typeof _pfRenderReview === "function") {
        try { _pfRenderReview(meta.review); } catch (e) {}
    }
    // ★ 老草稿迁移闸门（task-13）：V1 之前的草稿没有 thumb（老卡会把大 cover 兜底成 thumb）⇒
    //   直接保存会被后端 CARD_MISSING_THUMB 拦。这里当场补一张合规 thumb，并说清楚发生了什么；
    //   一张图都没有的老草稿则由 `_pfiValidate()` 给出"列表缩略图是必填"的人话原因（不静默失败）。
    try {
        if (typeof _pfiEnsureLegacyThumb === "function") {
            _pfiEnsureLegacyThumb().then((made) => {
                if (!made) return;
                showToast("已自动补上列表缩略图");
                _pfMsg("这张草稿是旧格式（没有列表缩略图）——已用现有图片自动生成一张"
                       + "（≤320px / ≤12KB）。确认无误再保存，也可以自己换一张。", false);
            });
        }
    } catch (e) {}
    try { if (_pfEl.pane && _pfEl.pane.scrollIntoView) _pfEl.pane.scrollIntoView({block: "start"}); } catch (e) {}
}

// ═══ 保存 / 审核 / 发布 / 下架 ═══════════════════════
async function _pfSave(btn) {
    if (_pfS.busy) return false;
    const why = _pfValidate(false);
    if (why) return false;
    _pfS.busy = true;
    _pfSyncBtns();
    const old = _pfEl.bSave ? _pfEl.bSave.textContent : "";
    if (_pfEl.bSave) _pfEl.bSave.textContent = "保存中…";
    const _card = _pfRead();
    const _display = String((_pf("pf-display") || {}).value || "").trim();
    let r = null;
    let _fb = null;
    // ★ 改编态（`_pflS.base` 有值）：**只上传 patch**（`/plaza/api/derive`）；
    //   服务端不支持时 `_pflSaveDerive` 内部自动退回整卡上传并给出人话（不丢内容）。
    if (typeof _pflSaveDerive === "function" && typeof _pflS === "object" && _pflS && _pflS.base) {
        const d = await _pflSaveDerive(_card, _display);
        r = d.res;
        _fb = d;
    } else {
        r = await _pfPost("/plaza/api/draft", {card: _card, display: _display});
    }
    _pfS.busy = false;
    if (_pfEl.bSave) _pfEl.bSave.textContent = old;
    _pfSyncBtns();
    if (!r.ok) {
        if (r.status === 401) { _pfGotoLogin(); return false; }
        _pfMsg(_pfWhy(r, "草稿保存失败，请稍后重试。"), false);
        return false;
    }
    const d = (r.data && r.data.draft) || {};
    _pfS.id = String(d.id || _pfS.id);
    _pfS.draftStatus = String(d.status || "draft");
    // 保存本身不改卡体，所以**不清 dirty**：审核结论只对"审核时那一份卡体"有效
    if (_pfS.verdict === "pass") {
        _pfStatus("已存草稿。注意：这份卡和通过审核的那一份已经不是同一份了，发布前请重新提交审核。", "warn");
        _pfS.dirty = true;
    } else {
        _pfStatus("已存草稿（" + (d.updated_at ? _pfFmtTime(d.updated_at) : "刚刚") + "）。", "ok");
    }
    _pfMsg("", false);
    if (_fb && _fb.why) {
        // 保守回退：服务端还没有 derive ⇒ 说清"已按整卡上传"，功能不中断
        _pfMsg(_fb.why + "（这次上传 " + _pfFmtSize((_fb.bytes || {}).full || 0) + "，"
               + "只传改动本可以是 " + _pfFmtSize((_fb.bytes || {}).derive || 0) + "）", false);
    } else if (_fb && _fb.summary) {
        _pfStatus("已存草稿（" + _pfFmtSize((_fb.bytes || {}).derive || 0) + " 上传）："
                  + _fb.summary + "。", "ok");
    }
    _pfSyncBtns();
    _pfLoadDrafts(true);
    showToast(_fb ? "改编草稿已保存（只传改动）" : "草稿已保存");
    return true;
}

function _pfReviewOpen() {
    if (_pfS.busy) return;
    const why = _pfValidate(false);
    if (why) return;
    _pfModalOpen();
}

function _pfModalOpen() {
    const wrap = _pf("pf-modal");
    if (!wrap) return;
    const seq = ++_pfModalSeq;
    wrap.textContent = "";
    wrap.classList.add("show");
    const box = _pfEl2("div", "pf-modal-box");
    box.appendChild(_pfEl2("div", "pf-modal-title", "提交 AI 审核"));
    const ul = _pfEl2("div", "pf-modal-note");
    for (const t of [
        "请粘贴你自己的 DeepSeek API Key（sk- 开头）。",
        "Key 只在这一刻读取，随这次请求发给本项目后端去调用审核模型，回来后立刻清空——不写浏览器存储、不进日志、不显示在页面上。",
        "审核通过后会自动存一份草稿，发布按钮随即可用。审核之后再改卡，会被要求重新审核。",
    ]) ul.appendChild(_pfEl2("div", "pf-note-item", "· " + t));
    const inp = document.createElement("input");
    inp.type = "password";
    inp.id = "pf-sk";
    inp.autocomplete = "off";
    inp.placeholder = "sk-…";
    inp.className = "pf-skinput";
    const errEl = _pfEl2("div", "pf-modal-err", "");
    errEl.style.display = "none";
    const acts = _pfEl2("div", "pf-modal-acts");
    const cancel = _pfEl2("button", "pz-btn", "取消");
    cancel.type = "button";
    cancel.onclick = () => _pfModalClose();
    const ok = _pfEl2("button", "pz-btn primary", "提交审核");
    ok.type = "button";
    ok.id = "pf-modal-ok";
    const st = _pfEl2("div", "pf-modal-status", "");
    acts.append(cancel, ok);
    box.append(ul, inp, errEl, acts, st);
    wrap.appendChild(box);
    const showErr = (m) => { errEl.textContent = m; errEl.style.display = "block"; };
    ok.onclick = async () => {
        // ★ 唯一一次读取 Key：读进局部变量 → 立刻清空输入框 → 发请求 → 出栈即丢弃
        let sk = String(inp.value || "").trim();
        inp.value = "";
        if (!/^sk-[A-Za-z0-9]{16,64}$/.test(sk)) {
            sk = "";                       // 形态不对：本地直接拒，别把垃圾发出去
            // 口径与服务端**逐字对齐**（app/plaza/review.py:30 `SK_RE`）：不带连字符、16~64 位字母数字。
            // 前端原来写的是 `/^sk-[\w-]{8,}/`（宽）⇒ 带连字符的 Key 会被放过、到服务端才 400。
            showErr("Key 形态不对：应为 sk- 加 16~64 位字母数字（不含连字符），"
                    + "请到 DeepSeek 控制台复制完整的 Key。");
            return;
        }
        const card = _pfRead();
        if (seq !== _pfModalSeq) { sk = ""; return; }   // 弹窗已被关掉
        ok.disabled = true; cancel.disabled = true;
        st.className = "pf-modal-status";
        st.textContent = "正在审核（用你自己的 Key 调用一次模型，稍等）…";
        const r = await _pfPost("/plaza/api/review", {
            sk: sk, card: card,
            display: String((_pf("pf-display") || {}).value || "").trim(),
        });
        sk = "";                            // 无论成败：本地引用立刻断开
        if (seq !== _pfModalSeq) return;    // 用户已经关掉/重开：不再动这层 UI
        ok.disabled = false; cancel.disabled = false;
        if (!r.ok) {
            st.textContent = "";
            if (r.status === 401) { _pfModalClose(); _pfGotoLogin(); return; }
            // ★ **系统问题 ≠ 业务结论**（2026-10-01 用户要求）：调用失败/无法解析必须说成"服务异常 + 可重试"，
            //   绝不能用"审核未通过"的口吻（那会让人以为自己的卡有问题）。`error_kind`/`raw_excerpt`
            //   若服务端给了就折叠展示，便于排查"为什么"。
            const why = _pfWhy(r, "审核服务异常，请稍后重试。");
            const d = (r.data || {});
            // 排障信息不丢：原始 error_kind 落控制台，用户可见文案里不出现机读值（裁定②）
            console.warn("[plaza-review] 审核返回异常 kind=", d && d.error_kind, d, r);
            _pfSystemError("审核这次没跑通——不是你的卡被拒，稍后重试即可（Key 与内容都不用改）。",
                           why, d);
            return;
        }
        const rev = (r.data && r.data.review) || {};
        const admin = (typeof _pfIsAdminSync === "function") && _pfIsAdminSync();
        // 判为"系统异常"的三种情形：显式 error_kind / 带 raw_excerpt / 原因里是"无法解析/调用失败"这类
        const revWhy = (Array.isArray(rev.reasons) ? rev.reasons.join(" ") : "");
        const sysErr = !!rev.error_kind || !!(rev.raw_excerpt)
            || /无法解析|解析失败|调用失败|超时|unparse|parse/i.test(revWhy);
        if (sysErr) {
            // 排障信息不丢：原始 error_kind 等落控制台，用户可见文案里不出现机读值（裁定②）
            console.warn("[plaza-review] 审核系统异常 kind=", rev.error_kind, revWhy, rev);
            _pfModalClose();
            _pfSystemError("审核这次没跑通——不是你的卡被拒，稍后重试即可（Key 与内容都不用改）。",
                           revWhy || "", rev);
            return;
        }
        _pfS.verdict = String(rev.verdict || "");
        _pfS.dirty = _pfS.verdict === "pass" ? false : true;
        _pfS.draftStatus = _pfS.verdict === "pass" ? "reviewed" : "rejected";
        if (_pfS.verdict === "pass") {
            _pfStatus("审核已通过——现在可以发布到广场了。", "ok");
            showToast("审核通过，可以发布了");
        } else if (_pfS.verdict === "manual") {
            _pfStatus("模型判为转人工复核（业务结论，不是系统故障）：等复核结果，"
                      + (admin ? "管理员可直接发布。" : "现在还不能发布。"), "warn");
        } else {
            _pfStatus("审核未通过：按下面的理由改好，改完必须重新提交审核。", "bad");
        }
        _pfModalClose();
        _pfRenderReview(rev);
        _pfSyncBtns();
        _pfLoadDrafts(true);                // 审核通过后端会自动存草稿，刷新状态
    };
    try { inp.focus(); } catch (e) {}
}

/** ★ **系统异常 ≠ 审核未通过**（2026-10-01 用户要求）：调用失败/无法解析时说成"服务异常 + 可重试"，
 *  绝不套"未通过"的口吻（那会让人以为自己的卡有问题）；`error_kind`/`raw_excerpt` 折叠展示。
 *  状态上：不清 verdict 到"rejected"，把 draftStatus 留在 draft（卡本身没被判有问题）。 */
function _pfSystemError(title, why, data) {
    _pfS.verdict = "";
    _pfS.dirty = true;
    _pfS.draftStatus = "draft";
    _pfStatus(title, "bad");
    _pfMsg(why ? (title + "　服务端原因：" + why) : title, {label: "重试", onClick: () => _pfReviewOpen()});
    _pfRenderSystemError(title, why, data);
    _pfSyncBtns();
}

/** 系统异常的**详情折叠**（有 error_kind / raw_excerpt 才显示），默认不刷屏但能查到"为什么"。 */
function _pfRenderSystemError(title, why, data) {
    const box = _pf("pf-review");
    if (!box) return;
    box.textContent = "";
    box.classList.add("show");
    const head = _pfEl2("div", "pf-rev-head rejected");
    head.appendChild(_pfEl2("span", "pf-badge rejected", "系统异常"));
    head.appendChild(_pfEl2("span", "pf-rev-meta", "不是「审核未通过」——卡本身没有被判有问题"));
    box.appendChild(head);
    box.appendChild(_pfEl2("div", "pf-rev-reason", title));
    const kind = String((data && data.error_kind) || "");
    const raw = String((data && data.raw_excerpt) || "");
    if (kind || raw || why) {
        const d = document.createElement("details");
        d.id = "pf-rev-detail";
        const s = document.createElement("summary");
        s.textContent = "技术细节（error_kind / 返回片段）";
        d.appendChild(s);
        if (kind) d.appendChild(_pfEl2("div", "pf-rev-reason", "error_kind：" + kind));
        if (why) d.appendChild(_pfEl2("div", "pf-rev-reason", "服务端原因：" + why));
        if (raw) d.appendChild(_pfEl2("div", "pf-rev-raw", raw.slice(0, 400)));
        box.appendChild(d);
    }
    const acts = _pfEl2("div", "pf-rev-act", "");
    const retry = _pfEl2("button", "pz-btn", "重试审核");
    retry.type = "button";
    retry.id = "pf-rev-retry";
    retry.onclick = () => _pfReviewOpen();
    const close = _pfEl2("button", "pf-rev-close", "收起");
    close.type = "button";
    close.onclick = () => { box.classList.remove("show"); box.textContent = ""; };
    acts.append(retry, close);
    box.appendChild(acts);
}

function _pfModalClose() {
    _pfModalSeq++;
    const wrap = _pf("pf-modal");
    if (!wrap) return;
    wrap.classList.remove("show");
    wrap.textContent = "";
}

/** ★ 审核**覆盖范围**如实文案（2026-10-02，服务端 `review.py` 只送**前 4 万字符**抽检）。
 *
 *  字段：`reviewed_chars`（实际送审字符）/ `truncated`（是否截断）/ `files_included` /
 *  `files_omitted`（未纳入的文件名）。**绝不允许**写成"全文已审核"——用户会以为整张卡都过审了。
 *  老结论没有这些字段时返回 ""（不编、不猜）。 */
function _pfCoverageText(rev) {
    const r = rev || {};
    const n = Number(r.reviewed_chars || 0);
    const inc = Array.isArray(r.files_included) ? r.files_included : [];
    const omi = Array.isArray(r.files_omitted) ? r.files_omitted : [];
    if (!n && !inc.length && !omi.length) return "";
    if (r.truncated || omi.length) {
        if (!omi.length) {
            // 只是单份文件被 `_clip` 截了，没有"整份没纳入"的文件
            return "审核按前 4 万字符抽检，已截断（超长文件只送了前 12000 字符）——这不等于全文过审。";
        }
        return "审核按前 4 万字符抽检，已截断；另有 " + omi.length + " 份文件未纳入"
               + "（" + omi.slice(0, 6).join("、") + (omi.length > 6 ? " 等" : "") + "）"
               + "——这不等于全文过审。";
    }
    const kb = inc.filter((p) => String(p).indexOf("knowledge/") === 0).length;
    return "审核按前 4 万字符抽检（已覆盖正文与 " + kb + " 份知识库，共 " + n + " 字符）。";
}

function _pfRenderReview(rev) {
    const box = _pf("pf-review");
    if (!box) return;
    box.textContent = "";
    box.classList.add("show");
    const v = String(rev.verdict || "");
    const head = _pfEl2("div", "pf-rev-head " + v);
    head.appendChild(_pfEl2("span", "pf-badge " + (v === "pass" ? "reviewed" : "rejected"),
                            "审核" + (_PF_VERDICT[v] || v || "未知")));
    const bits = [];
    if (rev.risk) bits.push("风险：" + ({low: "低", medium: "中", high: "高"}[rev.risk] || rev.risk));
    if (rev.model) bits.push("模型：" + rev.model);
    if (rev.reviewed_at) bits.push("时间：" + _pfFmtTime(rev.reviewed_at));
    head.appendChild(_pfEl2("span", "pf-rev-meta", bits.join(" · ")));
    box.appendChild(head);
    // ★ 覆盖范围（如实）：单独一行、带 `bad` 色标（截断时）——防"以为全文过审"
    const cov = _pfCoverageText(rev);
    if (cov) {
        const c = _pfEl2("div", "pf-rev-coverage" + (rev.truncated ? " warn" : ""), cov);
        c.id = "pf-rev-coverage";
        box.appendChild(c);
    }
    const reasons = Array.isArray(rev.reasons) ? rev.reasons : [];
    if (reasons.length) {
        const ul = _pfEl2("div", "pf-rev-reasons");
        for (const t of reasons) ul.appendChild(_pfEl2("div", "pf-rev-reason", "· " + String(t)));
        box.appendChild(ul);
    }
    const act = _pfEl2("div", "pf-rev-act", "");
    if (v === "pass") {
        act.textContent = "已通过，可发布。发布后可在广场里看到它。";
    } else if (v === "manual") {
        act.textContent = "转人工复核：发布按钮保持关闭，等复核结果。";
    } else {
        act.textContent = "按上面的理由修改后，点「提交审核」重新审一次（改完不重审，发布会报「审核后又被改动」）。";
    }
    box.appendChild(act);
    const close = _pfEl2("button", "pf-rev-close", "收起");
    close.type = "button";
    close.onclick = () => { box.classList.remove("show"); box.textContent = ""; };
    box.appendChild(close);
}

async function _pfPublish(id, btn) {
    if (_pfS.busy) return;
    const did = String(id || _pfS.id || "").trim();
    if (!did) { _pfMsg("请先存一次草稿，再发布。", false); return; }
    if (!id) {
        // 主按钮：状态必须与服务端一致地"已通过且未改动"——**管理员除外**（服务端对 admin 免审）
        const admin = (typeof _pfIsAdminSync === "function") && _pfIsAdminSync();
        if (!admin && (_pfS.verdict !== "pass" || _pfS.dirty)) {
            _pfMsg("这张卡还没有可用的审核结论（改动过就得重新提交审核）。", false);
            return;
        }
        if (admin && _pfS.dirty && !_pfS.id) {
            _pfMsg("先「存草稿」拿到卡片 id，然后就能直接发布（管理员免审）。", false);
            return;
        }
    }
    _pfS.busy = true;
    _pfSyncBtns();
    const old = btn ? btn.textContent : (_pfEl.bPub ? _pfEl.bPub.textContent : "");
    if (btn) { btn.disabled = true; btn.textContent = "发布中…"; }
    else if (_pfEl.bPub) _pfEl.bPub.textContent = "发布中…";
    const r = await _pfPost("/plaza/api/publish",
                            {id: did, replace: false, official: _pfOfficialWanted()});
    _pfS.busy = false;
    if (btn) { btn.disabled = false; btn.textContent = old; }
    else if (_pfEl.bPub) _pfEl.bPub.textContent = old;
    _pfSyncBtns();
    if (!r.ok) {
        if (r.status === 401) { _pfGotoLogin(); return; }
        const why = _pfWhy(r, "发布失败，请稍后重试。");
        if (/审核后又被改动|重新提交审核|还没有通过审核|不能发布/.test(why)) {
            _pfS.verdict = "";
            _pfS.dirty = true;
            _pfStatus("服务器判定：这张卡在审核后又被改动过——请重新提交审核再发布。", "warn");
            _pfSyncBtns();
        }
        _pfMsg(why, false);
        if (/已存在|同 id|覆盖/.test(why)) {
            const b = _pfEl2("button", "pz-btn", "覆盖发布");
            b.type = "button";
            b.id = "pf-replace-btn";
            // ★ 2026-10-01：不再"一点就盖"。防误覆盖的默认仍是 `replace:false`；
            //   这一下只是打开**二次确认**（说清"会用当前草稿替换广场上这张卡的已发布版本"），
            //   用户确认后才真正传 `replace:true`。
            b.onclick = () => _pfReplaceConfirm(did, why);
            const m = _pfEl.msg;
            if (m) m.appendChild(b);
        }
        return;
    }
    const card = (r.data && r.data.card) || {};
    _pfS.draftStatus = "published";
    _pfS.verdict = "";
    _pfS.dirty = true;
    _pfSyncBtns();          // ★ 状态变了要立刻重算按钮（「下架」只对已发布的卡显示）
    _pfStatus("已发布到广场：「" + (card.name || did) + "」。", "ok");
    _pfMsg("", false);
    showToast("已发布到广场");
    _pfLoadDrafts(true);
}

/** 覆盖发布的**二次确认**（2026-10-01 用户要求"更新我已发布的卡"要有入口）。
 *
 * 防误覆盖的两道门：① 主发布按钮永远 `replace:false`（服务端拒绝同 id）；
 * ② 这一层要说清楚"会替换广场上这一张的**已发布版本**、旧内容不可恢复"，用户点确认才真覆盖。
 * 平台页与 App 共用本模块 ⇒ 两侧都有这个入口。 */
function _pfReplaceConfirm(id, why) {
    const wrap = _pf("pf-modal");
    if (!wrap) return;
    wrap.textContent = "";
    wrap.classList.add("show");
    const box = _pfEl2("div", "pf-modal-box");
    box.appendChild(_pfEl2("div", "pf-modal-title", "覆盖广场上的已发布版本？"));
    const note = _pfEl2("div", "pf-modal-note");
    for (const t of [
        "广场上已经有一张同 id（" + id + "）的卡。",
        "继续会用当前草稿替换它的「已发布版本」：广场上看到的内容立刻变成这一份，"
        + "旧版本不留副本、不可恢复。",
        why ? ("服务端原话：" + why) : "",
    ]) {
        if (t) note.appendChild(_pfEl2("div", "pf-note-item", "· " + t));
    }
    const acts = _pfEl2("div", "pf-modal-acts");
    const cancel = _pfEl2("button", "pz-btn", "取消");
    cancel.type = "button";
    cancel.id = "pf-modal-replace-cancel";
    cancel.onclick = () => _pfModalClose();
    const ok = _pfEl2("button", "pz-btn primary", "确认覆盖发布");
    ok.type = "button";
    ok.id = "pf-modal-replace-ok";
    ok.onclick = () => { _pfModalClose(); _pfPublishReplace(id); };
    acts.append(cancel, ok);
    box.append(note, acts);
    wrap.appendChild(box);
}

async function _pfPublishReplace(id) {
    if (_pfS.busy) return;
    _pfS.busy = true;
    _pfSyncBtns();
    const r = await _pfPost("/plaza/api/publish",
                            {id: id, replace: true, official: _pfOfficialWanted()});
    _pfS.busy = false;
    _pfSyncBtns();
    if (!r.ok) {
        _pfMsg(_pfWhy(r, "覆盖发布失败，请稍后重试。"), false);
        return;
    }
    _pfMsg("", false);
    _pfS.draftStatus = "published";
    _pfSyncBtns();          // 覆盖发布后同样是"已发布"态（「下架」应可用）
    _pfStatus("已覆盖发布：广场上这一张已更新为当前版本。", "ok");
    showToast("已覆盖发布（广场上的版本已更新）");
    _pfLoadDrafts(true);
}

async function _pfUnpublish(id, btn) {
    const did = String(id || _pfS.id || "").trim();
    if (!did) return;
    if (!confirm("下架「" + did + "」？\n广场上不再显示这张卡（作者可再次发布）。")) return;
    if (btn) { btn.disabled = true; btn.textContent = "下架中…"; }
    const r = await _pfPost("/plaza/api/unpublish", {id: did});
    if (btn) { btn.disabled = false; btn.textContent = "下架"; }
    if (!r.ok) {
        if (r.status === 401) { _pfGotoLogin(); return; }
        _pfMsg(_pfWhy(r, r.status === 403 ? "只有作者本人或管理员可以下架这张卡。" : "下架失败，请稍后重试。"), false);
        return;
    }
    _pfS.draftStatus = "draft";
    _pfSyncBtns();          // 已下架 ⇒ 不再是 published ⇒「下架」按钮应收起
    showToast("已从广场下架");
    _pfStatus("已下架：广场上不再显示这张卡。", "warn");
    _pfLoadDrafts(true);
}

async function _pfDelete(it) {
    if (!confirm("删除草稿「" + (it.name || it.id) + "」？\n删除后无法恢复，卡体和审核结论一起消失。")) return;
    const r = await _pfPost("/plaza/api/draft/delete", {id: it.id});
    if (!r.ok) {
        if (r.status === 401) { _pfGotoLogin(); return; }
        _pfMsg(_pfWhy(r, "删除失败，请稍后重试。"), false);
        return;
    }
    if (_pfS.id === it.id) {
        _pfS.verdict = "";
        _pfS.dirty = true;
        _pfS.draftStatus = "";
        _pfStatus("当前编辑的草稿已被删除，表单里的内容还在，可以再存一次。", "warn");
        _pfSyncBtns();
    }
    showToast("草稿已删除");
    _pfLoadDrafts(true);
}
