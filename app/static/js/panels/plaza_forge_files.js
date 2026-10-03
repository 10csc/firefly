// 角色卡制作（共创平台 M2/P4-7c）——**文本文件编辑层**：固定三件 + 任意多份知识库 + 开场白条目
//
// 契约：docs/设计/角色卡共创平台/06_制卡自由度与图片规格.md §3.1/§3.2
//   · 固定三件（语义不变）：core.md / identity.md / sms_samples.md
//   · 知识库：knowledge/<名字>.md，**1..6 份**，可增、可改名、可删（名字只允许中文/字母/数字/-/_）
//   · 开场白：opening = {narrations:[…], first_messages:[…]}，条目可自由增删
//   · 体积：**不再限单文件大小、不再限知识库份数**（2026-10-01 用户要求松绑），
//     只由外壳的「单卡文字总量」一处把关；文本只在**保存那一刻**随 payload 全量提交（不每次改动都发）
//
// 约束（与本域其它模块同源）：
//  1. 前缀 `_pff` 防撞（bundle 是单作用域，重名会静默覆盖）；
//  2. 能用 textContent 就不用 innerHTML：路径/正文都是用户数据；
//  3. 不用属性选择器找 JS 属性（docs/错误总结.md #19）：本模块的 DOM 引用走闭包变量；
//  4. 不引外部依赖：只用 document/createElement。
import { showToast } from "../util.js";

// 2026-10-01 松绑：本模块**不再持有**任何文字体积/份数常量 ——
// 体积口径只有一处（外壳 `plaza_forge.js` 的 `_PF_TEXT_TOTAL_MAX`），不要再在这里写数字。
const _PFF_FIXED = [["core.md", "核心设定", "角色是谁、经历过什么、在意什么（写清楚，AI 靠这段演她）"],
                    ["identity.md", "口吻与人际", "身份、说话方式、与你的关系"],
                    ["sms_samples.md", "短信样例", "几条示范对话，帮 AI 抓住语气"]];

const _pffS = {
    mount: null,                          // 挂载点（由外壳 _pfBuildForm 传入）
    rows: [],                             // 知识库行：[{name, el, nameEl, taEl}]
    first: [],                            // first_messages 行：[{el, input}]
    narr: [],                             // narrations 行：[{el, ta}]
    els: {},                              // 固定三件的 textarea
    seq: 0,                               // 行代际（重渲染时作废旧引用）
};

// ═══ 小工具 ═══════════════════════════════════════════
function _pffEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
}

function _pffBtn(cls, text, onClick) {
    const b = _pffEl("button", cls, text);
    b.type = "button";
    b.onclick = onClick;
    return b;
}

function _pffCount(el) {
    // 与外壳的计数条同款：只报数，不拦（拦在 _pffValidate / _pfValidate 的"文字总量"）
    // 2026-10-01 松绑：**不再有单文件上限**，只显示这一份的真实体积（UTF-8 字节）
    const box = _pffEl("div", "pf-count");
    const t = _pffEl("span", "", "");
    box.appendChild(t);
    const upd = () => { t.textContent = _pfFmtSize(_pfTextBytes(el.value)); };
    el.addEventListener("input", upd);
    upd();
    return box;
}

/** 知识库路径**分段**校验（2026-10-01 用户实测被误拒：一层分组 `knowledge/<域>/<名>.md` 被按
 *  **扁平名**规则校验，`/` 不在允许集 ⇒ 合法卡也被拦）。
 *
 *  口径与服务端逐条对齐（`card_format._is_free_text`）：
 *    · 分组段：非空、不是 `.`/`..`、不以 `.` 开头、**≤40 字**、不含 `\`、不含控制字符；
 *    · 文件段：`^[^\x00-\x1f/\\]{1,40}\.(md|txt|json)$`，词干不以空白或 `.` 开头；
 *    · 允许的**可见字符**：中文/字母/数字/`-`/`_`/空格/`.`/`·`/`（）`……服务端只禁控制字符与
 *      斜杠/反斜杠 ⇒ 前端**不额外收窄**（比服务端更严 = 又一次"合法卡被拦"）。
 *  报错**指名道姓**：哪一段、哪个字符、当前多长。 */
const _PFF_SEG_MAX = 40;                    // ← card_format：dir `len(group) > 40` / `_TEXT_NAME_RE{1,40}`

