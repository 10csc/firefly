// 角色卡制作（共创平台 M2/P4-7c）——**图片层**：头像 / 列表缩略图 thumb / 详情图 / ≤8 张表情包
//
// 契约：docs/设计/角色卡共创平台/06_制卡自由度与图片规格.md §3.3
//   槽位        列表用   长边     体积上限
//   thumb       是       320px    **12KB**   ← 列表只发它（流量大头在这里砍掉）
//   avatar      小圆头像 256px    30KB
//   display     否       1024px   300KB
//   sticker-N   否       160px    20KB ×≤8
//
// 压缩策略（契约 §3.3）：**先复用 imgzip.compressImage**（大图先降到 ≤1024、≤300KB），
// 再按槽位**先尺寸后质量**逐级降（webp 优先、jpg 兜底），直到 ≤ 上限；实在降不下去就**拒收**
// 并把人话原因写在槽位上（宁可不填，也不让用户提交后才发现）。
//
// 约束：前缀 `_pfi` 防撞；DOM 引用走闭包；文案 textContent；不引外部依赖。
import { showToast } from "../util.js";
import { compressImage } from "../imgzip.js";

const _PFI_SPEC = {
    avatar: {label: "头像", maxSide: 256, maxBytes: 30 * 1024, hint: "≤256px / ≤30KB"},
    thumb: {label: "列表缩略图", maxSide: 320, maxBytes: 12 * 1024, hint: "≤320px / ≤12KB（列表只发它）"},
    display: {label: "详情图", maxSide: 1024, maxBytes: 300 * 1024, hint: "≤1024px / ≤300KB"},
};
const _PFI_STK = {label: "表情包", maxSide: 160, maxBytes: 20 * 1024, maxCount: 8,
                  labelMax: 12};      // label ≤12 字（契约 06 §3.2 变更 2026-10-01 18:20）
const _PFI_QUALITIES = [0.92, 0.85, 0.78, 0.7, 0.62, 0.54, 0.45];
const _PFI_SIDES = [1, 0.78, 0.6, 0.46, 0.34];   // 质量降到底还超限 → 再逐级缩尺寸
// （2026-10-01 task-13：档位加到 0.34 —— 老草稿迁移要把一张大封面压成 ≤12KB，
//   噪点类的"不可压缩"图在原三档下仍可能压不下去，于是迁移静默失败、用户一保存就吃 400。）

const _pfiS = {
    mount: null,
    data: {avatar: "", thumb: "", display: "", stickers: []},   // dataURL
    meta: {avatar: null, thumb: null, display: null},           // {bytes, w, h}
    stkMeta: [],                                                // 每张表情包 {bytes, w, h}
    seq: 0,                                                     // 换图代际：连续选图时旧结果作废
    webp: null,                                                 // 浏览器是否支持 webp 编码（探一次）
    els: {},
};

// ═══ 小工具 ═══════════════════════════════════════════
function _pfiEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
}

function _pfiBtn(cls, text, onClick) {
    const b = _pfiEl("button", cls, text);
    b.type = "button";
    b.onclick = onClick;
    return b;
}

function _pfiFmtSize(n) {
    const b = Number(n) || 0;
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(b < 10 * 1024 ? 1 : 0) + " KB";
    return (b / 1024 / 1024).toFixed(1) + " MB";
}

/** data URL 的**真实字节数**（base64 长度换算，不 decode 整张图）。 */
function _pfiDataBytes(dataUrl) {
    const s = String(dataUrl || "");
    const i = s.indexOf(",");
    if (i < 0) return 0;
    const b64 = s.slice(i + 1);
    const pad = (b64.match(/=*$/) || [""])[0].length;
    return Math.max(0, Math.floor(b64.length * 3 / 4) - pad);
}

function _pfiBlobToDataUrl(blob) {
    return new Promise((res) => {
        try {
            const fr = new FileReader();
            fr.onload = () => res(String(fr.result || ""));
            fr.onerror = () => res("");
            fr.readAsDataURL(blob);
        } catch (e) { res(""); }
    });
}

