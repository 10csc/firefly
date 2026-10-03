// 角色卡制作（共创平台 M2/P4-7c）——**表单数据层**：元数据 + 标签 + 读写/校验/回填
//
// 2026-10-01（P4-7c）：契约 06 §3.2 的 payload 从"固定几栏"改成
//   {…元数据…, opening:{narrations,first_messages}, files:[{path,text}], images:{avatar,thumb,display,stickers[]}}
// 于是本文件只留**元数据与标签**的读写校验；文本文件在 plaza_forge_files.js（_pff*），
// 图片在 plaza_forge_images.js（_pfi*）。三者共用外壳的 `_pfS`/`_pfEl`（bundle 单作用域）。
// 新加的名字**必须带 `_pf` 前缀**（重名会静默覆盖）。
import { showToast } from "../util.js";

// ═══ 计数条（只报数，不拦；拦在 _pfValidate）═══════════
function _pfCount(box, n, max) {
    if (!box) return;
    box.txt.textContent = n + " / " + max;
    box.el.classList.toggle("over", n > max);
}

function _pfCountAll() {
    const pairs = [["desc", _pfEl.desc, _pfEl.descCount],
                   ["tagline", _pfEl.tagline, _pfEl.taglineCount]];
    for (const [k, el, box] of pairs) if (el) _pfCount(box, el.value.length, _PF_LIMITS[k]);
}

