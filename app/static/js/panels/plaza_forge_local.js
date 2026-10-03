// ═══ 已有角色卡 → 发布到广场（2026-10-01，用户点名要的功能）═══════════════
//
// 用户原话：「用户自己的角色卡直接发布做了吗？没有的话，先把这一个功能做一下。」
//
// **不新造发布协议**：本模块只做"读已有卡 → 组装成制卡页认得的 card 对象 → 交给制卡页填表"，
// 之后走的是**已经上线、已被测试覆盖**的三步 API（`POST /plaza/api/draft` → `review` → `publish`）。
//
// 读数据全部用**已有端点**（不新增服务端接口）：
//   · `GET /modes`                 包元数据 + 形象资产投影（avatar/cover，含槽位回落链）
//   · `GET /pack-files?mode=`      core.md / identity.md / sms_samples.md
//   · `GET /pack-knowledge?mode=`  + `/pack-knowledge/file`  知识库（扁平 + 一层分组）
//   · `GET /pack-file?mode=&path=opening.json`               开场白
//   · `GET /stickers?enabled=1`    本包专属表情包（`pack === mode`，label 一起带上）
// 图片一律转 data URL（契约 06 §3.2）；**thumb 不自己造**：交给制卡页既有的
// `_pfiEnsureLegacyThumb()`（task-13 那条"老草稿自动补 thumb"的路径，≤320px/≤12KB）。
//
// 两处入口：
//   ① App「角色卡管理」包详情页 → 动态按钮「发布到广场」（`packs.js`，绑定 `_packViewMode`）
//   ② 制卡页头部「载入已有卡」→ 选择器（App 与平台页共用；平台的「我的卡」就是制卡页）
// 依赖（同 bundle 作用域，顺序在前）：util / plaza_forge / plaza_forge_form / plaza_forge_review / imgzip
import { showToast } from "../util.js";

const _PFL_STK_MAX = 8;                // 契约 06 §3.1：表情包最多 8 张（**图片限制保留**）