function _pfiCanvasBlob(canvas, mime, q) {
    return new Promise((res) => {
        try { canvas.toBlob((b) => res(b), mime, q); } catch (e) { res(null); }
    });
}

/** webp 编码可用性（探一次就记住）：老 WebView 可能只支持 jpg/png。 */
async function _pfiWebpOk() {
    if (_pfiS.webp !== null) return _pfiS.webp;
    try {
        const cv = document.createElement("canvas");
        cv.width = cv.height = 8;
        const b = await _pfiCanvasBlob(cv, "image/webp", 0.8);
        _pfiS.webp = !!(b && b.type === "image/webp");
    } catch (e) { _pfiS.webp = false; }
    return _pfiS.webp;
}

/** 逐级压到槽位上限。返回 {dataUrl, bytes, w, h} 或 {tooBig: true, bytes} 或 null（读图失败）。 */
async function _pfiCompress(file, spec) {
    let src = file;
    try {
        const out = await compressImage(file);     // 复用 imgzip：大图先降到 ≤1024/≤300KB
        if (out && out.blob) src = out.blob;
    } catch (e) { /* 压缩是优化不是门槛：失败就用原图继续 */ }
    if (typeof createImageBitmap !== "function") return null;
    let bmp;
    try { bmp = await createImageBitmap(src); } catch (e) { return null; }
    try {
        const w0 = bmp.width, h0 = bmp.height;
        if (!w0 || !h0) return null;
        const webp = await _pfiWebpOk();
        const mime = webp ? "image/webp" : "image/jpeg";
        let lastBytes = 0;
        let lastUrl = "";
        for (const f of _PFI_SIDES) {
            const maxSide = Math.max(48, Math.round(spec.maxSide * f));
            const sc = Math.min(1, maxSide / Math.max(w0, h0));
            const cw = Math.max(1, Math.round(w0 * sc));
            const ch = Math.max(1, Math.round(h0 * sc));
            const canvas = document.createElement("canvas");
            canvas.width = cw; canvas.height = ch;
            const ctx = canvas.getContext("2d");
            if (!webp) { ctx.fillStyle = "#10101e"; ctx.fillRect(0, 0, cw, ch); }  // jpg 兜底：暗底与面板一致
            ctx.drawImage(bmp, 0, 0, cw, ch);
            for (const q of _PFI_QUALITIES) {
                const blob = await _pfiCanvasBlob(canvas, mime, q);
                if (!blob) continue;
                if (blob.size > spec.maxBytes) { lastBytes = blob.size; continue; }
                const url = await _pfiBlobToDataUrl(blob);
                if (!url) continue;
                return {dataUrl: url, bytes: blob.size, w: cw, h: ch};
            }
            // 这一档尺寸全质量都超限 → 记住最好结果，缩尺寸再来
            const big = await _pfiCanvasBlob(canvas, mime, _PFI_QUALITIES[_PFI_QUALITIES.length - 1]);
            if (big) { lastBytes = big.size; lastUrl = await _pfiBlobToDataUrl(big); }
        }
        return {tooBig: true, bytes: lastBytes || _pfiDataBytes(lastUrl)};
    } finally {
        try { bmp.close(); } catch (e) {}
    }
}

// ═══ 槽位 UI ══════════════════════════════════════════
function _pfiSlotField(slot, spec) {
    const f = _pfiEl("div", "field");
    f.appendChild(_pfiEl("div", "pf-label", spec.label + "（" + spec.hint + "）"));
    const box = _pfiEl("div", "pfi-img");
    box.id = "pf-imgbox-" + slot;
    const ph = _pfiEl("span", "pfi-ph", "未选择");
    const img = document.createElement("img");
    img.alt = "";
    img.decoding = "async";
    img.id = "pf-img-" + slot;               // 稳定句柄（真浏览器断言用）
    box.append(ph, img);
    const bar = _pfiEl("div", "pfi-bar");
    const file = document.createElement("input");
    file.type = "file";
    file.accept = "image/png,image/jpeg,image/webp,image/gif";
    file.className = "pf-hidefile";
    file.id = "pf-file-" + slot;
    const pick = _pfiBtn("pf-btn small", "选择图片", () => file.click());
    pick.id = "pf-pick-" + slot;
    const clear = _pfiBtn("pf-btn small", "清除", () => _pfiSet(slot, ""));
    clear.id = "pf-clear-" + slot;
    bar.append(pick, clear, file);
    const info = _pfiEl("div", "pfi-info", "未选择（" + spec.hint + "）");
    info.id = "pf-imginfo-" + slot;
    file.addEventListener("change", () => {
        const f0 = file.files && file.files[0];
        file.value = "";                      // 允许重复选同一个文件
        if (f0) _pfiPick(slot, f0);
    });
    f.append(box, bar, info);
    _pfiS.els["box_" + slot] = box;
    _pfiS.els["img_" + slot] = img;
    _pfiS.els["ph_" + slot] = ph;
    _pfiS.els["info_" + slot] = info;
    return f;
}