function _pffSeg(seg, label) {
    const s = String(seg == null ? "" : seg).trim();
    if (!s) return {ok: false, why: label + "不能为空。"};
    if (s.length > _PFF_SEG_MAX) {
        return {ok: false, why: label + "太长：当前 " + s.length + " 字，上限 " + _PFF_SEG_MAX
                   + " 字（请缩短）。"};
    }
    const ctl = s.split("").find((ch) => ch.charCodeAt(0) < 32 || ch.charCodeAt(0) === 127);
    if (ctl) {
        return {ok: false, why: label + "里有控制字符（不可见字符，编码 "
                   + ctl.charCodeAt(0) + "），请删掉后重试。"};
    }
    if (s.indexOf("..") >= 0) return {ok: false, why: label + "不能包含「..」（防路径穿越）。"};
    if (s.indexOf("/") >= 0) {
        return {ok: false, why: label + "不能包含「/」：要分组请填在左边的「域」栏（一层分组）。"};
    }
    if (s.indexOf("\\") >= 0) return {ok: false, why: label + "不能包含「\\」（防路径穿越）。"};
    if (s.charAt(0) === ".") return {ok: false, why: label + "不能以「.」开头（隐藏文件）。"};
    return {ok: true, seg: s};
}

/** 知识库名字合法性：允许中文/字母/数字/-/_/空格/./·/（）等可见字符，≤40 字（与后端同口径）。 */
function _pffNameOk(name) {
    const s = String(name || "").trim().replace(/\.md$/i, "");
    const r = _pffSeg(s, "知识库文件名");
    return r.ok ? r.seg : "";
}

/** 知识库**一层分组**（契约 06 §3.1 变更 2026-10-01 18:20）：域可空=扁平，两种都要能编。
 *  建议值就是 App 编辑器写的五域；也允许用户自定名（空格/括号/·都行）。 */
const _PFF_DIRS = ["world", "factions", "story", "character", "dialogues"];

function _pffDirOk(dir) {
    const s = String(dir || "").trim().replace(/^\/+|\/+$/g, "");
    if (!s) return "";                                 // 空 = 扁平 knowledge/<名字>.md
    const r = _pffSeg(s, "知识库分组名");
    return r.ok ? r.seg : "";
}

/** 分段校验 + 人话原因（校验与报错**同一个来源**，避免"两处规则漂移"）。返回 {path} 或 {why}。 */
function _pffKbCheck(dir, name) {
    const d = String(dir || "").trim().replace(/^\/+|\/+$/g, "");
    if (d) {
        const r = _pffSeg(d, "知识库分组名");
        if (!r.ok) return {why: r.why};
    }
    const n0 = String(name || "").trim().replace(/\.md$/i, "");
    const r2 = _pffSeg(n0, "知识库文件名");
    if (!r2.ok) return {why: r2.why};
    return {path: "knowledge/" + (d ? (d + "/") : "") + r2.seg + ".md"};
}

/** 域 + 名字 → 契约里的 path（域为空则扁平）。校验不通过返回 ""（原因由 `_pffKbCheck` 给）。 */
function _pffKbPath(dir, name) {
    const chk = _pffKbCheck(dir, name);
    return chk.path || "";
}