// ── 改编的会话状态（2026-10-01：改编改为**只上传改动**，用户明确要求省流量）──
//   `base`：改编基线（原卡 id/名字 + 载入那一刻各文件、开场白、图片的**内容指纹**）。
//   提交时只把"与基线不同"的文件/图片放进 patch —— **未改的图片不下载也不上传**，
//   由服务端 `POST /plaza/api/derive` 从原卡复制过来（省流量的关键）。
const _pflS = {base: null, last: null};
/** 内容指纹：长度 + FNV-1a 32 位。只用来判"变没变"，不做安全用途。 */
function _pflHash(s) {
    const t = String(s == null ? "" : s);
    let h = 0x811c9dc5;
    for (let i = 0; i < t.length; i++) {
        h ^= t.charCodeAt(i);
        h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return t.length + ":" + h.toString(16);
}
/** 载入时刻的基线快照（文件/开场白/图片/表情包 → 指纹）。 */
function _pflSnap(card) {
    const c = card || {};
    const files = {};
    for (const f of (Array.isArray(c.files) ? c.files : [])) {
        const p = String((f && f.path) || "");
        if (p) files[p] = _pflHash((f || {}).text);
    }
    const im = c.images || {};
    const images = {};
    for (const slot of ["avatar", "thumb", "display"]) {
        if (im[slot]) images[slot] = _pflHash(im[slot]);
    }
    const stk = (Array.isArray(im.stickers) ? im.stickers : [])
        .map((s) => [_pflHash((s && s.data) || s), String((s && s.label) || "")]);
    return {files: files, images: images, stickers: _pflHash(JSON.stringify(stk)),
            opening: _pflHash(JSON.stringify(c.opening || {}))};
}
/** 与基线比 → patch（新增/修改/删除三份清单 + 只带"换了"的图片）。 */
function _pflPatch(card) {
    const base = (_pflS.base && _pflS.base.snap) || null;
    const p = {files: {add: [], update: [], delete: []}, images: {}};
    if (!base) return p;
    const cur = {};
    for (const f of (Array.isArray(card.files) ? card.files : [])) {
        const path = String((f && f.path) || "");
        if (path) cur[path] = {h: _pflHash((f || {}).text), text: String((f || {}).text || "")};
    }
    for (const path of Object.keys(cur)) {
        if (!(path in base.files)) p.files.add.push({path: path, text: cur[path].text});
        else if (base.files[path] !== cur[path].h) p.files.update.push({path: path, text: cur[path].text});
    }
    for (const path of Object.keys(base.files)) {
        if (!(path in cur)) p.files.delete.push(path);
    }
    // 图片：**未改的不带**（服务端从 base 复制）；显式删图用空串（服务端按"删槽位"处理）
    const im = card.images || {};
    for (const slot of ["avatar", "thumb", "display"]) {
        const v = String(im[slot] || "");
        if (!v) { if (base.images[slot]) p.images[slot] = ""; continue; }
        if (base.images[slot] !== _pflHash(v)) p.images[slot] = v;
    }
    const stk = (Array.isArray(im.stickers) ? im.stickers : [])
        .map((s) => [_pflHash((s && s.data) || s), String((s && s.label) || "")]);
    if (base.stickers !== _pflHash(JSON.stringify(stk))) p.images.stickers = im.stickers || [];
    return p;
}
/** 改编提交体：**只有变化的部分**（+ 唯一必须的元信息）。没在改编态 ⇒ 返回 null。
 *
 *  ⚠ **报文形状以服务端契约 02 为准**（`app/plaza/api.py::plaza_derive`，2026-10-01 serverops 落地）：
 *    `{base_id, card: {id, 元数据…, files:[{path,text}]（只放新增/替换的）, images:{只放被替换的图},
 *      opening}, remove: {files:[路径], images:[槽位]}, display}`
 *    —— **不是** 我最初设想的 `patch.files.{add,update,delete}`；两者差别只在这一个函数里，改这里即可。
 *    服务端会用 `copied{copied.files,images}` 告诉我们"它自己从原卡复制了多少"，`uploaded` 是这次真传的。 */
function _pflDeriveBody(card, display) {
    if (!_pflS.base) return null;
    const p = _pflPatch(card);
    const bcard = _pflS.base.card || {};
    const partial = {id: String(card.id || "")};
    for (const k of ["name", "char_name", "user_name", "presentation", "desc", "tagline",
                     "category", "tags"]) {
        if (JSON.stringify(card[k]) !== JSON.stringify(bcard[k])) partial[k] = card[k];
    }
    const files = [];
    for (const it of (p.files.add || [])) files.push({path: it.path, text: it.text});
    for (const it of (p.files.update || [])) files.push({path: it.path, text: it.text});
    if (files.length) partial.files = files;
    const imgs = {};
    for (const slot of ["avatar", "thumb", "display"]) {
        if (Object.prototype.hasOwnProperty.call(p.images, slot) && p.images[slot]) {
            imgs[slot] = p.images[slot];
        }
    }
    if (Object.prototype.hasOwnProperty.call(p.images, "stickers")) {
        imgs.stickers = p.images.stickers || [];
    }
    if (Object.keys(imgs).length) partial.images = imgs;
    const opH = _pflHash(JSON.stringify(card.opening || {}));
    if (opH !== (_pflS.base.snap.opening || "")) partial.opening = card.opening || {};

    const body = {base_id: _pflS.base.id, card: partial,
                  display: String(display || "")};
    const remove = {};
    const rmFiles = p.files.delete || [];
    const rmImgs = [];
    for (const slot of ["avatar", "thumb", "display"]) {
        if (Object.prototype.hasOwnProperty.call(p.images, slot) && !p.images[slot]) rmImgs.push(slot);
    }
    if (rmFiles.length) remove.files = rmFiles;
    if (rmImgs.length) remove.images = rmImgs;
    if (Object.keys(remove).length) body.remove = remove;
    return body;
}
/** 一行字说清"这次到底传什么"（人话 + 给用户信心：图片没改就不用传）。 */
function _pflPatchSummary(body) {
    const partial = (body && body.card) || body || {};
    const files = (partial && partial.files) || [];
    const imgs = Object.keys((partial && partial.images) || {});
    return "文本改动 " + files.length + " 份"
           + (imgs.length ? ("、图片改动 " + imgs.length + " 处") : "、图片未改（不上传）");
}
/** 字节数（UTF-8）——用于"省了多少流量"的实测数字。 */
function _pflBytes(obj) {
    const s = JSON.stringify(obj || {});
    try { return new Blob([s]).size; } catch (e) { return s.length; }
}

/** 本地预检：**先给人话原因，再谈上传**（不然用户会拿到服务端 400 而不知道为什么）。
 *  2026-10-01 松绑：**不再限单文件大小、不再限知识库份数**（用户：「为什么知识库还限制份数？」），
 *  只卡 ① **单卡文字总量**（口径 = `_pfTextTotalMax()`：普通账号 8MB / **管理员不限**，与后端同常量）
 *  ② 整卡文件数 ≤ `_PF_FILES_MAX`（**500** 的安全天花板，不是配额；2026-10-01 由 24 放宽）。 */
function _pflPrecheck(mode, items) {
    const total = items.reduce((n, x) => n + (Number(x.bytes) || 0), 0);
    const cap = _pfTextTotalMax();
    if (total > cap) {
        const mb = (v) => Math.round(v / (1024 * 1024) * 10) / 10;
        return "这张卡的文字总量有 " + mb(total) + "MB，超过上限 " + mb(cap)
               + "MB —— 请精简正文，或拆成多张卡。";
    }
    if (items.length > _PF_FILES_MAX) {
        return "卡内文件过多：当前 " + items.length + " 个，上限 " + _PF_FILES_MAX
               + " 个（文本总量另有 8MB 上限）。";
    }
    return "";
}

async function _pflQ(url) {
    const r = await fetch(url, {headers: {"Accept": "application/json"}, cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
}

/** 取一张图 → data URL（失败返回 ""，不抛：少一张图不该让整次发布失败）。
 *
 *  ⚠ **必须显式带 `Authorization`**（2026-10-01）：`/assets/` 已改成**账号作用域**
 *  （serverops 同批修复）——`<img src>` 与**裸 fetch 都不带凭证**，未登录访问账号内资产给 401。
 *  不用 `?token=` 之类的 URL 令牌：token 进 URL 会落到日志/Referer（安全决策，已由 Lead 拍板）。 */
async function _pflDataURL(url) {
    if (!url) return "";
    try {
        const headers = {};
        try {
            const t = localStorage.getItem("firefly_token") || "";
            if (t) headers["Authorization"] = "Bearer " + t;
        } catch (e) { /* 无 localStorage：本地模式不需要凭证 */ }
        const r = await fetch(String(url), {headers: headers});
        if (!r.ok) return "";
        const blob = await r.blob();
        if (!blob || !blob.size) return "";
        return await new Promise((res) => {
            const fr = new FileReader();
            fr.onload = () => res(String(fr.result || ""));
            fr.onerror = () => res("");
            fr.readAsDataURL(blob);
        });
    } catch (e) {
        return "";
    }
}

/** 按候选顺序取第一张能拿到的图（拿不到返回 ""）。 */
async function _pflFirstDataURL(urls) {
    for (const u of urls) {
        if (!u) continue;
        const d = await _pflDataURL(u);
        if (d) return d;
    }
    return "";
}

/** 读一张已有角色卡 → 制卡页认得的 card 对象（+ 本次载入的说明性统计）。 */
async function _pflCardFromMode(mode) {
    const modes = await _pflQ("/modes");
    const meta = ((modes && modes.modes) || []).find((m) => m && m.id === mode) || {id: mode};
    const notes = {files: 0, stickers: 0, images: 0, opening: 0, viaPlaza: 0};

    // ── 文本：固定三件 + 知识库。**不静默截断**：超限在下面预检里给"哪份文件、多大、上限多少" ──
    const files = [];
    const items = [];
    const addText = (path, text) => {
        const t = String(text || "");
        if (!t.trim()) return;
        items.push({path: path, bytes: new Blob([t]).size});
        files.push({path: path, text: t});
    };
    let pf = {};
    try {
        pf = await _pflQ("/pack-files?mode=" + encodeURIComponent(mode));
    } catch (e) { /* 读不到就当没有文本 */ }
    for (const f of ((pf && pf.files) || [])) {
        const name = String((f && f.name) || "");
        if (["core.md", "identity.md", "sms_samples.md"].indexOf(name) < 0) continue;
        addText(name, (f && f.content) || "");
    }
    try {
        const kb = await _pflQ("/pack-knowledge?mode=" + encodeURIComponent(mode));
        for (const it of ((kb && kb.files) || [])) {
            const path = String((it && it.path) || "");
            if (!path || path.indexOf("..") >= 0) continue;
            const one = await _pflQ("/pack-knowledge/file?mode=" + encodeURIComponent(mode)
                                    + "&path=" + encodeURIComponent(path));
            addText("knowledge/" + path, (one && one.content) || "");
        }
    } catch (e) { /* 无知识库 */ }
    const why = _pflPrecheck(mode, items);
    if (why) throw new Error(why);          // 人话拒绝：调用方把它显示出来，**不动表单、不留草稿**
    notes.files = files.length;

    // ── 开场白：契约 §3.2 的 opening{narrations, first_messages}；老形状（单字符串）兜底 ──
    const opening = {narrations: [], first_messages: []};
    try {
        const op = await _pflQ("/pack-file?mode=" + encodeURIComponent(mode)
                               + "&path=opening.json");
        const raw = String((op && op.content) || "").trim();
        if (raw) {
            const j = JSON.parse(raw);
            if (j && typeof j === "object" && !Array.isArray(j)) {
                // ⚠ 开场白**条数不截断**（2026-10-01 全量扫描时发现这里原来 `.slice(0,3)`/`.slice(0,5)`
                //   —— 那是**静默丢用户内容**，而服务端对条数没有任何上限（只校验 opening.json 是合法
                //   JSON）⇒ 一条都不许丢，全带过去。）
                opening.narrations = (Array.isArray(j.narrations) ? j.narrations : [])
                    .map(String).filter(Boolean);
                opening.first_messages = (Array.isArray(j.first_messages) ? j.first_messages : [])
                    .map(String).filter(Boolean);
            } else if (typeof j === "string" && j.trim()) {
                opening.first_messages = [j.trim()];
            }
        }
    } catch (e) { /* 没有开场白 */ }
    notes.opening = opening.narrations.length + opening.first_messages.length;

    // ── 图片：两条路都试（**不新增任何服务端接口**）──
    //   ① `/modes` 给的资产投影 `/assets/character/<包>/assets/<文件>` —— **本地模式**直接可用；
    //   ② 服务器模式下这条目前**不按账号作用域下发**（`/assets/` 在鉴权前分发，且
    //      `resolve_asset` 的第二条只认全局 MODES），会 404 ⇒ 回落到广场自己的资产接口
    //      `/plaza/api/asset?id=&slot=`（只对**已发布/已安装**的卡有效）。
    //   两条都拿不到时不硬造：交给制卡页的图片槽位与校验（缺 thumb 会给人话原因）。
    let dc = {};
    try {
        const det = await _pflQ("/plaza/api/card?id=" + encodeURIComponent(mode));
        dc = (det && det.card) || {};
    } catch (e) { dc = {}; }
    const slotURL = (slot) => "/plaza/api/asset?id=" + encodeURIComponent(mode)
                              + "&slot=" + encodeURIComponent(slot);
    const images = {};
    const av = await _pflFirstDataURL([meta.avatar, dc.avatar ? slotURL("avatar") : "",
                                       dc.thumb ? slotURL("thumb") : ""]);
    const cv = await _pflFirstDataURL([meta.cover, dc.display ? slotURL("display") : "",
                                       dc.thumb ? slotURL("thumb") : ""]);
    if (av) { images.avatar = av; notes.images++; }
    if (cv) { images.display = cv; notes.images++; }
    if (!av && !cv && (dc.thumb || dc.display)) notes.viaPlaza = 1;

    // ── 表情包：① 本包专属注册表（`pack === mode`，本地模式/已桥接的卡）→ ② 广场详情的 stickers ──
    //   注意 `store._public_view()` 的 `card.stickers` 是**文件名字符串数组**（不是 {file,label} 对象），
    //   所以这里两种形状都吃；label 从注册表按顺序补（详情里没有 label）。
    const out = [];
    const kbLabels = [];
    try {
        const st = await _pflQ("/stickers?enabled=1");
        for (const s of ((st && st.stickers) || [])) {
            if (String((s && s.pack) || "") !== mode) continue;
            kbLabels.push(String((s && s.label) || ""));
            const data = await _pflDataURL("/assets/" + String((s && s.file) || ""));
            if (!data) continue;
            out.push({data: data, label: String((s && s.label) || "").slice(0, 12)});
            if (out.length >= _PFL_STK_MAX) break;
        }
    } catch (e) { /* 读不到注册表 */ }
    if (!out.length && Array.isArray(dc.stickers)) {
        let i = 0;
        for (const raw of dc.stickers) {
            const name = typeof raw === "string" ? raw
                                               : String((raw && (raw.file || raw.name)) || "");
            const stem = name.replace(/\.[a-z0-9]+$/i, "");
            if (!stem) continue;
            const data = await _pflDataURL(slotURL(stem));
            if (!data) continue;
            const lab = (typeof raw === "object" && raw && raw.label)
                ? String(raw.label) : (kbLabels[i] || "");
            out.push({data: data, label: lab.slice(0, 12)});
            i++;
            if (out.length >= _PFL_STK_MAX) break;
        }
        if (out.length) notes.viaPlaza = 1;
    }
    if (out.length) { images.stickers = out; notes.stickers = out.length; }

    if (notes.kbCut) showToast("知识库超过 " + _PFL_KB_MAX + " 份，只带前 " + _PFL_KB_MAX + " 份");

    const card = {
        id: String(mode),
        name: String(meta.name || mode),
        char_name: String(meta.char_name || ""),
        user_name: String(meta.user_name || "你"),
        presentation: String(meta.presentation || "sticker"),
        desc: String(meta.desc || ""),
        tagline: String(meta.tagline || ""),
        category: "其他",              // 本地卡没有分类：交服务端归一（未知值 → 其他）
        tags: [],
        opening: opening,
        files: files,
        images: images,
    };
    return {card: card, notes: notes};
}

/** 等制卡页骨架就绪（表单是打开制卡页时才建的）。 */
async function _pflWaitForm(timeout) {
    const t0 = Date.now();
    const lim = timeout || 4000;
    while (Date.now() - t0 < lim) {
        if (typeof _pf === "function" && _pf("pf-name")) return true;
        await new Promise((r) => setTimeout(r, 120));
    }
    return false;
}

/** 关掉"宿主页"的全屏层（App 的角色卡管理/详情、菜单、设置…）。
 *  平台页没有这些函数 ⇒ 全是 `window.X && X()` 守卫式调用，平台侧零影响。 */
function _pflCloseHostOverlays() {
    for (const fn of ["closePackView", "closeCardsView", "closeSettings", "closeMenu",
                      "closeVoiceView", "closePlazaDetail"]) {
        try { if (typeof window[fn] === "function") window[fn](); } catch (e) { /* 关不掉不阻塞 */ }
    }
}

/** 载入已有角色卡到制卡页（打开广场 + 制卡层 → 填表 → 补 thumb）。 */
async function _pflLoadIntoForge(mode) {
    if (!mode) return;
    _pflAdaptReset();          // ★ 这条路径**不是改编**：清掉可能残留的改编基线（否则会张冠李戴）
    showToast("正在读取角色卡…");
    let built;
    try {
        built = await _pflCardFromMode(mode);
    } catch (e) {
        // 预检拒绝（超限/非法）：给人话原因，**不动表单、不产生草稿**
        const why = String((e && e.message) || "读取角色卡失败");
        _pflCloseHostOverlays();
        if (window.openPlazaForge) window.openPlazaForge();
        await _pflWaitForm();
        if (typeof _pfMsg === "function") _pfMsg("这张卡暂时不能发布：" + why, false);
        showToast("不能发布：" + why);
        return;
    }
    _pflCloseHostOverlays();
    if (window.openPlaza) window.openPlaza();
    if (window.openPlazaForge) window.openPlazaForge();
    const ok = await _pflWaitForm();
    if (!ok) { showToast("制卡页没打开，请重试"); return; }
    _pfSetForm(built.card, {status: "draft", display: built.card.name});
    // ★ 先归一化图片（逐槽位压到合规），**再**派生缩略图（thumb 从合规图派生更省事）
    let comp = [];
    try { if (typeof _pfiNormalizeLoaded === "function") comp = await _pfiNormalizeLoaded(); } catch (e) {}
    const compTxt = (typeof _pfiNormalizeSummary === "function") ? _pfiNormalizeSummary(comp) : "";
    let madeThumb = false;
    try {
        if (typeof _pfiEnsureLegacyThumb === "function") madeThumb = await _pfiEnsureLegacyThumb();
    } catch (e) { /* 没图可派生：交给校验给人话 */ }
    const n = built.notes;
    _pfMsg("已载入已有角色卡「" + built.card.name + "」：文本 " + n.files + " 份"
           + (n.stickers ? ("、表情包 " + n.stickers + " 张") : "")
           + (n.opening ? ("、开场白 " + n.opening + " 条") : "")
           + (compTxt ? ("；" + compTxt) : "")
           + (madeThumb ? "；缩略图已自动派生（≤320px/≤12KB）" : "")
           + (n.images ? "" : "；没读到可用图片，请在下方图片区补一张缩略图")
           + "。检查无误后依次点「存草稿」→「提交审核」→「发布到广场」。", false);
    _pfStatus("已载入已有角色卡，尚未提交审核。", "warn");
    if (typeof _pfSyncBtns === "function") _pfSyncBtns();
}

// ── 改编：广场卡 → 新卡（2026-10-01 用户：「如果用户要在我的基础上改呢？」）──────────
/** 给改编产物起一个**不与原卡冲突**的新 id（`<原 id>-2`、`-3`…，仍满足 `^[a-z0-9_-]{1,32}$`）。 */
function _pflAdaptId(base) {
    const b = String(base || "").replace(/[^a-z0-9_-]/gi, "").toLowerCase().slice(0, 28) || "adapted";
    for (let i = 2; i <= 9; i++) {
        const cand = (b + "-" + i).slice(0, 32);
        if (cand !== base) return cand;
    }
    return (b.slice(0, 28) + "-copy");
}

/** 广场卡的正文从哪来（按代价从低到高，全是**现成端点**）：
 *    ① `/plaza/api/draft?id=` —— **我自己的卡**草稿体（发布后仍保留卡体，作者可回看/改版）；
 *    ② 本机已装的副本（`/pack-files` + `/pack-knowledge`，即 `_pflCardFromMode`）；
 *    ③ 都没装 ⇒ 问用户要不要**先装到本机**（`POST /plaza/api/install`，占 1 个安装名额）再读。
 *  服务端目前**没有** `based_on`/来源字段 ⇒ 先不发送（要加来源标记需服务端加参数，已回报 Lead）。 */
async function _pflCardFromPlaza(id) {
    try {
        const d = await _pflQ("/plaza/api/draft?id=" + encodeURIComponent(id));
        const card = d && d.draft && d.draft.card;
        if (card && Array.isArray(card.files) && card.files.length) {
            return await _pflFinishPlazaCard(card, id, "draft");
        }
    } catch (e) { /* 不是我自己的卡 / 没有草稿体 */ }
    try {
        const built = await _pflCardFromMode(id);
        built.notes.from = "installed";
        return built;
    } catch (e) { /* 没装到本机 */ }
    // ③ 先安装再改编（要让用户知道会占名额）
    const inst = await _pflConfirmInstall(id);
    if (!inst) return null;
    const built = await _pflCardFromMode(id);
    built.notes.from = "installed-now";
    return built;
}

/** 把广场详情填成"制卡 payload 形状"（图片走 `/plaza/api/asset`，缩略图缺失时由 forge 派生）。 */
async function _pflFinishPlazaCard(card, id, from) {
    const det = await (async () => {
        try { return await _pflQ("/plaza/api/card?id=" + encodeURIComponent(id)); } catch (e) { return null; }
    })();
    const dc = (det && det.card) || {};
    const slotURL = (slot) => "/plaza/api/asset?id=" + encodeURIComponent(id)
                              + "&slot=" + encodeURIComponent(slot);
    const images = {};
    const av = await _pflFirstDataURL([dc.avatar ? slotURL("avatar") : "", dc.thumb ? slotURL("thumb") : ""]);
    const cv = await _pflFirstDataURL([dc.display ? slotURL("display") : "", dc.thumb ? slotURL("thumb") : ""]);
    const th = await _pflFirstDataURL([dc.thumb ? slotURL("thumb") : ""]);
    if (av) images.avatar = av;
    if (cv) images.display = cv;
    if (th) images.thumb = th;              // 有就复用（省一次派生）；没有则 forge 自己压
    const stk = [];
    for (const it of (Array.isArray(dc.stickers) ? dc.stickers : [])) {
        const name = typeof it === "string" ? it : String((it && (it.file || it.name)) || "");
        const stem = name.replace(/\.[a-z0-9]+$/i, "");
        if (!stem) continue;
        const data = await _pflDataURL(slotURL(stem));
        if (!data) continue;
        stk.push({data: data, label: String((it && it.label) || "").slice(0, 12)});
        if (stk.length >= _PFL_STK_MAX) break;
    }
    if (stk.length) images.stickers = stk;
    const out = {
        id: String(card.id || id), name: String(card.name || dc.name || id),
        char_name: String(card.char_name || dc.char_name || ""),
        user_name: String(card.user_name || dc.user_name || "你"),
        presentation: String(card.presentation || dc.presentation || "sticker"),
        desc: String(card.desc || dc.desc || ""),
        tagline: String(card.tagline || dc.tagline || ""),
        category: String(card.category || dc.category || "其他"),
        tags: Array.isArray(card.tags) ? card.tags.slice(0, 5) : [],
        opening: card.opening || {narrations: [], first_messages: []},
        files: card.files || [],
        images: images,
    };
    let bytes = 0;
    for (const f of out.files) bytes += _pfTextBytes(f && f.text);
    return {card: out,
            notes: {files: out.files.length, stickers: stk.length,
                    opening: ((out.opening.first_messages || []).length
                              + (out.opening.narrations || []).length),
                    images: (av ? 1 : 0) + (cv ? 1 : 0), from: from, bytes: bytes}};
}

/** 改编前的确认：这张卡没装到本机，要先装（占 1 个安装名额）。返回 true=用户同意。 */
function _pflConfirmInstall(id) {
    return new Promise((resolve) => {
        const wrap = document.getElementById("pf-modal");
        if (!wrap) { resolve(false); return; }
        wrap.textContent = "";
        wrap.classList.add("show");
        const box = document.createElement("div");
        box.className = "pf-modal-box";
        const title = document.createElement("div");
        title.className = "pf-modal-title";
        title.textContent = "先安装这张卡，再改编？";
        const note = document.createElement("div");
        note.className = "pf-modal-note";
        note.textContent = "改编要读这张卡的正文，而正文只存在卡包里。"
                           + "先把「" + id + "」装到本机就能读全（会占用 1 个安装名额，"
                           + "最多 5 张，随时可卸载）。改编产物是一张**新卡**，不会动原卡。";
        const acts = document.createElement("div");
        acts.className = "pf-modal-acts";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "pz-btn";
        cancel.id = "pf-adapt-cancel";
        cancel.textContent = "取消";
        cancel.onclick = () => { wrap.classList.remove("show"); wrap.textContent = ""; resolve(false); };
        const ok = document.createElement("button");
        ok.type = "button";
        ok.className = "pz-btn primary";
        ok.id = "pf-adapt-install";
        ok.textContent = "安装并继续";
        ok.onclick = async () => {
            ok.disabled = true;
            ok.textContent = "安装中…";
            const r = await _pflPost("/plaza/api/install", {id: id});
            if (!r || !r.ok) {
                const why = (r && r.data && r.data.error) || "安装失败，请稍后重试。";
                _pfMsg("改编失败：" + why, false);
                showToast("安装失败：" + why);
                wrap.classList.remove("show"); wrap.textContent = "";
                resolve(false);
                return;
            }
            wrap.classList.remove("show"); wrap.textContent = "";
            resolve(true);
        };
        acts.append(cancel, ok);
        box.append(title, note, acts);
        wrap.appendChild(box);
    });
}

async function _pflPost(url, body) {
    try {
        const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                                    body: JSON.stringify(body)});
        let d = null;
        try { d = await r.json(); } catch (e) { d = null; }
        return {ok: r.ok, status: r.status, data: d};
    } catch (e) {
        return {ok: false, status: 0, data: null};
    }
}

/** 「以此为模板改编」：把广场卡载入制卡页，**id 换成新的**（原卡不动）。 */
/** 改编态横幅：一眼看出"这是在原卡基础上改"，并给「放弃改编」。 */
function _pflBanner(name) {
    const pane = document.querySelector("#plaza-forge .pfc-pane") || document.querySelector(".pfc-pane");
    if (!pane) return;
    let bar = document.getElementById("pf-adapt-bar");
    if (!bar) {
        bar = document.createElement("div");
        bar.id = "pf-adapt-bar";
        bar.className = "pf-adapt-bar";
        const t = document.createElement("span");
        t.id = "pf-adapt-text";
        bar.appendChild(t);
        const drop = document.createElement("button");
        drop.type = "button";
        drop.id = "pf-adapt-drop";
        drop.className = "pz-btn";
        drop.textContent = "放弃改编";
        drop.title = "不再基于原卡：改回普通制卡（整卡上传、不再有来源关系）";
        drop.onclick = () => _pflDropAdapt();
        bar.appendChild(drop);
        const head = pane.querySelector(".cv-head");
        if (head && head.nextSibling) pane.insertBefore(bar, head.nextSibling);
        else pane.insertBefore(bar, pane.firstChild);
    }
    const txt = document.getElementById("pf-adapt-text");
    if (txt) {
        txt.textContent = "基于《" + name + "》改编 · 只上传你改动的部分（未改的图片不下载也不上传）";
    }
    bar.style.display = "flex";
}
function _pflBannerOff() {
    const bar = document.getElementById("pf-adapt-bar");
    if (bar) bar.style.display = "none";
}
/** 放弃改编：清掉基线（回到普通制卡/整卡上传），表单内容**保留**，不丢用户的改动。 */
function _pflDropAdapt() {
    if (!_pflS.base) return;
    const name = _pflS.base.name || _pflS.base.id;
    _pflS.base = null;
    _pflBannerOff();
    showToast("已放弃改编：接下来按普通制卡上传");
    if (typeof _pfMsg === "function") {
        _pfMsg("已放弃改编（原卡不再受影响）。这张卡会按普通制卡整卡上传，id 仍是 "
               + (typeof _pf === "function" && _pf("pf-id") ? _pf("pf-id").value : "") + "。", false);
    }
    if (typeof _pfStatus === "function") _pfStatus("已放弃基于《" + name + "》的改编。", "warn");
}

/** 改编提交：**只传 patch** 到 `/plaza/api/derive`；服务端不支持（404/405/501）⇒ 自动退回整卡上传。 */
async function _pflSaveDerive(card, display) {
    const body = _pflDeriveBody(card, display);
    if (!body) return null;
    const full = {card: card, display: display};
    const bytes = {derive: _pflBytes(body), full: _pflBytes(full)};
    const r = await _pflPost("/plaza/api/derive", body);
    if (r.ok) {
        const d = (r.data && r.data) || {};
        _pflS.last = {mode: "derive", bytes: bytes, patch: body.card || {},
                      copied: d.copied || null, uploaded: d.uploaded || null};
        return {res: r, fellBack: false, bytes: bytes, summary: _pflPatchSummary(body.card || {}),
                copied: d.copied || null, uploaded: d.uploaded || null};
    }
    if ([404, 405, 501, 0].indexOf(r.status) >= 0) {
        // ★ 保守回退：服务端还没上 derive（或路径不对）⇒ 走原来的整卡上传，**不丢内容**
        const r2 = await _pflPost("/plaza/api/draft", full);
        _pflS.last = {mode: "fallback", bytes: bytes, patch: body.card || {}};
        return {res: r2, fellBack: true, bytes: bytes, summary: _pflPatchSummary(body),
                why: "服务端暂不支持只传改动（derive 返回 " + r.status + "），已按整卡上传。"};
    }
    _pflS.last = {mode: "failed", bytes: bytes, patch: body.card || {}};
    return {res: r, fellBack: false, bytes: bytes, summary: _pflPatchSummary(body)};
}
/** 退出改编态（**普通载入/新建**时必须调用）：清基线 + 收横幅，**不动表单内容**。
 *  ⚠ 不清会出现"张冠李戴"：把一张**无关的卡**当成原卡的 patch 提交（2026-10-01 实测踩到）。 */
function _pflAdaptReset() {
    _pflS.base = null;
    _pflS.last = null;
    _pflBannerOff();
}
window.plazaAdaptReset = _pflAdaptReset;
/** 给测试/诊断看的状态（也是"省了多少流量"的唯一数据源）。 */
window.plazaAdaptState = function () {
    return {base: _pflS.base ? {id: _pflS.base.id, name: _pflS.base.name,
                                files: Object.keys(_pflS.base.snap.files).length,
                                images: Object.keys(_pflS.base.snap.images).length} : null,
            last: _pflS.last};
};

async function _pflAdaptFromPlaza(id) {
    if (!id) return;
    showToast("正在读取广场卡…");
    _pflCloseHostOverlays();
    if (window.openPlaza) window.openPlaza();
    if (window.openPlazaForge) window.openPlazaForge();
    await _pflWaitForm();
    let built = null;
    try {
        built = await _pflCardFromPlaza(id);
    } catch (e) {
        _pfMsg("改编失败：" + String((e && e.message) || "读取这张卡失败，请稍后重试。"), false);
        return;
    }
    if (!built) return;                     // 用户取消了"先安装"
    const src = built.card;
    const nid = _pflAdaptId(id);
    const card = Object.assign({}, src, {id: nid});
    // ★ 建立**基线快照**：之后提交只传"与这一刻不同"的部分
    //   （`card` 留的是**原卡**对象，用于元数据字段的差异比较；`id` 单独记 base 的 id）
    _pflS.base = {id: String(id), name: String(src.name || id), snap: _pflSnap(src), card: src};
    _pflS.last = null;
    _pfSetForm(card, {status: "draft", display: card.name});
    // ★ 图片归一化（载入即压到合规，别让用户面对"54KB/30KB"的红字）
    let comp = [];
    try { if (typeof _pfiNormalizeLoaded === "function") comp = await _pfiNormalizeLoaded(); } catch (e) {}
    const compTxt = (typeof _pfiNormalizeSummary === "function") ? _pfiNormalizeSummary(comp) : "";
    // ⚠ 改编态的特殊处理：自动压缩会改字节，**不能**让它看起来像"用户改了图"（否则未编辑的图也会被上传，
    //   违背"只传改动"）。归一化发生在载入之后、用户编辑之前 ⇒ 就地把基线重拍一次，把"压过的原图"
    //   视为起点（未改动的图仍然不上传、由服务端从原卡复制）。
    if (comp.some((x) => x.changed) && typeof _pflSnap === "function" && typeof _pfRead === "function") {
        try { _pflS.base.snap = _pflSnap(_pfRead()); } catch (e) {}
    }
    let madeThumb = false;
    try {
        if (typeof _pfiEnsureLegacyThumb === "function") madeThumb = await _pfiEnsureLegacyThumb();
    } catch (e) { /* 没图可派生：交给校验 */ }
    const n = built.notes || {};
    _pflBanner(String(src.name || id));          // ★ 改编态一眼可见 + 「放弃改编」
    _pfMsg("已把广场卡「" + (src.name || id) + "」载入为模板：新卡 id = " + nid
           + "（不会改动原卡）；文本 " + (n.files || 0) + " 份"
           + (n.stickers ? ("、表情包 " + n.stickers + " 张") : "")
           + (compTxt ? ("；" + compTxt) : "")
           + (madeThumb ? "；缩略图已自动派生" : "")
           + "。只上传你改动的部分（未改的图片不下载也不上传，服务端从原卡复制）；"
           + "改完依次点「存草稿」→「提交审核」→「发布到广场」。"
           + (n.from === "installed-now" ? "（刚把它装到了本机，用来读正文）" : ""), false);
    _pfStatus("改编自「" + (src.name || id) + "」，尚未提交审核。", "warn");
    if (typeof _pfSyncBtns === "function") _pfSyncBtns();
}

// ── 选择器（制卡页头部「载入已有卡」）──────────────────────────
async function _pflPickerOpen() {
    const wrap = document.getElementById("pf-modal");
    if (!wrap) return;
    let modes = [];
    try {
        const d = await _pflQ("/modes");
        modes = ((d && d.modes) || []).filter((m) => m && m.id);
    } catch (e) {
        showToast("读取角色卡列表失败");
        return;
    }
    wrap.textContent = "";
    wrap.classList.add("show");
    const box = document.createElement("div");
    box.className = "pf-modal-box";
    const title = document.createElement("div");
    title.className = "pf-modal-title";
    title.textContent = "载入已有角色卡";
    const note = document.createElement("div");
    note.className = "pf-modal-note";
    note.textContent = "选一张你自己的角色卡，内容会填进制卡表单；检查后按原有流程"
                       + "「存草稿 → 提交审核 → 发布到广场」。";
    const list = document.createElement("div");
    list.className = "pfl-list";
    const acts = document.createElement("div");
    acts.className = "pf-modal-acts";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "pz-btn";
    cancel.textContent = "取消";
    cancel.onclick = () => { wrap.classList.remove("show"); wrap.textContent = ""; };
    acts.appendChild(cancel);
    box.append(title, note, list, acts);
    wrap.appendChild(box);
    if (!modes.length) {
        const e = document.createElement("div");
        e.className = "pf-modal-note";
        e.textContent = "还没有可用的角色卡。";
        list.appendChild(e);
        return;
    }
    for (const m of modes) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "pz-btn pfl-item";
        b.textContent = (m.name || m.id) + (m.char_name ? ("（" + m.char_name + "）") : "");
        b.onclick = () => { wrap.classList.remove("show"); wrap.textContent = ""; _pflLoadIntoForge(m.id); };
        list.appendChild(b);
    }
}

// 对外钩子：App 包详情页按钮（带 mode）与制卡页头部按钮（开选择器）
window.plazaPublishLocalMode = function (mode) {
    if (mode) { _pflLoadIntoForge(mode); return; }
    _pflPickerOpen();
};
window.plazaPublishLocalPick = _pflPickerOpen;
// 广场详情页「以此为模板改编」（plaza_detail.js 的入口调它）
window.plazaAdaptFromPlaza = _pflAdaptFromPlaza;