function _pfiStickerSection() {
    const f = _pfiEl("div", "field");
    const head = _pfiEl("div", "pf-label", "");
    head.append(_pfiEl("span", "", _PFI_STK.label + "（≤160px / ≤20KB，最多 " + _PFI_STK.maxCount + " 张）"),
                _pfiEl("span", "pfi-stkcount", "0 / " + _PFI_STK.maxCount));
    _pfiS.els.stkcount = head.querySelector(".pfi-stkcount");
    const grid = _pfiEl("div", "pfi-stk-grid");
    grid.id = "pf-stk-grid";
    const file = document.createElement("input");
    file.type = "file";
    file.accept = "image/png,image/jpeg,image/webp,image/gif";
    file.className = "pf-hidefile";
    file.id = "pf-file-sticker";
    file.multiple = true;
    file.addEventListener("change", () => {
        const fs = Array.prototype.slice.call(file.files || []);
        file.value = "";
        if (fs.length) _pfiPickStickers(fs);
    });
    const add = _pfiBtn("pf-btn small primary", "+ 添加表情包", () => file.click());
    add.id = "pf-stk-add";
    const info = _pfiEl("div", "pfi-info",
        "表情包是可选内容；**建议每张都填标签**（App 组织器按标签语义选图，没标签基本选不中）。"
        + "列表页不会加载它们。");
    info.id = "pf-stkinfo";
    f.append(head, grid, add, info, file);
    _pfiS.els.stkGrid = grid;
    _pfiS.els.stkInfo = info;
    return f;
}

function _pfiMount(parent) {
    const sec = _pfiEl("div", "pfi-sec");
    sec.appendChild(_pfiEl("div", "pf-sec", "⑤ 图片（客户端先压好，超限不收）"));
    sec.appendChild(_pfiEl("div", "pff-note",
        "压缩在本地完成（webp 优先）；每张都实时显示当前体积与上限。列表页只请求缩略图。"));
    sec.appendChild(_pfiSlotField("thumb", _PFI_SPEC.thumb));
    sec.appendChild(_pfiSlotField("avatar", _PFI_SPEC.avatar));
    sec.appendChild(_pfiSlotField("display", _PFI_SPEC.display));
    sec.appendChild(_pfiStickerSection());
    parent.appendChild(sec);
    _pfiS.mount = sec;
    return sec;
}

// ═══ 选图 ════════════════════════════════════════════
async function _pfiPick(slot, file) {
    const spec = _PFI_SPEC[slot];
    const info = _pfiS.els["info_" + slot];
    const seq = ++_pfiS.seq;
    if (info) { info.className = "pfi-info"; info.textContent = "正在压缩…"; }
    const out = await _pfiCompress(file, spec);
    if (seq !== _pfiS.seq) return;                    // 用户又换了一张：这次结果作废
    if (!out) {
        if (info) { info.className = "pfi-info bad"; info.textContent = "读取图片失败，请换一张（png/jpg/webp）"; }
        return;
    }
    if (out.tooBig) {
        _pfiSet(slot, "");                            // 超限 → 宁可不填，也不让提交后才发现
        if (info) {
            info.className = "pfi-info bad";
            info.textContent = "压到最小仍有 " + _pfiFmtSize(out.bytes) + "，超过 "
                + _pfiFmtSize(spec.maxBytes) + " 上限，请换一张更小的图";
        }
        return;
    }
    _pfiS.data[slot] = out.dataUrl;
    _pfiS.meta[slot] = {bytes: out.bytes, w: out.w, h: out.h};
    _pfiRenderSlot(slot);
}