// ═══ 知识库行 ═════════════════════════════════════════
function _pffAddRow(dir, name, text, focus) {
    if (!_pffS.mount) return null;
    // 2026-10-01 松绑：**不再限制知识库份数**（用户原话「为什么知识库还限制份数？」）。
    // 体积由"单卡文字总量"一处把关（_pfValidate）；份数只受**整卡文件数 ≤500** 的安全天花板约束
    // （2026-10-01：由 24 放宽 —— 用户被"33 个文件超 24"直接挡住）。
    if (_pffS.rows.length >= _PF_FILES_MAX) {
        showToast("卡内文件过多：当前 " + _pffS.rows.length + " 个，上限 " + _PF_FILES_MAX
                  + " 个（文本总量另有 8MB 上限）");
        return null;
    }
    const box = _pffS.mount.querySelector(".pff-kb-list");
    const row = _pffEl("div", "pff-kb");
    const head = _pffEl("div", "pff-kb-head");
    const dirIn = document.createElement("input");
    dirIn.type = "text";
    dirIn.className = "pff-kdir";
    dirIn.placeholder = "域（可空）";
    dirIn.maxLength = 24;
    dirIn.value = String(dir || "");
    dirIn.setAttribute("list", "pff-dirs");            // 五域建议（也可自定名）
    const slash = _pffEl("span", "pff-kext", "/");
    const nameIn = document.createElement("input");
    nameIn.type = "text";
    nameIn.className = "pff-kname";
    nameIn.placeholder = "文件名（如：世界观）";
    nameIn.maxLength = 24;
    nameIn.value = String(name || "");
    const tail = _pffEl("span", "pff-kext", ".md");
    const del = _pffBtn("pf-btn small danger", "删除", () => {
        const i = _pffS.rows.findIndex(r => r.el === row);
        if (i >= 0) _pffS.rows.splice(i, 1);
        row.remove();
        _pffSyncCount();
        _pffDirty();
    });
    const tip = _pffEl("div", "pf-idtip", "");
    head.append(dirIn, slash, nameIn, tail, del);
    const ta = document.createElement("textarea");
    ta.className = "pff-ktext";
    ta.rows = 6;
    ta.placeholder = "这个文件的内容（写世界观/设定/资料，AI 检索时能看到）";
    ta.value = String(text || "");
    const rec = {el: row, dirEl: dirIn, nameEl: nameIn, taEl: ta, tipEl: tip};
    _pffS.rows.push(rec);
    const sync = () => {
        // 每行下方实时提示 = 与预检**同一个来源**（`_pffKbCheck`），所以"看着是绿的就能存"
        const chk = _pffKbCheck(dirIn.value, nameIn.value);
        const touched = !!(dirIn.value.trim() || nameIn.value.trim());
        tip.className = "pf-idtip " + (chk.why ? (touched ? "bad" : "") : "ok");
        tip.textContent = chk.why || chk.path || "";
        _pffDirty();
    };
    dirIn.addEventListener("input", sync);
    nameIn.addEventListener("input", sync);
    ta.addEventListener("input", _pffDirty);
    row.append(head, tip, ta, _pffCount(ta));
    box.appendChild(row);
    sync();
    _pffSyncCount();
    if (focus) { try { nameIn.focus(); } catch (e) {} }
    return rec;
}

function _pffSyncCount() {
    const n = _pffS.els.kbcount;
    if (n) n.textContent = "已加 " + _pffS.rows.length + " 份";
}

// ═══ 开场白条目 ═══════════════════════════════════════
function _pffAddFirst(text, focus) {
    const box = _pffS.mount && _pffS.mount.querySelector(".pff-first-list");
    if (!box) return null;
    const row = _pffEl("div", "pff-item");
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "pff-first";
    inp.placeholder = "角色对你说的一句开场白";
    // ⚠ 第一行保留 `#pf-opening`：老脚本/老外部调用按这个 id 找"开场白"（兼容句柄）
    if (!_pffS.first.length) inp.id = "pf-opening";
    inp.value = String(text || "");
    inp.addEventListener("input", _pffDirty);
    const del = _pffBtn("pf-btn small", "×", () => {
        const i = _pffS.first.findIndex(r => r.el === row);
        if (i >= 0) _pffS.first.splice(i, 1);
        row.remove();
        _pffSyncOpenIds();
        _pffDirty();
    });
    del.setAttribute("aria-label", "删除这条开场白");
    row.append(inp, del);
    box.appendChild(row);
    _pffS.first.push({el: row, input: inp});
    _pffSyncOpenIds();
    if (focus) { try { inp.focus(); } catch (e) {} }
    return row;
}

function _pffAddNarr(text, focus) {
    const box = _pffS.mount && _pffS.mount.querySelector(".pff-narr-list");
    if (!box) return null;
    const row = _pffEl("div", "pff-item");
    const ta = document.createElement("textarea");
    ta.className = "pff-narr";
    ta.rows = 2;
    ta.placeholder = "一段旁白（演出用）";
    ta.value = String(text || "");
    ta.addEventListener("input", _pffDirty);
    const del = _pffBtn("pf-btn small", "×", () => {
        const i = _pffS.narr.findIndex(r => r.el === row);
        if (i >= 0) _pffS.narr.splice(i, 1);
        row.remove();
        _pffDirty();
    });
    del.setAttribute("aria-label", "删除这段旁白");
    row.append(ta, del);
    box.appendChild(row);
    _pffS.narr.push({el: row, ta: ta});
    if (focus) { try { ta.focus(); } catch (e) {} }
    return row;
}