// ═══ 标签（≤5 个、每个 ≤12 字；与后端 MAX_TAGS/MAX_TAG_LEN 同口径）═══
function _pfTagsAdd(raw, toast) {
    const L = _PF_LIMITS;
    let s = String(raw || "").trim().replace(/^#+/, "").replace(/[,，]/g, "");
    if (!s) return;
    if (s.length > L.tag) {
        if (toast) showToast("标签最长 " + L.tag + " 字");
        s = s.slice(0, L.tag);
    }
    if (_pfS.tags.includes(s)) {
        if (_pfEl.tagInput) _pfEl.tagInput.value = "";
        return;
    }
    if (_pfS.tags.length >= L.tags) {
        // 与后端 `MAX_TAGS` 同口径（2026-10-02：5 → 20）；提示按 Lead 要求带"当前"数量
        if (toast) showToast("标签最多 " + L.tags + " 个（当前 " + (_pfS.tags.length + 1) + " 个）");
        return;
    }
    _pfS.tags.push(s);
    if (_pfEl.tagInput) _pfEl.tagInput.value = "";
    _pfRenderTags();
    _pfMarkDirty();
}

function _pfRenderTags() {
    const box = _pfEl.tagList;
    if (!box) return;
    box.textContent = "";
    if (!_pfS.tags.length) {
        box.appendChild(_pfEl2("span", "pf-tagempty", "还没有标签"));
        return;
    }
    _pfS.tags.forEach((t, i) => {
        const chip = _pfEl2("span", "pf-tagchip");
        chip.appendChild(_pfEl2("span", "", "#" + t));
        const x = _pfEl2("button", "pf-tagx", "×");
        x.type = "button";
        x.setAttribute("aria-label", "删除标签 " + t);
        x.onclick = () => {
            _pfS.tags.splice(i, 1);
            _pfRenderTags();
            _pfMarkDirty();
        };
        chip.appendChild(x);
        box.appendChild(chip);
    });
}

// ═══ 读：表单 → 契约 payload 的 card ═══════════════════
function _pfRead() {
    const g = (id) => { const e = _pf(id); return e ? String(e.value || "").trim() : ""; };
    const card = {
        id: g("pf-id"),
        name: g("pf-name"),
        char_name: g("pf-char"),
        user_name: g("pf-user"),
        desc: g("pf-desc"),
        tagline: g("pf-tagline"),
        category: g("pf-cat") || _PF_CATS[0],
        tags: _pfS.tags.slice(0, _PF_LIMITS.tags),
        presentation: g("pf-presentation") || "sticker",
    };
    const ff = _pffCollect();                 // {files:[{path,text}], opening:{narrations,first_messages}}
    card.files = ff.files;                    // ★契约 §3.2：自由文本文件
    card.opening = ff.opening;                // ★契约 §3.2：开场白改为对象
    card.images = _pfiCollect();              // ★契约 §3.2：图片一律 data URL（客户端已压好）
    return card;
}

/** 整卡体积预检用的字节合计：文本按 **UTF-8 真实字节**（中文 1 字 3 字节，与后端同口径），
 *  图片按 zip 里的原始字节（不是 base64 文本长度）。 */
function _pfPayloadBytes(card) {
    let n = _pfTextTotal(card);
    const im = card.images || {};
    for (const slot of ["avatar", "thumb", "display"]) n += _pfiDataBytes(im[slot]);
    for (const s of (im.stickers || [])) n += _pfiDataBytes((s && s.data) || s);   // 契约 §3.2：{data,label}
    return n;
}

/** 本地预检（顺序与后端一致：必填 → 长度 → id → 标签 → 文本文件 → 图片 → 整卡体积）。
 *  返回 null（通过）或人话错误。**先拦下来**，不让用户提交后才发现。 */
function _pfValidate(silent) {
    const L = _PF_LIMITS;
    const v = (id) => { const e = _pf(id); return e ? String(e.value || "").trim() : ""; };
    const bad = (id, why) => {
        if (!silent) {
            const e = _pf(id);
            if (e && e.focus) { try { e.focus(); } catch (err) {} }
            _pfMsg(why, true);
        }
        return why;
    };
    if (!v("pf-name")) return bad("pf-name", "卡名不能为空。");
    if (v("pf-name").length > L.name) return bad("pf-name", "卡名超过 " + L.name + " 字上限。");
    if (!v("pf-char")) return bad("pf-char", "角色名不能为空。");
    if (v("pf-char").length > L.char_name) return bad("pf-char", "角色名超过 " + L.char_name + " 字上限。");
    if (!v("pf-user")) return bad("pf-user", "「角色怎么称呼你」不能为空。");
    if (v("pf-user").length > L.user_name) return bad("pf-user", "称呼超过 " + L.user_name + " 字上限。");
    if (!v("pf-desc")) return bad("pf-desc", "简介不能为空。");
    if (v("pf-desc").length > L.desc) return bad("pf-desc", "简介超过 " + L.desc + " 字上限。");
    if (v("pf-tagline").length > L.tagline) return bad("pf-tagline", "副标题超过 " + L.tagline + " 字上限。");
    const cid = v("pf-id");
    if (!_PF_ID_RE.test(cid)) return bad("pf-id", "卡片 id 只能用 1-32 位小写字母/数字/下划线/短横线。");
    if (!_PF_CATS.includes(v("pf-cat"))) return bad("pf-cat", "分类只能是：" + _PF_CATS.join("/") + "。");
    if (_pfS.tags.length > L.tags) {
        return bad("pf-tag", "标签最多 " + L.tags + " 个（当前 " + _pfS.tags.length + " 个）。");
    }
    for (const t of _pfS.tags) {
        if (t.length > L.tag) return bad("pf-tag", "标签「" + t + "」超过 " + L.tag + " 字上限。");
    }
    const ffWhy = (typeof _pffValidate === "function") ? _pffValidate() : null;
    if (ffWhy) return bad(null, ffWhy);
    const imWhy = (typeof _pfiValidate === "function") ? _pfiValidate() : null;
    if (imWhy) return bad(null, imWhy);
    // ★ 文字总量（2026-10-01 松绑后**只有这一道文字闸**：不再卡单文件大小、不再卡知识库份数）。
    //   上限**随角色走**：普通账号 8MB（`MAX_TEXT_TOTAL_BYTES`）、**管理员不限**（后端同口径）；
    //   按 UTF-8 字节算，人话给出"已用 / 上限"。
    const card0 = _pfRead();
    const txt = _pfTextTotal(card0);
    const txtMax = _pfTextTotalMax();
    if (txt > txtMax) {
        return bad(null, "卡的文字总量太大了：已用 " + _pfFmtSize(txt) + "，上限 "
                   + _pfFmtSize(txtMax) + "。请精简知识库或正文（可以拆成多张卡）。");
    }
    const approx = _pfPayloadBytes(card0);
    const dataMax = _pfDataMax();
    if (approx > dataMax) {
        return bad(null, "整张卡太大了（约 " + _pfFmtSize(approx) + "，上限 "
                   + _pfFmtSize(dataMax) + "）。请缩短正文或换小一点的图片。");
    }
    return null;
}

// ═══ 回填：草稿/卡 → 表单 ═════════════════════════════
function _pfSetForm(card, meta) {
    const c = card || {};
    _pfSeq++;
    const L = _PF_LIMITS;
    const set = (id, val, max) => {
        const e = _pf(id);
        if (!e) return;
        e.value = String(val === undefined || val === null ? "" : val).slice(0, max || 100000);
    };
    set("pf-name", c.name, L.name);
    set("pf-char", c.char_name, L.char_name);
    set("pf-user", c.user_name, L.user_name);
    set("pf-desc", c.desc, L.desc);
    set("pf-tagline", c.tagline, L.tagline);
    set("pf-display", (meta && meta.display) || "", L.display);
    if (_pf("pf-cat")) _pf("pf-cat").value = _PF_CATS.includes(c.category) ? c.category : _PF_CATS[0];
    if (_pf("pf-presentation")) {
        const pv = ["sticker", "narration", "none"].includes(c.presentation) ? c.presentation : "sticker";
        _pf("pf-presentation").value = pv;
    }
    // id：草稿回填算"用户已知情"，之后改卡名不再自动改 id（避免把已发布的 id 改掉）
    set("pf-id", c.id, 32);
    _pfEl.setIdTouched(!!c.id);
    _pfS.id = String(c.id || "");
    _pfS.draftStatus = String((meta && meta.status) || "draft");
    _pfS.tags = (Array.isArray(c.tags) ? c.tags : []).map(x => String(x).slice(0, L.tag))
        .slice(0, L.tags);
    _pfRenderTags();
    _pffLoad(c);          // 文本文件（新形状 files / 老字段 core·identity·sms_samples）
    _pfiLoad(c);          // 图片（新形状 images / 老字段 cover·avatar）
    _pfCountAll();
    _pfMarkDirty();
}

function _pfNew() {
    // ★「新建」= 与任何原卡无关：必须退出改编态（否则会把新卡当作原卡的 patch 提交）
    try { if (window.plazaAdaptReset) window.plazaAdaptReset(); } catch (e) {}
    _pfSetForm({id: "", category: _PF_CATS[0], presentation: "sticker"}, {status: "draft"});
    _pfEl.setIdTouched(false);
    _pfS.draftStatus = "";
    _pfS.verdict = "";
    _pfS.dirty = true;
    _pffNew();            // 清空文本 + 给一行默认开场白
    _pfiNew();            // 清空全部图片槽位
    _pfMsg("", false);
    _pfStatus("新卡：填完可以直接存草稿，或提交审核后发布。", "");
    try { _pf("pf-name").focus(); } catch (e) {}
}