async function _pfiPickStickers(files) {
    const room = _PFI_STK.maxCount - _pfiS.data.stickers.length;
    if (room <= 0) { showToast("表情包最多 " + _PFI_STK.maxCount + " 张"); return; }
    const take = files.slice(0, room);
    if (files.length > room) showToast("表情包最多 " + _PFI_STK.maxCount + " 张，多余的选择已忽略");
    const info = _pfiS.els.stkInfo;
    for (const f of take) {
        const seq = ++_pfiS.seq;
        if (info) { info.className = "pfi-info"; info.textContent = "正在压缩表情包…"; }
        const out = await _pfiCompress(f, _PFI_STK);
        if (seq !== _pfiS.seq) return;
        if (!out || out.tooBig) {
            if (info) {
                info.className = "pfi-info bad";
                info.textContent = "有表情包压到 "
                    + _pfiFmtSize(out && out.bytes) + "，超过 " + _pfiFmtSize(_PFI_STK.maxBytes)
                    + " 上限，已跳过（换更小的图）";
            }
            continue;
        }
        _pfiS.data.stickers.push({data: out.dataUrl, label: ""});
        _pfiS.stkMeta.push({bytes: out.bytes, w: out.w, h: out.h});
    }
    _pfiRenderStickers();
    if (info) {
        info.className = "pfi-info ok";
        info.textContent = "已加 " + _pfiS.data.stickers.length + " / " + _PFI_STK.maxCount + " 张";
    }
}

function _pfiSet(slot, dataUrl) {
    _pfiS.data[slot] = String(dataUrl || "");
    if (!_pfiS.data[slot]) _pfiS.meta[slot] = null;
    _pfiRenderSlot(slot);
}

function _pfiRenderSlot(slot) {
    const spec = _PFI_SPEC[slot];
    const img = _pfiS.els["img_" + slot], box = _pfiS.els["box_" + slot], info = _pfiS.els["info_" + slot];
    const data = _pfiS.data[slot];
    if (!img || !box || !info) return;
    if (!data) {
        img.removeAttribute("src");
        box.classList.remove("has");
        info.className = "pfi-info";
        info.textContent = "未选择（" + spec.hint + "）";
        return;
    }
    img.src = data;
    box.classList.add("has");
    const n = _pfiDataBytes(data);
    const from = (_pfiS.meta[slot] || {}).from;      // ★ 载入时自动压缩过 ⇒ 记着"压缩前"
    const over = n > spec.maxBytes;
    info.className = "pfi-info " + (over ? "bad" : "ok");
    info.textContent = _pfiFmtSize(n) + " / " + _pfiFmtSize(spec.maxBytes)
        + (from && from > n ? ("（" + _pfiFmtSize(from) + " → " + _pfiFmtSize(n) + "，已自动压缩）") : "")
        + "（" + (over ? "超过上限！" : "合格") + "）";
}