/** 首行删掉后，把 `#pf-opening` 让给新的第一行（保持"总有一个开场白句柄"）。 */
function _pffSyncOpenIds() {
    _pffS.first.forEach((r, i) => {
        if (i === 0) r.input.id = "pf-opening";
        else r.input.removeAttribute("id");
    });
}

// ═══ 挂载（外壳建骨架时调一次）════════════════════════
function _pffMount(parent) {
    const sec = _pffEl("div", "pff-sec");
    const head = _pffEl("div", "pf-sec", "③ 文本文件（自由增删改）");
    sec.appendChild(head);

    // 固定三件
    sec.appendChild(_pffEl("div", "pff-note", "下面三件是每张卡都有的：核心设定、口吻、短信样例（体积计入单卡文字总量）。"));
    for (const [path, label, tip] of _PFF_FIXED) {
        const box = _pffEl("div", "pff-fixed");
        box.appendChild(_pffEl("div", "pf-label", label + "（" + path + "）"));
        const ta = document.createElement("textarea");
        ta.id = "pf-" + path.replace(/\.md$/, "").replace(/\W/g, "_");   // pf-core / pf-identity / pf-sms_samples
        ta.rows = path === "core.md" ? 8 : 5;
        ta.placeholder = tip;
        ta.addEventListener("input", _pffDirty);
        box.append(ta, _pffCount(ta));
        _pffS.els[path] = ta;
        sec.appendChild(box);
    }

    // 知识库
    const kbTitle = _pffEl("div", "pff-sub-title", "");
    kbTitle.append(_pffEl("span", "", "知识库文件（可平铺，也可放一层「域/」下）"),
                   _pffEl("span", "pff-kbcount", "已加 0 份"));
    _pffS.els.kbcount = kbTitle.querySelector(".pff-kbcount");
    const kbList = _pffEl("div", "pff-kb-list");
    // 五域建议值（datalist：可点选，也可自己敲；契约 06 §3.1 允许用户自定域）
    const dl = document.createElement("datalist");
    dl.id = "pff-dirs";
    for (const d of _PFF_DIRS) {
        const o = document.createElement("option");
        o.value = d;
        dl.appendChild(o);
    }
    const addKb = _pffBtn("pz-btn small primary", "+ 添加知识库文件",
                          () => _pffAddRow("", "", "", true));
    addKb.id = "pf-kb-add";
    sec.append(kbTitle, dl, kbList, addKb);

    // 开场白
    sec.appendChild(_pffEl("div", "pf-sec", "④ 开场白（条目可自由增删）"));
    sec.appendChild(_pffEl("div", "pff-note", "第一句对你说的话，以及正式对话前的旁白；都可以留空。"));
    const firstTitle = _pffEl("div", "pff-sub-title", "开场白句子");
    const firstList = _pffEl("div", "pff-first-list");
    const addFirst = _pffBtn("pz-btn small", "+ 加一句开场白", () => _pffAddFirst("", true));
    addFirst.id = "pf-open-add";
    const narrTitle = _pffEl("div", "pff-sub-title", "旁白");
    const narrList = _pffEl("div", "pff-narr-list");
    const addNarr = _pffBtn("pz-btn small", "+ 加一段旁白", () => _pffAddNarr("", true));
    addNarr.id = "pf-narr-add";
    sec.append(firstTitle, firstList, addFirst, narrTitle, narrList, addNarr);

    parent.appendChild(sec);
    _pffS.mount = sec;
    return sec;
}

// ═══ 读写 ════════════════════════════════════════════
function _pffDirty() {
    try { _pfMarkDirty(); } catch (e) {}
}