function _pfiRenderStickers() {
    const grid = _pfiS.els.stkGrid;
    if (!grid) return;
    grid.textContent = "";
    const n = _pfiS.data.stickers.length;
    if (_pfiS.els.stkcount) _pfiS.els.stkcount.textContent = n + " / " + _PFI_STK.maxCount;
    _pfiS.data.stickers.forEach((item, i) => {
        const url = String((item && item.data) || item || "");
        const cell = _pfiEl("div", "pfi-stk");
        const im = document.createElement("img");
        im.alt = "";
        im.src = url;
        const meta = _pfiS.stkMeta[i] || {};
        const kb = _pfiEl("div", "pfi-stkkb", _pfiFmtSize(meta.bytes || _pfiDataBytes(url)));
        const del = _pfiBtn("pfi-stkdel", "×", () => {
            _pfiS.data.stickers.splice(i, 1);
            _pfiS.stkMeta.splice(i, 1);
            _pfiRenderStickers();
            _pfiDirty();
        });
        del.setAttribute("aria-label", "删除第 " + (i + 1) + " 张表情包");
        // label：契约 §3.2 —— 建议填、≤12 字；一律 textContent（当输入框的 value，绝不当 HTML）
        const lab = document.createElement("input");
        lab.type = "text";
        lab.className = "pfi-stk-label";
        lab.maxLength = _PFI_STK.labelMax;
        lab.placeholder = "标签（建议填）";
        lab.title = "App 组织器按这个标签语义选图；留空 = 装上了也基本选不中";
        lab.value = String((item && item.label) || "");
        lab.addEventListener("input", () => {
            if (_pfiS.data.stickers[i]) _pfiS.data.stickers[i].label = lab.value.slice(0, _PFI_STK.labelMax);
            _pfiDirty();
        });
        cell.append(im, kb, del, lab);
        grid.appendChild(cell);
    });
}

function _pfiDirty() {
    try { _pfMarkDirty(); } catch (e) {}
}

/** data URL → Blob（迁移用：老草稿的图只以 data URL 形式在手）。 */
function _pfiDataUrlToBlob(url) {
    try {
        const s = String(url || "");
        const i = s.indexOf(",");
        const mime = (s.slice(0, i).match(/data:([^;]+)/) || [])[1] || "image/png";
        const bin = atob(s.slice(i + 1));
        const arr = new Uint8Array(bin.length);
        for (let k = 0; k < bin.length; k++) arr[k] = bin.charCodeAt(k);
        return new Blob([arr], {type: mime});
    } catch (e) { return null; }
}

/** data URL 的像素尺寸（拿不到返回 null；不抛）。 */
async function _pfiDims(dataUrl) {
    if (typeof createImageBitmap !== "function") return null;
    const blob = _pfiDataUrlToBlob(dataUrl);
    if (!blob) return null;
    let bmp = null;
    try {
        bmp = await createImageBitmap(blob);
        return {w: bmp.width, h: bmp.height};
    } catch (e) {
        return null;
    } finally {
        try { if (bmp) bmp.close(); } catch (e) {}
    }
}

/** ★ 载入路径的图片归一化（2026-10-01 用户实测：载入**已有卡/官方卡**时原图**直接透传**，
 *  UI 里 头像 54KB/30KB、详情图 338KB/300KB 全是超限红字 ⇒ 只能手改，等于"载入即坏"）。
 *
 *  这里对每个已有槽位走**与选图完全同一条**逐级压缩（`_pfiCompress` ⇒ imgzip + 逐级降尺寸/质量）：
 *    · 只在"超体积或超长边"时才压（合规的原样保留，不做无谓重编码）；
 *    · 压到合规 ⇒ 替换 + 记 `meta.from`（UI 显示「338KB → 187KB，已自动压缩」）；
 *    · 压不动 ⇒ 保留原图，交给 `_pfiValidate` 红字（人话）。
 *  返回逐槽位 {label, before, after, changed, ok}，供调用方汇成人话进度。 */