/** 收集 payload 的 `files` + `opening`（只在保存/审核那一刻调）。 */
function _pffCollect() {
    const files = [];
    for (const [path] of _PFF_FIXED) {
        const ta = _pffS.els[path];
        const text = ta ? String(ta.value || "") : "";
        if (text.trim()) files.push({path: path, text: text});
    }
    for (const r of _pffS.rows) {
        const path = _pffKbPath(r.dirEl.value, r.nameEl.value);   // 扁平或 knowledge/<域>/<名>.md
        const text = String(r.taEl.value || "");
        if (!path || !text.trim()) continue;
        files.push({path: path, text: text});
    }
    const first = _pffS.first.map(r => String(r.input.value || "").trim()).filter(Boolean);
    const narr = _pffS.narr.map(r => String(r.ta.value || "").trim()).filter(Boolean);
    return {files: files, opening: {narrations: narr, first_messages: first}};
}

/** 回填：优先新形状 `files`/`opening`；老草稿（core/identity/sms_samples + 字符串 opening）兜底。 */
function _pffLoad(card) {
    const c = card || {};
    _pffS.seq++;
    const texts = {};
    const knowledge = [];
    const list = Array.isArray(c.files) ? c.files : [];
    for (const f of list) {
        const p = String((f && f.path) || "");
        const t = String((f && f.text) || "");
        if (!p) continue;
        if (p.indexOf("knowledge/") === 0) {
            // 两种都吃：knowledge/<名>.md（扁平）与 knowledge/<域>/<名>.md（一层分组）
            const rest = p.slice(10).replace(/\.md$/i, "");
            const parts = rest.split("/").filter(Boolean);
            if (parts.length >= 2) knowledge.push([parts[parts.length - 2], parts[parts.length - 1], t]);
            else if (parts.length === 1) knowledge.push(["", parts[0], t]);
        } else {
            texts[p] = t;
        }
    }
    // 兼容：老字段等价映射（契约 §3.2「原字段继续接受」的反向）
    if (!list.length) {
        if (typeof c.core === "string") texts["core.md"] = c.core;
        if (typeof c.identity === "string") texts["identity.md"] = c.identity;
        if (typeof c.sms_samples === "string") texts["sms_samples.md"] = c.sms_samples;
    }
    for (const [path] of _PFF_FIXED) {
        const ta = _pffS.els[path];
        if (ta) ta.value = texts[path] || "";
    }
    // 知识库：先清后建（最多 6）
    _pffS.rows.forEach(r => r.el.remove());
    _pffS.rows = [];
    for (const [dir, name, text] of knowledge) _pffAddRow(dir, name, text, false);
    _pffSyncCount();

    // 开场白：新形状对象 / 老形状字符串
    _pffS.first.forEach(r => r.el.remove());
    _pffS.first = [];
    _pffS.narr.forEach(r => r.el.remove());
    _pffS.narr = [];
    const op = c.opening;
    if (op && typeof op === "object") {
        for (const s of (Array.isArray(op.first_messages) ? op.first_messages : [])) _pffAddFirst(s, false);
        for (const s of (Array.isArray(op.narrations) ? op.narrations : [])) _pffAddNarr(s, false);
    } else if (typeof op === "string" && op.trim()) {
        _pffAddFirst(op, false);                 // 老草稿：opening 是一个字符串
    }
    _pffSyncOpenIds();
}

function _pffNew() {
    for (const [path] of _PFF_FIXED) {
        const ta = _pffS.els[path];
        if (ta) ta.value = "";
    }
    _pffS.rows.forEach(r => r.el.remove());
    _pffS.rows = [];
    _pffS.first.forEach(r => r.el.remove());
    _pffS.first = [];
    _pffS.narr.forEach(r => r.el.remove());
    _pffS.narr = [];
    _pffAddFirst("", false);                     // 默认给一行，用户不用先点"添加"
    _pffSyncCount();
    _pffSyncOpenIds();
}

/** 本地预检：返回人话错误或 null。
 *  2026-10-01 松绑后这里**只管结构**（域/文件名/路径重复）；文本**体积**只由 `_pfValidate` 的
 *  "单卡文字总量"一处把关（不再有单文件 ≤64KB、不再有知识库 ≤6 份）。 */
function _pffValidate() {
    const seen = new Set();
    for (const r of _pffS.rows) {
        // 分段校验 + **指名道姓**的报错（哪一段、哪个字符、多长）——不再一句笼统话
        const chk = _pffKbCheck(r.dirEl.value, r.nameEl.value);
        if (chk.why) return chk.why;
        if (seen.has(chk.path)) return "知识库路径重复：" + chk.path;
        seen.add(chk.path);
    }
    return null;
}