async function _pfiNormalizeLoaded() {
    const out = [];
    for (const slot of ["avatar", "thumb", "display"]) {
        const spec = _PFI_SPEC[slot];
        const cur = _pfiS.data[slot];
        if (!spec || !cur) continue;
        const before = _pfiDataBytes(cur);
        const dim = await _pfiDims(cur);
        const overSize = before > spec.maxBytes;
        const overSide = !!dim && Math.max(dim.w, dim.h) > spec.maxSide;
        if (!overSize && !overSide) {
            out.push({slot: slot, label: spec.label, before: before, after: before,
                      changed: false, ok: true});
            continue;
        }
        const blob = _pfiDataUrlToBlob(cur);
        let r = null;
        try { r = blob ? await _pfiCompress(blob, spec) : null; } catch (e) { r = null; }
        if (r && !r.tooBig && r.dataUrl) {
            _pfiS.data[slot] = r.dataUrl;
            _pfiS.meta[slot] = {bytes: r.bytes, w: r.w, h: r.h, from: before, auto: true};
            _pfiRenderSlot(slot);
            out.push({slot: slot, label: spec.label, before: before, after: r.bytes,
                      changed: true, ok: true, w: r.w, h: r.h});
        } else {
            out.push({slot: slot, label: spec.label, before: before, after: before,
                      changed: false, ok: false});
        }
    }
    // 表情包逐张（≤160px / ≤20KB），label 保留
    const keep = [];
    let i = 0;
    for (const it of _pfiS.data.stickers) {
        i++;
        const data = String((it && it.data) || it || "");
        const label = (it && it.label) || "";
        const lb = _PFI_STK.label + " " + i;
        const before = _pfiDataBytes(data);
        const dim = await _pfiDims(data);
        const overSize = before > _PFI_STK.maxBytes;
        const overSide = !!dim && Math.max(dim.w, dim.h) > _PFI_STK.maxSide;
        if (!overSize && !overSide) {
            keep.push({data: data, label: label});
            _pfiS.stkMeta[i - 1] = {bytes: before};
            out.push({slot: "sticker-" + i, label: lb, before: before, after: before,
                      changed: false, ok: true});
            continue;
        }
        const blob = _pfiDataUrlToBlob(data);
        let r = null;
        try { r = blob ? await _pfiCompress(blob, _PFI_STK) : null; } catch (e) { r = null; }
        if (r && !r.tooBig && r.dataUrl) {
            keep.push({data: r.dataUrl, label: label});
            _pfiS.stkMeta[i - 1] = {bytes: r.bytes, from: before, auto: true};
            out.push({slot: "sticker-" + i, label: lb, before: before, after: r.bytes,
                      changed: true, ok: true});
        } else {
            keep.push({data: data, label: label});
            out.push({slot: "sticker-" + i, label: lb, before: before, after: before,
                      changed: false, ok: false});
        }
    }
    _pfiS.data.stickers = keep;
    _pfiRenderStickers();
    return out;
}

/** 把归一化结果汇成一句人话（只列**压过**与**压不动**的）。 */
function _pfiNormalizeSummary(list) {
    const ch = (list || []).filter((x) => x.changed);
    const bad = (list || []).filter((x) => !x.ok);
    const bits = ch.map((x) => x.label + " " + _pfiFmtSize(x.before) + " → " + _pfiFmtSize(x.after));
    const bads = bad.map((x) => x.label + " " + _pfiFmtSize(x.after) + "（压不动）");
    if (!bits.length && !bads.length) return "";
    return (bits.length ? ("已自动压缩：" + bits.join("、")) : "")
           + (bads.length ? ((bits.length ? "；" : "") + "仍超限：" + bads.join("、")) : "");
}

/** ★ 老草稿迁移闸门（契约 06 §3.2 + 后端 CARD_MISSING_THUMB，2026-10-01）：
 *  V1 之前的草稿没有 `images.thumb`（老卡还会把大 cover 兜底成 thumb）⇒ 用户不改图直接保存
 *  会被后端 400 拦下。加载草稿后调用这里：用现有的一张图（display → avatar → 原 thumb）
 *  就地压一张 ≤320px/≤12KB 的 thumb 填进表单，并由调用方给人话提示。
 *  **只读现有图，不动用户别的数据**；一张图都没有时返回 false（交给必填校验给人话原因）。 */
async function _pfiEnsureLegacyThumb() {
    const cur = _pfiS.data.thumb;
    if (cur && _pfiDataBytes(cur) <= _PFI_SPEC.thumb.maxBytes) return false;   // 已经合规，无需迁移
    const src = _pfiS.data.display || _pfiS.data.avatar || cur || "";
    if (!src) return false;                                                    // 一张图都没有
    const blob = _pfiDataUrlToBlob(src);
    if (!blob) return false;
    const out = await _pfiCompress(blob, _PFI_SPEC.thumb);
    if (!out || out.tooBig || !out.dataUrl) return false;
    _pfiS.data.thumb = out.dataUrl;
    _pfiS.meta.thumb = {bytes: out.bytes, w: out.w, h: out.h};
    _pfiRenderSlot("thumb");
    return true;
}

// ═══ 读写 / 校验 ══════════════════════════════════════
function _pfiCollect() {
    const out = {avatar: "", thumb: "", display: "", stickers: []};
    for (const slot of ["avatar", "thumb", "display"]) {
        const d = _pfiS.data[slot];
        if (d) out[slot] = d;
    }
    // 契约 §3.2：stickers = [{data, label}]，label ≤12 字、可选（缺了合法，但 UI 提示"建议填"）
    out.stickers = _pfiS.data.stickers.slice(0, _PFI_STK.maxCount).map((it) => {
        const rec = {data: String((it && it.data) || it || "")};
        const lab = String((it && it.label) || "").trim().slice(0, _PFI_STK.labelMax);
        if (lab) rec.label = lab;
        return rec;
    }).filter(r => !!r.data);
    return out;
}

/** 回填：`images` 新形状；老草稿的 `cover`/`avatar` 字符串兜底（cover → 同时当 thumb/display）。
 *  stickers 两种都吃：`[{data,label}]`（新）与 `["data:…"]`（老）。 */
function _pfiLoad(card) {
    const c = card || {};
    const im = (c.images && typeof c.images === "object") ? c.images : {};
    const legacyCover = typeof c.cover === "string" ? c.cover : "";
    const legacyAvatar = typeof c.avatar === "string" ? c.avatar : "";
    _pfiS.data.avatar = String(im.avatar || legacyAvatar || "");
    _pfiS.data.thumb = String(im.thumb || legacyCover || "");
    _pfiS.data.display = String(im.display || legacyCover || "");
    _pfiS.data.stickers = (Array.isArray(im.stickers) ? im.stickers : [])
        .map((x) => (typeof x === "string" ? {data: x, label: ""}
                                           : {data: String((x && x.data) || ""),
                                              label: String((x && x.label) || "")}))
        .filter(x => !!x.data).slice(0, _PFI_STK.maxCount);
    _pfiS.meta = {avatar: null, thumb: null, display: null};
    _pfiS.stkMeta = _pfiS.data.stickers.map(() => ({}));
    for (const slot of ["avatar", "thumb", "display"]) _pfiRenderSlot(slot);
    _pfiRenderStickers();
}

function _pfiNew() {
    _pfiS.data = {avatar: "", thumb: "", display: "", stickers: []};
    _pfiS.meta = {avatar: null, thumb: null, display: null};
    _pfiS.stkMeta = [];
    for (const slot of ["avatar", "thumb", "display"]) _pfiRenderSlot(slot);
    _pfiRenderStickers();
}

/** 预检：thumb 必填（契约 §3.3「thumb 缺失视为校验不通过」）+ 各槽位体积复核。 */
function _pfiValidate() {
    for (const slot of ["avatar", "thumb", "display"]) {
        const d = _pfiS.data[slot];
        if (!d) continue;
        const n = _pfiDataBytes(d);
        if (n > _PFI_SPEC[slot].maxBytes) {
            return "「" + _PFI_SPEC[slot].label + "」有 " + _pfiFmtSize(n) + "，超过 "
                + _pfiFmtSize(_PFI_SPEC[slot].maxBytes) + " 上限，请换一张更小的图。";
        }
    }
    if (_pfiS.data.stickers.length > _PFI_STK.maxCount) {
        return "表情包最多 " + _PFI_STK.maxCount + " 张。";
    }
    for (const s of _pfiS.data.stickers) {
        const d = String((s && s.data) || s || "");
        const n = _pfiDataBytes(d);
        if (n > _PFI_STK.maxBytes) {
            return "有表情包 " + _pfiFmtSize(n) + "，超过 " + _pfiFmtSize(_PFI_STK.maxBytes) + " 上限。";
        }
        if (String((s && s.label) || "").length > _PFI_STK.labelMax) {
            return "表情包标签最长 " + _PFI_STK.labelMax + " 字。";
        }
    }
    if (!_pfiS.data.thumb) return "「列表缩略图」是必填的：广场列表只发它（≤320px / ≤12KB）。";
    return null;
}
