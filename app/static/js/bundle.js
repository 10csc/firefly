/* ═══════════════════════════════════════════
   流萤前端运行时 bundle（classic script）—— 本文件由 tools/build_frontend_bundle.py 生成，
   请勿手改！源码在 app/static/js/*.js（ES Module），改完跑该脚本重新生成。
   ═══════════════════════════════════════════ */

/* ── 来源：js/state.js ── */
// 共享状态与 DOM 引用（原 app.js 头部 + 跨模块可变状态 S）
// Firefly 聊天 App — 前端逻辑（统一前端 0.8.0：本地 / 服务器双模式一套代码）

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const SESSION_ID = "firefly-" + Date.now();

// 跨模块共享可变状态（ESM 导入绑定只读，跨模块赋值的 let 统一收口到 S）
const S = {
    _flushTimer: null,
    _hasMore: false,
    _hiddenEnabled: true,
    _hintTimer: null,
    _lastRenderTs: 0,
    _rendering: false,
    waiting: false,
};


/* ── 来源：js/util.js ── */
// 通用小工具：toast / HTML 转义（从各节抠出合并）
// 轻提示（3s 自动消失）
let toastTimer = null;
function showToast(msg) {
    let el = document.getElementById("app-toast");
    if (!el) {
        el = document.createElement("div");
        el.id = "app-toast";
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}
function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
/** 轻量 toast（页面顶部浮层，3 秒消失；不打断输入） */
function _toast(msg) {
    let t = document.getElementById("app-toast");
    if (!t) {
        t = document.createElement("div");
        t.id = "app-toast";
        t.style.cssText = "position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:999;background:rgba(28,30,46,.96);color:#e8e0d0;border:1px solid rgba(255,196,107,.45);border-radius:10px;padding:9px 16px;font-size:0.8em;max-width:86vw;text-align:center;box-shadow:0 6px 22px rgba(0,0,0,.45);display:none;pointer-events:none";
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.style.display = "none"; }, 3200);
}
function _esc(s) {
    return String(s == null ? "" : s)
        .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

// ═══ 本机 API Key 单点读取（服务器模式）═══
// 正式存储：firefly_providers（W1 多供应商，key 在 active 供应商的 api_key 字段）；
// 遗留兜底：firefly_api_key（旧版直接存此处 + W1 后的兼容镜像字段）。
// providers 优先是为了防「兼容镜像字段被覆写为空、但主存储还有 key」的偶发丢失
// （用户反馈：服务器模式偶发每轮对话要求重新设置 Key）。
function getLocalApiKey() {
    try {
        const list = JSON.parse(localStorage.getItem("firefly_providers") || "[]");
        const arr = Array.isArray(list) ? list : [];
        const active = localStorage.getItem("firefly_active_provider") || "";
        const p = arr.find(x => x && x.id === active) || arr[0] || null;
        if (p && p.api_key) return p.api_key;
    } catch (e) { /* 解析失败走 legacy 兜底 */ }
    try { return localStorage.getItem("firefly_api_key") || ""; } catch (e) { return ""; }
}

// ═══ 媒体本地存储（A2：服务器只传输不保存图片；本体存 WebView IndexedDB）═══
// 键 = 内容 sha256（服务器返回的 local:<sha256>.<ext> 引用）；Blob 存取。
let _idbPromise = null;
function _idb() {
    if (!_idbPromise) {
        _idbPromise = new Promise((res, rej) => {
            try {
                if (!window.indexedDB) { rej(new Error("IndexedDB 不可用")); return; }
                const req = indexedDB.open("firefly_media", 1);
                req.onupgradeneeded = () => {
                    const db = req.result;
                    if (!db.objectStoreNames.contains("media")) db.createObjectStore("media");
                };
                req.onsuccess = () => res(req.result);
                req.onerror = () => rej(req.error);
            } catch (e) { rej(e); }
        });
    }
    return _idbPromise;
}

async function idbSaveMedia(key, blob) {
    try {
        const db = await _idb();
        return await new Promise((res, rej) => {
            const tx = db.transaction("media", "readwrite");
            tx.objectStore("media").put(blob, key);
            tx.oncomplete = () => res(true);
            tx.onerror = () => rej(tx.error);
        });
    } catch (e) { return false; }
}

async function idbGetMedia(key) {
    try {
        const db = await _idb();
        return await new Promise((res) => {
            const tx = db.transaction("media", "readonly");
            const rq = tx.objectStore("media").get(key);
            rq.onsuccess = () => res(rq.result || null);
            rq.onerror = () => res(null);
        });
    } catch (e) { return null; }
}

/** 表情包图片源解析：local: 引用 → IndexedDB dataURL（无图返回 null=占位）；否则服务器资产 URL。 */
async function stickerSrc(file, isServer, apiBase) {
    if (!file) return null;
    if (String(file).startsWith("local:")) {
        const blob = await idbGetMedia(String(file).slice(6));
        if (!blob) return null;
        try { return URL.createObjectURL(blob); } catch (e) { return null; }
    }
    return (isServer ? apiBase : "") + "/assets/" + encodeURI(String(file));
}


/* ── 来源：js/session_crypto.js ── */
// 会话密钥 · 敏感请求头加密（见 docs/审计-服务器与开发版-2026-09-18.md §2.1）
//
// 背景：服务器只有明文 HTTP（不买域名/证书，用户明确约束），于是登录凭证与用户自己的
// API Key 在链路上裸奔 —— 同一 WiFi 下的人抓包即得。零成本对策：**应用层加密敏感头**。
//
// 做什么：把 `Authorization` / `X-API-Key` / `X-API-Base` 三个头的值装进一个信封，
// 用服务器公钥加密后放在 `X-Firefly-Enc` 里发出去；服务器用私钥解开。
// 抓包者看到的是密文（**能防"链路偷看"**；防重放需要时间戳+nonce，属第二步，本版不做）。
//
// 信封格式（`enc.v1.<b64url(key)>.<b64url(iv)>.<b64url(ct)>`）：
//   · key = RSA-2048 / OAEP-SHA256(公开指数 65537) 加密的 32 字节内容密钥
//   · iv  = 16 字节随机数（每次请求都换 ⇒ 同一内容两次的密文不同）
//   · ct  = 用 HMAC-SHA256 计数器流（keystream = HMAC(key, iv||counter_be32)）异或的明文
//
// 为什么用 HMAC 流而不是 AES：AES 在 JS 里只能靠 WebCrypto；而 WebView 在 file:// 页面
// （安卓服务器模式就是这个形态）**没有 crypto.subtle** —— 于是必须有纯 BigInt 的自实现兜底。
// HMAC 只需要 SHA-256，`crypto.subtle` 没有时可以用纯 JS SHA-256 顶上，两条路都短、都能测。
// 明文不加密时保持原样（服务器两种都收），旧客户端不受影响。
//
// ⚠️ 明文内容里**再带一份 SHA-256 摘要**：不是为了防篡改（没有 MAC），而是为了让"解错了"
//    变成一个**必然失败**而不是"碰巧解出一串乱码凭据"——服务器解出的 JSON 必须字段齐全
//    且 sha256 对得上，否则整条信封丢弃（退回明文，宁可不加密也不要用错凭据）。

const ENC_PREFIX = "enc.v1";

/** 服务器公钥（SHA-256 用）：由 tools/build_session_keys.py 生成，勿手改。 */
const SESSION_PUBKEY = {
    n_b64: "9KNHioGtk7fZxARe7cAaIUpdz/mDq33ZYbpGw0OusocCLL3EDmDVi2viOTOBmBVy3pDQSP1Jw4ZnKaj8Tav+fsQGsa+h8p1m88qrQf4mIEZTaJv7l7Q9+gh4P4unl4FioI4hF6MZitELRYKoJuuZMZ8WO81kVgembPFRRFyaqcTRzvrutCdBExqNoq0mn5YFSdMfB4jdBmI0T2DzbCKkmP0PtrnSbQredP5OBSZj6A6PJj6kaoPnBMvZwKuSYtbJp5W1oifuB5OqG+b45KbkWVZj5CzPx5c7HdxmSMJ1PLfIREtBz58S+zIkKM6nLrUZWt2CwMB64aGr1ozU0DpSZQ==",
    e: 65537,
};
const ENC_HEADER = "X-Firefly-Enc";
// 只有这几个头值得加密（值里有凭证或用户自填地址）；其余头保持明文，
// 免得把每个请求都拖进 RSA 运算（一次 BigInt 模幂 ~1ms，够用但没必要人人有份）
const SECRET_HEADERS = ["Authorization", "X-API-Key", "X-API-Base"];

const _enc = new TextEncoder();

// ── base64url ─────────────────────────────────────
function _b64u(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ── 纯 JS SHA-256（WebCrypto 不可用时兜底；只在 file:// WebView 上被用到）──
const _K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];

function _sha256Raw(bytes) {
    const l = bytes.length;
    const withPad = new Uint8Array((((l + 9) >> 6) + 1) << 6);
    withPad.set(bytes);
    withPad[l] = 0x80;
    const bitLen = l * 8;
    // 长度按 64 位大端写在末尾（JS 数字安全整数 2^53，够表示任何现实长度）
    withPad[withPad.length - 4] = (bitLen >>> 24) & 0xff;
    withPad[withPad.length - 3] = (bitLen >>> 16) & 0xff;
    withPad[withPad.length - 2] = (bitLen >>> 8) & 0xff;
    withPad[withPad.length - 1] = bitLen & 0xff;
    let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a,
        h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
    const w = new Array(64);
    for (let off = 0; off < withPad.length; off += 64) {
        for (let i = 0; i < 16; i++) {
            w[i] = (withPad[off + i * 4] << 24) | (withPad[off + i * 4 + 1] << 16)
                 | (withPad[off + i * 4 + 2] << 8) | (withPad[off + i * 4 + 3]);
        }
        for (let i = 16; i < 64; i++) {
            const x = w[i - 15], y = w[i - 2];
            const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
            const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
            w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
        }
        let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
        for (let i = 0; i < 64; i++) {
            const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
            const ch = (e & f) ^ (~e & g);
            const t1 = (h + S1 + ch + _K[i] + w[i]) | 0;
            const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
            const maj = (a & b) ^ (a & c) ^ (b & c);
            const t2 = (S0 + maj) | 0;
            h = g; g = f; f = e; e = (d + t1) | 0;
            d = c; c = b; b = a; a = (t1 + t2) | 0;
        }
        h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0; h3 = (h3 + d) | 0;
        h4 = (h4 + e) | 0; h5 = (h5 + f) | 0; h6 = (h6 + g) | 0; h7 = (h7 + h) | 0;
    }
    const out = new Uint8Array(32);
    [h0, h1, h2, h3, h4, h5, h6, h7].forEach((v, i) => {
        out[i * 4] = (v >>> 24) & 0xff; out[i * 4 + 1] = (v >>> 16) & 0xff;
        out[i * 4 + 2] = (v >>> 8) & 0xff; out[i * 4 + 3] = v & 0xff;
    });
    return out;
}

// ── HMAC-SHA256（keystream 与摘要共用；优先 WebCrypto，缺失时用纯 JS）──
let _subtleKey = null, _subtleKeyUse = null;

async function _hmac(keyBytes, data) {
    // 备注：`crypto.subtle` 在 file:// 页面不存在 —— 此时直接走纯 JS。
    try {
        const subtle = (typeof crypto !== "undefined" && crypto.subtle) ? crypto.subtle : null;
        if (subtle) {
            if (!_subtleKey || _subtleKeyUse !== keyBytes) {
                _subtleKey = await subtle.importKey(
                    "raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
                _subtleKeyUse = keyBytes;
            }
            const buf = await subtle.sign("HMAC", _subtleKey, data);
            return new Uint8Array(buf);
        }
    } catch (e) { /* 落回纯 JS */ }
    const block = 64;
    let k = keyBytes;
    if (k.length > block) k = _sha256Raw(k);
    const kp = new Uint8Array(block);
    kp.set(k);
    const ipad = new Uint8Array(block), opad = new Uint8Array(block);
    for (let i = 0; i < block; i++) {
        ipad[i] = kp[i] ^ 0x36;
        opad[i] = kp[i] ^ 0x5c;
    }
    const inner = new Uint8Array(block + data.length);
    inner.set(ipad); inner.set(data, block);
    const outer = new Uint8Array(block + 32);
    outer.set(opad); outer.set(_sha256Raw(inner), block);
    return _sha256Raw(outer);
}

async function _sha256(bytes) {
    try {
        const subtle = (typeof crypto !== "undefined" && crypto.subtle) ? crypto.subtle : null;
        if (subtle) {
            const buf = await subtle.digest("SHA-256", bytes);
            return new Uint8Array(buf);
        }
    } catch (e) { /* 落回纯 JS */ }
    return _sha256Raw(bytes);
}

async function _keystream(keyBytes, iv, len) {
    const out = new Uint8Array(len);
    let off = 0, counter = 0;
    while (off < len) {
        const block = new Uint8Array(iv.length + 4);
        block.set(iv);
        block[iv.length] = (counter >>> 24) & 0xff;
        block[iv.length + 1] = (counter >>> 16) & 0xff;
        block[iv.length + 2] = (counter >>> 8) & 0xff;
        block[iv.length + 3] = counter & 0xff;
        const ks = await _hmac(keyBytes, block);
        for (let i = 0; i < ks.length && off < len; i++, off++) out[off] = ks[i];
        counter++;
    }
    return out;
}

// ── RSA-OAEP(SHA-256) 公钥加密 ────────────────────
function _b64ToBytes(s) {
    const bin = atob(String(s).replace(/-/g, "+").replace(/_/g, "/"));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}

function _bytesToBigInt(bytes) {
    let hex = "";
    for (let i = 0; i < bytes.length; i++) hex += bytes[i].toString(16).padStart(2, "0");
    return BigInt("0x" + (hex || "0"));
}

function _bigIntToBytes(v, len) {
    let hex = v.toString(16);
    if (hex.length % 2) hex = "0" + hex;
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
    if (out.length === len) return out;
    if (out.length > len) throw new Error("整数超长");
    const pad = new Uint8Array(len);
    pad.set(out, len - out.length);
    return pad;
}

function _modPow(base, exp, mod) {
    let result = 1n, b = base % mod, e = exp;
    while (e > 0n) {
        if (e & 1n) result = (result * b) % mod;
        b = (b * b) % mod;
        e >>= 1n;
    }
    return result;
}

async function _mgf1(seed, len) {
    const out = new Uint8Array(len);
    let off = 0, counter = 0;
    while (off < len) {
        const input = new Uint8Array(seed.length + 4);
        input.set(seed);
        input[seed.length] = (counter >>> 24) & 0xff;
        input[seed.length + 1] = (counter >>> 16) & 0xff;
        input[seed.length + 2] = (counter >>> 8) & 0xff;
        input[seed.length + 3] = counter & 0xff;
        const h = await _sha256(input);
        for (let i = 0; i < h.length && off < len; i++, off++) out[off] = h[i];
        counter++;
    }
    return out;
}

async function _oaepEncrypt(msg, nB64, e) {
    const nBytes = _b64ToBytes(nB64);
    const k = nBytes.length;
    const n = _bytesToBigInt(nBytes);
    const hLen = 32;
    if (msg.length > k - 2 * hLen - 2) throw new Error("消息过长（OAEP 上限 " + (k - 2 * hLen - 2) + "B）");
    const lHash = await _sha256(new Uint8Array(0));
    const ps = new Uint8Array(k - msg.length - 2 * hLen - 2);
    const db = new Uint8Array(k - hLen - 1);
    db.set(lHash);
    db.set(ps, hLen);                       // ps 全 0，set 即写入
    db[hLen + ps.length] = 0x01;
    db.set(msg, hLen + ps.length + 1);
    const seed = new Uint8Array(hLen);
    (typeof crypto !== "undefined" && crypto.getRandomValues)
        ? crypto.getRandomValues(seed)
        : seed.set(_randomFallback(hLen));
    const dbMask = await _mgf1(seed, k - hLen - 1);
    for (let i = 0; i < db.length; i++) db[i] ^= dbMask[i];
    const seedMask = await _mgf1(db, hLen);
    for (let i = 0; i < seed.length; i++) seed[i] ^= seedMask[i];
    // ★ EM = 0x00 || maskedSeed || maskedDB（RFC 8017 §7.1.1 step 2(i)）
    //   这个前导 0x00 极易漏：第一版就漏了 —— 漏掉后 EM 成了 seed||DB（还少 1 字节），
    //   客户端自己"看着正常"，只有服务端 OAEP 会以 "EM 首字节非 0" 拒绝全部请求。
    //   抓住它的是 tests/test_session_crypto.py 的**跨语言往返**（不是文案断言）。
    const em = new Uint8Array(k);
    em[0] = 0x00;
    em.set(seed, 1);
    em.set(db, 1 + hLen);
    const m = _bytesToBigInt(em);
    if (m >= n) throw new Error("EM 超模数");
    return _bigIntToBytes(_modPow(m, BigInt(e), n), k);
}

/** 极少数环境连 getRandomValues 都没有时的兜底（Math.random 不可用于密钥，
 *  但这里只用于"至少不崩"；调用方在真正加密前会先确认 crypto 存在）。 */
function _randomFallback(n) {
    const out = new Uint8Array(n);
    for (let i = 0; i < n; i++) out[i] = Math.floor(Math.random() * 256);
    return out;
}

// ── 对外：把敏感头装进信封 ────────────────────────
/**
 * 从 headers 里挑出敏感头 → 加密 → 返回 {enc: "<信封>", rest: {…剩余明文头…}}。
 * 任何异常都返回 {enc: "", rest: 原样}（**加密失败不能挡住请求**，服务器两种都收）。
 */
async function encHead(headerPairs) {
    const secrets = {}, rest = {};
    for (const [name, value] of headerPairs) {
        if (!value) continue;
        if (SECRET_HEADERS.some(h => h.toLowerCase() === String(name).toLowerCase())) secrets[name] = value;
        else rest[name] = value;
    }
    const names = Object.keys(secrets);
    if (!names.length) return { enc: "", rest };
    try {
        const payload = _enc.encode(JSON.stringify({ v: 1, h: secrets }));
        const digest = await _sha256(payload);
        const body = new Uint8Array(payload.length + digest.length);
        body.set(payload); body.set(digest, payload.length);
        const key = new Uint8Array(32);
        const iv = new Uint8Array(16);
        if (typeof crypto === "undefined" || !crypto.getRandomValues) throw new Error("无安全随机源");
        crypto.getRandomValues(key);
        crypto.getRandomValues(iv);
        const ks = await _keystream(key, iv, body.length);
        const ct = new Uint8Array(body.length);
        for (let i = 0; i < body.length; i++) ct[i] = body[i] ^ ks[i];
        // 先取 SHA-256 明文摘要，再剥掉（服务器解出后再核一次）
        const wrappedKey = await _oaepEncrypt(key, SESSION_PUBKEY.n_b64, SESSION_PUBKEY.e);
        return {
            enc: [ENC_PREFIX, _b64u(wrappedKey), _b64u(iv), _b64u(ct)].join("."),
            rest,
        };
    } catch (e) {
        return { enc: "", rest: Object.assign({}, rest, secrets) };
    }
}


/* ── 来源：js/ui_select.js ── */
// 自绘下拉组件（ui_select）：替代原生 <select> 的系统弹窗
// 背景：安卓 WebView 的 <select> 点击弹系统级白色选项弹窗，页面 CSS 管不到（图1 实测）。
// 方案：原生 select 保留为值存储（display:none，所有既有 change 监听/取值代码零改动），
// 视觉层换成暗色「按钮 + 自绘弹层」。外观/键盘/触摸/点外关闭齐全。
// 用法：uiSelectEnhance(root=document) 扫描 select[data-ui] 或调用方指定选择器。

const _UI_SEL_KEY = "data-ui-select-enhanced";

function _closeAllPopups(except) {
    document.querySelectorAll(".ui-select-pop.show").forEach(p => {
        if (p !== except) p.classList.remove("show");
    });
}

// 全局关闭：点外 / Esc / 滚动（弹层跟随是 fixed，滚动时直接关，防错位）
document.addEventListener("pointerdown", e => {
    if (!e.target.closest(".ui-select-pop") && !e.target.closest(".ui-select-btn")) _closeAllPopups();
}, { capture: true });
document.addEventListener("keydown", e => { if (e.key === "Escape") _closeAllPopups(); });
window.addEventListener("scroll", () => _closeAllPopups(), { capture: true, passive: true });

function uiSelectEnhance(root) {
    (root || document).querySelectorAll("select").forEach(sel => {
        if (sel.hasAttribute(_UI_SEL_KEY)) return;
        sel.setAttribute(_UI_SEL_KEY, "1");
        sel.classList.add("ui-select-native");

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ui-select-btn";
        btn.setAttribute("aria-haspopup", "listbox");
        const label = document.createElement("span");
        label.className = "ui-select-label";
        const arrow = document.createElement("span");
        arrow.className = "ui-select-arrow";
        arrow.textContent = "▾";
        btn.append(label, arrow);

        const pop = document.createElement("div");
        pop.className = "ui-select-pop";
        pop.setAttribute("role", "listbox");

        const syncLabel = () => {
            const opt = sel.selectedOptions && sel.selectedOptions[0];
            label.textContent = opt ? opt.textContent : "";
        };
        const rebuild = () => {
            pop.innerHTML = "";
            [...sel.options].forEach(o => {
                const item = document.createElement("button");
                item.type = "button";
                item.className = "ui-select-opt" + (o.value === sel.value ? " on" : "");
                item.setAttribute("role", "option");
                item.textContent = o.textContent;
                item.addEventListener("click", () => {
                    sel.value = o.value;
                    sel.dispatchEvent(new Event("change", { bubbles: true }));
                    syncLabel(); rebuild(); _closeAllPopups();
                });
                pop.appendChild(item);
            });
        };
        btn.addEventListener("click", () => {
            const willOpen = !pop.classList.contains("show");
            _closeAllPopups();
            if (willOpen) {
                rebuild(); syncLabel();
                // 定位：fixed 贴按钮下缘；下方空间不足则翻上
                const r = btn.getBoundingClientRect();
                pop.style.minWidth = r.width + "px";
                pop.style.left = r.left + "px";
                pop.style.top = "";
                pop.style.bottom = "";
                pop.classList.add("show");
                const ph = pop.offsetHeight;
                if (r.bottom + ph + 8 > innerHeight && r.top - ph - 8 > 0) {
                    pop.style.top = (r.top - ph - 6) + "px";
                } else {
                    pop.style.top = (r.bottom + 6) + "px";
                }
            }
        });
        syncLabel();
        sel.insertAdjacentElement("afterend", btn);
        document.body.appendChild(pop);
        // 弹层随按钮销毁（本项目的 select 都是长驻元素，不销毁；防御性留个口）
        sel._uiSelectPopup = pop;
        sel._uiSelectBtn = btn;
        sel._uiSelectSync = () => { syncLabel(); rebuild(); };
    });
}

// 动态重建内容的 select（如 provider-select 会被 _renderProviderSelect 重写 options）：
// 调用方在重写后调 sel._uiSelectSync() 即可（settings.js 已接）。


/* ── 来源：js/imgzip.js ── */
// 图片压缩（发送前）：大图等比缩到单边 ≤1024 并重编码，控制上传/落盘体积。
// compressImage(file) → Promise<{blob, ext, wasCompressed}>
//   - GIF 原样返回（动帧过 canvas 会丢）；
//   - 单边 ≤1024 且 ≤300KB 原样返回（够小不折腾）；
//   - 否则 canvas 等比缩到单边 1024：JPEG / 无透明通道 PNG → image/jpeg q0.85；
//     有透明通道的 PNG 保持 image/png（只缩放，不丢透明）。
// 降级兜底：任何一步失败都原样返回 + console.warn（压缩是优化，不是发送门槛）。
const IMGZIP_MAX_SIDE = 1024;
const IMGZIP_SKIP_BYTES = 300 * 1024;

/** 从 MIME/文件名推扩展名（压缩失败原样返回时用） */
function _imgzipExt(file) {
    const m = /^image\/(png|jpe?g|webp|gif)$/.exec((file && file.type) || "");
    if (m) return m[1] === "jpeg" ? "jpg" : m[1];
    const n = ((file && file.name) || "").split(".").pop().toLowerCase();
    return /^[a-z0-9]{2,5}$/.test(n) ? n : "jpg";
}

/** 抽样检测画布是否含透明像素（每 16 像素采一个，够判透明 PNG） */
function _imgzipHasAlpha(ctx, w, h) {
    try {
        const data = ctx.getImageData(0, 0, w, h).data;
        for (let i = 3; i < data.length; i += 4 * 16) {
            if (data[i] < 255) return true;
        }
    } catch (e) {}
    return false;
}

async function compressImage(file) {
    const fallback = { blob: file, ext: _imgzipExt(file), wasCompressed: false };
    try {
        if (!file || typeof createImageBitmap !== "function") return fallback;
        if (file.type === "image/gif") return fallback;   // GIF 原样
        const bmp = await createImageBitmap(file);
        try {
            const w = bmp.width, h = bmp.height;
            if (!w || !h) return fallback;
            const needResize = Math.max(w, h) > IMGZIP_MAX_SIDE;
            if (!needResize && file.size <= IMGZIP_SKIP_BYTES) return fallback;
            const scale = needResize ? IMGZIP_MAX_SIDE / Math.max(w, h) : 1;
            const cw = Math.max(1, Math.round(w * scale));
            const ch = Math.max(1, Math.round(h * scale));
            const canvas = document.createElement("canvas");
            canvas.width = cw;
            canvas.height = ch;
            const ctx = canvas.getContext("2d");
            // 先画一次取 alpha：有透明才保留 png，否则转 jpeg 时白底重绘
            ctx.drawImage(bmp, 0, 0, cw, ch);
            const keepPng = file.type === "image/png" && _imgzipHasAlpha(ctx, cw, ch);
            if (!keepPng) {
                ctx.fillStyle = "#ffffff";
                ctx.fillRect(0, 0, cw, ch);
                ctx.drawImage(bmp, 0, 0, cw, ch);
            }
            const blob = await new Promise(res => {
                if (keepPng) canvas.toBlob(res, "image/png");
                else canvas.toBlob(res, "image/jpeg", 0.85);
            });
            // toBlob 为空/失败：原样返回（压缩失败不等于发送失败）
            if (!blob) return fallback;
            if (!needResize && !keepPng && blob.size >= file.size) return fallback;
            return { blob, ext: keepPng ? "png" : "jpg", wasCompressed: true };
        } finally {
            try { bmp.close(); } catch (e) {}
        }
    } catch (e) {
        console.warn("compressImage 压缩失败，原样返回", e);
        return fallback;
    }
}


/* ── 来源：js/api.js ── */
// 双模式与请求封装：FIREFLY_MODE / fetch 鉴权注入 / 登录与注册表单

// ═══════════════════════════════════════════
// 双模式（0.8.0）
// ═══════════════════════════════════════════
// FIREFLY_MODE：'local'（默认完全本地）/ 'server'（服务器后端处理）
// 来源：config.js（先于本文件加载）——
//   - 本地（PC 浏览器 / 安卓内置引擎）：app/static/config.js → local
//   - 服务器网页：server/frontend/config.js 定义 FIREFLY_SERVER_BASE → server
//   - 安卓服务器模式（file:// 加载）：壳拦截 config.js 请求动态注入
// 差异点：
// 1. server：登录态 Bearer token（30 天）+ 数据按 user_id 隔离；local：无账号概念
// 2. server：API Key 存本机浏览器（localStorage），每请求带 X-API-Key（服务器不落盘）；
//    local：Key 存本地后端 config.json（POST /set-config）
// 3. server：API 跨域到 FIREFLY_SERVER_BASE（file:// 页面）；local：同源相对请求
// 4. server：relay 引擎代发 LLM + 资产本地化；local：后端 direct 直发
// 5. 检查更新：server 读服务器 version.json；local 走 GitHub/Gitee（后端优先）
const FIREFLY_MODE = window.FIREFLY_MODE || (window.FIREFLY_SERVER_BASE ? "server" : "local");
const IS_SERVER = FIREFLY_MODE === "server";
const API_BASE = IS_SERVER ? (window.FIREFLY_SERVER_BASE || "") : "";
const _serverFetch = window.fetch;

// 服务器模式：账号 + Key 请求头注入（本地模式原样直通，同源无跨域）
//
// ★ 2026-09-25 敏感头应用层加密（见 docs/审计-服务器与开发版-2026-09-18.md §2.1）：
//   全站明文 HTTP 的零成本对策 —— `Authorization` / `X-API-Key` / `X-API-Base` 三个头的
//   值用服务器公钥加密后放进 `X-Firefly-Enc`，链路上看不到明文 token/Key。
//   实现要点：
//     · 加密是**异步**的（crypto.subtle / BigInt 模幂）⇒ 本包装器改成 `async` 返回 Promise
//       （调用方本来就在 await / .then，语义不变）。
//     · **加密失败绝不放行加密头但漏掉明文头**：失败时按原样发明文（旧客户端行为），
//       服务器两种情况都收 —— 宁可暂时退回明文，也不能让用户登不进去。
//     · 只对**服务器模式**加密：本地版同源 127.0.0.1，加解密纯属浪费。
window.fetch = async function (url, opts) {
    if (!IS_SERVER) return _serverFetch(url, opts);
    opts = opts || {};
    const headers = new Headers(opts.headers || {});
    let k = getLocalApiKey();   // 单点读取：providers 优先 + legacy 兜底（防偶发“每轮需重设 Key”）
    let b = ""; try { b = localStorage.getItem("firefly_api_base") || ""; } catch (e) {}
    let src = ""; try { src = localStorage.getItem("firefly_api_source") || ""; } catch (e) {}
    if (src === "proxy") {
        // 托管模式：不传用户 Key，标记服务器用运营者 Key 直发（OpenCode Go）
        headers.set("X-API-Mode", "proxy");
    } else {
        if (k) headers.set("X-API-Key", k);
        if (b) headers.set("X-API-Base", b);
    }
    // 服务器版账号：登录态带 Bearer token（Key 仍只存本机，token 是账号会话）
    let t = ""; try { t = localStorage.getItem("firefly_token") || ""; } catch (e) {}
    if (t) headers.set("Authorization", "Bearer " + t);
    // ── 敏感头加密（失败退回明文，见上方说明）──
    try {
        const pairs = [];
        SECRET_HEADERS.forEach(function (name) {
            const v = headers.get(name);
            if (v) pairs.push([name, v]);
        });
        if (pairs.length) {
            const r = await encHead(pairs);
            if (r && r.enc) {
                SECRET_HEADERS.forEach(function (name) { headers.delete(name); });
                headers.set(ENC_HEADER, r.enc);
            }
        }
    } catch (e) { /* 保持明文 —— 服务器两种都收 */ }
    // 相对路径 → 服务器绝对 URL（本地 file:// 页面无同源相对路径）
    let fullUrl = String(url);
    if (fullUrl.startsWith("/")) fullUrl = API_BASE + fullUrl;
    opts = Object.assign({}, opts, { headers: headers });
    const resp = await _serverFetch(fullUrl, opts);
    // 401：登录失效/未登录。仅对用户主动操作（/chat）提示并亮出登录模块；
    // 后台轮询端点（proactive-status/config/history/relay 等）静默——否则
    // 未登录时「请先登录后使用」toast 每 10s 弹一次刷屏。
    if (resp.status === 401 && String(url).indexOf("/chat") >= 0 && !String(url).includes("/auth/")) {
        try { showToast("请先登录后使用"); } catch (e) {}
        try { showAuthModule(); } catch (e) {}
    }
    return resp;
};

// API 来源切换：托管模式隐藏 Key/供应商输入，显示隐私提示
function applyApiSource(isProxy) {
    const ownFields = document.getElementById("api-own-fields");
    const providerField = document.getElementById("provider-field");
    const tip = document.getElementById("api-proxy-tip");
    if (ownFields) ownFields.style.display = isProxy ? "none" : "";
    if (providerField) providerField.style.display = isProxy ? "none" : "";
    if (isProxy) {
        // 托管模式：供应商/K 由服务器运营方指定，前端不管理
        const modelCustom = document.getElementById("model-custom");
        if (modelCustom) modelCustom.style.display = "none";
    }
    if (tip) tip.style.display = isProxy ? "block" : "none";
}

// ═══ 登录状态模块（双模式：服务器版 localStorage token / 本地版后端 auth.json）═══
function showAuthModule() {
    const mod = document.getElementById("auth-module");
    if (mod) mod.style.display = "block";
}
function initAuth() {
    initAuthForms();          // 内联登录/注册/重置表单接线（幂等；本地模式走后端 /auth/* 代理）
    const loginEntry = document.getElementById("auth-login-entry");
    const userEntry = document.getElementById("auth-user-entry");
    if (IS_SERVER) {
        const token = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
        if (!token) {
            showAuthModule();
            if (loginEntry) loginEntry.style.display = "flex";
            if (userEntry) userEntry.style.display = "none";
            return;
        }
        fetch("/auth/me").then(r => r.json()).then(d => {
            if (d.error) {
                try { localStorage.removeItem("firefly_token"); } catch (e) {}
                if (loginEntry) loginEntry.style.display = "flex";
                if (userEntry) userEntry.style.display = "none";
            } else {
                const emailEl = document.getElementById("auth-email");
                const meta = document.getElementById("auth-meta");
                if (emailEl) emailEl.textContent = "邮箱 " + (d.email || "");
                if (meta) meta.textContent = "注册于 " + (d.created_at || "-").slice(0, 10);
                if (loginEntry) loginEntry.style.display = "none";
                if (userEntry) userEntry.style.display = "flex";
                initAssets();   // 登录态确认：资产本地化（relay 代发前占位符填充用）
            }
            showAuthModule();
        }).catch(() => {});
        return;
    }
    // 本地版（A7）：登录态由本地后端持有（auth.json）；/auth/state 隐式 verify（滚动续期）
    fetch("/auth/state").then(r => r.json()).then(d => {
        showAuthModule();
        if (d.logged_in) {
            const emailEl = document.getElementById("auth-email");
            const meta = document.getElementById("auth-meta");
            if (emailEl) emailEl.textContent = "邮箱 " + (d.email || "");
            if (meta) meta.textContent = d.offline_ok ? "已登录 · 云端同步就绪" : "登录已过期，请重新登录";
            if (loginEntry) loginEntry.style.display = "none";
            if (userEntry) userEntry.style.display = "flex";
            try { window.autoSyncNow && window.autoSyncNow(); } catch (e) {}   // 登录态确认：补一次云端同步（10 分钟节流内自动跳过）
        } else {
            if (loginEntry) loginEntry.style.display = "flex";
            if (userEntry) userEntry.style.display = "none";
        }
    }).catch(() => { /* 本地后端未就绪：静默（首次启动拉服务时） */ });
}
function logout() {
    const t = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
    if (t) fetch("/auth/logout", {method: "POST"}).catch(() => {});
    try { localStorage.removeItem("firefly_token"); } catch (e) {}
    location.reload();
}
window.logout = logout;   // 内联 onclick（账号卡片「退出登录」）

// ═══ 内联登录/注册/重置表单（0.8.0：单页完成，不跳转 login.html）═══
function toggleAuthForms() {
    const forms = document.getElementById("auth-forms");
    const btn = document.getElementById("auth-toggle-btn");
    if (!forms) return;
    const show = forms.style.display === "none";
    forms.style.display = show ? "block" : "none";
    if (btn) btn.textContent = show ? "收起 ▴" : "登录 / 注册 ▾";
}
window.toggleAuthForms = toggleAuthForms;

function initAuthForms() {
    // A7：本地版同样接线（表单请求走本地后端 /auth/* 代理 → 认证服务器）
    const $ = id => document.getElementById(id);
    const LOGIN = $("loginForm"), REG = $("registerForm"), RESET = $("resetForm");
    if (!LOGIN || !REG || !RESET || LOGIN.dataset.wired) return;
    LOGIN.dataset.wired = "1";

    const showErr = (el, msg) => { el.textContent = msg; el.style.display = "block"; el.classList.remove("green"); };
    const showOk = (el, msg) => { el.textContent = msg; el.style.display = "block"; el.classList.add("green"); };
    const hideErr = el => { el.style.display = "none"; };
    const qqRe = /^[^@\s]+@(qq\.com|foxmail\.com)$/;

    // 安装隐藏代码：首次访问生成，一个安装一个（注册门槛；crypto 随机防预测）
    const getInstallId = () => {
        try {
            let id = localStorage.getItem("firefly_install_id");
            if (!id) {
                const rand = () => {
                    const buf = new Uint32Array(1);
                    crypto.getRandomValues(buf);
                    return buf[0].toString(16).padStart(8, "0");
                };
                id = "inst-" + (rand() + rand() + rand() + rand());
                localStorage.setItem("firefly_install_id", id);
            }
            return id;
        } catch (e) { return ""; }
    };
    const startCountdown = btn => {
        let sec = 60;
        btn.textContent = sec + "s";
        btn.disabled = true;
        const t = setInterval(() => {
            sec--;
            if (sec <= 0) { clearInterval(t); btn.textContent = "获取验证码"; btn.disabled = false; }
            else btn.textContent = sec + "s";
        }, 1000);
    };

    // 表单切换（表单外的链接按钮独立显隐，与 login.html 同语义）
    $("toRegister").onclick = () => { LOGIN.style.display = "none"; REG.style.display = "block"; $("toLogin").style.display = "block"; };
    $("toLogin").onclick = () => { REG.style.display = "none"; LOGIN.style.display = "block"; $("toLogin").style.display = "none"; };
    $("toReset").onclick = () => {
        LOGIN.style.display = "none"; RESET.style.display = "block";
        $("toRegister").style.display = "none"; $("toReset").style.display = "none"; $("toLogin2").style.display = "block";
    };
    $("toLogin2").onclick = () => {
        RESET.style.display = "none"; LOGIN.style.display = "block";
        $("toLogin2").style.display = "none"; $("toRegister").style.display = "block"; $("toReset").style.display = "block";
    };

    // 登录
    LOGIN.onsubmit = e => {
        e.preventDefault(); hideErr($("loginErr"));
        fetch("/auth/login", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: $("loginEmail").value.trim(), password: $("loginPass").value, device: "app"})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                localStorage.setItem("firefly_token", d.token);
                location.reload();
            } else showErr($("loginErr"), d.error || "登录失败");
        }).catch(() => showErr($("loginErr"), "网络错误"));
    };

    // 注册：获取邮箱验证码（60s 倒计时）
    $("sendCodeBtn").onclick = () => {
        const email = $("regEmail").value.trim();
        const btn = $("sendCodeBtn");
        hideErr($("regErr"));
        if (!qqRe.test(email)) { showErr($("regErr"), "请使用 QQ 邮箱"); return; }
        if (btn.disabled) return;
        btn.disabled = true;
        fetch("/auth/mail-send", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: email})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                showOk($("regErr"), "验证码已发送，请查收邮箱");
                startCountdown(btn);
            } else {
                btn.disabled = false;
                showErr($("regErr"), d.error || "发送失败");
            }
        }).catch(() => { btn.disabled = false; showErr($("regErr"), "网络错误"); });
    };

    // 注册提交
    REG.onsubmit = e => {
        e.preventDefault(); hideErr($("regErr"));
        fetch("/auth/register", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: $("regEmail").value.trim(), password: $("regPass").value,
                qq_group: $("regGroup").value.trim(), mail_code: $("regCode").value.trim(),
                install_id: getInstallId()})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                // 注册成功 → 自动切回登录表单并预填邮箱
                REG.style.display = "none"; LOGIN.style.display = "block";
                $("toLogin").style.display = "none";
                $("loginEmail").value = $("regEmail").value.trim();
                $("loginPass").value = "";
                showOk($("loginErr"), "注册成功，请登录");
                $("loginPass").focus();
            } else showErr($("regErr"), d.error || "注册失败");
        }).catch(() => showErr($("regErr"), "网络错误"));
    };

    // 忘记密码：发送重置验证码
    $("rstSendBtn").onclick = () => {
        const email = $("rstEmail").value.trim();
        const btn = $("rstSendBtn");
        hideErr($("rstErr"));
        if (!qqRe.test(email)) { showErr($("rstErr"), "请使用 QQ 邮箱"); return; }
        if (btn.disabled) return;
        btn.disabled = true;
        fetch("/auth/reset-send", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: email})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                showOk($("rstErr"), "验证码已发送（若该邮箱已注册），请查收");
                startCountdown(btn);
            } else {
                btn.disabled = false;
                showErr($("rstErr"), d.error || "发送失败");
            }
        }).catch(() => { btn.disabled = false; showErr($("rstErr"), "网络错误"); });
    };

    // 重置密码提交
    RESET.onsubmit = e => {
        e.preventDefault(); hideErr($("rstErr"));
        fetch("/auth/reset-password", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email: $("rstEmail").value.trim(), code: $("rstCode").value.trim(),
                password: $("rstPass").value})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                RESET.style.display = "none"; LOGIN.style.display = "block";
                $("toLogin2").style.display = "none"; $("toRegister").style.display = "block"; $("toReset").style.display = "block";
                $("loginEmail").value = $("rstEmail").value.trim();
                $("loginPass").value = "";
                showOk($("loginErr"), "密码已重置，请用新密码登录");
                $("loginPass").focus();
            } else showErr($("rstErr"), d.error || "重置失败");
        }).catch(() => showErr($("rstErr"), "网络错误"));
    };
}


/* ── 来源：js/panels.js ── */
// 面板族：菜单抽屉 / 各面板开关壳 / 头像选择 / 状态 / 收藏 / 请求记录 / 流程日志 / 用户记忆 / 设定文件 / 手账 / 表情包管理（阶段 2.5 拆分后 = 面板外壳）

// 开拓者头像
const TB_AVATARS = { 穹: "开拓者_穹.png", 星: "开拓者_星.png" };
let tbChoice = localStorage.getItem("tb_avatar") || "穹";

function openAvatarPicker() {
    const picker = document.getElementById("avatar-picker");
    const mask = document.getElementById("avatar-picker-mask");
    picker.style.display = "block";
    mask.style.display = "block";
    // 高亮当前选择
    document.querySelectorAll(".avatar-option").forEach(opt => {
        opt.classList.toggle("selected", opt.dataset.key === tbChoice);
    });
}
function closeAvatarPicker() {
    document.getElementById("avatar-picker").style.display = "none";
    document.getElementById("avatar-picker-mask").style.display = "none";
}
window.closeAvatarPicker = closeAvatarPicker;
document.querySelectorAll(".avatar-option").forEach(opt => {
    opt.addEventListener("click", () => {
        tbChoice = opt.dataset.key;
        localStorage.setItem("tb_avatar", tbChoice);
        document.querySelectorAll(".tb-avatar").forEach(el => { el.src = TB_AVATARS[tbChoice]; });
        closeAvatarPicker();
    });
});

// ═══════════════════════════════════════════
// 汉堡菜单
// ═══════════════════════════════════════════
const menuBtn = document.getElementById("menu-btn");
const menuDrawer = document.getElementById("menu-drawer");
const menuOverlay = document.getElementById("menu-overlay");

menuBtn.addEventListener("click", openMenu);
menuOverlay.addEventListener("click", closeMenu);
function openMenu() {
    menuDrawer.classList.add("open");
    menuOverlay.classList.add("show");
    // 默认 tab 是设定文件（DOM active），无点击事件，需主动加载
    // （2026-09-18：loadCharFiles 已随「用户设定」编辑器一起去掉——它属角色卡管理域）
    loadJournal(); loadUserMemory(); loadArchive();
}
function closeMenu() {
    menuDrawer.classList.remove("open");
    menuOverlay.classList.remove("show");
}
window.closeMenu = closeMenu;

// ═══════════════════════════════════════════
// 设置面板（首页 ⚙ 打开，API 配置独立于此）
// ═══════════════════════════════════════════
const settingsPanel = document.getElementById("settings-panel");
function openSettings() {
    settingsPanel.classList.add("show");
    loadConfig();
    try { loadSnapshots(); } catch (e) {}   // 快照列表（chat.js 同作用域函数）
    try { window.btActivate && window.btActivate("mine"); } catch (e) {}
}
function closeSettings() { settingsPanel.classList.remove("show"); }
window.openSettings = openSettings;
window.closeSettings = closeSettings;

// 反馈面板（首页 ✉ 打开）
const feedbackPanel = document.getElementById("feedback-panel");
function openFeedback() { feedbackPanel.classList.add("show"); }
function closeFeedback() { feedbackPanel.classList.remove("show"); }
window.openFeedback = openFeedback;
window.closeFeedback = closeFeedback;

// 点击 drawer 背景（非内容区域）也关闭菜单
menuDrawer.addEventListener("click", (e) => {
    if (e.target === menuDrawer) closeMenu();
});

// 菜单 tab 切换
document.querySelectorAll(".menu-tab").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".menu-tab").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".menu-content").forEach(c => c.classList.remove("active"));
        btn.classList.add("active");
        const target = document.getElementById("tab-" + btn.dataset.tab);
        if (target) target.classList.add("active");
        if (btn.dataset.tab === "char") { loadJournal(); loadUserMemory(); loadArchive(); }
        if (btn.dataset.tab === "state") loadStateTab();
        if (btn.dataset.tab === "fav") loadFavorites();
        if (btn.dataset.tab === "log") loadRequestLog();
        if (btn.dataset.tab === "pipeline") loadPipeline();
    });
});

// ═══════════════════════════════════════════
// 状态 tab（数值状态系统已下线，接回后再渲染条）
// ═══════════════════════════════════════════


/* ── 来源：js/panels/packs.js ── */
// 角色包面板：表情包管理 + 包详情页（人设文案 / 主动消息 / 专属表情包 / 头像封面）
// 阶段 2.5 自 panels.js 拆出。包详情页的写回一律用 _packViewMode（F-3 代际保护）。



// （A7c 运行模式切换已删除：本地优先 + 失败自动回落服务器——保留此注记防旧代码复活）

// ═══ 角色编辑页（全屏 #pack-view：封面横幅 + 大头像 + 人设文案编辑 + 自建角色删除）═══
const _PACK_PROMPT_LABELS = {
    "core.md": "核心设定（身份/经历/价值观）",
    "identity.md": "人际关系与认知边界",
    "sms_samples.md": "短信风格示例",
    "prompts/polisher.md": "回复器人设（核心文案）",
    "prompts/analyzer_extra.md": "分析器·剧本事实核查补充",
    "prompts/organizer_sticker.md": "组织器·表情包调度提示词",
    "prompts/organizer_narration.md": "组织器·旁白生成提示词",
    "prompts/proactive_context.md": "主动消息·情境文案",
    "prompts/env_suffix.md": "环境句·世界后缀",
};

// F-3（2026-09-13）包详情页的两个捕获量：
// - _packViewMode：本页展示的是哪个包。所有写回一律用它，**不读实时的 CURRENT_MODE** ——
//   原来 A→B 快速切换后，A 页残留的"保存"会把 A 的文案写进 B。
// - _packViewGen：进入本页时的模式代际，await 后模式已切换就丢弃本次结果。
let _packViewMode = "";
let _packViewGen = -1;

async function loadPackView() {
    const mode = CURRENT_MODE;      // 捕获：本次渲染/写回都属于这个包
    const gen = _modeGen;
    _packViewMode = mode;
    _packViewGen = gen;
    let data;
    try {
        const resp = await fetch(`/pack-files?mode=${encodeURIComponent(mode)}`);
        data = await resp.json();
    } catch (e) {
        console.warn("[packs] 加载角色包失败", e);
        showToast("角色卡加载失败：连不上本机服务，请稍后重试");
        return;
    }
    if (gen !== _modeGen) return;   // 模式已切换：丢弃本次结果，防止 A 的内容渲染进 B 的页面
    if (!data || !data.files) return;

    const presLabel = {sticker: "短信+表情包", narration: "短信+旁白", none: "纯短信"}[data.presentation] || data.presentation;
    const t = Date.now();
    const coverEl = document.getElementById("pv-cover");
    // F-5：无封面/头像的包必须清掉旧 src——否则详情页沿用上一个包的图（"看起来还是流萤"）
    if (coverEl) {
        if (data.assets && data.assets.cover) coverEl.src = data.assets.cover + "?t=" + t;
        else coverEl.removeAttribute("src");
    }
    const avatarEl = document.getElementById("pv-avatar");
    if (avatarEl) {
        if (data.assets && data.assets.avatar) avatarEl.src = data.assets.avatar + "?t=" + t;
        else avatarEl.removeAttribute("src");
    }
    document.getElementById("pv-name").textContent = "";
    document.getElementById("pv-scene").textContent = data.name || data.mode;
    document.getElementById("pv-pres").textContent = presLabel;
    document.getElementById("pv-tagline").textContent = "";
    // 角色名/签名从注册表取（经 views.js 的 window 桥——bundle 拼接后 import() 会产生第二份模块实例）
    try {
        const mods = (window.__getPresets && window.__getPresets()) || [];
        const p = mods.find(m => m.id === data.mode) || {};
        document.getElementById("pv-name").textContent = p.char_name || data.name || data.mode;
        document.getElementById("pv-tagline").textContent = p.tagline || "";
    } catch (e) {}
    // 封面/头像编辑按钮绑定
    document.getElementById("pv-cover-edit").onclick = () => _packAssetUpload("cover");
    document.getElementById("pv-avatar-edit").onclick = () => _packAssetUpload("avatar");
    // ★「发布到广场」（2026-10-01 用户点名要）：把**这张**角色卡载入制卡流程。
    //   按钮动态建、模式绑 `_packViewMode`（本页的 F-3 代际纪律：写回一律用它，
    //   **不读**实时的当前模式——那个字面量在本文件里只允许出现一次，见 E/A6 守卫）。
    try {
        const ops = document.querySelector("#pack-view .pv-asset-ops");
        if (ops && !document.getElementById("pv-publish-btn")) {
            const pb = document.createElement("a");
            pb.id = "pv-publish-btn";
            pb.setAttribute("type", "button");
            pb.title = "把这张角色卡做成广场卡（存草稿 → 提交审核 → 发布）";
            pb.textContent = "发布到广场 →";
            pb.onclick = () => {
                if (window.plazaPublishLocalMode) window.plazaPublishLocalMode(_packViewMode);
            };
            ops.appendChild(pb);
        }
    } catch (e) { /* 按钮是锦上添花：失败不影响详情页 */ }

    // 主动消息区填值
    const pro = data.proactive || {};
    const setChk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    setChk("pv-pro-enabled", pro.enabled);
    setChk("pv-pro-prob", pro.pro_enabled);
    setChk("pv-pro-hidden", pro.hidden_enabled);
    // 后台主动消息的客户端门控跟随当前角色卡的「隐藏式」开关（服务端不含该判断）
    S._hiddenEnabled = !!pro.hidden_enabled;
    const hardEl = document.getElementById("pv-pro-hard");
    const softEl = document.getElementById("pv-pro-soft");
    if (hardEl) { hardEl.value = pro.hard ?? 6; document.getElementById("pv-pro-hard-v").textContent = hardEl.value; }
    if (softEl) { softEl.value = Math.round((pro.soft ?? 0.35) * 100); document.getElementById("pv-pro-soft-v").textContent = softEl.value + "%"; }

    _loadPackStickers(mode);

    // 用户形象区（05）：称呼 + 用户头像，数据来自 /modes（经 __getPresets 桥）
    try {
        const mods = (window.__getPresets && window.__getPresets()) || [];
        const pm = mods.find(m => m.id === mode) || {};
        const nameInp = document.getElementById("pv-user-name");
        if (nameInp) nameInp.value = pm.user_name || "";
        const uav = document.getElementById("pv-user-avatar");
        if (uav) {
            // 预览回落链：包头像 → 全局内置形象（穹/星）——空 src 会显示破图+alt，必须给兜底
            const tbNow = (() => { try { return localStorage.getItem("tb_avatar") || "穹"; } catch (e) { return "穹"; } })();
            uav.src = pm.user_avatar ? pm.user_avatar + "?t=" + t : TB_AVATARS[tbNow];
            document.querySelectorAll(".pv-id-choice").forEach(x => x.classList.toggle("on", !pm.user_avatar && x.dataset.key === tbNow));
        }
        const resetLink = document.getElementById("pv-user-avatar-reset");
        if (resetLink) resetLink.style.display = pm.user_avatar ? "" : "none";
    } catch (e) {}
    try { loadPackTree(mode); } catch (e) {}   // 角色卡管理树（数据驱动，2026-09-15）

    // 角色形象「恢复默认」—— 2026-09-19 从"危险区"挪到顶部大图旁。
    // 理由：它只是撤销用户自己换的图，**不是危险操作**；而官方包又不能删除，
    // 于是"危险区"对官方包既没内容也没存在理由。形象相关的操作现在全在顶部一处。
    for (const [slot, elId, label] of [["avatar", "pv-asset-reset-avatar", "恢复默认头像"],
                                       ["cover", "pv-asset-reset-cover", "恢复默认封面"]]) {
        const a = document.getElementById(elId);
        if (!a) continue;
        a.onclick = async () => {
            if (!confirm(`${label}？（删除你换的图，回落到角色卡自带的）`)) return;
            try {
                await fetch("/pack-asset/delete", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({mode, slot})});
                showToast("已恢复默认");
                loadPackView();
                try { window.__modesReload && window.__modesReload(); } catch (e) {}
            } catch (e) { showToast("操作失败"); }
        };
    }

    // 危险区（2026-09-19 重排）：
    //   · 「恢复头像默认 / 恢复封面默认」**不是危险操作**，已挪到顶部大图旁（.pv-asset-ops）；
    //   · 官方（内置）包不能归档/删除 ⇒ 这个区对它们**完全不渲染**（见 pack_tree.js 的 danger 分支），
    //     所以这里只在自建包时才会有内容。
    const danger = document.getElementById("pv-danger");
    danger.innerHTML = "";
    if (data.custom) {
        // 活跃包的危险区主按钮 = 「归档」（3.5 一级：不删数据，可反悔）。
        // 「彻底删除」**只在首页归档区**（views.js 的 _renderArchivedPacks）——那是二级操作，
        // 需要输入包名确认，且后端强制先打一份快照。放在这里当主按钮会诱导手滑。
        if ((data.state || "active") === "archived") {
            const note = document.createElement("div");
            note.style.cssText = "font-size:0.8em;color:var(--fg-muted)";
            note.textContent = "该包已归档：请在首页「已归档」区选择恢复或彻底删除。";
            danger.appendChild(note);
        } else {
            const archBtn = document.createElement("button");
            archBtn.className = "pv-danger-btn";
            archBtn.type = "button";
            archBtn.textContent = `归档「${data.name}」（数据保留，可随时恢复）`;
            archBtn.onclick = async () => {
                // 动作实现与首页归档区共用（window.__packLifecycle，见 views.js）
                if (!await window.__packLifecycle("archive", data.mode, data.name || data.mode)) return;
                try { window.closePackView(); } catch (e) {}
                await window.__modesReload();   // 重载后会切到合法包（归档包已不在清单里）
            };
            danger.appendChild(archBtn);
        }
    }

}
window.loadPackView = loadPackView;

// 主动消息区：400ms 防抖自动保存（与设置页同风格）
let _proSaveTimer = null;
function _proScheduleSave() {
    clearTimeout(_proSaveTimer);
    _proSaveTimer = setTimeout(async () => {
        const msg = document.getElementById("pv-pro-msg");
        const payload = {
            // F-3：写回"本页展示的包"，不用实时 CURRENT_MODE（切换后定时器仍会烧到新包上）
            mode: _packViewMode || CURRENT_MODE,
            enabled: document.getElementById("pv-pro-enabled").checked,
            hard: parseInt(document.getElementById("pv-pro-hard").value) || 6,
            soft: (parseInt(document.getElementById("pv-pro-soft").value) || 35) / 100,
            prob_enabled: document.getElementById("pv-pro-prob").checked,
            hidden_enabled: document.getElementById("pv-pro-hidden").checked,
        };
        try {
            const r = await fetch("/pack-config", {method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)});
            const d = await r.json();
            if (msg) msg.textContent = d.ok ? "已保存" : ("保存失败：" + (d.error || ""));
            if (d.ok) S._hiddenEnabled = payload.hidden_enabled;   // 后台主动门控即时跟随
        } catch (e) { if (msg) msg.textContent = "保存失败（网络）"; }
    }, 400);
}
for (const id of ["pv-pro-enabled", "pv-pro-prob", "pv-pro-hidden"]) {
    document.getElementById(id)?.addEventListener("change", _proScheduleSave);
}
document.getElementById("pv-pro-hard")?.addEventListener("input", () => {
    const el = document.getElementById("pv-pro-hard");
    document.getElementById("pv-pro-hard-v").textContent = el.value;
    _proScheduleSave();
});
document.getElementById("pv-pro-soft")?.addEventListener("input", () => {
    const el = document.getElementById("pv-pro-soft");
    document.getElementById("pv-pro-soft-v").textContent = el.value + "%";
    _proScheduleSave();
});

// ═══════════════════════════════════════════
// 详情页表情包区（本包可用 = 全局共享 + 本包专属；专属可增删启停）
// ═══════════════════════════════════════════
async function _loadPackStickers(mode = _packViewMode || CURRENT_MODE) {
    const gen = _modeGen;
    const grid = document.getElementById("pv-stk-grid");
    if (!grid) return;
    grid.innerHTML = "";
    let items = [];
    try {
        const resp = await fetch("/stickers");
        const data = await resp.json();
        items = Array.isArray(data.stickers) ? data.stickers : [];
    } catch (e) { return; }
    if (gen !== _modeGen) return;   // 模式已切换：不把 A 的表情包渲染进 B 的格子
    const usable = items.filter(s => !s.pack || s.pack === mode);
    for (const s of usable) {
        if (gen !== _modeGen) return;
        const cell = document.createElement("div");
        cell.className = "pv-stk-item" + (s.enabled ? "" : " off");
        const img = document.createElement("img");
        try {
            const url = await stickerSrc(s.file, IS_SERVER, API_BASE);
            if (url) img.src = url;
        } catch (e) {}
        if (s.pack === mode) {
            const pk = document.createElement("span");
            pk.className = "pk";
            pk.textContent = "专属";
            cell.appendChild(pk);
        }
        const lb = document.createElement("div");
        lb.className = "lb";
        lb.textContent = s.label || "";
        const ops = document.createElement("div");
        ops.className = "ops";
        const tgl = document.createElement("button");
        tgl.textContent = s.enabled ? "◐" : "○";
        tgl.title = s.enabled ? "停用" : "启用";
        tgl.onclick = async () => {
            try {
                await fetch("/sticker-update", {method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({id: s.id, enabled: !s.enabled})});
                _loadPackStickers(mode);
            } catch (e) {}
        };
        ops.appendChild(tgl);
        if (s.pack === mode && s.editable) {
            const del = document.createElement("button");
            del.textContent = "×";
            del.title = "删除（仅专属）";
            del.onclick = async () => {
                if (!confirm(`删除表情包「${s.label}」？`)) return;
                try {
                    await fetch("/sticker-delete", {method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({id: s.id})});
                    _loadPackStickers(mode);
                } catch (e) {}
            };
            ops.appendChild(del);
        }
        cell.append(img, lb, ops);
        grid.appendChild(cell);
    }
    if (!usable.length) {
        const empty = document.createElement("div");
        empty.style.cssText = "color:var(--fg-muted);font-size:0.78em;grid-column:1/-1";
        empty.textContent = "还没有表情包——点下方按钮给这个角色添加第一张。";
        grid.appendChild(empty);
    }
}

// 详情页表情包添加（自动归属当前包）
document.getElementById("pv-stk-add")?.addEventListener("click", () => {
    const f = document.getElementById("pv-stk-addform");
    if (f) f.style.display = f.style.display === "none" ? "block" : "none";
});
document.getElementById("pv-stk-submit")?.addEventListener("click", async () => {
    const file = document.getElementById("pv-stk-file").files[0];
    const category = document.getElementById("pv-stk-category").value;
    const label = document.getElementById("pv-stk-label").value.trim();
    const msg = document.getElementById("pv-stk-msg");
    if (!file) { msg.textContent = "请先选择图片"; return; }
    if (!label) { msg.textContent = "请填写含义描述"; return; }
    const fd = new FormData();
    fd.append("file", file);
    fd.append("category", category);
    fd.append("label", label);
    fd.append("mode", _packViewMode || CURRENT_MODE);   // 归属"本页展示的包"（F-3）
    try {
        const resp = await fetch("/add-sticker", {method: "POST", body: fd});
        const data = await resp.json();
        if (data.ok) {
            if (data.local && data.file && file instanceof Blob) {
                try { await idbSaveMedia(String(data.file).slice("local:".length), file); } catch (e) {}
            }
            msg.textContent = "已添加";
            document.getElementById("pv-stk-label").value = "";
            document.getElementById("pv-stk-file").value = "";
            _loadPackStickers(_packViewMode || CURRENT_MODE);
        } else {
            msg.textContent = "失败：" + (data.error || "");
        }
    } catch (e) { msg.textContent = "网络错误"; }
});

// 头像/封面上传（复用图片压缩，选文件后上传为包资产）
function _packAssetUpload(slot) {    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "image/png,image/jpeg,image/webp";
    inp.onchange = async () => {
        const f = inp.files && inp.files[0];
        if (!f) return;
        if (f.size > 5 * 1024 * 1024) { showToast("图片过大（上限 5MB）"); return; }
        try {
            const fd = new FormData();
            fd.append("mode", _packViewMode || CURRENT_MODE);   // 本页展示的包（F-3）
            fd.append("slot", slot);
            fd.append("file", f, f.name || (slot + ".png"));
            const r = await fetch("/pack-asset", {method: "POST", body: fd});
            const d = await r.json();
            if (d.ok) {
                showToast("已替换");
                loadPackView();
                // 封面/头像换了：模式卡片与品牌标识刷新（重拉注册表资产 URL）
                try { window.__modesReload && window.__modesReload(); } catch (e) {}
            } else {
                showToast("上传失败：" + (d.error || ""));
            }
        } catch (e) { showToast("网络错误"); }
    };
    inp.click();
}

// ═══ 用户形象区接线（05：每包独立的用户称呼 + 头像）═══
document.getElementById("pv-user-name-save")?.addEventListener("click", async () => {
    const msg = document.getElementById("pv-user-msg");
    const v = document.getElementById("pv-user-name").value.trim();
    try {
        const r = await fetch("/pack-config", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _packViewMode || CURRENT_MODE, user_name: v})});
        const d = await r.json();
        if (d.ok) {
            if (msg) msg.textContent = "已保存（称呼：" + (d.user_name || "默认") + "）";
            try { window.__modesReload && window.__modesReload(); } catch (e) {}   // /modes 携带新称呼
        } else if (msg) msg.textContent = "保存失败：" + (d.error || "");
    } catch (e) { if (msg) msg.textContent = "网络错误"; }
});
document.getElementById("pv-user-avatar-edit")?.addEventListener("click", () => _packAssetUpload("user_avatar"));
document.getElementById("pv-user-avatar-reset")?.addEventListener("click", async () => {
    if (!confirm("恢复默认用户头像？（删除你上传的头像，回落到内置形象）")) return;
    try {
        const r = await fetch("/pack-asset/delete", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _packViewMode || CURRENT_MODE, slot: "user_avatar"})});
        const d = await r.json();
        if (d.ok) { showToast("已恢复默认"); loadPackView(); try { window.__modesReload && window.__modesReload(); } catch (e) {} }
        else showToast(d.error || "操作失败");
    } catch (e) { showToast("网络错误"); }
});

// 用户形象区：内置形象选择（穹/星，无包自定义头像时生效；复用全局 TB 选择存储）
document.querySelectorAll(".pv-id-choice").forEach(el => {
    el.addEventListener("click", () => {
        try {
            localStorage.setItem("tb_avatar", el.dataset.key);
            document.querySelectorAll(".tb-avatar").forEach(x => { x.src = TB_AVATARS[el.dataset.key]; });
        } catch (e) {}
        loadPackView();   // 预览刷新（无包头像时显示内置选择）
    });
});


/* ── 来源：js/panels/data.js ── */
// 资料面板：状态 / 收藏 / 用户记忆 / 设定文件 / 手账（阶段 2.5 自 panels.js 拆出）
// 只注册监听 + 挂 window.*（菜单 tab 切换在外壳 panels.js 里按 tab 调用这些 loader）。


function loadStateTab() {
    const list = document.getElementById("state-list");
    if (list) {
        list.innerHTML = '<div style="color:#8a8a8a;line-height:1.6">状态系统尚未接入。<br>当前流水线：检索 → 分析 → 回复 → 表情包。</div>';
    }
}

// ═══════════════════════════════════════════
// 收藏（长按消息 → 收藏；菜单 → 收藏 查看）
// ═══════════════════════════════════════════
async function loadFavorites() {
    const list = document.getElementById("fav-list");
    const count = document.getElementById("fav-count");
    if (!list) return;
    try {
        const resp = await fetch(`/favorites?mode=${encodeURIComponent(CURRENT_MODE)}`);
        const data = await resp.json();
        const items = Array.isArray(data.items) ? data.items : [];
        if (count) count.textContent = `收藏 ${items.length} 条`;
        if (!items.length) {
            list.innerHTML = `<div class="fav-empty">还没有收藏。<br>在聊天页长按一条消息，点「收藏」即可保存到这里。</div>`;
            return;
        }
        list.innerHTML = items.map(f => {
            const body = f.type === "sticker"
                ? "[表情包：" + escapeHtml(f.label || "") + "]"
                : f.type === "narration"
                    ? escapeHtml(f.text || "")
                    : escapeHtml(f.content || "");
            const who = f.who === "user" ? "我" : escapeHtml(charName());
            return `<div class="fav-item">
                <div class="fav-head"><span class="fav-who">${who}</span><span class="fav-time">${escapeHtml((f.time || "").slice(5, 16))}</span></div>
                <div class="fav-body">${body}</div>
                <button class="fav-del" type="button" data-id="${escapeHtml(String(f.id))}">删除</button>
            </div>`;
        }).join("");
        list.querySelectorAll(".fav-del").forEach(btn => {
            btn.addEventListener("click", async () => {
                try {
                    const resp = await fetch("/favorites/delete", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({mode: CURRENT_MODE, id: btn.dataset.id}),
                    });
                    const d = await resp.json();
                    if (d.ok) loadFavorites();
                    else showToast("删除失败：" + (d.error || ""));
                } catch (e) { showToast("删除失败，请重试"); }
            });
        });
    } catch (e) {
        if (count) count.textContent = "读取失败";
        list.innerHTML = `<div class="fav-empty">收藏读取失败（服务器模式需先登录；本地后端未就绪时也会这样）</div>`;
    }
}
window.loadFavorites = loadFavorites;

async function loadUserMemory() {
    const editor = document.getElementById("user-memory-editor");
    const msg = document.getElementById("user-memory-msg");
    if (!editor) return;
    try {
        const resp = await fetch(`/user-memory?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        editor.value = data.content || "";
        if (msg) msg.textContent = data.content ? `${data.content.length} 字` : "空";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}

document.getElementById("user-memory-save").addEventListener("click", async () => {
    const editor = document.getElementById("user-memory-editor");
    const msg = document.getElementById("user-memory-msg");
    msg.textContent = "保存中…";
    try {
        const resp = await fetch("/save-user-memory", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content: editor.value, mode: CURRENT_MODE}),
        });
        const data = await resp.json();
        msg.textContent = data.ok ? "✓ 已保存（下次对话生效）" : "失败：" + (data.error || "未知");
    } catch (e) { msg.textContent = "网络错误"; }
});
document.getElementById("user-memory-reload").addEventListener("click", loadUserMemory);

// ── 历史对话存档（整理后的原文；只进检索器）────────────────────────
// 用户 2026-09-18 口径：整理搬走的那段**原文**要看得见、改得动，
// 也能手动让它「AI 压缩」进「用户记忆」。存/读都走 /archive 与 /memory-action。
//
// （顺带说明：「用户设定」编辑器已从本页移除 —— 它是**用户手写的设定**、不是聊天产物，
//   与「清除历史会清空本页」的语义冲突；现统一在「角色卡管理 → 人设与口吻」编辑。）
async function loadArchive(month) {
    const sel = document.getElementById("archive-month");
    const editor = document.getElementById("archive-editor");
    const msg = document.getElementById("archive-msg");
    if (!editor) return;
    const m = month || (sel && sel.value) || "";
    try {
        const url = `/archive?mode=${encodeURIComponent(CURRENT_MODE)}` + (m ? `&month=${encodeURIComponent(m)}` : "");
        const data = await (await fetch(url)).json();
        if (sel) {
            const months = data.months || [];
            const keep = months.includes(m) ? m : (months[0] || "");
            sel.innerHTML = months.map(x => `<option value="${x}">${x}</option>`).join("");
            sel.value = keep;
            // 自绘下拉：options 被重写后要让它重读（否则弹层还是旧的）
            try {
                if (sel._uiSelectSync) sel._uiSelectSync();
                else uiSelectEnhance(document.getElementById("tab-char"));
            } catch (e) {}
        }
        editor.value = data.content || "";
        if (msg) msg.textContent = data.content ? `${data.content.length} 字` : "（还没有存档）";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}

async function _archiveAction(action, extra) {
    const msg = document.getElementById("archive-msg");
    if (msg) msg.textContent = action === "compress" ? "压缩中…（要调一次模型，稍等）" : "保存中…";
    try {
        const resp = await fetch("/memory-action", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(Object.assign({action: action, mode: CURRENT_MODE}, extra || {})),
        });
        const data = await resp.json();
        if (!data.ok) { if (msg) msg.textContent = "失败：" + (data.error || "未知"); return; }
        if (action === "compress") {
            if (msg) msg.textContent = `✓ 已压缩进「用户记忆」（新增 ${data.added || 0} 条，头部 ${data.head_chars || 0} 字）`;
            if (typeof loadUserMemory === "function") loadUserMemory();   // 摘要变了，顺手刷新上面那块
        } else if (msg) {
            msg.textContent = "✓ 已保存（检索器下次就用改后的原文）";
        }
    } catch (e) { if (msg) msg.textContent = "网络错误"; }
}

(function _wireArchive() {
    const sel = document.getElementById("archive-month");
    const save = document.getElementById("archive-save");
    const reload = document.getElementById("archive-reload");
    const comp = document.getElementById("archive-compress");
    if (save) save.addEventListener("click", () => {
        const editor = document.getElementById("archive-editor");
        _archiveAction("save_archive", {
            month: (sel && sel.value) || "",
            content: editor ? editor.value : "",
        });
    });
    if (reload) reload.addEventListener("click", () => loadArchive());
    if (sel) sel.addEventListener("change", () => loadArchive(sel.value));
    if (comp) comp.addEventListener("click", () => {
        const month = (sel && sel.value) || "";
        if (!month) { const m = document.getElementById("archive-msg"); if (m) m.textContent = "还没有存档可压缩"; return; }
        if (!confirm(`把 ${month} 的存档原文压缩进「用户记忆」？\n（原文会保留，可以反复压；压缩会调用一次模型）`)) return;
        _archiveAction("compress", {month: month});
    });
})();
window.loadArchive = loadArchive;

// 手账
async function loadJournal() {
    const editor = document.getElementById("journal-editor");
    const msg = document.getElementById("journal-msg");
    try {
        const resp = await fetch(`/journal?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (editor) editor.value = data.content || "";
        msg.textContent = data.content ? `${data.content.length} 字` : "空";
    } catch (e) { if (msg) msg.textContent = "加载失败"; }
}
document.getElementById("journal-reload").addEventListener("click", loadJournal);
document.getElementById("journal-save").addEventListener("click", async () => {
    const content = document.getElementById("journal-editor").value;
    const msg = document.getElementById("journal-msg");
    msg.textContent = "保存中…";
    try {
        const resp = await fetch("/save-journal", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content, mode: CURRENT_MODE}),
        });
        const data = await resp.json();
        msg.textContent = data.ok ? `✓ 已保存（${content.length} 字）` : "失败";
    } catch (e) { msg.textContent = "网络错误"; }
});


/* ── 来源：js/panels/debug.js ── */
// 调试面板：请求记录 / 流水线日志（阶段 2.5 自 panels.js 拆出）


// 调试面板里的用户称呼随当前角色包（F-6.x 残留：曾硬编码「开拓者」，自建包下称呼错误）
// 2026-09-18：收敛到 views.userName()，且**不再回落字面量**（取不到就留空）。
function _userName() {
    return userName();
}

// ═══════════════════════════════════════════
// 请求记录
// ═══════════════════════════════════════════

async function loadRequestLog() {
    const list = document.getElementById("log-list");
    const countEl = document.getElementById("log-count");
    if (!list) return;
    try {
        const resp = await fetch("/requests");
        const data = await resp.json();
        if (countEl) {
            const totalCost = (data.requests || []).reduce((s, r) => s + (Number(r.cost_cny) || 0), 0);
            countEl.textContent = totalCost > 0
                ? `共 ${data.count} 次请求 · 累计约 ¥${totalCost.toFixed(3)}`
                : `共 ${data.count} 次请求`;
        }
        const rows = (data.requests || []).slice().reverse();
        if (rows.length === 0) {
            list.innerHTML = '<div style="color:#8a8a8a;padding:10px">暂无记录</div>';
            return;
        }
        // A6（审计 2026-09-15）：r.module / r.model / r.time 原先未转义直插 innerHTML
        // （model 来自上游响应/自定义模型名，注入面真实）——已套 _esc，与本文件其余字段一致
        list.innerHTML = rows.map(r => `
        <div style="display:flex;align-items:center;gap:4px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.8em;color:#c8d0e0">
            <span style="flex-shrink:0;width:50px;color:#8a8a8a">${_esc(r.time || "?")}</span>
            <span style="flex-shrink:0;width:64px">${_esc(r.module)}</span>
            <span style="flex-shrink:0;width:52px">${_esc(r.model || "?")}</span>
            <span style="flex-shrink:0;width:22px;text-align:center">${r.success ? '<span style="color:#6c8">✓</span>' : '<span style="color:#c66">✗</span>'}</span>
            <span style="flex:1;text-align:right">${r.total_tokens || 0}</span>
            <span style="flex-shrink:0;width:70px;text-align:right;color:#8a8a8a">¥${(r.cost_cny || 0).toFixed(6)}</span>
        </div>`).join("");
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;padding:10px">加载失败</div>';
    }
}
window.loadRequestLog = loadRequestLog;

// ═══════════════════════════════════════════
// 流程日志：每轮各阶段的输入/输出/思考过程
// ═══════════════════════════════════════════

function _stageBlock(title, elapsed, fields) {
    const rows = fields
        .filter(([, v]) => v != null && String(v).trim() !== "")
        .map(([k, v]) => {
            const body = _esc(typeof v === "string" ? v : JSON.stringify(v, null, 1));
            if (k === "思考过程") {
                return `<details style="margin:2px 0"><summary style="cursor:pointer;color:#8a8a8a">思考过程（点开）</summary><pre style="white-space:pre-wrap;word-break:break-all;color:#8a8a8a;margin:4px 0;font-size:0.95em">${body}</pre></details>`;
            }
            return `<div style="margin:2px 0"><span style="color:#8a8a8a">${k}:</span> <span style="white-space:pre-wrap;word-break:break-all">${body}</span></div>`;
        }).join("");
    return `<details open style="margin:4px 0;padding:4px 8px;background:rgba(255,255,255,0.03);border-radius:6px">
        <summary style="cursor:pointer;color:#c8d0e0">${title}${elapsed != null ? ` <span style="color:#8a8a8a;font-size:0.85em">${elapsed}s</span>` : ""}</summary>
        <div style="padding:4px 0 2px">${rows}</div></details>`;
}

async function loadPipeline() {
    const list = document.getElementById("pipeline-list");
    const countEl = document.getElementById("pipeline-count");
    if (!list) return;
    try {
        const resp = await fetch(`/pipeline?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (countEl) countEl.textContent = `最近 ${data.count} 轮`;
        const rows = (data.pipeline || []).slice().reverse();
        if (rows.length === 0) {
            list.innerHTML = '<div style="color:#8a8a8a;padding:10px">暂无记录（本次启动后还没聊过）</div>';
            return;
        }
        list.innerHTML = rows.map(p => {
            let inner = "";
            if (p.error) {
                inner = `<div style="color:#c66;padding:4px 0">流水线异常: ${_esc(p.error)}</div>`;
            } else {
                const a = p.analyzer || {}, o = p.organizer || {}, po = p.polisher || {}, rt = p.retriever || {};
                inner =
                    _stageBlock("⓪ 知识检索", rt.elapsed, [
                        ["摘要", rt.knowledge],
                    ]) +
                    _stageBlock("① 分析器", a.elapsed, [
                        ["意图", a.intent],
                        ["事实核查", (a.fact_check || []).length ? a.fact_check : ""],
                        ["摘要", a.summary],
                        ["原始输出", a.raw_json],
                        ["思考过程", a.reasoning],
                    ]) +
                    _stageBlock("② 回复器", po.elapsed, [
                        ["原始输出", po.raw],
                        ["思考过程", po.reasoning],
                    ]) +
                    _stageBlock("③ 工具调度（表情包）", o.elapsed, [
                        ["选图", o.sticker_label || "（不发）"],
                        ["原始输出", o.raw],
                        ["思考过程", o.reasoning],
                    ]);
            }
            return `<div style="margin-bottom:14px;padding:8px;border:1px solid rgba(255,255,255,0.08);border-radius:8px;font-size:0.8em;color:#c8d0e0">
                <div style="margin-bottom:4px"><span style="color:#8a8a8a">${_esc(p.time || "?")}</span> ${_esc(_userName())}: <span style="color:#e0d5c1">${_esc(p.user_input)}</span>${p.hint ? ` <span style="color:#8a8a8a">(hint:${_esc(p.hint)})</span>` : ""}</div>
                ${inner}
            </div>`;
        }).join("");
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;padding:10px">加载失败</div>';
    }
}
window.loadPipeline = loadPipeline;

// ═══════════════════════════════════════════
// 用户记忆（= memory.md，休息时自动整理的过往摘要）/ 用户设定（补充设定）
// ═══════════════════════════════════════════


/* ── 来源：js/panels/stickers.js ── */
// 表情包管理（菜单抽屉「表情包」页签：添加 / 映射表 / 启停 / 删除）
// 阶段 B（2026-09-15）自 panels/packs.js 拆出：这段属菜单域，与角色包详情页无关。

// ═══════════════════════════════════════════
// 表情包管理
// ═══════════════════════════════════════════
const stickerAddBtn = document.getElementById("sticker-add-btn");
const stickerAddForm = document.getElementById("sticker-add-form");
if (stickerAddBtn) stickerAddBtn.addEventListener("click", () => {
    stickerAddForm.style.display = stickerAddForm.style.display === "none" ? "flex" : "none";
});

document.getElementById("sticker-submit").addEventListener("click", async () => {
    const file = document.getElementById("sticker-file").files[0];
    const category = document.getElementById("sticker-category").value;
    const label = document.getElementById("sticker-label").value.trim();
    const msg = document.getElementById("sticker-add-msg");
    if (!file) { msg.textContent = "请先选择图片"; return; }
    if (!label) { msg.textContent = "请填写含义描述"; return; }
    const fd = new FormData(); fd.append("file", file); fd.append("category", category); fd.append("label", label);
    fd.append("mode", CURRENT_MODE);   // 归属当前包（阶段6）
    try {
        const resp = await fetch("/add-sticker", { method: "POST", body: fd });
        const data = await resp.json();
        if (data.ok) {
            // A2 媒体本地策略（服务器版）：图片本体存本机 IndexedDB（key=内容哈希），
            // 服务器只保留文字元数据（label/category/哈希）；上传后立即本地化
            if (data.local && data.file && file instanceof Blob) {
                await idbSaveMedia(String(data.file).slice("local:".length), file);
                msg.textContent = "已添加：" + data.label + "（图片仅存本机）";
            } else {
                msg.textContent = "已添加：" + data.label;
            }
            document.getElementById("sticker-file").value = "";
            document.getElementById("sticker-label").value = "";
            loadStickerList();
        } else msg.textContent = "失败：" + (data.error || "未知");
    } catch(e) { msg.textContent = "网络错误"; }
});

document.getElementById("sticker-manage-btn").addEventListener("click", () => {
    const panel = document.getElementById("sticker-manage-panel");
    panel.style.display = panel.style.display === "none" ? "flex" : "none";
    if (panel.style.display !== "none") loadStickerList();
});

async function loadStickerList() {
    const msg = document.getElementById("sticker-manage-msg");
    const list = document.getElementById("sticker-list");
    msg.textContent = "加载中…";
    try {
        const resp = await fetch("/stickers");
        const data = await resp.json();
        const stickers = data.stickers || [];
        msg.textContent = `共 ${stickers.length} 个`;
        // A2：缩略图异步解析（local: 引用 → IndexedDB；无图显示占位块）
        const rows = await Promise.all(stickers.map(async s => {
            const src = await stickerSrc(s.file, IS_SERVER, API_BASE);
            const thumb = src
                ? `<img class="stk-thumb" src="${escapeHtml(src)}" loading="lazy" onerror="this.style.opacity=0.2">`
                : `<div class="stk-thumb" style="display:flex;align-items:center;justify-content:center;opacity:0.35;font-size:0.6em">无图</div>`;
            return `
        <div class="sticker-row" data-id="${escapeHtml(s.id)}">
            <div class="stk-head">
                ${thumb}
                <button class="stk-toggle ${s.enabled ? "on" : ""}" data-on="${s.enabled ? "1" : ""}" ${(s.editable || s.is_default) ? "" : "disabled"}>${s.enabled ? "启用中" : "已停用"}</button>
            </div>${s.pack ? `<div style="font-size:0.62em;color:var(--fg-accent);margin-top:2px">专属：${escapeHtml(s.pack)}</div>` : ""}
            <div class="stk-main">
                <select class="stk-cat-sel" ${(s.editable || s.is_default) ? "" : "disabled"}>
                    <option value="可爱" ${s.category==="可爱"?"selected":""}>可爱</option>
                    <option value="帅气" ${s.category==="帅气"?"selected":""}>帅气</option>
                </select>
                <input class="stk-label-input" type="text" value="${escapeHtml(s.label)}" maxlength="120" ${(s.editable || s.is_default) ? "" : "readonly"}>
                <div class="stk-actions">
                    <button class="stk-save" disabled>保存</button>
                    <button class="stk-del" ${(s.is_default || !s.editable) ? "disabled" : ""}>删</button>
                </div>
            </div>
        </div>`;
        }));
        list.innerHTML = rows.join("");
        try { uiSelectEnhance(list); } catch (e) {}   // 动态生成的分类下拉也走自绘（守卫幂等）
        list.querySelectorAll(".sticker-row").forEach(row => {
            const id = row.dataset.id;
            const inp = row.querySelector(".stk-label-input");
            const cat = row.querySelector(".stk-cat-sel");
            const save = row.querySelector(".stk-save");
            const del = row.querySelector(".stk-del");
            const toggle = row.querySelector(".stk-toggle");
            const origLabel = inp.value;
            const origCat = cat.value;

            function checkChanged() {
                save.disabled = (inp.value.trim() === origLabel && cat.value === origCat) || (!inp.value.trim() && !cat.value);
            }
            inp.addEventListener("input", checkChanged);
            cat.addEventListener("change", checkChanged);

            toggle.addEventListener("click", async () => {
                const next = toggle.dataset.on !== "1";
                toggle.disabled = true;
                try {
                    const r = await fetch("/sticker-update", {
                        method:"POST",
                        headers:{"Content-Type":"application/json"},
                        body:JSON.stringify({id, enabled: next}),
                    });
                    const d = await r.json();
                    if (d.ok) {
                        toggle.dataset.on = next ? "1" : "";
                        toggle.classList.toggle("on", next);
                        toggle.textContent = next ? "启用中" : "已停用";
                        msg.textContent = next ? "已启用：" + d.label : "已停用：" + d.label;
                    }
                } catch(e) {}
                toggle.disabled = false;
            });

            save.addEventListener("click", async () => {
                const label = inp.value.trim();
                const category = cat.value;
                try {
                    const r = await fetch("/sticker-update", {
                        method:"POST",
                        headers:{"Content-Type":"application/json"},
                        body:JSON.stringify({id, label: label || undefined, category}),
                    });
                    const d = await r.json();
                    if (d.ok) {
                        inp.value = d.label;
                        cat.value = d.category;
                        save.textContent="已存"; save.disabled=true;
                        msg.textContent="已更新："+d.label;
                    }
                } catch(e) {}
            });
            del.addEventListener("click", async () => {
                if (!confirm("确认删除？")) return;
                try {
                    await fetch("/sticker-delete", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id}) });
                    row.remove();
                } catch(e) {}
            });
        });
    } catch(e) { msg.textContent = "加载失败"; }
}


/* ── 来源：js/panels/pack_assist.js ── */
// 逐文件 AI 辅助抽屉（阶段 E）：✨ 图标 → 与 AI 对话 → diff 提案 → 应用才写盘
// AI 永不直接写盘；提案经后端静态校验 + 备份 + 原子写（见 modules/pack_assist.py 边界表）。

let _av = { mode: "", file: "", history: [], pending: null };

function openAssist(mode, file, label) {
    _av = { mode: mode || CURRENT_MODE, file, history: [], pending: null };
    const v = document.getElementById("assist-view");
    if (!v) return;
    document.getElementById("av-title").textContent = `✨ AI 辅助 · ${label || file}`;
    document.getElementById("av-chat").innerHTML =
        `<div class="fix-hist">说说想把这个文件改成什么样，AI 会给出具体修改提案（应用前一定会给你看 diff）。</div>`;
    document.getElementById("av-proposal").style.display = "none";
    document.getElementById("av-input").value = "";
    v.style.display = "flex";
    setTimeout(() => document.getElementById("av-input").focus(), 200);
}
window.__openAssist = openAssist;

function closeAssist() {
    const v = document.getElementById("assist-view");
    if (v) v.style.display = "none";
}
window.closeAssist = closeAssist;

function _avMsg(who, text) {
    const chat = document.getElementById("av-chat");
    chat.insertAdjacentHTML("beforeend",
        `<div class="fix-msg ${who === "user" ? "me" : "ai"}"><div class="fix-who">${who === "user" ? "我" : "AI"}</div><div class="fix-text">${escapeHtml(text)}</div></div>`);
    chat.scrollTop = chat.scrollHeight;
}

function _renderProposal(changes) {
    const box = document.getElementById("av-proposal");
    box.style.display = "block";
    box.innerHTML = `<div class="fix-proposal-title"><span>修改提案（尚未生效）</span><span class="fix-op-tag">待确认</span></div>`
        + changes.map(c => `<div class="fix-change"><div class="fix-change-head">
            <span class="fix-op-tag ${c.op === "append" ? "add" : "fix"}">${c.op === "append" ? "补充" : "纠正"}</span></div>
            ${c.op === "replace" ? `<div class="fix-diff-old">− ${escapeHtml(c.old || "")}</div>` : ""}
            <div class="fix-diff-new">+ ${escapeHtml(c.new || "")}</div>
            <div class="fix-reason">${escapeHtml(c.reason || "")}</div></div>`).join("")
        + `<div class="fix-proposal-actions">
             <button class="hb-btn" type="button" id="av-discard">放弃</button>
             <button class="hb-btn primary" type="button" id="av-apply">应用修改</button>
           </div>`;
    document.getElementById("av-discard").onclick = () => {
        _av.pending = null;
        box.style.display = "none";
        _avMsg("ai", "已放弃这次提案。继续说想怎么改就行。");
    };
    document.getElementById("av-apply").onclick = _applyPending;
}

async function _applyPending() {
    if (!_av.pending) return;
    const changes = _av.pending;
    try {
        const r = await fetch("/pack-assist/apply", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _av.mode, file: _av.file, changes})});
        const d = await r.json();
        if (d.ok) {
            showToast("已应用（写入前有自动备份）");
            document.getElementById("av-proposal").style.display = "none";
            _av.pending = null;
            _avMsg("ai", "已写入并生效。还要改别的地方吗？");
            // 让背后的编辑页刷新（详情页/知识库列表/记忆区各自的重载函数都在）
            try { window.loadPackView && window.loadPackView(); } catch (e) {}
        } else {
            _avMsg("ai", "校验没通过：" + (d.error || ""));
        }
    } catch (e) { showToast("网络错误"); }
}

async function _avSend() {
    const inp = document.getElementById("av-input");
    const text = (inp.value || "").trim();
    if (!text) return;
    inp.value = "";
    _avMsg("user", text);
    _av.history.push({who: "user", text});
    _avMsg("ai", "（正在想…）");
    try {
        const r = await fetch("/pack-assist", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _av.mode, file: _av.file, message: text, history: _av.history})});
        const d = await r.json();
        // 摘掉"正在想"
        const chat = document.getElementById("av-chat");
        const last = chat.querySelectorAll(".fix-msg.ai");
        if (last.length) last[last.length - 1].remove();
        if (d.need_key) { _avMsg("ai", "需要先去 ⚙ 设置里填 API Key。"); return; }
        if (!d.ok) { _avMsg("ai", "出了点问题：" + (d.error || "")); return; }
        _av.history.push({who: "ai", text: d.reply});
        _avMsg("ai", (d.searched ? "🌐 已联网检索官方资料\n\n" : "") + d.reply);
        if (Array.isArray(d.changes) && d.changes.length) {
            _av.pending = d.changes;
            _renderProposal(d.changes);
        }
    } catch (e) {
        const chat = document.getElementById("av-chat");
        const last = chat.querySelectorAll(".fix-msg.ai");
        if (last.length) last[last.length - 1].remove();
        _avMsg("ai", "网络错误，稍后再试");
    }
}

document.getElementById("av-send")?.addEventListener("click", _avSend);
document.getElementById("av-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); _avSend(); }
});
document.getElementById("av-back")?.addEventListener("click", closeAssist);


/* ── 来源：js/panels/pack_tree.js ── */
// 角色卡管理树（数据驱动 · 2026-09-15）
// 数据源 = GET /pack-structure（core/pack_structure.py 的声明式规范）——
// 本模块只按返回的域/来源渲染树：可收起可展开，文件点击**就地展开**编辑（不跳页底）。
// 业务结构由后端规范决定；新域/新槽位出现后，本模块自动跟随，无需改代码。

let _treeMode = "";

async function loadPackTree(mode) {
    _treeMode = mode || CURRENT_MODE;
    const box = document.getElementById("pv-tree");
    if (!box) return;
    try {
        const resp = await fetch(`/pack-structure?mode=${encodeURIComponent(_treeMode)}`);
        const data = await resp.json();
        if (!data.ok) { box.innerHTML = "结构读取失败"; return; }
        _render(box, data);
    } catch (e) {
        box.innerHTML = "结构读取失败（网络）";
    }
}

function _render(box, data) {
    _parkEmbeds();   // 先把嵌入块泊出——innerHTML 清空会销毁树内一切（含事件接线）
    box.innerHTML = "";
    _domainLabels = {};
    for (const d of data.domains) _domainLabels[d.key] = d.label;
    for (const d of data.domains) {
        // ★ 2026-09-19：两个域整体不渲染（用户真机反馈）——
        //   · `assets`（形象资产）：功能早已挪到顶部大图，这个域只剩一句说明，是空壳；
        //   · `danger`（危险区）：官方包不能归档/删除 ⇒ 没有任何危险操作；而原来给官方包
        //     塞的"恢复头像/封面默认"根本不是危险操作（已挪到顶部大图旁）。
        //   两者都该"没有内容就没有这个区"，而不是留一个空的折叠条。
        if (d.key === "assets") continue;
        if (d.key === "danger" && !data.custom) continue;
        const det = document.createElement("details");
        det.className = "pt-domain";
        det.dataset.key = d.key;
        const sum = document.createElement("summary");
        sum.innerHTML = `<b>${escapeHtml(d.label)}</b> <span class="pt-desc">${escapeHtml(d.desc || "")}</span>`;
        det.appendChild(sum);
        for (const s of d.sources) {
            // 节点可能"故意不渲染"（见 _renderSource 的 assets / 官方包 danger 分支）——
            // appendChild(null) 会抛错，所以这里必须判空。
            const node = _renderSource(d, s, data);
            if (node) det.appendChild(node);
        }
        box.appendChild(det);
    }
}

// ── 嵌入块车位 ──
// 树重渲 = #pv-tree innerHTML 清空。被 _moveInto 搬进树的既有区块（用户形象/主动消息/
// 表情包/危险区）若不清空前泊出，会随清空被销毁：二次渲染后这些域全空，
// 且模块级事件接线（防抖保存/上传/称呼保存）随元素死亡。车位 = display:none 的隐藏容器，
// 泊入其中元素存活、getElementById 可达，渲染后再搬回新节点。
const _EMBED_GETTERS = {
    identity: () => document.getElementById("pv-identity"),
    config: () => document.querySelector(".pv-proactive"),
    stickers: () => document.querySelector(".pv-stickers"),
    danger: () => document.getElementById("pv-danger"),
};
let _embedParking = null;
let _domainLabels = {};

function _parkEmbeds() {
    if (!_embedParking) {
        _embedParking = document.createElement("div");
        _embedParking.style.display = "none";
        document.body.appendChild(_embedParking);
    }
    for (const get of Object.values(_EMBED_GETTERS)) {
        const el = get();
        if (el) _embedParking.appendChild(el);
    }
}

function _renderSource(domain, s, data) {
    const t = s.type;
    if (t === "slot" || t === "file") return _fileNode(s, data);
    if (t === "dir") return _kbNode(s);
    if (t === "sys_prompt") return _sysPromptNode(s);
    if (t === "ref") return _refNode(s);
    // ★ 2026-09-19：`assets` 节点不再渲染 —— 它只剩一句"去顶部大图换图"的说明，
    //   留一个空折叠区纯属噪音（用户："形象自从在上面改就不要在下面放个空的"）。
    //   角色形象的所有操作（换封面/换头像/恢复默认）现在都在顶部大图区。
    if (t === "assets") return null;
    // ★ 2026-09-19：危险区**只对自建包**渲染。
    //   · 官方（内置）包不能归档、不能删除 ⇒ 里面没有任何"危险"操作；
    //   · 原来给官方包塞的"恢复头像/封面默认"根本不是危险操作，已挪到顶部大图旁。
    //   两个理由都指向同一结论：官方包不该有这个区（用户："官方角色卡删不了就别放危险区"）。
    if (t === "danger") return (data && data.custom) ? _moveInto(t) : null;
    if (t === "identity" || t === "config" || t === "stickers") return _moveInto(t);
    const ph = document.createElement("div");
    ph.className = "pt-src";
    return ph;
}

// 控件型来源：把既有工作区块搬入树节点（不重写功能，只换归属；元素来自车位或初始位置）
function _moveInto(type) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src pt-embed";
    const get = _EMBED_GETTERS[type];
    const el = get ? get() : null;
    if (el) wrap.appendChild(el);
    return wrap;
}

// 引用型来源：只读跳转行——交叉文件的唯一编辑点在目标域，不重复放编辑器（避免两处改同一文件）
function _refNode(s) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src";
    const row = document.createElement("div");
    row.className = "pt-row pt-ref";
    const target = _domainLabels[s.target] || s.target || "";
    row.title = s.note || "";
    row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span><span class="pt-tag">见「${escapeHtml(target)}」→</span>`;
    row.addEventListener("click", () => {
        const dom = document.querySelector(`#pv-tree .pt-domain[data-key="${s.target}"]`);
        if (dom) { dom.open = true; dom.scrollIntoView({block: "start", behavior: "smooth"}); }
    });
    wrap.appendChild(row);
    return wrap;
}

// ── 文件节点（slot/file）：行 + 就地展开编辑器 ──
const _STATE_LABEL = {baseline: "", inherited: "", customized: "（已修改）"};

function _fileNode(s, data) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src pt-file";
    const row = document.createElement("div");
    row.className = "pt-row";
    const shared = (s.used_by || []).length > 1
        ? `<span class="pt-tag pt-shared" title="本文件被多个阶段读取：${escapeHtml(s.used_by.join("、"))}">共用</span>` : "";
    const feed = s.feeds ? `<span class="pt-tag" title="本文件的输出流向">→ ${escapeHtml(s.feeds)}</span>` : "";
    const ai = s.assistable === false ? "" : `<span class="assist-ico" title="AI 辅助修改本文件">✨</span>`;
    // 包未附带的文件（如 sticker 包无 opening.json）：灰态只展示，不提供编辑入口
    if (s.exists === false && !s.user_copy) {
        row.classList.add("pt-row-missing");
        row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span><span class="pt-tag">本包未附带</span>`;
        wrap.appendChild(row);
        return wrap;
    }
    row.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span>
        ${s.state && _STATE_LABEL[s.state] ? `<span class="pt-tag">${_STATE_LABEL[s.state]}</span>` : ""}
        ${!s.state && s.user_copy ? `<span class="pt-tag">已改</span>` : ""}
        ${shared}${feed}${ai}`;
    const ed = document.createElement("div");
    ed.className = "pt-editor";
    ed.style.display = "none";
    wrap.append(row, ed);
    row.addEventListener("click", () => _toggleFile(ed, s));
    const ico = row.querySelector(".assist-ico");
    if (ico) ico.addEventListener("click", e => {
        e.stopPropagation();
        const file = s.type === "slot" ? s.file : s.path;
        if (window.__openAssist) window.__openAssist(_treeMode, file, s.label);
    });
    return wrap;
}

async function _loadFileContent(s) {
    if (s.type === "slot") {
        const resp = await fetch(`/pack-files?mode=${encodeURIComponent(_treeMode)}`);
        const data = await resp.json();
        const f = (data.files || []).find(x => x.name === s.file);
        return f ? (f.content || "") : "";
    }
    // file 类型统一走 /pack-file（白名单 = 结构规范声明的 file 路径）——
    // 修复：此前硬编码 /pack-memory，opening.json 的读写会落在出厂记忆上
    const resp = await fetch(`/pack-file?mode=${encodeURIComponent(_treeMode)}&path=${encodeURIComponent(s.path)}`);
    const data = await resp.json();
    return data.content || "";
}

async function _toggleFile(ed, s) {
    if (ed.style.display === "none") {
        if (!ed.dataset.loaded) {
            ed.innerHTML = `<div class="pt-editing">${escapeHtml(s.type === "slot" ? s.file : s.path)}</div>
                <textarea class="pt-ta"></textarea>
                <div class="btn-row" style="display:flex;gap:8px;margin-top:6px">
                    <button class="pt-save" type="button" style="flex:1">保存</button>
                    <button class="pt-revert" type="button" style="flex:1">恢复默认</button>
                    <button class="pt-close" type="button" style="flex:1">收起</button>
                </div><div class="pt-msg" style="font-size:0.72em;color:var(--fg-muted);margin-top:4px"></div>`;
            const content = await _loadFileContent(s);
            ed.querySelector(".pt-ta").value = content;
            ed.dataset.loaded = "1";
            ed.querySelector(".pt-save").onclick = () => _saveFile(ed, s);
            ed.querySelector(".pt-revert").onclick = () => _revertFile(ed, s);
            ed.querySelector(".pt-close").onclick = () => { ed.style.display = "none"; };
        }
        ed.style.display = "block";
    } else {
        ed.style.display = "none";
    }
}

async function _saveFile(ed, s) {
    const msg = ed.querySelector(".pt-msg");
    const content = ed.querySelector(".pt-ta").value;
    if (!content.trim()) { msg.textContent = "内容不能为空"; return; }
    msg.textContent = "保存中…";
    const isSlot = s.type === "slot";
    try {
        const resp = await fetch(isSlot ? "/character-file-update" : "/pack-file/update", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(isSlot
                ? {mode: _treeMode, filename: s.file, content}
                : {mode: _treeMode, path: s.path, content}),
        });
        const d = await resp.json();
        msg.textContent = d.ok ? "已保存（下轮对话生效）" : "保存失败：" + (d.error || "");
    } catch (e) { msg.textContent = "网络错误"; }
}

async function _revertFile(ed, s) {
    if (!confirm("恢复默认？（删除你的修改）")) return;
    const msg = ed.querySelector(".pt-msg");
    const isSlot = s.type === "slot";
    try {
        // file 类型走 /pack-file/delete 删用户副本——修复：此前向 /pack-memory/update 发空串，
        // 后端拒绝空内容，「恢复默认」永远失败
        const resp = await fetch(isSlot ? "/character-file/delete" : "/pack-file/delete", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify(isSlot ? {mode: _treeMode, filename: s.file} : {mode: _treeMode, path: s.path}),
        });
        const d = await resp.json();
        if (d.ok) { showToast("已恢复默认"); ed.style.display = "none"; loadPackTree(_treeMode); }
        else msg.textContent = d.error || "操作失败";
    } catch (e) { msg.textContent = "网络错误"; }
}

// ── 知识库目录节点：组内文件列表 + 就地展开 ──
const _KB_GROUPS = {world: "世界观", factions: "势力", story: "主线剧情", character: "角色个人", dialogues: "对话"};

function _kbNode(s) {
    const wrap = document.createElement("div");
    wrap.className = "pt-src";
    const head = document.createElement("div");
    head.className = "pt-row";
    const feed = s.feeds ? `<span class="pt-tag" title="本目录内容的输出流向">→ ${escapeHtml(s.feeds)}</span>` : "";
    head.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span> <span class="pt-tag">${s.count || 0} 个文件</span>${feed}`;
    const body = document.createElement("div");
    body.className = "pt-kb";
    body.style.display = "none";
    wrap.append(head, body);
    head.addEventListener("click", () => _toggleKB(body, head));
    return wrap;
}

async function _toggleKB(body, head) {
    if (body.style.display === "none") {
        if (!body.dataset.loaded) {
            const resp = await fetch(`/pack-knowledge?mode=${encodeURIComponent(_treeMode)}`);
            const data = await resp.json();
            const files = (data.ok && data.files) || [];
            const groups = {};
            for (const f of files) (groups[f.path.split("/")[0]] = groups[f.path.split("/")[0]] || []).push(f);
            let html = "";
            for (const top of Object.keys(groups)) {
                html += `<div class="pt-kb-grp">${escapeHtml(_KB_GROUPS[top] || top)}/</div>`;
                for (const f of groups[top]) {
                    // ★ 显示名（2026-10-01 修真实显示 bug）：契约 06 §3.1 现在**允许扁平知识名**
                    //   `knowledge/世界观.md` —— 那种路径 split("/") 只有一段，`.slice(1)` 会是空串，
                    //   于是行内文件名整个空掉（能点能编辑，但用户看不见点的是哪个文件）。
                    //   规则：有分组就去掉 `knowledge/` 前缀保留"域/名.md"；扁平就用完整 path。
                    const label = f.path.includes("/") ? f.path.split("/").slice(1).join("/") : f.path;
                    html += `<div class="pt-row pt-kb-file" data-path="${escapeHtml(f.path)}">
                        <span class="pt-name" style="font-size:0.78em">${escapeHtml(label)}</span>
                        ${f.user_copy ? '<span class="pt-tag">已改</span>' : ""}
                        <span class="assist-ico" data-ai="${escapeHtml(f.path)}" title="AI 辅助修改本文件">✨</span>
                    </div>`;
                }
            }
            body.innerHTML = html || `<div class="pt-note">还没有知识文件</div>`;
            body.dataset.loaded = "1";
            body.querySelectorAll(".pt-kb-file").forEach(row => {
                row.addEventListener("click", () => _toggleKBFile(row));
            });
            body.querySelectorAll(".assist-ico").forEach(ico => {
                ico.addEventListener("click", e => {
                    e.stopPropagation();
                    if (window.__openAssist) window.__openAssist(_treeMode, "knowledge/" + ico.dataset.ai, ico.dataset.ai);
                });
            });
        }
        body.style.display = "block";
    } else {
        body.style.display = "none";
    }
}

async function _toggleKBFile(row) {
    let ed = row.nextElementSibling;
    if (ed && ed.classList && ed.classList.contains("pt-editor")) {
        ed.style.display = ed.style.display === "none" ? "block" : "none";
        return;
    }
    ed = document.createElement("div");
    ed.className = "pt-editor";
    const path = row.dataset.path;
    ed.innerHTML = `<div class="pt-editing">${escapeHtml(path)}</div>
        <textarea class="pt-ta"></textarea>
        <div class="btn-row" style="display:flex;gap:8px;margin-top:6px">
            <button class="pt-save" type="button" style="flex:1">保存</button>
            <button class="pt-revert" type="button" style="flex:1">恢复默认</button>
            <button class="pt-close" type="button" style="flex:1">收起</button>
        </div><div class="pt-msg" style="font-size:0.72em;color:var(--fg-muted);margin-top:4px"></div>`;
    row.after(ed);
    try {
        const resp = await fetch(`/pack-knowledge/file?mode=${encodeURIComponent(_treeMode)}&path=${encodeURIComponent(path)}`);
        const data = await resp.json();
        ed.querySelector(".pt-ta").value = data.ok ? (data.content || "") : "";
    } catch (e) { ed.querySelector(".pt-ta").value = ""; }
    ed.querySelector(".pt-save").onclick = async () => {
        const msg = ed.querySelector(".pt-msg");
        const content = ed.querySelector(".pt-ta").value;
        if (!content.trim()) { msg.textContent = "内容不能为空"; return; }
        const resp = await fetch("/pack-knowledge/update", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _treeMode, path, content})});
        const d = await resp.json();
        msg.textContent = d.ok ? "已保存（下轮对话生效）" : "保存失败：" + (d.error || "");
    };
    ed.querySelector(".pt-revert").onclick = async () => {
        if (!confirm("恢复默认？（删除你的修改）")) return;
        const resp = await fetch("/pack-knowledge/delete", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: _treeMode, path})});
        const d = await resp.json();
        if (d.ok) { showToast("已恢复默认"); ed.remove(); row.remove(); }
        else ed.querySelector(".pt-msg").textContent = d.error || "操作失败";
    };
    ed.querySelector(".pt-close").onclick = () => { ed.style.display = "none"; };
}

// ── 系统提示词节点（只读）──
function _sysPromptNode(s) {
    const det = document.createElement("details");
    det.className = "pt-src pt-sysprompt";
    const sum = document.createElement("summary");
    sum.innerHTML = `<span class="pt-name">${escapeHtml(s.label)}</span> <span class="pt-tag">代码内置 · 只读</span>`;
    const pre = document.createElement("pre");
    pre.className = "pt-pre";
    pre.textContent = s.text || "";
    det.append(sum, pre);
    return det;
}


/* ── 来源：js/panels/plaza.js ── */
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


/* ── 来源：js/panels/plaza_list.js ── */
// 角色卡广场（共创平台 M1）——**列表**：分类 / 标签 / 卡片 / 分页 / 清筛选 / 去登录
//
// 2026-10-01（P4-7）自 js/panels/plaza.js **原样拆出**（纯结构拆分，行为零变化）：
// 顶层状态（`_pzS`/`_pzCards`/`_pzGen`/`_pzAbort`）与 DOM 引用仍住在外壳 js/panels/plaza.js，
// 这里只放行为；新加的名字**必须带 `_pz` 前缀**（bundle 单作用域，重名会静默覆盖）。
// 四条实现约束见 js/panels/plaza.js 头部（图片走 objectURL / 不轮询 / 分页 ≤50 / 关闭即 revoke）。

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


/* ── 来源：js/panels/plaza_detail.js ── */
// 角色卡广场（共创平台 M1）——**详情 + 安装/卸载 + 已装清单与配额**
//
// 2026-10-01（P4-7）自 js/panels/plaza.js **原样拆出**（纯结构拆分，行为零变化）：
// 详情是覆盖在列表上的一层（#plaza-detail）；安装/卸载改的是**本端**数据根（必须由本端执行），
// 与配额条（#pz-quota）共用 `_pzInstalled`（住在 js/panels/plaza.js 外壳）。
// 新加的名字**必须带 `_pz` 前缀**（bundle 单作用域，重名会静默覆盖）。

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


/* ── 来源：js/panels/plaza_forge.js ── */
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


/* ── 来源：js/panels/plaza_forge_files.js ── */
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


/* ── 来源：js/panels/plaza_forge_images.js ── */
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


/* ── 来源：js/panels/plaza_forge_form.js ── */
// 角色卡制作（共创平台 M2/P4-7c）——**表单数据层**：元数据 + 标签 + 读写/校验/回填
//
// 2026-10-01（P4-7c）：契约 06 §3.2 的 payload 从"固定几栏"改成
//   {…元数据…, opening:{narrations,first_messages}, files:[{path,text}], images:{avatar,thumb,display,stickers[]}}
// 于是本文件只留**元数据与标签**的读写校验；文本文件在 plaza_forge_files.js（_pff*），
// 图片在 plaza_forge_images.js（_pfi*）。三者共用外壳的 `_pfS`/`_pfEl`（bundle 单作用域）。
// 新加的名字**必须带 `_pf` 前缀**（重名会静默覆盖）。

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


/* ── 来源：js/panels/plaza_forge_review.js ── */
// 角色卡制作（共创平台 M2）——**生命周期**：草稿箱 + 存草稿 + 审核闸门与弹窗 + 发布/下架/删除
//
// 2026-10-01（P4-7b）自 js/panels/plaza_forge.js **原样拆出**（纯结构拆分，行为零变化）：
// 这一层管"存了之后怎么走"：草稿箱列表 → 继续编辑/发布 → 提交 AI 审核 → 发布到广场 / 下架 / 删除。
// 审核闸门（_pfMarkDirty/_pfSyncBtns）是本层的地基：**任何一次改动都让审核结论作废**，
// 表单层的输入事件只是调它，不复制它的判断（约束 2）。
// 顶层状态（`_pfS`/`_pfSeq`/`_pfModalSeq`/`_pfEl`）与 DOM 引用仍住在外壳 js/panels/plaza_forge.js；
// 新加的名字**必须带 `_pf` 前缀**（bundle 单作用域，重名会静默覆盖）。
// ⚠ 审核弹窗**只在这一刻读一次用户自己的 API Key**，随即清空、不落任何存储（约束 1）。

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


/* ── 来源：js/panels/plaza_forge_local.js ── */
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


/* ── 来源：js/panels/plaza_admin.js ── */
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


/* ── 来源：js/settings.js ── */
// 设置面板配置：供应商与模型管理 / 主动性预设 / 配置加载（loadConfig）与自动保存（_scheduleAutoSave）

// ═══════════════════════════════════════════
// 配置管理
// ═══════════════════════════════════════════
// 配置管理（设置页分组：账号与连接 / 主动消息 / 模型与速度 / 外观 / 数据与系统）
// A8 多供应商：_providers 列表 + _activeId（本地存后端 config.json；服务器版存浏览器 localStorage）
let _providers = [];
let _activeId = "deepseek";
const PROVIDERS_LS_KEY = "firefly_providers";
const ACTIVE_PROVIDER_LS_KEY = "firefly_active_provider";
const _MODEL_INPUT_IDS = ["retriever-model-input", "analyzer-model-input", "polisher-model-input", "organizer-model-input"];

const _PROACTIVE_PRESETS = {
    less:   { hard: 8, soft: 0.25 },
    medium: { hard: 6, soft: 0.35 },
    often:  { hard: 4, soft: 0.50 },
};
let _configLoaded = false;
let _saveTimer = null;

function _$(id) { return document.getElementById(id); }

function _proactivePresetName(hard, soft) {
    if (hard <= 4 && soft >= 0.45) return "often";
    if (hard >= 8) return "less";
    return "medium";
}

function _activeProviderObject() {
    return _providers.find(p => p.id === _activeId) || _providers[0] || null;
}

function _providersLS() {
    try { return JSON.parse(localStorage.getItem(PROVIDERS_LS_KEY) || "[]"); } catch (e) { return []; }
}

function _setProvidersLS(list, active) {
    try {
        localStorage.setItem(PROVIDERS_LS_KEY, JSON.stringify(list));
        localStorage.setItem(ACTIVE_PROVIDER_LS_KEY, active);
    } catch (e) {}
    // 兼容旧字段：relay.js / fetch 包装器仍读 firefly_api_key / firefly_api_base
    try {
        const p = list.find(x => x.id === active);
        if (p) {
            localStorage.setItem("firefly_api_key", p.api_key || "");
            localStorage.setItem("firefly_api_base", p.base_url || "");
        }
    } catch (e) {}
}

async function _getProviderModels(provider) {
    // 本地版：后端带 Key 转发；服务器版：浏览器直连（Key 在本机）
    if (!provider.base_url || !/^https?:\/\//.test(provider.base_url)) {
        throw new Error("接口地址必须是 http(s) 开头");
    }
    if (!provider.api_key) throw new Error("请先填写该供应商的 API Key");
    let resp;
    if (IS_SERVER) {
        resp = await fetch(provider.base_url.replace(/\/+$/, "") + "/models", {
            headers: { "Authorization": "Bearer " + provider.api_key },
        });
    } else {
        resp = await fetch("/models?provider=" + encodeURIComponent(provider.id));
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok && !IS_SERVER) throw new Error(data.error || "获取失败");
    if (IS_SERVER && !resp.ok) {
        // 直连失败（CORS/网络）→ 让后端代查（服务器转发自带 Key 的请求）
        throw new Error("直连获取失败（" + (data.error && data.error.message ? data.error.message : resp.status) + "）");
    }
    const models = IS_SERVER ? (data.data || []).map(m => m.id).filter(Boolean)
                             : (data.models || []);
    if (!models.length) throw new Error(IS_SERVER ? "供应商未返回模型清单" : "未返回模型清单");
    return models;
}

function _renderProviderSelect() {
    const sel = _$("provider-select");
    if (!sel) return;
    sel.innerHTML = "";
    for (const p of _providers) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name + ((p.api_key || p._has_key) ? "" : "（未填 Key）");
        sel.appendChild(opt);
    }
    if (_providers.length) sel.value = _activeId;
    try { sel._uiSelectSync && sel._uiSelectSync(); } catch (e) {}   // 自绘层跟随 options 重建
}

function _renderModelSuggest() {
    const dl = _$("model-suggest");
    const p = _activeProviderObject();
    if (!dl) return;
    const models = (p && p.models && p.models.length) ? p.models
        : ["deepseek-flash", "deepseek-v4-pro"];
    dl.innerHTML = "";
    for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m;
        dl.appendChild(opt);
    }
}

function _updateKeyGuide() {
    const box = _$("provider-guide");
    if (!box) return;
    const p = _activeProviderObject();
    if (!p) { box.innerHTML = ""; return; }
    const isDp = /deepseek\.com/.test(p.base_url || "");
    // A5（审计 2026-09-15）：p.name / p.base_url 是用户表单原样输入（持久化在
    // localStorage），原样插 innerHTML = 存储型 XSS——可直接读同域 firefly_providers
    // 里的 API Key 与 firefly_token。先转义再拼模板。
    const pname = escapeHtml(p.name || "");
    const pbase = escapeHtml(String(p.base_url || "").replace(/\/+$/, ""));
    box.innerHTML = isDp
        ? `① 浏览器打开 <b>platform.deepseek.com</b>，注册并登录<br>② 左侧「API Keys」→ 创建，复制 <b>sk-</b> 开头的 Key<br>③ 粘贴到「${pname}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`
        : `① 打开供应商控制台（<b>${pbase}</b> 所在站点主页）创建 API Key<br>② 粘贴到「${pname}」的 Key 输入框 → 保存（Key 只存本机，不会上传）`;
}

function _openProviderForm(provider) {
    const form = _$("provider-form");
    if (!form) return;
    form.style.display = "block";
    _$("provider-name").value = provider ? provider.name : "";
    _$("provider-base").value = provider ? provider.base_url : "https://";
    _$("provider-key").value = "";
    _$("provider-key").placeholder = provider && provider.api_key ? "已设置，留空保留" : "sk-...";
    _$("provider-form-msg").textContent = "";
    form.dataset.editing = provider ? provider.id : "";
    _$("provider-delete").style.display = provider ? "" : "none";
}

function _closeProviderForm() {
    const form = _$("provider-form");
    if (form) form.style.display = "none";
}

async function _fetchAndFillModels() {
    const msg = _$("provider-form-msg");
    const id = _$("provider-form").dataset.editing;
    const name = _$("provider-name").value.trim();
    const base = _$("provider-base").value.trim().replace(/\/+$/, "");
    const key = _$("provider-key").value.trim();
    const cur = _providers.find(p => p.id === id) || {};
    if (!/^https?:\/\//.test(base)) { msg.textContent = "接口地址必须是 http(s) 开头"; return; }
    msg.textContent = "获取中…";
    try {
        const models = await _getProviderModels({ id: id || cur.id || "custom", base_url: base, api_key: key || cur.api_key });
        const p = _providers.find(x => x.id === id);
        if (p) { p.models = models; _renderModelSuggest(); msg.textContent = "已获取 " + models.length + " 个模型，记得点「保存供应商」"; }
        else { msg.textContent = "模型清单：" + models.slice(0, 5).join("、") + (models.length > 5 ? " 等" + models.length + " 个" : "") + "（保存供应商后生效）"; }
    } catch (e) { msg.textContent = "获取失败：" + e.message; }
}

async function _saveProviderForm() {
    const msg = _$("provider-form-msg");
    const name = _$("provider-name").value.trim();
    const base = _$("provider-base").value.trim().replace(/\/+$/, "");
    const key = _$("provider-key").value.trim();
    const editing = _$("provider-form").dataset.editing;
    if (!name) { msg.textContent = "请填写名称"; return; }
    if (!/^https?:\/\//.test(base)) { msg.textContent = "接口地址必须是 http(s) 开头"; return; }
    const existing = editing && _providers.find(p => p.id === editing);
    if (existing) {
        existing.name = name; existing.base_url = base;
        if (key) existing.api_key = key;
    } else {
        const id = "p" + Date.now().toString(36);
        _providers.push({ id, name, base_url: base, api_key: key,
                          models: [], caps: {} });
        _activeId = id;
    }
    _renderProviderSelect();
    _renderModelSuggest();
    _updateKeyGuide();
    updateSettingsSummaries();
    await saveConfigNow(true);
    _closeProviderForm();
}

function _deleteProviderForm() {
    const editing = _$("provider-form").dataset.editing;
    if (!editing || _providers.length <= 1) { _$("provider-form-msg").textContent = "至少保留一个供应商"; return; }
    if (!confirm("删除供应商「" + editing + "」？")) return;
    _providers = _providers.filter(p => p.id !== editing);
    if (_activeId === editing) _activeId = _providers[0].id;
    _renderProviderSelect();
    _renderModelSuggest();
    _updateKeyGuide();
    updateSettingsSummaries();
    saveConfigNow(true);
    _closeProviderForm();
}

function _applyProactivePreset(name) {
    const p = _PROACTIVE_PRESETS[name] || _PROACTIVE_PRESETS.medium;
    _$("proactive-hard-slider").value = p.hard;
    _$("proactive-soft-slider").value = Math.round(p.soft * 100);
    _$("proactive-hard-value").textContent = p.hard;
    _$("proactive-soft-value").textContent = Math.round(p.soft * 100) + "%";
    updateSettingsSummaries();
}

function updateSettingsSummaries() {
    const ps = _$("proactive-summary");
    if (ps) {
        const on = _$("proactive-enabled").checked;
        const preset = _$("proactive-preset");
        ps.textContent = on ? ("开启 · " + (preset && preset.selectedOptions[0] ? preset.selectedOptions[0].textContent : "偶尔")) : "已关闭";
    }
    const ms = _$("model-summary");
    if (ms) {
        const p = _activeProviderObject();
        const pm = _$("polisher-model-input") ? _$("polisher-model-input").value.trim() : "";
        ms.textContent = (p ? p.name : "DeepSeek") + " · " + (pm || "Flash");
    }
}

function _buildSettingsPayload() {
    // 主动消息设置已移入角色详情页（包级配置），此处不再提交——后端保留既有值
    return {
        analyzer_model: _$("analyzer-model-input").value.trim(),
        retriever_model: _$("retriever-model-input").value.trim(),
        organizer_model: _$("organizer-model-input").value.trim(),
        polisher_model: _$("polisher-model-input").value.trim(),
        retriever_effort: _$("retriever-effort-select").value,
        analyzer_effort: _$("analyzer-effort-select").value,
        polisher_effort: _$("polisher-effort-select").value,
        organizer_effort: _$("organizer-effort-select").value,
        retriever_temperature: parseFloat(_$("retriever-temp-slider").value) || 0,
    };
}

async function _postSettings(payload, msg) {
    const resp = await fetch("/set-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        msg.textContent = "保存失败：" + (data.error || "请稍后再试");
        return false;
    }
    // 注意：这里不再改 S._hiddenEnabled——本页已无「隐藏式」控件，
    // 该开关按角色卡存（角色详情页 → 主动消息），由 panels.js 同步。
    msg.textContent = "已保存 ✓";
    clearTimeout(msg._timer);
    msg._timer = setTimeout(() => { msg.textContent = ""; }, 2500);
    return true;
}

async function saveConfigNow(explicit) {
    const msg = _$("config-msg");
    const srcSel = _$("api-source-select");
    const src = srcSel ? srcSel.value : "own";
    const keyInput = _$("key-input");
    const k = keyInput ? keyInput.value.trim() : "";
    const p0 = _activeProviderObject();
    if (p0 && k) p0.api_key = k;   // Key 输入框 → 当前激活供应商
    if (IS_SERVER) {
        try { localStorage.setItem("firefly_api_source", src); } catch (e) {}
        if (src !== "proxy") _setProvidersLS(_providers, _activeId);
        if (keyInput) keyInput.value = "";
    }
    const payload = _buildSettingsPayload();
    if (!IS_SERVER) {
        // 本地版：供应商列表 + 激活项随配置落盘（Key 在 providers 内）
        payload.providers = _providers;
        payload.active_provider = _activeId;
        if (k) payload.api_key = k;   // 后端写入激活供应商
    }
    if (explicit) msg.textContent = "保存中…";
    const ok = await _postSettings(payload, msg);
    if (ok && keyInput) keyInput.value = "";
}

function _scheduleAutoSave() {
    if (!_configLoaded) return;
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        const msg = _$("config-msg");
        if (msg) msg.textContent = "自动保存中…";
        saveConfigNow(false);
    }, 400);
}

// ═══════════════════════════════════════════
// 运行版本三元组（用户报障时的唯一凭据）
//
// 为什么要有它：热更新**不改版本号**（改了会破坏“补丁针对哪个底座”的契约），
// 所以“用户在跑哪份代码”= 底座版本 + 热更序号 + 清单指纹 三者合起来才说得清。
// 用户 2026-09-25 的问题正是“不然无法确定用户处于哪个版本”，这是它的答案：
// 让用户一句话（或一次点击）就能把这三项给我们，并且随诊断包一起发。
// ═══════════════════════════════════════════
let _runningIdText = "";

function _renderRunningInfo(running) {
    const el = document.getElementById("running-id-text");
    const msg = document.getElementById("running-id-msg");
    if (msg) msg.textContent = "";
    if (!el) return;
    if (!running || typeof running !== "object") {
        el.textContent = "运行版本：读取失败";
        _runningIdText = "";
        return;
    }
    const base = String(running.base_version || "");
    const serial = Number(running.hot_serial || 0);
    const hash = String(running.patch_hash || "");
    const layer = String(running.layer || "");
    // 展示口径（用户 2026-09-25 拍板）：0.9.0_hot1 —— 下划线后缀，hot=热更新、数字=第几个。
    // 没打补丁就只显示底座版本（不显示 _hot0：后缀的含义就是"打过补丁"）。
    const disp = String(running.display_version || "") ||
        (serial ? base + "_hot" + serial : base);
    _runningIdText = String(running.id || "") || disp;
    el.textContent = "运行版本：" + disp +
        (serial && layer ? `（${layer} 层）` : "") +
        (hash ? ` · ${hash}` : "");
}

function _runningMsg(text, ok) {
    const el = document.getElementById("running-id-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = ok ? "var(--fg-accent)" : "var(--fg-muted)";
}

/** 复制运行版本串（报障时直接贴给我们）。老 WebView 无 clipboard API → 退回选中提示。 */
async function copyRunningId() {
    const text = _runningIdText || "（还没读到运行版本，先打开设置面板）";
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            throw new Error("no clipboard api");
        }
        _runningMsg("已复制：" + text, true);
        try { showToast("运行版本已复制，报问题时贴给我即可"); } catch (e) {}
    } catch (e) {
        _runningMsg("复制失败，请手动记下：" + text);
    }
}

const _copyRunningBtn = document.getElementById("copy-running-id-btn");
if (_copyRunningBtn) {
    _copyRunningBtn.addEventListener("click", () => { copyRunningId(); });
}
window.copyRunningId = copyRunningId;

async function loadConfig() {
    const ids = {
        a: "analyzer-model-input", r: "retriever-model-input",
        o: "organizer-model-input", p: "polisher-model-input",
        re: "retriever-effort-select", ae: "analyzer-effort-select",
        pe: "polisher-effort-select", oe: "organizer-effort-select",
        rt: "retriever-temp-slider", rtv: "retriever-temp-value",
        k: "key-input", m: "config-msg",
        pe_: "proactive-enabled", ph: "proactive-hard-slider",
        phv: "proactive-hard-value", ps: "proactive-soft-slider",
        psv: "proactive-soft-value",
        pr_: "prob-reply-enabled", pr: "prob-reply-slider",
        prv: "prob-reply-value",
        hr_: "hidden-reply-enabled",
    };
    try {
        const resp = await fetch("/config");
        const data = await resp.json();
        _renderRunningInfo(data.running);   // ← 运行版本三元组（拿到配置就顺手刷新）
        const el = {};
        for (const [k, id] of Object.entries(ids)) el[k] = document.getElementById(id);

        const normEffort = v => (v === "low" ? "low" : v);
        if (el.a) el.a.value = data.analyzer_model || "deepseek-flash";
        if (el.r) el.r.value = data.retriever_model || "deepseek-flash";
        if (el.o) el.o.value = data.organizer_model || "deepseek-flash";
        if (el.p) el.p.value = data.polisher_model || "deepseek-flash";
        if (el.re) el.re.value = normEffort(data.retriever_effort || "none");
        if (el.ae) el.ae.value = normEffort(data.analyzer_effort || "high");
        if (el.pe) el.pe.value = normEffort(data.polisher_effort || "high");
        if (el.oe) el.oe.value = normEffort(data.organizer_effort || "none");

        const hard = data.proactive_hard != null ? data.proactive_hard : 6;
        const soft = data.proactive_soft != null ? data.proactive_soft : 0.35;
        const prob = data.prob_reply_value != null ? data.prob_reply_value : 0.10;
        if (el.pe_) el.pe_.checked = data.proactive_enabled !== false;
        if (el.ph) {
            el.ph.value = hard;
            if (el.phv) el.phv.textContent = hard;
        }
        if (el.ps) {
            el.ps.value = Math.round(soft * 100);
            if (el.psv) el.psv.textContent = Math.round(soft * 100) + "%";
        }
        if (el.pr_) el.pr_.checked = data.prob_reply_enabled !== false;
        if (el.pr) {
            el.pr.value = Math.round(prob * 100);
            if (el.prv) el.prv.textContent = Math.round(prob * 100) + "%";
        }
        if (el.hr_) el.hr_.checked = data.hidden_reply_enabled !== false;
        S._hiddenEnabled = data.hidden_reply_enabled !== false;
        const pp = _$("proactive-preset");
        if (pp) pp.value = _proactivePresetName(hard, soft);

        // 供应商（A8）：本地版走后端 /config；服务器版供应商配置在浏览器 localStorage
        if (IS_SERVER) {
            _providers = _providersLS();
            _activeId = (() => { try { return localStorage.getItem(ACTIVE_PROVIDER_LS_KEY) || ""; } catch (e) { return ""; } })();
            if (!_providers.length) {
                // 服务器模式首次初始化：继承 legacy 字段（firefly_api_key/api_base——
                // 旧版/升级前直接存这里），避免升级后首个会话把已有 Key 当“未设置”丢一次
                const legacyKey = getLocalApiKey();
                const legacyBase = (() => { try { return localStorage.getItem("firefly_api_base") || ""; } catch (e) { return ""; } })();
                _providers = [{ id: "deepseek", name: "DeepSeek",
                                base_url: legacyBase || "https://api.deepseek.com/v1",
                                api_key: legacyKey || "", models: [], caps: {} }];
                _activeId = "deepseek";
                _setProvidersLS(_providers, _activeId);
            }
            const p0 = _providers.find(p => p.id === _activeId) || _providers[0];
            if (p0) _activeId = p0.id;
        } else {
            _providers = (data.providers || []).map(p => ({
                id: p.id, name: p.name, base_url: p.base_url,
                api_key: "", models: p.models || [], caps: p.caps || {},
                _has_key: !!p.has_key, _key_prefix: p.key_prefix || "",
            }));
            _activeId = data.active_provider || (_providers[0] && _providers[0].id) || "deepseek";
        }

        const srcSel = _$("api-source-select");
        const srcField = _$("api-source-field");
        const localSrc = (() => { try { return localStorage.getItem("firefly_api_source") || "own"; } catch (e) { return "own"; } })();
        if (srcSel) {
            srcSel.value = localSrc === "proxy" ? "proxy" : "own";
            applyApiSource(IS_SERVER && localSrc === "proxy");
        }
        if (srcField) srcField.style.display = IS_SERVER ? "" : "none";
        // A5：增量同步（本地版登录后可用；服务器版数据天然在云端）
        const syncNowField = _$("sync-now-field");
        if (syncNowField) syncNowField.style.display = IS_SERVER ? "none" : "";

        if (data.retriever_temperature != null && el.rt) {
            el.rt.value = data.retriever_temperature;
            if (el.rtv) el.rtv.textContent = Number(data.retriever_temperature).toFixed(1);
        }

        if (el.m) {
            const keyLabel = _$("key-label");
            const pA = _activeProviderObject();
            const providerLabel = pA ? pA.name : "DeepSeek";
            if (IS_SERVER) {
                const localKey = getLocalApiKey();
                el.m.textContent = localKey ? "Key 已设置（仅存本机浏览器）" : "尚未设置 API Key（不会上传服务器）";
                if (keyLabel) keyLabel.textContent = "API Key（存于本机浏览器，不会上传服务器）";
            } else {
                el.m.textContent = pA && pA._has_key
                    ? "Key 已设置（" + (pA._key_prefix || "仅本机") + "）"
                    : "尚未设置 API Key";
                if (keyLabel) keyLabel.textContent = "API Key（存本机配置文件，仅本机使用）";
            }
        }
        if (el.k) {
            if (IS_SERVER) {
                const localKey = getLocalApiKey();
                el.k.placeholder = localKey ? "已设置，留空则保留" : "sk-...";
            } else {
                const pA = _activeProviderObject();
                el.k.placeholder = pA && pA._has_key ? "已设置，留空则保留原 Key" : "sk-...";
            }
            el.k.value = "";
        }

        // 供应商 UI（选择器 / 模型建议 / 获取 Key 教程）
        _renderProviderSelect();
        _renderModelSuggest();
        _updateKeyGuide();

        const hiddenField = _$("hidden-reply-field");
        if (hiddenField) hiddenField.style.display = window.androidWakeLock ? "" : "none";

        const exitRow = _$("app-exit-row");
        if (exitRow && data.platform === "pc") {
            exitRow.style.display = "";
            const exitBtn = _$("app-exit-btn");
            if (exitBtn) exitBtn.onclick = () => {
                if (!confirm("确定退出 Firefly 吗？聊天数据已实时保存，下次启动继续。")) return;
                exitBtn.disabled = true;
                exitBtn.textContent = "正在退出…";
                fetch("/shutdown", {method: "GET"}).catch(() => {});
            };
        }

        _configLoaded = true;
        updateSettingsSummaries();
        uiSelectEnhance(document.getElementById("settings-panel"));   // 自绘下拉（原生 select 弹窗无法主题化）
        return data;
    } catch (e) { return {has_key: false}; }
}

async function checkKey() {
    try { await loadConfig(); } catch (e) { /* 服务未就绪，静默 */ }
}

(function initSetGroups() {
    document.querySelectorAll("#settings-panel .set-head").forEach(head => {
        head.addEventListener("click", () => {
            const group = head.closest(".set-group");
            const willOpen = !group.classList.contains("open");
            document.querySelectorAll("#settings-panel .set-group").forEach(g => g.classList.remove("open"));
            if (willOpen) group.classList.add("open");
        });
    });
})();

_$("retriever-temp-slider")?.addEventListener("input", () => {
    _$("retriever-temp-value").textContent = Number(_$("retriever-temp-slider").value).toFixed(1);
});
_$("proactive-hard-slider")?.addEventListener("input", () => {
    _$("proactive-hard-value").textContent = _$("proactive-hard-slider").value;
});
_$("proactive-soft-slider")?.addEventListener("input", () => {
    _$("proactive-soft-value").textContent = _$("proactive-soft-slider").value + "%";
});
_$("prob-reply-slider")?.addEventListener("input", () => {
    _$("prob-reply-value").textContent = _$("prob-reply-slider").value + "%";
});

_$("proactive-preset")?.addEventListener("change", () => {
    _applyProactivePreset(_$("proactive-preset").value);
    _scheduleAutoSave();
});

["retriever-model-input", "analyzer-model-input", "polisher-model-input", "organizer-model-input",
 "retriever-effort-select", "analyzer-effort-select", "polisher-effort-select", "organizer-effort-select",
 "retriever-temp-slider", "proactive-enabled", "proactive-hard-slider", "proactive-soft-slider",
 "prob-reply-enabled", "prob-reply-slider", "hidden-reply-enabled"].forEach(id => {
    const el = _$(id);
    if (el) el.addEventListener("change", () => { updateSettingsSummaries(); _scheduleAutoSave(); });
});

// A8 供应商 UI 接线
_$("provider-select")?.addEventListener("change", () => {
    const sel = _$("provider-select");
    if (sel && sel.value) {
        _activeId = sel.value;
        _renderModelSuggest();
        _updateKeyGuide();
        updateSettingsSummaries();
        _scheduleAutoSave();
    }
});
_$("provider-add")?.addEventListener("click", () => _openProviderForm(null));
_$("provider-edit")?.addEventListener("click", () => {
    const p = _activeProviderObject();
    if (p) _openProviderForm(p);
});
_$("provider-save")?.addEventListener("click", () => _saveProviderForm());
_$("provider-delete")?.addEventListener("click", () => _deleteProviderForm());
_$("provider-cancel")?.addEventListener("click", () => _closeProviderForm());
_$("provider-fetch-models")?.addEventListener("click", () => _fetchAndFillModels());

const apiSourceSel = _$("api-source-select");
if (apiSourceSel) {
    apiSourceSel.addEventListener("change", () => applyApiSource(apiSourceSel.value === "proxy"));
}

_$("key-save")?.addEventListener("click", () => saveConfigNow(true));


/* ── 来源：js/update.js ── */
// 检查更新（★ 2026-09-24 改为**服务器主导**，见 docs/版本更新规范.md）
// 注意：CURRENT_VERSION 是前端版本号单一来源，tools/check_version.py 校验本文件（及 server/frontend 同步副本）

// ═══════════════════════════════════════════
// 检查更新（主通道 = 服务器 /update-manifest；GitHub/Gitee 仅作最后兜底）
//
// 为什么改（旧实现的两个硬伤）：
//   1. GitHub 国内不稳 —— 用户侧超时/被墙，检测时好时坏；
//   2. "成功即返回"吞掉更新 —— 旧代码按 GitHub→Gitee 顺序，**先成功者胜**。
//      GitHub 停留在 v0.8.1 时直接 return，Gitee 上的 v0.9.0 被静默丢弃，
//      0.8.1 客户端于是永远显示「已是最新版本」。
//      （实测：Gitee 有 0.9.0、GitHub 忘发 → 大量用户收不到更新提示。）
// 现在：服务器统一管理版本号与下载直链，客户端只认一个端点；
//      兜底阶段也改为**按版本号取最高**，而非先到先得。
// ═══════════════════════════════════════════
const CURRENT_VERSION = "0.9.1";   // 与 android versionName / 安装器 AppVersion 保持一致
// PC 三栏外壳（pc_shell.js，独立 classic script）底部状态栏要显示版本号，
// 它看不到 bundle 作用域，所以暴露一个只读副本（不要在这里写版本，单一来源仍是本文件）。
window.__appVersion = CURRENT_VERSION;
// 设置面板版本号动态显示（单一版本源：CURRENT_VERSION；替代 index.html 硬编码文案）
const curVersionEl = document.getElementById("current-version");
if (curVersionEl) curVersionEl.textContent = "v" + CURRENT_VERSION;
const UPDATE_SOURCES = [
    { api: "https://api.github.com/repos/10csc/firefly/releases/latest", html: "https://github.com/10csc/firefly/releases" },
    { api: "https://gitee.com/api/v5/repos/cpt-asymmetry/firefly/releases/latest", html: "https://gitee.com/cpt-asymmetry/firefly/releases" },
];
// 公共下载页：APK 主通道走 Gitee，微信/QQ 等不支持 blob 下载的内置浏览器会自动走服务器直连（正确 MIME）
const DOWNLOAD_PAGE_URL = "http://101.200.14.126:8787/download/";
function compareVersions(a, b) {
    const pa = String(a).split(".").map(n => parseInt(n) || 0);
    const pb = String(b).split(".").map(n => parseInt(n) || 0);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const d = (pa[i] || 0) - (pb[i] || 0);
        if (d !== 0) return d;
    }
    return 0;
}
function _isAndroid() {
    return /Android/i.test(navigator.userAgent) && !/Windows|Mac|Linux/i.test(navigator.userAgent);
}
// 资产匹配：PC 装包 exe / 安卓 apk（Gitee 资产名可能带前缀，模糊匹配）
function _matchAsset(assets, re) {
    if (!Array.isArray(assets)) return "";
    for (const a of assets) {
        const n = String(a.name || a.browser_download_url || "");
        if (re.test(n)) return a.browser_download_url || n;
    }
    return "";
}
// 从服务器清单挑本平台的下载直链；未随发（url 空）→ 回退下载页
function _pickFromManifest(m, isAndroid) {
    const a = (m && m.assets && (isAndroid ? m.assets.apk : m.assets.exe)) || {};
    return String(a.url || "").trim();
}
// 渲染「发现新版本」——三种可下载形态（服务器自动下载 / 清单直链 / 下载页）
function _renderFound(msg, latest, cur, opts) {
    const notes = opts.notes ? ` ｜ <a href="${escapeHtml(opts.notes)}" target="_blank" rel="noopener" style="color:var(--fg-muted)">发行说明</a>` : "";
    const head = `发现新版本 <b style="color:var(--fg-accent)">${escapeHtml(latest)}</b>（当前 ${escapeHtml(cur)}）`;
    if (opts.auto) {
        msg.innerHTML = head + `<br>` +
            `<button id="auto-update-btn" style="margin-top:6px;padding:4px 12px;border-radius:6px;border:none;background:var(--fg-accent);color:#fff;cursor:pointer">自动更新</button>` +
            notes;
        const btn = document.getElementById("auto-update-btn");
        if (btn) btn.addEventListener("click", () => autoUpdate(opts.isAndroid));
        return;
    }
    if (opts.directUrl) {
        msg.innerHTML = head + `<br>` +
            `<a href="${escapeHtml(opts.directUrl)}" target="_blank" rel="noopener" style="color:var(--fg-bright)">下载安装包</a>` + notes;
        return;
    }
    msg.innerHTML = head + `<br>` +
        `<a href="${DOWNLOAD_PAGE_URL}" target="_blank" rel="noopener" style="color:var(--fg-bright)">前往下载页</a>` + notes;
}
async function checkUpdate() {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "检查中…";
    const isAndroid = _isAndroid();
    if (IS_SERVER) {
        // 服务器模式：问本进程的 /update-manifest（服务器管理员维护 update.json）
        try {
            const resp = await fetch("/update-manifest", {cache: "no-store"});
            const m = await resp.json();
            if (!m.ok) throw new Error(m.error || "no manifest");
            const latest = String(m.tag || "").replace(/^v/i, "");
            const cur = String(CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            if (compareVersions(latest, cur) > 0) {
                _renderFound(msg, latest, cur, {
                    notes: m.notes_url,
                    directUrl: _pickFromManifest(m, isAndroid),
                });
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
        } catch (e) {
            msg.textContent = "检查更新失败：暂时连不上更新服务，请稍后重试";
        }
        return;
    }
    // 本地模式：优先走本地后端（后端会去问服务器清单，权威版本源 + 自动下载能力），
    // 失败退回纯前端双源检测（取版本最高者）
    try {
        // /check-update 只注册在 POST_ROUTES（GET 会 404，曾长期被前端双源兜底掩盖）
        const lr = await fetch("/check-update", {method: "POST", cache: "no-store"});
        if (lr.ok) {
            const d = await lr.json();
            if (!d.ok) throw new Error(d.error || "check fail");
            const latest = String(d.tag || "").replace(/^v/i, "");
            const cur = String(d.current || CURRENT_VERSION);
            if (!latest) throw new Error("no tag");
            if (compareVersions(latest, cur) > 0) {
                _renderFound(msg, latest, cur, {
                    auto: true, isAndroid,
                    notes: d.notes_url || d.html_url,
                });
            } else {
                msg.textContent = `已是最新版本 ${cur} ✓`;
            }
            return;
        }
    } catch (e) { /* 降级到前端直连 */ }
    // 前端直连双源（后端接口不可用时）——★ 取**版本最高**者，不再"先成功者胜"
    let best = null;
    for (const src of UPDATE_SOURCES) {
        try {
            const resp = await fetch(src.api, {cache: "no-store"});
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            const data = await resp.json();
            const latest = String(data.tag_name || "").replace(/^v/i, "");
            if (!latest) throw new Error("no tag");
            if (!best || compareVersions(latest, best.latest) > 0) {
                best = { latest, data, src };
            }
        } catch (e) { /* 单源失败继续看下一个 */ }
    }
    if (!best) {
        msg.textContent = "检查更新失败：暂时连不上更新服务，请稍后重试";
        return;
    }
    const { latest, data, src } = best;
    if (compareVersions(latest, CURRENT_VERSION) > 0) {
        const exeUrl = _matchAsset(data.assets, /\.exe$/i);
        const apkUrl = _matchAsset(data.assets, /\.apk$/i);
        _renderFound(msg, latest, CURRENT_VERSION, {
            notes: src.html,
            directUrl: isAndroid ? (apkUrl || "") : (exeUrl || ""),
        });
    } else {
        msg.textContent = `已是最新版本 ${CURRENT_VERSION} ✓`;
    }
}
// 检查更新按钮接线（设置面板版本区；修复前该按钮无任何事件绑定，点击无反应）
const checkUpdateBtn = document.getElementById("check-update-btn");
if (checkUpdateBtn) checkUpdateBtn.addEventListener("click", checkUpdate);

// 自动更新：后端下载安装包 → PC 静默安装并重启；安卓交**系统安装器**（APP 内装完，不用再下第二遍）
//
// ★ 2026-09-25 修掉的那半：旧实现在安卓上"下载完 85MB 之后，提示用户去下载页再下一次"
//   —— 等于一个包下两遍、一次都不装。现在由壳（FireflyJs.installApk）把**已经校验过 sha256**
//   的包交给系统安装器；没有壳（PC 浏览器 / 服务器模式）才退回旧路径。
async function autoUpdate(isAndroid) {
    const msg = document.getElementById("update-msg");
    if (!msg) return;
    msg.textContent = "下载中…（约 30-60 秒，请勿关闭应用）";
    try {
        const resp = await fetch("/update-download", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({kind: isAndroid ? "apk" : "exe"}),
        });
        const data = await resp.json();
        if (!data.ok) { msg.textContent = "下载失败：" + (data.error || ""); return; }
        if (isAndroid) {
            // 安卓：后端已下好并校验 → 让壳交系统安装器。
            // 壳返回空串 = 已经拉起安装器；非空 = 拒绝/失败原因（如实显示，不假装成功）
            let err = "";
            try {
                const sh = (window.FireflyJs
                    && typeof window.FireflyJs.installApk === "function")
                    ? window.FireflyJs : null;
                if (sh) err = String(sh.installApk(data.name || "") || "");
                else err = "（当前环境不是应用内，无法自动安装）";
            } catch (e) {
                err = (e && e.message) ? e.message : String(e);
            }
            if (err) {
                msg.textContent = "下载完成，但自动安装未启动：" + err
                    + " ｜ 也可前往下载页手动安装";
                return;
            }
            msg.textContent = "下载完成 → 请在系统弹窗里点「安装」（覆盖安装，聊天数据保留）";
            return;
        }
        if (data.installing) {
            msg.textContent = "下载完成，安装程序即将启动…应用会自动关闭，请稍候。";
            setTimeout(() => { location.href = "about:blank"; }, 1500);
        } else {
            msg.innerHTML = `下载完成 → <a href="file://${data.path}" target="_blank" rel="noopener" style="color:var(--fg-bright)">点击运行安装</a>`;
        }
    } catch (e) {
        console.warn("[update] 自动更新失败", e);   // 原始异常落日志，不拼进用户可见串
        msg.textContent = "自动更新没成功；请稍后重试，或前往下载页手动安装最新版";
    }
}


/* ── 来源：js/sync.js ── */
// 增量同步 UI（/sync/now）：进度条 / 冲突警告 toast / 10 分钟节流 / 回前台补同步

/** A1 增量同步（W4 编排端点 /sync/now）：登录后把本地文字数据与云端账号双向合并。
 *  window.autoSyncNow(force)：仅本地版 + 已登录（/auth/state 确认）才发起；
 *  force=false 时 10 分钟节流（localStorage 时间戳），force=true（手动「立即同步」）跳过节流。
 *  进行中显示非阻塞状态条「正在同步…」；完成 toast 计数；冲突橙色警告；失败 toast 原因。
 *  挂接点：登录态确认（api.js initAuth）/ 进聊天页与模式切换（views.js showChat）/ 回前台（下方 visibilitychange）。 */
const _SYNC_THROTTLE_MS = 10 * 60 * 1000;
let _syncing = false;
let _syncPill = null;
let _warnToastTimer = null;

function _syncPillShow(text) {
    if (!_syncPill) {
        _syncPill = document.createElement("div");
        _syncPill.style.cssText = "position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:998;background:rgba(28,30,46,.96);color:#e8e0d0;border:1px solid rgba(255,196,107,.45);border-radius:10px;padding:7px 14px;font-size:0.75em;box-shadow:0 6px 22px rgba(0,0,0,.45);pointer-events:none;display:none";
        document.body.appendChild(_syncPill);
    }
    _syncPill.textContent = text;
    _syncPill.style.display = "block";
}
function _syncPillHide() { if (_syncPill) _syncPill.style.display = "none"; }

/** 橙色警告 toast（样式沿用 _toast 浮层，边框改橙）：冲突备份等需要用户注意但非阻断的提醒 */
function _warnToast(msg) {
    let t = document.getElementById("app-warn-toast");
    if (!t) {
        t = document.createElement("div");
        t.id = "app-warn-toast";
        t.style.cssText = "position:fixed;top:56px;left:50%;transform:translateX(-50%);z-index:999;background:rgba(46,34,22,.97);color:#ffd9b0;border:1px solid rgba(255,150,80,.65);border-radius:10px;padding:9px 16px;font-size:0.8em;max-width:86vw;text-align:center;box-shadow:0 6px 22px rgba(0,0,0,.45);pointer-events:none;display:none";
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(_warnToastTimer);
    _warnToastTimer = setTimeout(() => { t.style.display = "none"; }, 4000);
}

window.autoSyncNow = async function (force) {
    if (IS_SERVER) return;   // 服务器版数据天然在云端，无需同步
    if (_syncing) return;    // 并发护栏：上一次同步还在飞
    if (!force) {
        let last = 0;
        try { last = parseInt(localStorage.getItem("firefly_last_sync") || "0", 10) || 0; } catch (e) {}
        if (Date.now() - last < _SYNC_THROTTLE_MS) return;   // 节流期内静默跳过
    }
    try {
        const st = await (await fetch("/auth/state")).json();
        if (!st.logged_in || st.offline_ok === false) return;   // 未登录/登录已过期：静默跳过
    } catch (e) { return; }   // 本地后端未就绪：静默
    _syncing = true;
    try { localStorage.setItem("firefly_last_sync", String(Date.now())); } catch (e) {}
    const msg = document.getElementById("sync-now-msg");
    if (msg) msg.textContent = "同步中…";
    _syncPillShow("正在同步…");
    try {
        const r = await fetch("/sync/now", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: "all"}),   // 全包同步（全部角色包）
        });
        const d = await r.json().catch(() => ({}));
        if (d.ok) {
            const text = `同步完成：上传 ${(d.uploaded || []).length} · 下载 ${(d.downloaded || []).length} · 合并 ${(d.merged || []).length}`;
            const conflicts = d.conflicts || 0;
            if (msg) msg.textContent = "✓ " + text + (conflicts ? `，冲突 ${conflicts}（见 .sync_backups）` : "");
            showToast(text);
            if (conflicts > 0) _warnToast(`${conflicts} 处冲突已各自备份`);
        } else {
            const err = "同步未完成：" + (d.error || "请先登录账号");
            if (msg) msg.textContent = err;
            showToast(err);
        }
    } catch (e) {
        if (msg) msg.textContent = "网络错误，稍后再试";
        showToast("同步失败：网络错误，稍后再试");
    } finally {
        _syncPillHide();
        _syncing = false;
    }
};
window.syncNow = function () { window.autoSyncNow(true); };   // 手动「立即同步」：强制不节流

// 回前台补一次同步（节流期内自动跳过，不打扰）
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") window.autoSyncNow();
});


/* ── 来源：js/chat_render.js ── */
// 聊天渲染：消息行（文本/表情包/图片/旁白）/ 引用小卡片 / 时间分割 / 打字机占位 / 逐条渲染动画

// 消息渲染
// ═══════════════════════════════════════════
// ═══════════════════════════════════════════
// 滚动到底部（rAF 延迟：等 DOM 更新/键盘 resize 后再滚，QQ/微信式自动拉底）
// ═══════════════════════════════════════════
function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    });
}
// 键盘弹起/收起导致可视高度变化时：若用户原本在底部则自动补滚
if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", () => {
        if (messagesEl.scrollTop + messagesEl.clientHeight >= messagesEl.scrollHeight - 60) {
            scrollToBottom();
        }
    });
}

/** 发送者显示名 —— 统一走 views.js 的 charName()/userName()（**只从当前角色卡取**）。
 *  取不到返回空串，调用方不渲染名字行。 */
function _whoName(who) {
    return who === "user" ? userName() : charName();
}

/** 消息内容外壳：`.msg-col` = 名字行 +（可选引用卡片）+ 内容。
 *
 *  为什么现在**总是**用它（以前只有带引用时才包）：
 *  名字行要与气泡同一侧对齐，就得有个纵向容器；顺带让 `align-items:flex-start`
 *  能把头像对到**名字行顶部**（官方就是这样）——以前没有名字行时用 flex-end，
 *  两行气泡的头像会被拽到气泡底部（用户真机发现"第二行头像又下去了"）。
 */
function _mkCol(who, inner, quote) {
    const col = document.createElement("div");
    col.className = "msg-col";
    const nm = _whoName(who);
    if (nm) {                       // 角色卡没填称呼 → 不渲染名字行（不写死假名字）
        const el = document.createElement("div");
        el.className = "msg-who";
        el.textContent = nm;
        col.appendChild(el);
    }
    if (quote) col.appendChild(_buildQuotePreview(quote));
    if (inner) col.appendChild(inner);
    return col;
}

/** 容错替换：内容节点现在可能不在 row 的直接子层（被 .msg-col 包住），
 *  所以不能用 `row.replaceChild`（会抛 NotFoundError，表现为"占位不显示"）。 */
function _replaceNode(oldNode, newNode) {
    if (oldNode && oldNode.parentNode) oldNode.parentNode.replaceChild(newNode, oldNode);
}

function _addAvatar(row, who) {
    const p = who === "user" ? null : currentPreset();
    if (who !== "user" && !(p && p.avatar)) {
        // F-5：无头像包 → 首字占位圆（否则每条消息行都是破图）
        const d = document.createElement("div");
        d.className = "msg-avatar pack-noimg";
        d.textContent = ((p && (p.char_name || p.name)) || "？").slice(0, 1);
        row.insertBefore(d, row.firstChild);
        return;
    }
    const img = document.createElement("img");
    img.className = "msg-avatar";
    if (who === "user") {
        // 05：用户头像随当前角色包（包内 user_avatar 资产优先，回落内置穹/星选择）
        const p = typeof currentPreset === "function" ? currentPreset() : null;
        img.src = (p && p.user_avatar) || TB_AVATARS[tbChoice];
        img.classList.add("tb-toggle");
        img.title = "点击切换形象";
        img.addEventListener("click", openAvatarPicker);
        img.classList.add("tb-avatar");
    } else {
        // 角色头像按当前预设包（角色预设化）
        img.src = p.avatar;
    }
    row.insertBefore(img, row.firstChild);
}

/** 按 seq 插序：DOM 顺序 = 记录顺序（不管渲染先后）。
 * 有 seq → 找到第一个 seq 更大的行，插它前面（同 seq 追加末尾）；无 seq → 追加。
 * 修复：多批回复动画交错时表情包"堆积"错位——插序保证显示与记录一致。 */
function _insertRow(row, seq) {
    if (seq === null || seq === undefined) { messagesEl.appendChild(row); return; }
    const rows = messagesEl.querySelectorAll(".msg-row[data-seq]");
    let anchor = null;
    for (const r of rows) {
        const s = parseInt(r.dataset.seq, 10);
        if (!isNaN(s) && s > seq) { anchor = r; break; }
    }
    if (anchor) messagesEl.insertBefore(row, anchor);
    else messagesEl.appendChild(row);
}

function addTextMessage(text, who, prepend = false, seq = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");   // 新消息从下方浮现（历史加载不带动画）
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    // 名字行 + 引用卡片 + 气泡统一进 .msg-col（官方每条消息都有名字行；
    // 头像靠 align-items:flex-start 对到名字行顶部）
    row.appendChild(_mkCol(who, bubble, quote));
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    // 已有语音的消息：立刻把语音条贴到气泡下面（缓存表已在内存时走这条快路径；
    // 首次进聊天还没拉到表的情况由 voiceRestoreBarsAuto() 事后补扫）。
    try {
        if (seq !== null && seq !== undefined
            && typeof voiceHas === "function" && voiceHas(seq)
            && typeof voiceAttachBar === "function") {
            const m = (typeof _voiceSeqMap !== "undefined" && _voiceSeqMap)
                ? _voiceSeqMap[CURRENT_MODE] : null;
            voiceAttachBar(row, seq, m ? m[String(seq)] : 0);
        }
    } catch (e) { /* 语音条失败绝不影响消息渲染 */ }
    return row;
}

/** 引用快照 → 气泡上方的小卡片（谁 + 内容摘要） */
function _quoteContentText(q) {
    if (!q) return "";
    if (q.type === "sticker") return "[表情包：" + (q.label || "") + "]";
    if (q.type === "image") return "[图片：" + (q.desc || "（无描述）") + "]";
    if (q.type === "narration") return q.text || "";
    return q.content || "";
}
// 引用卡片里的"谁"：**从当前角色卡取**（原来写死了「流萤」）。
// 用户侧固定「我」——第一人称代词，不是角色名字面量。
function _quoteWhoName(q) {
    return (q && q.who === "user") ? "我" : charName();
}
function _buildQuotePreview(q) {
    const div = document.createElement("div");
    div.className = "quote-preview";
    const who = document.createElement("span");
    who.className = "qp-who";
    who.textContent = _quoteWhoName(q);
    const txt = document.createElement("span");
    txt.className = "qp-text";
    txt.textContent = _quoteContentText(q);
    div.appendChild(who);
    div.appendChild(txt);
    return div;
}

async function addSticker(stickerPath, who, prepend = false, seq = null, label = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (label) row.dataset.stickerLabel = label;   // 长按菜单需要表情含义
    if (!prepend) row.classList.add("float-in");
    // A2 媒体本地策略：local: 引用 → IndexedDB 取本体；缺失降级占位（服务器不保存图片）
    const src = await stickerSrc(stickerPath, IS_SERVER, API_BASE);
    let img = null;
    if (src) {
        img = document.createElement("img");
        img.className = "sticker-img";
        img.src = src;
        img.dataset.stickerPath = stickerPath;
        // 容错：表情包文件缺失（历史遗留/用户删除）时降级为文字占位，不显示裂图
        img.onerror = () => {
            if (img.dataset.fallback) return;
            img.dataset.fallback = "1";
            const span = document.createElement("span");
            span.className = "sticker-fallback";
            span.textContent = "（表情包已失效）";
            _replaceNode(img, span);
        };
    } else {
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = "（表情包已失效）";
        img = span;
    }
    row.appendChild(_mkCol(who, img, quote));
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    return row;
}

/** A9 图片消息渲染：两端统一走后端 /image?id=&mode= 取字节（服务器版也上传落盘，IndexedDB 图片存储已退役）。
 *  服务器版 <img> 标签带不了 Bearer：经鉴权 fetch 拿字节转 objectURL；本地版同源直接 <img src>。
 *  缺失/失败 → 显示 desc 文字占位。 */
async function addImage(msg, who, prepend = false, seq = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly") + " image-row";
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");
    const imgId = (msg.img_id || "").toString().slice(0, 200);
    const desc = (msg.desc || "").toString().slice(0, 300);
    let src = null;
    if (imgId) {
        const url = "/image?id=" + encodeURIComponent(imgId) + "&mode=" + encodeURIComponent(CURRENT_MODE);
        if (IS_SERVER) {
            try {
                const resp = await fetch(url);
                if (resp.ok) { try { src = URL.createObjectURL(await resp.blob()); } catch (e) {} }
            } catch (e) {}
        } else {
            src = url;
        }
    }
    let content;
    if (src) {
        const img = document.createElement("img");
        img.className = "image-img";
        img.style.cssText = "max-width:min(56vw,320px);max-height:280px;border-radius:12px;display:block";
        img.src = src;
        img.dataset.imgId = imgId;
        img.dataset.desc = desc;
        img.onerror = () => {
            if (img.dataset.fallback) return;
            img.dataset.fallback = "1";
            const span = document.createElement("span");
            span.className = "sticker-fallback";
            span.textContent = desc ? `（图片：${desc}）` : "（图片已失效）";
            _replaceNode(img, span);
        };
        content = img;
    } else {
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = desc ? `（图片：${desc}）` : "（图片已失效）";
        span.dataset.imgId = imgId;
        span.dataset.desc = desc;
        content = span;
    }
    row.appendChild(_mkCol(who, content, quote));
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { messagesEl.appendChild(row); scrollToBottom(); }
    return row;
}

function addNarration(text, style, prepend = false, seq = null) {
    // 视觉小说式旁白：scene=居中小字（环境/事件），action=居中括号（动作）
    // 防御：历史数据/LLM 可能自带括号，先剥离避免双重括号
    let t = (text || "").trim();
    if ((t.startsWith("（") && t.endsWith("）")) || (t.startsWith("(") && t.endsWith(")"))) {
        t = t.slice(1, -1).trim();
    }
    const row = document.createElement("div");
    row.className = "msg-row narration-row";
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");
    const el = document.createElement("div");
    el.className = "narration " + (style === "scene" ? "narration-scene" : "narration-action");
    if (style === "action") el.textContent = "（" + t + "）";
    else el.textContent = t;
    row.appendChild(el);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { messagesEl.appendChild(row); scrollToBottom(); }
    return row;
}

function addTimeDivider(timeStr) {
    const div = document.createElement("div");
    div.className = "time-divider";
    div.textContent = timeStr;
    messagesEl.appendChild(div);
}

/** 消息加载占位：三个流水灯圆点（0.5~1s 后替换为真实内容） */
function addTypingBubble(who) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    const bubble = document.createElement("div");
    bubble.className = "bubble typing-bubble";
    bubble.innerHTML = "<span></span><span></span><span></span>";
    row.appendChild(bubble);
    _addAvatar(row, who);
    messagesEl.appendChild(row);
    scrollToBottom();
    return row;
}

function renderMessages(messages, who, data) {
    if (!messages || messages.length === 0) return;
    S._lastRenderTs = Date.now();   // 渲染时间戳（供主动性轮询门控；原二次包装已合并进来）
    const gen = _modeGen;   // 捕获渲染启动时的模式代际
    S._rendering = true;   // 渲染动画开始：防主动轮询中途插入乱序
    // 时间标注：取第一条消息的时间，放居中分割线
    const ts = messages[0].time ? messages[0].time.slice(11, 16) : new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    addTimeDivider(ts);
    // 逐条消息加载：先显示三圆点占位，再替换为真实内容（消息含文本与表情包）
    // 加载时长按字数 0.7~1.5s（表情包按最短 0.7s）；消息之间留 0.5s 空白模拟游戏节奏
    let seq = 0;
    const showNext = () => {
        if (gen !== _modeGen) { S._rendering = false; return; }   // 模式已切换：丢弃剩余动画
        if (seq >= messages.length) {
            S._rendering = false;   // 渲染动画完成
            return;
        }
        const msg = messages[seq++];
        const chars = (msg.content || msg.text || "").length;
        const loadMs = Math.min(1500, Math.max(700, 700 + chars * 25));
        const typingRow = addTypingBubble(who);
        setTimeout(() => {
            if (gen !== _modeGen) { typingRow.remove(); S._rendering = false; return; }
            typingRow.remove();
            if (msg.type === "sticker") addSticker(msg.path, who);
            else if (msg.type === "narration") addNarration(msg.text, msg.style);
            else if (msg.type === "image") addImage(msg, who);
            else addTextMessage(msg.content, who);
            setTimeout(showNext, 500);   // 消息间隔：0.5s 空白
        }, loadMs);
    };
    showNext();
}

// 消息渲染后记录时间（renderMessages 内调用；0.9.0 起已合并进函数本体，保留此注释防回归）


/* ── 来源：js/chat.js ── */
// 聊天核心：长按菜单 / 引用 / 发送四阶段 / 提交窗口状态机 / 数据备份导入导出

// ═══════════════════════════════════════════
// 长按消息菜单（QQ 式）：引用 / 收藏
// ═══════════════════════════════════════════
const _LONG_PRESS_MS = 500;
let _lpTimer = null, _lpRow = null, _lpStart = null;
let _msgMenu = null;
let _quoteTarget = null;   // 引用快照（发送后随消息提交，然后清空）

/** 从消息行提取快照（who/type/content…；seq 只在历史渲染的消息上有） */
function _msgSnapshot(row) {
    const who = row.classList.contains("user") ? "user" : "firefly";
    const snap = {who};
    const seq = parseInt(row.dataset.seq, 10);
    if (!isNaN(seq)) snap.seq = seq;
    if (row.classList.contains("narration-row")) {
        snap.type = "narration";
        const el = row.querySelector(".narration");
        let t = el ? el.textContent : "";
        t = t.replace(/^（|）$/g, "").trim();   // 剥掉旁白自带括号，引用内容更干净
        snap.text = t;
    } else if (row.querySelector(".sticker-img")) {
        snap.type = "sticker";
        snap.label = row.dataset.stickerLabel || "";
        const img = row.querySelector(".sticker-img");
        if (img) snap.path = img.dataset.stickerPath || "";
    } else if (row.querySelector(".image-img")) {
        snap.type = "image";
        const img = row.querySelector(".image-img");
        if (img) {
            snap.img_id = img.dataset.imgId || "";
            snap.desc = img.dataset.desc || "";
        }
    } else {
        snap.type = "text";
        const b = row.querySelector(".bubble");
        snap.content = b ? b.textContent : "";
    }
    return snap;
}

function _snapHasContent(s) {
    if (!s) return false;
    if (s.type === "text") return !!(s.content && s.content.trim());
    if (s.type === "sticker") return !!(s.label || s.path);
    if (s.type === "narration") return !!(s.text && s.text.trim());
    if (s.type === "image") return !!(s.img_id || s.desc);
    return false;
}

function _closeMsgMenu() {
    if (_msgMenu) { _msgMenu.remove(); _msgMenu = null; }
}
window._closeMsgMenu = _closeMsgMenu;

/** PC 侧栏导航用：打开菜单抽屉并切到指定 tab（双栏下抽屉即右栏视图） */
function openMenuTab(tab) {
    openMenu();
    const b = document.querySelector('.menu-tab[data-tab="' + tab + '"]');
    if (b) b.click();
}
window.openMenuTab = openMenuTab;

// ═══════════════════════════════════════════
// 语音插件：长按消息 →「转语音」（docs/工具/tts.md §2）
//  · 语音是**消息的附属产物**（v{seq}.wav），不是新消息类型 → 不写 conversation.jsonl
//  · 已有缓存直接播；没有才合成
//  · **同时只能有一个合成**：本地 _voiceBusy 拦重复点击，后端还会再串行一次
//  · 插件不可用 → 按钮点击时给出**原因**（不静默失败）
// ═══════════════════════════════════════════
let _voiceBusy = false;
let _voiceMood = localStorage.getItem("firefly_voice_mood") || "happy";

async function _voiceStatus(force) {
    if (window.__voiceStatusCache && !force) return window.__voiceStatusCache;
    try {
        const r = await fetch("/voice/status");
        window.__voiceStatusCache = await r.json();
    } catch (e) {
        window.__voiceStatusCache = {engine_ok: false, reason: "无法连接后端"};
    }
    return window.__voiceStatusCache;
}

/** 语音文件 URL（播放由消息下方的语音条负责，见 voice_plugin.js） */
function _voiceUrl(mode, seq) {
    return "/voice-file?mode=" + encodeURIComponent(mode) + "&name=v" + seq + ".wav";
}

/**
 * 转语音。**每次都强制重生成**（force）—— 用户要求"再点一次就自动重新生成"；
 * 单纯回放由消息下方的语音条承担。生成期间先在消息下面贴一个占位条（⏳ …）。
 */
async function _voiceConvert(mode, snap) {
    if (_voiceBusy) { showToast("正在生成上一句语音，请稍候"); return; }
    if (snap.who !== "firefly") { showToast(`只能给${charName() || "角色"}的消息转语音`); return; }
    if (snap.seq == null) { showToast("这条消息还没有序号（刷新页面后可用）"); return; }
    if (snap.type !== "text" && snap.type !== "narration") {
        showToast("这条消息没有可念的文本"); return;
    }
    const st = await _voiceStatus(true);
    if (!st.engine_ok) {
        showToast("语音插件不可用：" + (st.reason || "未知原因"));
        return;
    }

    const had = (typeof voiceHas === "function") && voiceHas(snap.seq);
    const row = (typeof messagesEl !== "undefined" && messagesEl)
        ? messagesEl.querySelector('.msg-row[data-seq="' + snap.seq + '"]') : null;
    let bar = null;
    if (row && typeof voiceAttachBar === "function") {
        bar = voiceAttachBar(row, snap.seq, 0);
        if (bar) {
            bar.classList.add("generating");
            bar.querySelector(".vb-ico").textContent = "⏳";
        }
    }

    _voiceBusy = true;
    showToast(had ? "正在重新生成语音…（约 20 秒）" : "正在生成语音…（首次约 20 秒）");
    try {
        const r = await fetch("/voice/tts", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: mode, seq: snap.seq, mood: _voiceMood, force: true}),
        });
        const d = await r.json();
        if (d && d.ok) {
            if (typeof voiceSyncCache === "function") await voiceSyncCache();
            if (!bar && row && typeof voiceAttachBar === "function") {
                bar = voiceAttachBar(row, snap.seq, d.dur);
            }
            if (bar) {
                bar.classList.remove("generating");
                bar.querySelector(".vb-ico").textContent = "🔊";
                const dd = bar.querySelector(".vb-dur");
                if (dd) dd.textContent = Math.round(d.dur || 0) + "″";
                if (typeof _voicePlayBar === "function") _voicePlayBar(bar, snap.seq);
            }
            showToast((had ? "已重新生成" : "已生成") + "（" + (d.seconds || "?") + "s）");
        } else {
            if (bar) bar.remove();
            showToast("生成失败：" + ((d && d.error) || "未知原因"));
            _voiceStatus(true);   // 失败后刷新状态，下次点能拿到新原因
        }
    } catch (e) {
        if (bar) bar.remove();
        console.warn("[chat] 语音生成失败：连不上本机后端", e);   // 原始异常落日志（顾客可见串里不放）
        showToast("语音没生成成功：连不上本机后端，请确认程序在运行后重试");
    } finally {
        _voiceBusy = false;
    }
}
window._voiceMood = () => _voiceMood;
window.setVoiceMood = (m) => { _voiceMood = m; localStorage.setItem("firefly_voice_mood", m); };

function _showMsgMenu(row, x, y) {
    const snap = _msgSnapshot(row);
    if (!_snapHasContent(snap)) return;
    _closeMsgMenu();   // 唯一性：先清掉旧菜单，保证同一时刻只有一个「引用/收藏」菜单
    const menu = document.createElement("div");
    menu.id = "msg-long-menu";
    _msgMenu = menu;   // 登记为当前菜单（_closeMsgMenu 靠它清理；漏掉会越积越多）
    const mkBtn = (label, icon, fn) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "mlm-btn";
        b.textContent = icon + " " + label;
        b.addEventListener("click", fn);
        return b;
    };
    menu.appendChild(mkBtn("引用", "📎", () => { _closeMsgMenu(); _setQuote(snap); }));
    menu.appendChild(mkBtn("收藏", "⭐", async () => {
        _closeMsgMenu();
        try {
            const resp = await fetch("/favorite", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({mode: CURRENT_MODE, message: snap}),
            });
            const d = await resp.json();
            showToast(d.ok ? "已收藏（菜单 → 收藏可查看）" : "收藏失败：" + (d.error || ""));
        } catch (e) {
            console.warn("[chat] 收藏失败", e);
            showToast("收藏没成功，请重试；仍失败请到「反馈」附诊断包");
        }
    }));
    // 转语音：只对流萤的 text/narration 且有 seq 的消息显示。
    // 已有语音时**再点一次即重新生成** —— 不再单列「重新生成」：
    //   2026-09-18 真机实测 4 项会把菜单撑出屏幕（最左「引用」被裁到屏幕外）。
    // 播放改由消息下方的语音条承担（见 voice_plugin.js 的 voiceAttachBar）。
    if (snap.who === "firefly" && snap.seq != null
        && (snap.type === "text" || snap.type === "narration")) {
        menu.appendChild(mkBtn("转语音", "🔊", () => { _closeMsgMenu(); _voiceConvert(CURRENT_MODE, snap); }));
    }
    document.body.appendChild(menu);
    // 定位：消息在上半屏 → 菜单放下方；下半屏 → 放上方（QQ 式，且不超出视口）
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    const r = row.getBoundingClientRect();
    const vw = innerWidth, vh = innerHeight;
    let left = Math.min(Math.max(8, x - mw / 2), vw - mw - 8);
    // 菜单比视口还宽时上式会得到负数 → 左边被裁（真机踩过）。兜到 8。
    left = Math.max(8, left);
    const cy = r.top + r.height / 2;
    let top = cy < vh / 2 ? r.bottom + 10 : r.top - mh - 10;
    top = Math.max(8, Math.min(top, vh - mh - 8));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
}

function _cancelLongPress() {
    if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
    _lpRow = null; _lpStart = null;
}

// 长按（触屏/鼠标按住）与 PC 右键都弹菜单
messagesEl.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const row = e.target.closest(".msg-row");
    if (!row || e.target.closest(".msg-avatar")) return;          // 头像长按留给形象切换
    if (row.querySelector(".typing-bubble")) return;              // 加载占位不可长按
    _cancelLongPress();
    _lpRow = row;
    _lpStart = {x: e.clientX, y: e.clientY};
    _lpTimer = setTimeout(() => {
        _lpTimer = null;
        if (_lpRow && _lpRow.isConnected) {
            try { if (navigator.vibrate) navigator.vibrate(20); } catch (e) {}   // 触觉反馈
            _showMsgMenu(_lpRow, _lpStart.x, _lpStart.y);
        }
    }, _LONG_PRESS_MS);
});
window.addEventListener("pointermove", (e) => {
    if (_lpTimer && _lpStart) {
        const dx = e.clientX - _lpStart.x, dy = e.clientY - _lpStart.y;
        if (dx * dx + dy * dy > 64) _cancelLongPress();   // 移动超 8px = 滚动/滑动，取消长按
    }
}, {passive: true});
window.addEventListener("pointerup", _cancelLongPress);
window.addEventListener("pointercancel", _cancelLongPress);
messagesEl.addEventListener("scroll", _closeMsgMenu, {passive: true});
// 点击菜单外任意处关闭
document.addEventListener("pointerdown", (e) => {
    if (_msgMenu && !e.target.closest("#msg-long-menu")) _closeMsgMenu();
}, {capture: true});
// PC 右键同样弹菜单
messagesEl.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".msg-row");
    if (!row) return;
    e.preventDefault();
    _showMsgMenu(row, e.clientX, e.clientY);
});

// ── 引用：设置引用条（输入框上方） ──
function _setQuote(snap) {
    _quoteTarget = snap;
    const bar = document.getElementById("quote-bar");
    if (!bar) return;
    const whoEl = document.getElementById("quote-who");
    const txtEl = document.getElementById("quote-text");
    if (whoEl) whoEl.textContent = "引用 " + _quoteWhoName(snap);
    if (txtEl) txtEl.textContent = _quoteContentText(snap);
    bar.style.display = "flex";
    inputEl.focus();
}
function _clearQuote() {
    _quoteTarget = null;
    const bar = document.getElementById("quote-bar");
    if (bar) bar.style.display = "none";
}
document.getElementById("quote-cancel")?.addEventListener("click", _clearQuote);

// ═══════════════════════════════════════════
// 发送消息 — 四阶段模型：输入 → 发送 → 提交 → 回复
//   输入：打字（内容只在输入框，不触发队列）
//   发送：Enter / 发送按钮 / 点表情 → 消息**立即 POST 后端**（不等 5 秒，切后台不丢）
//   提交：后端 5 秒滑动窗口合并（后端控制；/chat/hint 重置窗口、/chat/flush 提前结束）
//   回复：流萤回复渲染
// 关键：发送 ≠ 提交。后端窗口合并连续消息；输入框未发送的内容永不提交（绝不自动发送）。
// ═══════════════════════════════════════════
let _inflight = 0;        // 在飞请求数（WakeLock 引用计数：全部完成才释放）
let _stageTimer = null;   // 阶段进度轮询句柄（等待回复期间轮询 /chat-stage）

// LLM 错误分类 → 人话提示（后端 /chat 返回 error_code 时展示）
// 每类都按「现象 → 原因 → 我该做什么」写；归因一律照 api_client.py 的错误码来，
// 上游/系统的锅不写成用户的操作问题（见 docs/设计/0.9.1用户可见文本清单.md §一 映射表）。
const ERROR_TIPS = {
    key_invalid: "你的 API Key 无效或已过期：请到设置里重新粘贴 Key（本机对话与角色卡不受影响）",
    no_balance: "你的上游账号余额不足：请到所选供应商的控制台充值后重试（与本机数据、角色卡无关）",
    rate_limit: "这是上游接口的临时限制，不是你的卡或操作的问题：约 1 分钟后会自动重试",
    network: "本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）",
    server_error: "上游服务临时不可用，不是你那边的问题：稍后重试即可，不必改 Key",
    bad_response: "上游返回的内容无法识别，不是你那边的问题：稍后重试即可",
    relay_timeout: "这次请求超时了，不是你那边的问题：请确认应用在前台、网络正常后重试",
    timeout: "这次生成超过了本轮时间上限，不是你那边的问题：稍等再发一次即可",
    cooldown: "上游服务波动中，不是你那边的问题：稍等约 1 分钟再试",
    quota_exhausted: "今日服务器托管额度已用完，可在设置中切换为自带 Key 模式",
    unknown: "发生了未归类的问题：请重试一次；仍失败请在「反馈」里附诊断包",
};



/** 导入 zip 备份（覆盖当前模式数据；导入前后端自动备份现有数据）。 */
function importData() {
    const input = document.getElementById("import-file");
    if (!input) return;
    input.onchange = async () => {
        const f = input.files && input.files[0];
        input.value = "";   // 允许重复选同一文件
        if (!f) return;
        if (!confirm(`导入将覆盖当前「${MODE_NAMES[CURRENT_MODE] || CURRENT_MODE}」的全部数据（导入前会自动备份现有数据）。\n\n确定导入 ${f.name} 吗？`)) return;
        _toast("正在导入…");
        try {
            const fd = new FormData();
            fd.append("file", f);
            fd.append("mode", CURRENT_MODE);
            const resp = await fetch("/import-data", { method: "POST", body: fd });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok && data.ok) {
                _toast("导入成功，正在重新加载…");
                setTimeout(() => location.reload(), 800);
            } else {
                _toast("导入失败：" + (data.error || "请检查文件"));
            }
        } catch (e) { _toast("导入失败，请检查网络"); }
    };
    input.click();
}
window.importData = importData;

// ══ 导出备份（2026-09-10 加；2026-09-25 改名）══
// 为什么需要它：此前前端只有「导入」没有「导出」——用户无法自助备份，唯一的备份入口是
// 全量快照，而快照对自建角色包曾存在丢包问题（R-01）。导出走既有 GET /export-data?mode=，
// 产出本地 zip（含该角色的包定义与全部对话/手账/记忆，_config.json 已剥离 Key），
// 可离线留存、也可用「导入」在本机或其他设备还原。
//
// ★ 2026-09-25 改名「导出当前角色」→「导出备份」：那一列按钮里「保存快照（全部角色）」
//   已经用括号标注了范围，导出再标范围就会让人以为是两种并列的东西；而它与「导入备份」
//   本来就是一对（导出→导入 是同一个闭环），叫「备份」才对上。功能没变：仍然导**当前角色**。
function exportData() {
    const name = MODE_NAMES[CURRENT_MODE] || CURRENT_MODE;
    if (!confirm(`导出「${name}」的全部数据为本地 zip 备份？\n\n包含：角色设定、对话记录、手账、记忆（不含 API Key）。\n可用「导入备份」在本机或其他设备还原。`)) return;
    _toast("正在打包导出…");
    const url = API_BASE + "/export-data?mode=" + encodeURIComponent(CURRENT_MODE);
    // 用隐藏链接触发下载（保留 Content-Disposition 文件名；window.open 在部分 WebView 会被拦）
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1500);
}
window.exportData = exportData;

// ══ 数据快照（保存全部角色到服务器；每账号留最近 5 份，换机可恢复）══
function _fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
}

async function createSnapshot() {
    _toast("正在打包快照…");
    try {
        const resp = await fetch("/snapshot/create", { method: "POST" });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast(data.pushed ? "快照已保存到服务器（" + _fmtSize(data.size) + "）"
                               : "快照已保存到本机（" + _fmtSize(data.size) + "）"
                                 + (data.push_error ? "；" + data.push_error : ""));
            loadSnapshots();
        } else {
            _toast("快照失败：" + (data.error || ""));
        }
    } catch (e) { _toast("快照失败，请检查网络"); }
}

async function loadSnapshots() {
    const list = document.getElementById("snapshot-list");
    if (!list) return;
    try {
        const resp = await fetch("/snapshot/list");
        const data = await resp.json();
        const items = data.snapshots || [];
        if (!items.length) {
            list.innerHTML = '<div style="color:var(--fg-muted);font-size:0.75em">还没有快照，点「保存快照」存一份</div>';
            return;
        }
        list.innerHTML = items.map(s => `
        <div class="snapshot-row" data-name="${escapeHtml(s.name)}" style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.75em">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.name)}">${escapeHtml(s.time || s.name)}</span>
            <span style="color:var(--fg-muted);flex-shrink:0">${_fmtSize(s.size)}</span>
            <button type="button" class="sn-restore">恢复</button>
            <button type="button" class="sn-download">下载</button>
            <button type="button" class="sn-del">删除</button>
        </div>`).join("");
        list.querySelectorAll(".snapshot-row").forEach(row => {
            const name = row.dataset.name;
            row.querySelector(".sn-restore").addEventListener("click", () => restoreSnapshot(name));
            row.querySelector(".sn-del").addEventListener("click", () => deleteSnapshot(name));
            row.querySelector(".sn-download").addEventListener("click", () => {
                if (!IS_SERVER) {
                    window.location.href = `/snapshot/download?name=${encodeURIComponent(name)}`;
                } else {
                    fetch(`/snapshot/download?name=${encodeURIComponent(name)}`)
                        .then(r => r.blob()).then(blob => {
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement("a");
                            a.href = url; a.download = name;
                            document.body.appendChild(a); a.click();
                            setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
                        }).catch(() => _toast("下载失败"));
                }
            });
        });
    } catch (e) {
        list.innerHTML = '<div style="color:#c66;font-size:0.75em">快照列表加载失败</div>';
    }
}

async function restoreSnapshot(name) {
    if (!confirm(`用快照「${name}」覆盖全部角色与聊天记录？\n恢复前会自动保存当前状态的快照。\n\n确定恢复吗？`)) return;
    _toast("正在恢复快照…");
    try {
        const resp = await fetch("/snapshot/restore", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            // E-1 配套：恢复前自动备份失败时后端会回落逐包备份并标 backup_ok=false——
            // 此时若快照本身有问题，损失不可回滚，必须让用户知道
            _toast(data.backup_ok === false
                ? "已恢复（注意：恢复前的保险快照生成失败）"
                : "恢复成功，正在重新加载…");
            setTimeout(() => location.reload(), 800);
        } else {
            _toast("恢复失败：" + (data.error || ""));
        }
    } catch (e) { _toast("恢复失败，请检查网络"); }
}

async function deleteSnapshot(name) {
    if (!confirm(`删除快照「${name}」？此操作不可撤销。`)) return;
    try {
        const resp = await fetch("/snapshot/delete", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.ok) {
            _toast("快照已删除");
            loadSnapshots();
        } else {
            _toast("删除失败：" + (data.error || ""));
        }
    } catch (e) { _toast("删除失败，请检查网络"); }
}

document.getElementById("snapshot-create-btn")?.addEventListener("click", createSnapshot);
document.getElementById("snapshot-list") && loadSnapshots();


/** 等待回复期间轮询流水线阶段（检索→分析→回复→表情包），把"对方正在输入…"换成具体阶段。
 *  仅在拿到 stage 时替换文本；请求结束由 _chatSend 的 finally 清除。
 *  A3：消费后端 waited 字段——等待偏久（>60s）提示"上游有点慢"，不再像卡死。 */
let _waitedWarned = false;
function _pollStage(statusEl) {
    clearInterval(_stageTimer);
    _waitedWarned = false;
    _stageTimer = setInterval(async () => {
        if (_inflight <= 0) { clearInterval(_stageTimer); _stageTimer = null; return; }
        try {
            const r = await fetch(`/chat-stage?sid=${encodeURIComponent(SESSION_ID)}&mode=${encodeURIComponent(CURRENT_MODE)}`);
            const d = await r.json();
            if (d.stage && d.label && _inflight > 0 && statusEl) statusEl.textContent = d.label;
            if (d.waited != null && d.waited > 60 && !_waitedWarned && _inflight > 0 && statusEl) {
                _waitedWarned = true;
                statusEl.textContent = "上游有点忙，正在努力回复…";
            }
        } catch (e) { /* 网络抖动静默，状态保持"对方正在输入" */ }
    }, 2000);
}

// ═══════════════════════════════════════════
// 提交窗口状态机（0.8.1 简化，两判定 + 上限）
// 提交流程：消息入队（气泡上屏+写盘，不丢）→ 等待提交。
// 提交条件（两个都满足）：
//   ① 输入框为空（没在打下一句）
//   ② 距最新一条消息 ≥ 5 秒（_lastMsgTs 每次发送重置，以最新消息为准）
// 打字中（输入框有内容）→ 持续 hint 续期后端窗口（2s 节流），流萤继续等，绝不提交；
// 合并上限：单批连续消息 ≥ _MAX_BATCH_MSGS 条 → 立即提交（即使输入框有内容）。
// 与后端 _CHAT_WINDOW_MAX_MSGS=10 一致。
// ═══════════════════════════════════════════
const _MAX_BATCH_MSGS = 10;
let _lastMsgTs = 0;              // 最新一条已发送消息的时间戳
let _pendingBatch = 0;           // 本批已入队未提交条数
let _mediaBusy = false;          // 媒体选择中（图片相册/表情面板）：暂停提交（长期等待）

// 常驻检查器（页面级）：每 1 秒拍一次，只在有 pending 批时判定。
// 简化语义：
//   _pendingBatch === 0 → 无事可做，跳过；
//   _mediaBusy（相册/表情面板打开）→ 与打字中同构：持续 hint 续期后端窗口，
//     不提交（后端窗口是 5 秒滑动的，不续期照样到期——前端暂停 flush 不够，必须续期）；
//   输入框有内容（打字）→ 续期后端窗口，不提交；
//   距最新消息 ≥ 5 秒 → 提交（flush）。
let _checkTimer = setInterval(() => {
    if (_pendingBatch <= 0) return;
    if (_pendingBatch >= _MAX_BATCH_MSGS) { _batchCommit(); return; }   // 合并上限 10 条
    if (_mediaBusy) { _sendHint(); return; }                            // 媒体选择中：续期窗口+不提交
    if (inputEl && inputEl.value.trim()) { _sendHint(); return; }       // 打字中：续期窗口
    if (Date.now() - _lastMsgTs < 5000) return;                         // 距最新消息 <5 秒：继续等
    _batchCommit();
}, 1000);

function _batchArmSubmit() {
    _lastMsgTs = Date.now();
    _pendingBatch++;
}

/** 媒体选择结束后：重新计时 5 秒（不增加批计数——未产生新消息）。 */
function _batchRefreshTimer() {
    _lastMsgTs = Date.now();
}

function _batchCommit() {
    _pendingBatch = 0; _lastMsgTs = 0;
    _sendFlush();
}

/** 模式切换/离开聊天页时调用：提交批立即作废（旧模式消息不跨模式提交）。timer 常驻不清。 */
function resetBatchWindow() {
    _pendingBatch = 0; _lastMsgTs = 0; _mediaBusy = false;
}

/** 打字中：重置后端合并窗口（流萤继续等开拓者说完）。
 * 输入框仍有内容 → 持续定时重置（前端在且输入框有残留 = 用户在打字 → 永不提交）。 */
function _sendHint() {
    fetch("/chat/hint", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
    }).catch(() => {});
    // 输入框仍有残留（用户还在打字/未清空）→ 继续定时重置窗口（2s 节流）
    if (inputEl && inputEl.value.trim()) {
        clearTimeout(S._hintTimer);
        S._hintTimer = setTimeout(_sendHint, 2000);
    }
}

/** 提交窗口到期：立即结束后端合并窗口（前台加速；切后台冻结不触发，后端窗口兜底）。
 * 状态显示时机：只有提交（进入核心流水线）才显示"对方正在输入"，窗口等待期不显示。 */
function _sendFlush() {
    if (inputEl) inputEl.placeholder = "说点什么…";   // 提交窗口结束：还原补话提示（3.6）
    if (_inflight === 0) return;   // 无在飞请求（已完成）：不显示状态，防止卡"正在输入"
    const statusEl = document.querySelector("#header .status");
    if (statusEl) statusEl.textContent = "对方正在输入...";
    fetch("/chat/flush", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
    }).catch(() => {});
}

/** 发送消息到后端并处理响应：
 *  副请求（窗口内）→ 后端返回 {queued:true}，回复由主请求带回，忽略；
 *  主请求（窗口结束/新窗口）→ 挂起等回复，返回后渲染。
 *  状态显示不在此处设置——窗口等待期不显示"正在输入"，由 _sendFlush（提交）触发。
 *  超时护栏：/chat 挂 4 分钟 AbortController，上游（LLM 端点）卡住时
 *  自动放弃等待并提示，避免页面无限挂起"对方正在输入…"（历史上游 5xx 重试
 *  + 单阶段 120s 超时可拖 15 分钟以上，会话锁全线阻塞）。 */
const _CHAT_FETCH_TIMEOUT = 4 * 60 * 1000;   // 4 分钟（覆盖 4 阶段最长流水线）

async function _chatSend(msgs) {
    _inflight++;
    const statusEl = document.querySelector("#header .status");
    // 修复：不用"请求开始时的快照"恢复（快照可能已被 _sendFlush / 阶段轮询污染成
    // "对方正在输入"/"正在理解你的话…"），统一恢复为当前角色包的签名（无签名用默认句）。
    const defaultStatus = (currentPreset() || {}).tagline || "会找到的，属于我的梦...";
    const gen = _modeGen;   // 捕获发起时的模式代际
    // 后台保活（安卓 WebView JS Bridge）：回复流程（检索→分析→回复→调度）期间
    // 持 CPU/WiFi 锁，用户切后台/锁屏也能完成回复；引用计数归零才释放。
    // 浏览器端（PC/服务器版）无 androidWakeLock，此段安全跳过
    if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.acquire(); }
    _pollStage(statusEl);   // 启动阶段进度轮询（回复到达后 finally 清除）
    const abortCtrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const abortTimer = abortCtrl ? setTimeout(() => abortCtrl.abort(), _CHAT_FETCH_TIMEOUT) : null;
    try {
        const fetchOpts = {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ messages: msgs, session_id: SESSION_ID, mode: CURRENT_MODE }),
        };
        // 旧 WebView 无 AbortController：不带 signal 字段，行为与之前一致
        if (abortCtrl) fetchOpts.signal = abortCtrl.signal;
        const resp = await fetch("/chat", fetchOpts);
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃回复（消息已写盘到原模式，不渲染）
        if (data.need_key) openSettings();
        else if (data.messages) {
            renderMessages(data.messages, "firefly", data);
            _notifyFirefly(data.messages);   // 切后台时回复完成通知（桥判断前台与否）
        }
        else if (data.reply) addTextMessage(data.reply, "firefly");
        if (data.error_code) _toast(ERROR_TIPS[data.error_code] || ERROR_TIPS.unknown);
        // R-07（服务器版）：请求的包在本端不存在时后端会静默回退默认包并打 mode_fallback 标记——
        // 必须显式告知（否则用户以为"角色坏了"而不知原因）
        if (data.mode_fallback) {
            _toast("该角色包在服务器模式不可用，已回退到「" +
                   (MODE_NAMES[data.mode_used] || data.mode_used || "默认包") + "」");
        }
        // data.queued：副请求，回复由主请求带回，无 UI 操作
    } catch (e) {
        if (gen === _modeGen && _inflight === 1) {
            if (abortCtrl && e && e.name === "AbortError") {
                // 上游卡住被超时中止：消息已即时写盘不丢，提示用户稍后再试
                addTextMessage("嗯…上游有点忙，我先不打扰了，过会儿再试试？", "firefly");
            } else {
                // 与后端 polisher.DEGRADED_TEXT 保持一致（降级话术单一来源，2026-09-04 合并）
                addTextMessage("嗯…信号不太好，等会儿再试试？", "firefly");
            }
        }
    } finally {
        if (abortTimer) clearTimeout(abortTimer);
        if (window.androidWakeLock && _inflight === 1) { window.androidWakeLock.release(); }
        _inflight--;
        if (_inflight === 0) {
            // 请求完成（主请求带回回复 / 全部副请求结束）：批提交作废（timer 常驻不清）
            _pendingBatch = 0; _lastMsgTs = 0;
            clearTimeout(S._hintTimer); S._hintTimer = null;
            clearTimeout(S._flushTimer); S._flushTimer = null;   // 兼容旧引用
            clearInterval(_stageTimer); _stageTimer = null;  // 阶段轮询结束
            if (statusEl) statusEl.textContent = defaultStatus;
            inputEl.focus();
            // 响应式回复完成后 ≥1s 防抖，触发主动式判断（主动式未触发则服务端串联概率式）
            setTimeout(() => { if (_idleOk()) checkProactive(); }, 1000);
        }
    }
}

async function send() {
    const text = inputEl.value.trim();
    if (!text || S.waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    inputEl.value = "";
    const q = _quoteTarget;
    addTextMessage(text, "user", false, null, q);
    inputEl.focus();
    // 0.8.1：进入提交窗口状态机（两判定+10条上限；窗口到期由 _batchCommit 触发 flush）
    clearTimeout(S._hintTimer);
    inputEl.placeholder = "还可以继续说…";   // 3.6：提交窗口内提示"还能补话"
    const msg = {type: "text", content: text};
    if (q) msg.quote = q;   // 引用随消息提交（后端写盘 + LLM 上下文）
    _chatSend([msg]);   // 统一消息对象类型，立即发送
    _batchArmSubmit();
    _clearQuote();
}

sendBtn.addEventListener("click", send);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
// 提交窗口控制（0.8.1 简化，两判定+上限，见 _batchArmSubmit 注释）：
// - 输入框有内容（打字中）→ 暂停提交 + hint 续期后端窗口（流萤继续等）
// - 输入框清空 → 由 1s 检查器按「距最新消息 5 秒」判定提交
inputEl.addEventListener("input", () => {
    clearTimeout(S._hintTimer);
    if (inputEl.value.trim()) {
        inputEl.placeholder = "说点什么…";   // 开始打字即还原提示（3.6）
        _sendHint();   // 立即续期一次 + 2s 节流循环
    } else {
        // 清空输入框：hint 停止（_sendHint 循环见框空即止），交给检查器按 5s 判定
        clearTimeout(S._hintTimer);
        S._hintTimer = null;
    }
});


/* ── 来源：js/chat_media.js ── */
// 聊天媒体：发图片（选图/压缩/上传/描述）/ 表情包面板与发送

// ═══════════════════════════════════════════
// 发图片（A9）：🖼 按钮 → 选图 → 压缩 → 上传落盘 → 描述 → 发送 image 消息
// （图片链路统一：本地版/服务器版都走 /upload-image；服务器版请求由 fetch 包装器带登录 Bearer）
// ═══════════════════════════════════════════
const imageBtn = document.getElementById("image-btn");
const imageFileInput = document.getElementById("image-file-input");
if (imageBtn && imageFileInput) {
    // 点击 🖼：进入媒体选择状态（暂停提交计时——相册选择是长期操作，
    // 否则第一条消息会在选图中途被 5 秒窗口提前提交）
    imageBtn.addEventListener("click", () => {
        _mediaBusy = true;
        imageFileInput.value = "";
        imageFileInput.click();
    });
    imageFileInput.addEventListener("change", async () => {
        // 选择结束（无论成功/取消/失败）：恢复计时（重新 5 秒）
        const endMedia = (arm = false) => {
            _mediaBusy = false;
            if (arm) {   // 成功发送：进入新批计数
                _batchArmSubmit();
            } else {     // 取消/失败：重新计时但不计入批数
                _batchRefreshTimer();
            }
        };
        // PC/浏览器取消选图不触发 change（只发 cancel）→ _mediaBusy 永远为真、批次永不提交。
        // 补 cancel 监听：取消/重开选择器都恢复计时（Kimi 复审 #2；安卓 WebView 取消同理）。
        imageFileInput.addEventListener("cancel", () => { endMedia(false); });
        const f = imageFileInput.files && imageFileInput.files[0];
        imageFileInput.value = "";
        if (!f || S.waiting) { endMedia(false); return; }
        if (!/^image\/(png|jpe?g|webp|gif)$/.test(f.type || "")) { _toast("仅支持 png/jpg/webp/gif 图片"); endMedia(false); return; }
        if (f.size > 10 * 1024 * 1024) { _toast("图片过大（上限 10MB）"); endMedia(false); return; }
        // 发送前压缩（imgzip.js）：GIF/小图原样；任何失败降级原图不阻塞发送
        const {blob, ext} = await compressImage(f);
        let imgId = "";
        try {
            const fd = new FormData();
            fd.append("file", blob, "upload." + ext);   // 压缩产物是匿名 Blob：补文件名让后端拿到正确扩展名
            fd.append("mode", CURRENT_MODE);
            const resp = await fetch("/upload-image", {method: "POST", body: fd});
            const data = await resp.json();
            if (!data.ok) { _toast("图片上传失败：" + (data.error || "")); endMedia(false); return; }   // 配额满等错误直接透传后端文案
            imgId = data.img_id || "";
        } catch (e) { _toast("网络错误，图片未发送"); endMedia(false); return; }
        // 2026-08-29：不再弹窗让用户描述——图片理解统一由模型原生识图（发图当轮注入原图）；
        // 模型真不支持时由后端 polisher 降级为"看不清"说明。
        const q = _quoteTarget;
        addImage({img_id: imgId, desc: ""}, "user");
        const msg = {type: "image", img_id: imgId};
        if (q) msg.quote = q;
        _chatSend([msg]);
        endMedia(true);   // 0.8.1：图片与文字同批合并（两判定+10条上限）
        _clearQuote();
    });
}

// ═══════════════════════════════════════════
// 表情包面板（输入框内 😊 按钮）
// ═══════════════════════════════════════════
const stickerPanel = document.getElementById("sticker-panel");
const stickerGrid = document.getElementById("sticker-grid");
const stickerBtn = document.getElementById("sticker-btn");

// 表情面板打开/关闭的提交计时管理（媒体选择中暂停提交）
function _stickerPanelClose() {
    stickerPanel.classList.remove("show");
    if (_mediaBusy) { _mediaBusy = false; _batchRefreshTimer(); }   // 重新计时
}

stickerBtn.addEventListener("click", async () => {
    if (stickerPanel.classList.contains("show")) {
        _stickerPanelClose();
        return;
    }
    _mediaBusy = true;   // 面板打开期间暂停提交（长期等待用户选择）
    stickerPanel.classList.add("show");
    // F-6.2（2026-09-14）：面板缓存按包失效 + 按包过滤——原来 dataset.loaded 一旦置位永不过期，
    // 切包后仍显示旧包（含他包专属）的表情；现在切包必重拉，且只显示 全局共享 + 本包专属。
    if (stickerGrid.dataset.loaded && stickerGrid.dataset.mode === CURRENT_MODE) return;
    try {
        const resp = await fetch("/stickers?enabled=1");
        const data = await resp.json();
        const list = (data.stickers || []).filter(s => !s.pack || s.pack === CURRENT_MODE);
        stickerGrid.innerHTML = list.map(s =>
            `<img src="${IS_SERVER ? API_BASE : ""}/assets/${escapeHtml(s.file)}" alt="${escapeHtml(s.label)}" data-label="${escapeHtml(s.label)}" data-file="${escapeHtml(s.file)}">`).join("");
        stickerGrid.dataset.loaded = "1";
        stickerGrid.dataset.mode = CURRENT_MODE;
        stickerGrid.querySelectorAll("img").forEach(img => {
            img.addEventListener("click", () => {
                _mediaBusy = false;   // 选中即结束媒体状态（sendStickerMessage 内 _batchArmSubmit 重新计时）
                _stickerPanelClose();
                sendStickerMessage(img.dataset.label, img.dataset.file);
            });
        });
    } catch (e) { /* 静默 */ }
});
// 点击聊天区关闭表情面板
messagesEl.addEventListener("click", _stickerPanelClose);
document.getElementById("sticker-panel-close").addEventListener("click", _stickerPanelClose);

/** 发送表情包：作为一条消息立即发送（与文字同一窗口合并，不碰输入框内容） */
function sendStickerMessage(label, file) {
    if (S.waiting) return;   // 主动消息思考渲染中：禁止发送防乱序
    const q = _quoteTarget;
    if (file) addSticker(file, "user", false, null, label, q);   // 本地立即渲染表情图
    inputEl.focus();
    clearTimeout(S._hintTimer);
    const msg = {type: "sticker", label, file};
    if (q) msg.quote = q;
    _chatSend([msg]);
    _batchArmSubmit();   // 0.8.1：表情与文字同批合并（两判定+10条上限）
    _clearQuote();
}


/* ── 来源：js/chat_history.js ── */
// 聊天历史：历史加载（滚动翻页）/ 休息 / 清除 / 撤回

// 当前角色名统一走 views.charName()（角色卡化：框架不含角色名字面量）。
// 休息/起床的**提示句**需要一个语法上的主语，取不到时用中性词「角色」。
function _cn() {
    return charName() || "角色";
}

// ═══════════════════════════════════════════
// 记忆窗口提醒（聊天页头部的名字行尾，2026-09-18）
//
// 活跃窗口 = "自上次整理以来"的对话原文，除刚开场外常驻 30–100 轮：
//   · 分析器 / 回复器读它（所以压缩后也照样看得到最近 30 轮完整对话）
//   · 整理只把"最近 keep_turns 轮以外"的部分搬进「历史对话存档」（只进检索器）
// 阈值故意放得很宽（100 轮才触发）——整理一次要花 2 次 LLM 调用，频繁整理就是烧用户 token。
//
// 显示形态（用户 2026-09-18 反馈"太影响观感"后定的）：
//   **只是名字行尾一小截灰字**（`· 记忆 92/100`），不加行、不加边框、不加按钮。
//   理由是它属"低优先级知会"——手动整理入口本来就在菜单「让流萤休息」，
//   到上限后下一条回复也会自动整理，不需要再给按钮。
// 数据源 GET /memory-status（只读计数 + 游标文件，很轻）。
// ═══════════════════════════════════════════
const memHint = document.getElementById("chat-mem");
// `_chatVisible()` **只有一份定义**，在 js/proactive.js：bundle 是单作用域拼接，ORDER 里 proactive
// 排在 chat_history 之后 ⇒ 那一份一直就是**实际生效**的一份，这里原来的重复定义是死代码。
// 2026-10-01（P4-9）把本地重复定义删掉：两份并存时，改了其中一份会被另一份**静默覆盖**。
// 两份的语义差异（元素缺失时返回 `undefined` 还是 `false`）在调用点都是布尔上下文，无行为变化。
// ⚠ 勿再在本地重定义。

async function memoryBarRefresh() {
    if (!memHint) return;
    if (!_chatVisible() || typeof CURRENT_MODE === "undefined" || !CURRENT_MODE) {
        memHint.hidden = true;
        return;
    }
    try {
        const r = await fetch(`/memory-status?mode=${encodeURIComponent(CURRENT_MODE)}`);
        const d = await r.json();
        const lv = d.level || "ok";
        // ok/empty/off：不打扰（off = 自动整理被 config 关掉，用户既然关了就别提醒）
        if (lv !== "warn" && lv !== "full") { memHint.hidden = true; return; }
        memHint.hidden = false;
        memHint.className = "chat-mem " + lv;
        memHint.textContent = lv === "full"
            ? `· 记忆 ${d.active_turns}/${d.window_max} · 将整理`
            : `· 记忆 ${d.active_turns}/${d.window_max}`;
    } catch (e) {
        memHint.hidden = true;   // 提醒永不阻塞聊天：取不到就干脆不显示
    }
}
window.memoryBarRefresh = memoryBarRefresh;

// 轮询：本地端点、响应 ~200 字节；聊天页不可见时函数自己直接返回
setInterval(memoryBarRefresh, 15000);

// ═══════════════════════════════════════════
// 休息 / 清除 / 撤回
// ═══════════════════════════════════════════
const restOverlay = document.getElementById("rest-overlay");
// 休息结果展示后 3 秒自动收起（修复：原来先隐藏遮罩再写文案，用户看不见结果）
function _showRestResult(txt) {
    document.getElementById("rest-text").textContent = txt;
    setTimeout(() => { restOverlay.style.display = "none"; }, 3000);
}
/** 执行一次「休息」（整理）。菜单按钮与顶部状态条的「现在整理」共用同一条链。 */
async function _doRest() {
    restOverlay.style.display = "flex";
    document.getElementById("rest-text").textContent = `${_cn()}正在整理记忆…`;
    try {
        const resp = await fetch("/rest", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        // 0.8.1：文案用真实变化——added/resolved 为 LLM 解析数组（可能为空但头部/手账已更新）
        let msg = "";
        if (data.ok) {
            if (data.skipped) {
                msg = `${_cn()}已休息。这边没有新的对话内容需要整理，下次聊完再叫我吧。`;
            } else {
                const parts = [];
                if (data.added) parts.push(`新增记忆 ${data.added} 条`);
                if (data.resolved) parts.push(`解决 ${data.resolved} 条`);
                if (!parts.length && data.head_changed) parts.push("记忆已更新");
                msg = `${_cn()}已休息。${parts.join("，") || "记忆已更新"}。下次见。`;
            }
        } else {
            msg = "整理出了点问题：" + (data.error || "未知");
        }
        _showRestResult(msg);
    } catch (e) {
        _showRestResult("信号不好，等会儿再试。");
    }
}

document.getElementById("menu-rest-btn").addEventListener("click", async () => {
    if (!confirm(`让${_cn()}去休息吗？${_cn()}会整理这段对话的记忆。`)) return;
    closeMenu();
    await _doRest();
    memoryBarRefresh();
});

document.getElementById("menu-clear-btn").addEventListener("click", async () => {
    if (!confirm("确认清除全部对话历史？此操作不可撤销。")) return;
    closeMenu();
    try {
        const resp = await fetch("/clear-history", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (data.ok) { messagesEl.innerHTML = ""; }
    } catch (e) { alert("网络错误"); }
});

const undoBtn = document.getElementById("menu-undo-btn");
undoBtn.addEventListener("click", async () => {
    if (!confirm("撤回上一轮对话？")) return;
    closeMenu();
    undoBtn.disabled = true;
    try {
        const resp = await fetch("/undo", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (data.ok) {
            // 删除最后一段连续 user 块及其后的所有消息（整轮）
            const rows = messagesEl.querySelectorAll(".msg-row");
            const users = [...rows].filter(r => r.classList.contains("user"));
            if (users.length > 0) {
                let start = users[users.length - 1];
                let sib = start.previousElementSibling;
                while (sib && sib.classList.contains("msg-row") && sib.classList.contains("user")) {
                    start = sib; sib = sib.previousElementSibling;
                }
                let node = start;
                while (node) {
                    const nxt = node.nextElementSibling;
                    node.remove();
                    node = nxt;
                }
            }
        }
    } catch (e) {}
    undoBtn.disabled = false;
});

// ═══════════════════════════════════════════
// 历史加载
// ═══════════════════════════════════════════
let _loading = false;
let _lastWho = null;
function renderHistoryMessage(m, prepend=false) {
    const ts = m.time ? m.time.slice(11,16) : null;
    // 发送方变化时插入时间分割线
    if (m.who !== _lastWho && ts) {
        addTimeDivider(ts);
        _lastWho = m.who;
    }
    if (m.type==="sticker") addSticker(m.path, m.who, prepend, m.seq, m.label, m.quote);
    else if (m.type==="narration") addNarration(m.text, m.style, prepend, m.seq);
    else if (m.type==="image") addImage(m, m.who, prepend, m.seq, m.quote);
    else addTextMessage(m.content, m.who, prepend, m.seq, m.quote);
}
async function loadHistory(beforeSeq=null) {
    if (_loading) return; _loading = true;
    const gen = _modeGen;   // 捕获发起时的模式代际
    const url = beforeSeq ? `/history?limit=150&before_seq=${beforeSeq}&mode=${CURRENT_MODE}` : `/history?limit=150&mode=${CURRENT_MODE}`;
    _lastWho = null;
    try {
        const resp = await fetch(url);
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃旧模式历史，防止渲染进新模式界面
        if (!data.messages || data.messages.length===0) { S._hasMore=false; return; }
        if (!beforeSeq) { data.messages.forEach(m=>renderHistoryMessage(m,false)); messagesEl.scrollTop=messagesEl.scrollHeight; undoBtn.disabled=false; }
        else { const ph=messagesEl.scrollHeight, ps=messagesEl.scrollTop; data.messages.slice().reverse().forEach(m=>renderHistoryMessage(m,true)); messagesEl.scrollTop=ps+(messagesEl.scrollHeight-ph); }
        S._hasMore = !!data.has_more;
        // 历史渲染完再补扫一次语音条（首次进来时缓存表是异步拉的，赶不上逐条渲染）
        if (typeof voiceRestoreBarsAuto === "function") voiceRestoreBarsAuto();
        if (!beforeSeq) memoryBarRefresh();   // 进聊天页时同步一次记忆窗口状态
    } catch(e) {} finally { _loading=false; }
}
messagesEl.addEventListener("scroll", () => {
    if (messagesEl.scrollTop===0 && S._hasMore && !_loading) {
        const first = messagesEl.firstChild;
        const seq = first ? parseInt(first.dataset.seq) : null;
        if (seq) loadHistory(seq);
    }
});


/* ── 来源：js/voice_plugin.js ── */
// ═══════════════════════════════════════════
// 语音插件页（首页 →「语音插件」）
//   GET  /voice/plugin              → 状态（5 态 + 缺什么 + 下载进度）
//   POST /voice/plugin {action,...} → install/cancel/enable/disable/uninstall/rescan/set_mood/set_repo
// 设计与边界见 docs/工具/tts.md。本页只做展示与派发，不做任何模型逻辑。
// ═══════════════════════════════════════════
let _vpTimer = null;

const _VP_LABEL = {
    downloading: "⏳ 下载中",
    not_installed: "⬇ 未安装",
    installed_disabled: "⏸ 已安装 · 未启用",
    unavailable: "⚠ 已启用 · 不可用",
    ready: "✅ 可用",
};

function _vpEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
        {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function _vpMB(n) {
    return (n / 1048576).toFixed(1) + " MB";
}

async function _vpFetchState() {
    try {
        const r = await fetch("/voice/plugin");
        return await r.json();
    } catch (e) {
        return {id: "unavailable", label: "无法连接后端", reason: String(e)};
    }
}

async function _vpAct(action, extra) {
    try {
        const r = await fetch("/voice/plugin", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(Object.assign({action: action}, extra || {})),
        });
        const d = await r.json();
        if (d && d.ok === false) {
            console.warn("[voice] 操作失败", d.error);   // 原始错误落日志，不透传给用户
            showToast("操作没成功：请重试；仍失败请在「反馈」里附诊断包");
        }
        return d;
    } catch (e) {
        showToast("操作失败：无法连接后端");
        return null;
    }
}

function _vpRender(st) {
    const box = document.getElementById("vv-body");
    if (!box) return;
    const dl = st.download || {};
    const moodRows = Object.keys(st.moods || {}).map(k => {
        const on = (st.mood === k);
        return `<button class="vv-mood${on ? " on" : ""}" type="button"
             onclick="voiceSetMood('${_vpEsc(k)}')">${_vpEsc(st.moods[k])}</button>`;
    }).join(" ");

    // 操作按钮：按状态给出可用的那几个
    const btns = [];
    if (st.id === "downloading") {
        btns.push(`<button class="vv-btn" onclick="voiceAct('cancel')">取消下载</button>`);
    } else {
        if (st.repo_configured) {
            btns.push(`<button class="vv-btn primary" onclick="voiceAct('install')">${st.models_found ? "重新下载" : "下载模型"}</button>`);
        }
        btns.push(`<button class="vv-btn" onclick="voiceAct('rescan')">重新扫描</button>`);
        const vc = st.voice_cache || {};
        if (vc.files) {
            btns.push(`<button class="vv-btn danger" onclick="voiceAct('purge_voice')">清除语音缓存（${vc.files} 条）</button>`);
        }
        if (st.models_found) {
            btns.push(st.enabled
                ? `<button class="vv-btn" onclick="voiceAct('disable')">停用</button>`
                : `<button class="vv-btn primary" onclick="voiceAct('enable')">启用</button>`);
            btns.push(`<button class="vv-btn danger" onclick="voiceAct('uninstall')">卸载</button>`);
        }
    }

    // 下载进度：带 sha256 对账状态（2026-09-18）。
    //   verify=on  → 拿到了仓库清单，每个文件下完都会比对 sha256（坏文件不会落盘）
    //   verify=off → 拿不到清单（自建镜像无清单端点等），降级为只比大小
    const _vfy = dl.verify === "on"
        ? `· 已校验 ${dl.verified || 0} 个`
        : (dl.verify === "off" ? "· 未校验（仓库无清单）" : "");
    const _pct = Math.max(0, Math.min(100, Number(dl.percent) || 0));   // 纵深防御：后端若给出越界值也不显示
    const dlBar = dl.running ? `
      <div class="vv-dl">
        <div class="vv-dl-bar"><i style="width:${_pct}%"></i></div>
        <div class="vv-dl-txt">${_vpEsc(dl.file || "")} · ${_pct}%
          ${dl.total ? "（" + _vpMB(dl.done) + " / " + _vpMB(dl.total) + "）" : ""}
          · 已用 ${dl.elapsed || 0}s ${_vfy}</div>
      </div>` : (dl.error ? `<div class="vv-err">上次下载：${_vpEsc(dl.error)}</div>`
        : (dl.verified ? `<div class="vv-hint">上次下载已通过 sha256 校验（${dl.verified} 个文件）</div>` : ""));

    const missing = (st.missing_files || []).length ? `
      <div class="vv-err">缺 ${st.missing_files.length} 个文件：${_vpEsc(st.missing_files.join("、"))}</div>` : "";

    const repoBox = st.repo_configured ? "" : `
      <div class="vv-field">
        <div class="vv-hint">模型仓库地址（含 <code>{file}</code> 占位；留空则无法下载）</div>
        <input id="vv-repo" type="text" placeholder="https://www.modelscope.cn/api/v1/models/&lt;ns&gt;/&lt;name&gt;/repo?Revision=master&amp;FilePath={file}">
        <button class="vv-btn" onclick="voiceAct('set_repo',{url:document.getElementById('vv-repo').value})">保存地址</button>
      </div>`;

    box.innerHTML = `
      <div class="vv-card vv-state vv-state-${_vpEsc(st.id)}">
        <div class="vv-state-label">${_vpEsc(_VP_LABEL[st.id] || st.label || st.id)}</div>
        ${st.reason ? `<div class="vv-reason">${_vpEsc(st.reason)}</div>` : ""}
      </div>

      ${dlBar}${missing}

      <div class="vv-card">
        <div class="vv-h">语气</div>
        <div class="vv-moods">${moodRows}</div>
        <div class="vv-hint">作用范围：长按消息 →「转语音」时使用所选语气</div>
      </div>

      <div class="vv-card">
        <div class="vv-h">模型</div>
        <div class="vv-kv"><span>状态</span><b>${st.models_found ? "已就位" : "未安装"}</b></div>
        <div class="vv-kv"><span>体积</span><b>${st.models_found ? _vpMB(st.model_bytes) : "—"}</b></div>
        <div class="vv-kv"><span>目录</span><code>${_vpEsc(st.models_dir || (st.search_dirs || [])[0] || "")}</code></div>
        ${st.engine && st.engine.ready ? `<div class="vv-kv"><span>引擎</span><b>已装载</b></div>` : ""}
        ${(st.voice_cache && st.voice_cache.files)
            ? `<div class="vv-kv"><span>已生成语音</span><b>${st.voice_cache.files} 条 · ${_vpMB(st.voice_cache.bytes)}</b></div>`
            : ""}
      </div>

      <div class="vv-actions">${btns.join(" ")}</div>
      ${repoBox}

      <div class="vv-foot">
        语音模型来自第三方整合包；<b>语音原声版权归米哈游所有</b>，仅供个人二次创作使用，
        禁止售卖与再分发。模型仅存放在本机，不会上传到服务器。
      </div>`;
}

async function renderVoiceView() {
    const box = document.getElementById("vv-body");
    if (box && !box.innerHTML.trim()) box.innerHTML = `<div class="vv-loading">读取中…</div>`;
    const st = await _vpFetchState();
    _vpRender(st);
    // 下载中 → 自动轮询刷新进度
    if (_vpTimer) { clearTimeout(_vpTimer); _vpTimer = null; }
    if (st.id === "downloading") {
        _vpTimer = setTimeout(renderVoiceView, 1200);
    }
}

function openVoiceView() {
    homeView.classList.remove("show");
    const cv = document.getElementById("cards-view");
    if (cv) cv.classList.remove("show");
    const fv = document.getElementById("fix-view");
    if (fv) fv.classList.remove("show");
    const v = document.getElementById("voice-view");
    if (!v) return;
    v.classList.add("show");
    try { if (location.hash !== "#voice") history.pushState({voice: true}, "", "#voice"); } catch (e) {}
    renderVoiceView();
}
window.openVoiceView = openVoiceView;

function closeVoiceView() {
    const v = document.getElementById("voice-view");
    if (v) v.classList.remove("show");
    if (_vpTimer) { clearTimeout(_vpTimer); _vpTimer = null; }
    showHome();
    try { if (location.hash === "#voice") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closeVoiceView = closeVoiceView;

async function voiceAct(action, extra) {
    if (action === "uninstall" && !confirm("确定卸载语音插件？\n\n会删除本机的模型文件（约 872 MB）。\n已生成的语音不受影响（那些随「清理历史」处理）。")) return;
    if (action === "purge_voice" && !confirm("清除所有已生成的语音？\n\n只删音频文件 —— 模型、语气库、聊天记录都不受影响。\n清除后重新点「转语音」会用当前前端重新合成（旧音频可能带有已修复的问题）。")) return;
    await _vpAct(action, extra);
    renderVoiceView();
}
window.voiceAct = voiceAct;

async function voiceSetMood(mood) {
    await _vpAct("set_mood", {mood: mood});
    // 同步长按菜单用的默认语气（chat.js 的 _voiceMood）
    if (window.setVoiceMood) window.setVoiceMood(mood);
    renderVoiceView();
}
window.voiceSetMood = voiceSetMood;

// ═══════════════════════════════════════════
// 消息下面的「语音条」
//   参考微信「语音转文字」的形态：语音生成后**贴在消息气泡下方**（不是弹播放器）。
//   数据来源：/voice/plugin 的 voice_cache.seqs = {mode: {seq: 时长}}。
//   · 生成成功后立刻贴上去
//   · 历史渲染后再扫一遍贴回（刷新/翻页后仍在）
//   · 点它播放 / 再点暂停（播放区在条上，长按菜单不再承担"播放"职责）
// ═══════════════════════════════════════════
let _voiceSeqMap = null;        // {mode: {seq: dur}}
let _voiceAudio = null;
let _voicePlayingBar = null;

async function voiceSyncCache() {
    try {
        const st = await (await fetch("/voice/plugin")).json();
        _voiceSeqMap = (st && st.voice_cache && st.voice_cache.seqs) || {};
    } catch (e) {
        _voiceSeqMap = null;
    }
    return _voiceSeqMap;
}
window.voiceSyncCache = voiceSyncCache;

/** 该 seq 是否已有语音（同步查本地缓存表） */
function voiceHas(seq) {
    const m = _voiceSeqMap && _voiceSeqMap[CURRENT_MODE];
    return !!(m && Object.prototype.hasOwnProperty.call(m, String(seq)));
}
window.voiceHas = voiceHas;

function _voiceStopPlay() {
    if (_voiceAudio) { try { _voiceAudio.pause(); } catch (e) {} _voiceAudio = null; }
    if (_voicePlayingBar) {
        _voicePlayingBar.classList.remove("playing");
        const i = _voicePlayingBar.querySelector(".vb-ico");
        if (i) i.textContent = "🔊";
        _voicePlayingBar = null;
    }
}

function _voicePlayBar(bar, seq) {
    // 再点 = 暂停
    if (_voicePlayingBar === bar) { _voiceStopPlay(); return; }
    _voiceStopPlay();
    const a = new Audio("/voice-file?mode=" + encodeURIComponent(CURRENT_MODE)
                        + "&name=v" + seq + ".wav");
    _voiceAudio = a;
    _voicePlayingBar = bar;
    bar.classList.add("playing");
    const ico = bar.querySelector(".vb-ico");
    if (ico) ico.textContent = "⏸";
    const stop = () => { if (_voicePlayingBar === bar) _voiceStopPlay(); };
    a.addEventListener("ended", stop);
    a.addEventListener("error", () => { stop(); showToast("播放失败"); });
    a.play().catch(() => { stop(); showToast("播放失败（再点一次试试）"); });
}

/** 把语音条贴到某条消息下面（幂等）。返回条元素。
 *
 *  ★ 语音条是 `.msg-row` 的**直接子元素**，靠 `.msg-row.has-voice{flex-wrap:wrap}`
 *    换到第二行 —— 这样**头像仍与气泡底部对齐**（微信/QQ 的形态）。
 *    早先把条塞进 `.msg-col` 时，列变高 → `align-items:flex-end` 把头像拉到语音条底，
 *    头像会比气泡低一截（真机截图发现）。
 */
function voiceAttachBar(row, seq, dur) {
    if (!row || seq === null || seq === undefined) return null;
    const exist = row.querySelector(".voice-bar");
    if (exist) {
        if (dur) {
            const d = exist.querySelector(".vb-dur");
            if (d) d.textContent = Math.round(dur) + "″";
        }
        return exist;
    }
    if (!row.querySelector(".bubble")) return null;   // 没气泡的（表情包/旁白）不贴
    row.classList.add("has-voice");
    // 外层 .voice-row 占满一行（强制换行 → 头像仍与气泡底部对齐），
    // 内层 .voice-bar 保持内容宽度（早先直接给条 flex-basis:100% 会把它拉成整行宽）
    const wrap = document.createElement("div");
    wrap.className = "voice-row";
    const bar = document.createElement("div");
    bar.className = "voice-bar";
    bar.dataset.seq = String(seq);
    bar.title = "点击播放";
    bar.innerHTML = `<span class="vb-ico">🔊</span><span class="vb-dur">`
        + (dur ? Math.round(dur) + "″" : "…") + `</span>`;
    bar.addEventListener("click", (e) => { e.stopPropagation(); _voicePlayBar(bar, seq); });
    wrap.appendChild(bar);
    row.appendChild(wrap);
    return bar;
}
window.voiceAttachBar = voiceAttachBar;

/** 历史渲染后扫一遍，把已有语音的条贴回去 */
function voiceRestoreBars() {
    const m = _voiceSeqMap && _voiceSeqMap[CURRENT_MODE];
    if (!m) return 0;
    const root = (typeof messagesEl !== "undefined" && messagesEl) ? messagesEl : document;
    let n = 0;
    root.querySelectorAll(".msg-row[data-seq]").forEach((row) => {
        const s = parseInt(row.dataset.seq, 10);
        if (isNaN(s)) return;
        const k = String(s);
        if (Object.prototype.hasOwnProperty.call(m, k) && voiceAttachBar(row, s, m[k])) n++;
    });
    return n;
}
window.voiceRestoreBars = voiceRestoreBars;

/** 首次进聊天时：拉一次缓存表再贴 */
async function voiceInitBars() {
    await voiceSyncCache();
    voiceRestoreBars();
}
window.voiceInitBars = voiceInitBars;

/** 渲染后自动贴（首次会先拉一次缓存表；之后直接用内存里的表） */
let _voiceCacheSynced = false;
async function voiceRestoreBarsAuto() {
    try {
        if (!_voiceCacheSynced) {
            await voiceSyncCache();
            _voiceCacheSynced = true;
        }
        voiceRestoreBars();
    } catch (e) { /* 语音条失败绝不影响消息渲染 */ }
}
window.voiceRestoreBarsAuto = voiceRestoreBarsAuto;


/* ── 来源：js/fix.js ── */
// 设定纠错助手视图

// ═══════════════════════════════════════════
// 设定纠错助手（对齐 → 开始修改 → diff 审批 → 应用/回滚）
// ═══════════════════════════════════════════
const FIX_FILE_LABELS = {
    "core.md": "核心设定", "identity.md": "关系与习惯", "sms_samples.md": "短信风格",
    "用户设定.md": "用户补充设定", "memory.md": "过往摘要", "手账.md": "手账",
};
let FIX_MODE = "story";   // 首页卡片选择的模式；进入聊天后跟随最近使用模式
let _fixBusy = false;


function fixModeLabel(mode) { return MODE_NAMES[mode] || mode; }

function openFixView() {
    // F-6.1（2026-09-14）：进入纠错页跟随当前聊天包——原来 FIX_MODE 恒为初值 story，
    // 用户在 haruno/自建包里点「指出问题」，读的是 story 的历史与设定、写的也是 story。
    // 页内模式按钮仍可手动切换（setFixMode 语义不变）。
    if (PRESET_MODES.some(m => m.id === CURRENT_MODE)) FIX_MODE = CURRENT_MODE;
    document.querySelectorAll("#fix-view .fix-mode").forEach(b => {
        b.classList.toggle("active", b.dataset.mode === FIX_MODE);
    });
    homeView.classList.remove("show");
    appView.style.display = "none";
    const view = document.getElementById("fix-view");
    if (view) view.classList.add("show");
    try { if (location.hash !== "#fix") history.pushState({fix: true}, "", "#fix"); } catch (e) {}
    loadFixStatus();
    loadFixChatHistory();
    const input = document.getElementById("fix-input");
    // 引导教程演示纠错页时不要弹键盘（会遮住底部讲解气泡）
    if (input && !document.getElementById("guide-mask")) setTimeout(() => input.focus(), 300);
}
window.openFixView = openFixView;

function closeFixView() {
    const view = document.getElementById("fix-view");
    if (view) view.classList.remove("show");
    showHome();
    try { if (location.hash === "#fix") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closeFixView = closeFixView;

function toggleFixForms() {
    // 兼容旧入口：现在一律打开独立全屏页
    openFixView();
}
window.toggleFixForms = toggleFixForms;

function setFixMode(mode) {
    // 模式校验按预设包注册表（不写死两模式）
    if (!PRESET_MODES.some(m => m.id === mode)) mode = (PRESET_MODES[0] && PRESET_MODES[0].id) || "story";
    FIX_MODE = mode;
    document.querySelectorAll("#fix-view .fix-mode").forEach(b => {
        b.classList.toggle("active", b.dataset.mode === FIX_MODE);
    });
    loadFixStatus();
    loadFixChatHistory();
}
window.setFixMode = setFixMode;

function _fixChatScroll() {
    const el = document.getElementById("fix-chat");
    if (el) el.scrollTop = el.scrollHeight;
}

function _setFixStatus(stage) {
    const dot = document.getElementById("fix-status-dot");
    const text = document.getElementById("fix-view-status");
    if (!dot || !text) return;
    const map = {
        idle:     ["ok", "状态正常 · 等待你描述问题"],
        aligning: ["busy", "AI 正在和你对齐问题…"],
        ready:    ["ready", "已对齐 · 点「开始修改」生成清单"],
        proposal: ["warn", "方案待确认 · 点「应用修改」才生效"],
        busy:     ["busy", "AI 正在处理，请稍候…"],
        error:    ["error", "处理出错 · 请重试"],
    };
    const v = map[stage] || map.idle;
    dot.className = "fix-dot " + v[0];
    text.textContent = v[1];
}

function _fixHistText(m) {
    if (!m) return "";
    if (m.type === "sticker") return "[表情包：" + (m.label || m.path || m.file || "") + "]";
    if (m.type === "narration") return (m.text || m.content || "");
    return m.content || m.text || "";
}

function _fixHistMsgHtml(m) {
    const me = m.who === "user";
    return `<div class="fix-hist-msg ${me ? "me" : ""}">
        <div class="fix-hist-line">
            <span class="fix-hist-who">${me ? "我" : escapeHtml(charName())}</span>
            <span class="fix-hist-time">${escapeHtml((m.time || "").slice(5, 16))}</span>
        </div>
        <div class="fix-hist-text">${escapeHtml(_fixHistText(m))}</div>
    </div>`;
}

async function loadFixChatHistory() {
    const meta = document.getElementById("fix-chathist-meta");
    const count = document.getElementById("fix-chathist-count");
    const list = document.getElementById("fix-chathist-list");
    if (!list) return;
    try {
        const resp = await fetch(`/history?limit=20&mode=${encodeURIComponent(FIX_MODE)}`);
        const data = await resp.json();
        const msgs = Array.isArray(data.messages) ? data.messages : [];
        const total = data.total != null ? data.total : msgs.length;
        const label = fixModeLabel(FIX_MODE);
        if (meta) meta.textContent = msgs.length ? `${label} · 最近 ${msgs.length} 条` : `${label} · 暂无聊天记录`;
        if (count) count.textContent = msgs.length ? `显示最近 ${msgs.length} 条 / 共 ${total} 条` : "这个模式还没有聊天记录";
        list.innerHTML = msgs.length
            ? msgs.map(_fixHistMsgHtml).join("")
            : `<div class="fix-hist">这个模式还没有聊天记录；先去聊几句，再来描述问题会更方便。</div>`;
    } catch (e) {
        if (count) count.textContent = "聊天记录读取失败";
        list.innerHTML = `<div class="fix-hist">聊天记录读取失败（本地后端未就绪时会这样，不影响对齐功能）</div>`;
    }
}
window.loadFixChatHistory = loadFixChatHistory;

function _fixMsgHtml(m) {
    const who = m.who === "user" ? "我" : "设定助手";
    const cls = m.who === "user" ? "me" : "ai";
    const opts = (m.options || []).map((o, i) =>
        `<button class="fix-opt" data-opt="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join("");
    return `<div class="fix-msg ${cls}"><div class="fix-who">${who}</div>`
         + `<div class="fix-text">${escapeHtml(m.text)}</div>`
         + (opts ? `<div class="fix-options">${opts}</div>` : "") + `</div>`;
}

function _fixChangeHtml(ch) {
    const tag = ch.op === "append" ? "补充" : "纠正";
    const oldHtml = ch.op === "replace"
        ? `<div class="fix-diff-old">− ${escapeHtml(ch.old)}</div>` : "";
    return `<div class="fix-change">
        <div class="fix-change-head">
            <span class="fix-file-tag">${escapeHtml(FIX_FILE_LABELS[ch.file] || ch.file)}</span>
            <span class="fix-op-tag ${ch.op === "append" ? "add" : "fix"}">${tag}</span>
        </div>
        ${oldHtml}
        <div class="fix-diff-new">+ ${escapeHtml(ch.new)}</div>
        <div class="fix-reason">${escapeHtml(ch.reason || "")}</div>
    </div>`;
}

function _renderFix(status) {
    const chat = document.getElementById("fix-chat");
    const proposal = document.getElementById("fix-proposal");
    const startBtn = document.getElementById("fix-start-btn");
    const historyBox = document.getElementById("fix-history-box");
    if (!chat || !proposal || !startBtn) return;

    _setFixStatus(status.stage || "idle");

    if (Array.isArray(status.messages) && status.messages.length) {
        chat.innerHTML = status.messages.map(_fixMsgHtml).join("");
        _fixChatScroll();
    } else {
        chat.innerHTML = `<div class="fix-empty">先说说角色哪里说得不对，我会和你确认后再生成修改方案。</div>`
                       + `<div class="fix-hint">例如：角色还说自己在医疗舱，但设定里已经恢复得不错、能开机甲了。</div>`;
    }

    // 选项 chips：只在没有 pending 时启用（有 pending 时是改方案，选项已过期）
    chat.querySelectorAll(".fix-opt").forEach(btn => {
        btn.addEventListener("click", () => {
            if (_fixBusy || status.stage === "proposal") return;
            sendFixMessage(btn.dataset.opt);
        });
    });

    startBtn.style.display = (status.stage === "ready") ? "" : "none";
    startBtn.disabled = !!_fixBusy;

    // 提案面板
    if (status.stage === "proposal" && status.proposal) {
        const p = status.proposal;
        const changes = Array.isArray(p.changes) ? p.changes : [];
        proposal.style.display = "block";
        proposal.innerHTML = `<div class="fix-proposal-title"><span>修改清单（尚未生效）</span><span class="fix-op-tag">待确认</span></div>`
            + `<div class="fix-diag">${escapeHtml(p.diagnosis || "已生成修改方案，请确认后应用。")}</div>`
            + (changes.length ? changes.map(_fixChangeHtml).join("") : `<div class="fix-nochange">这次不需要修改设定文件。</div>`)
            + `<div class="fix-proposal-actions">
                 <button class="hb-btn" type="button" onclick="dismissFix()">放弃</button>
                 ${changes.length ? `<button class="hb-btn primary" type="button" onclick="applyFix()">应用修改</button>` : ""}
               </div>
               <div class="fix-refine-hint">想调整某一条？直接在下方说，例如：第二条先别改，橡木蛋糕卷那段保留。</div>`;
    } else {
        proposal.style.display = "none";
    }

    // 修正记录：列表 + 静态撤销按钮（全屏页固定位置，便于拇指操作）
    const hist = Array.isArray(status.history) ? status.history : [];
    const historyList = document.getElementById("fix-history-list") || historyBox;
    let histHtml = "";
    if (hist.length) {
        histHtml = hist.map(h => `<div class="fix-hist">v${h.v} · ${escapeHtml(h.action === "apply" ? "应用" : "回滚")} · ${escapeHtml((h.time || "").slice(5, 16))}${h.files ? " · " + escapeHtml(h.files.join("、")) : ""}</div>`).join("");
    } else {
        histHtml = `<div class="fix-hist">还没有修改记录</div>`;
    }
    historyList.innerHTML = histHtml;
    const rb = document.getElementById("fix-rollback-btn");
    if (rb) {
        rb.style.display = status.active_version > 0 ? "" : "none";
        rb.textContent = `撤销上次修改（回到 v${Math.max(0, status.active_version - 1)}）`;
    }
}

async function loadFixStatus(force) {
    if (_fixBusy && !force) return;
    try {
        const resp = await fetch(`/setting-fix/status?mode=${encodeURIComponent(FIX_MODE)}`);
        const data = await resp.json();
        if (data.ok) _renderFix(data);
        else if (data.error) _setFixStatus("error");
    } catch (e) { _setFixStatus("error"); }
}
window.loadFixStatus = loadFixStatus;

async function sendFixMessage(text) {
    text = (text || "").trim();
    if (!text || _fixBusy) return;
    const input = document.getElementById("fix-input");
    if (input) input.value = "";
    _fixBusy = true;
    _setFixStatus("busy");
    const sendBtn = document.getElementById("fix-send-btn");
    if (sendBtn) sendBtn.disabled = true;
    const chat = document.getElementById("fix-chat");
    if (chat) {
        chat.insertAdjacentHTML("beforeend", _fixMsgHtml({who: "user", text: text}));
        _fixChatScroll();
    }
    try {
        const resp = await fetch("/setting-fix/message", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE, text: text }),
        });
        const data = await resp.json();
        if (data.ok) {
            await loadFixStatus(true);
        } else if (data.need_key) {
            showToast("请先到 ⚙ 设置里填写 API Key");
            openSettings();
        } else {
            // A3：LLM 错误分类 → 人话提示（与聊天页 ERROR_TIPS 同口径：同一错误码全仓同一句话）
            const FIX_ERROR_TIPS = {
                key_invalid: "你的 API Key 无效或已过期：请到设置里重新粘贴 Key（本机对话与角色卡不受影响）",
                no_balance: "你的上游账号余额不足：请到所选供应商的控制台充值后重试（与本机数据、角色卡无关）",
                rate_limit: "这是上游接口的临时限制，不是你的卡或操作的问题：约 1 分钟后会自动重试",
                network: "本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）",
                server_error: "上游服务临时不可用，不是你那边的问题：稍后重试即可，不必改 Key",
                relay_timeout: "这次请求超时了，不是你那边的问题：请确认应用在前台、网络正常后重试",
                timeout: "这次生成超过了本轮时间上限，不是你那边的问题：稍等再发一次即可",
                cooldown: "上游服务波动中，不是你那边的问题：稍等约 1 分钟再试",
                quota_exhausted: "今日服务器托管额度已用完，可在设置中切换为自带 Key 模式",
                unknown: "发生了未归类的问题：请重试一次；仍失败请在「反馈」里附诊断包",
            };
            showToast(data.error_code
                ? (FIX_ERROR_TIPS[data.error_code] || FIX_ERROR_TIPS.unknown)
                : (data.error || "分析失败，请稍后再试"));
        }
    } catch (e) {
        console.warn("[fix] 网络请求失败", e);
        showToast("本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）");
    } finally {
        _fixBusy = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}
window.sendFixMessage = sendFixMessage;

async function startFix() {
    if (_fixBusy) return;
    _fixBusy = true;
    _setFixStatus("busy");
    const btn = document.getElementById("fix-start-btn");
    if (btn) { btn.disabled = true; btn.textContent = "正在生成修改清单…"; }
    try {
        const resp = await fetch("/setting-fix/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        if (data.ok) {
            showToast("修改清单已生成，确认后再点应用");
            await loadFixStatus(true);
        } else if (data.need_key) {
            showToast("请先到 ⚙ 设置里填写 API Key");
            openSettings();
        } else {
            showToast(data.error || "生成失败，请稍后再试");
        }
    } catch (e) {
        console.warn("[fix] 网络请求失败", e);
        showToast("本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）");
    } finally {
        _fixBusy = false;
        if (btn) { btn.disabled = false; btn.textContent = "开始修改"; }
    }
}
window.startFix = startFix;

async function applyFix() {
    if (_fixBusy) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/apply", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE, session_id: SESSION_ID }),
        });
        const data = await resp.json();
        if (data.ok) {
            showToast(data.message || "修改已生效");
            await loadFixStatus(true);
            loadFixChatHistory();
        } else {
            showToast(data.error || "应用失败");
        }
    } catch (e) {
        console.warn("[fix] 网络请求失败", e);
        showToast("本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）");
    } finally {
        _fixBusy = false;
    }
}
window.applyFix = applyFix;

async function dismissFix() {
    if (_fixBusy) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/dismiss", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        showToast(data.ok ? "已放弃本次修改方案" : (data.error || "操作失败"));
        await loadFixStatus(true);
    } catch (e) {
        console.warn("[fix] 网络请求失败", e);
        showToast("本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）");
    } finally {
        _fixBusy = false;
    }
}
window.dismissFix = dismissFix;

async function rollbackFix() {
    if (!confirm("撤销上次设定修改？将恢复到上一个版本，对话数据不受影响。")) return;
    if (_fixBusy) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/rollback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        if (data.ok) showToast("已撤销，设定恢复到上一版本");
        else showToast(data.error || "撤销失败");
        await loadFixStatus(true);
    } catch (e) {
        console.warn("[fix] 网络请求失败", e);
        showToast("本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）");
    } finally {
        _fixBusy = false;
    }
}
window.rollbackFix = rollbackFix;

async function resetFix() {
    if (!confirm("清空当前的问题描述和待确认方案？已应用的修改记录会保留。")) return;
    _fixBusy = true;
    try {
        const resp = await fetch("/setting-fix/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: FIX_MODE }),
        });
        const data = await resp.json();
        showToast(data.ok ? "已清空当前问题" : (data.error || "操作失败"));
        await loadFixStatus(true);
    } catch (e) {
        console.warn("[fix] 网络请求失败", e);
        showToast("本机网络或接口地址连不上：请检查网络后重试（不是账号问题，数据不受影响）");
    } finally {
        _fixBusy = false;
    }
}
window.resetFix = resetFix;

// 模式按钮按预设包注册表动态渲染（容器在 index.html 留空，此处填充）
function renderFixModes() {
    const box = document.getElementById("fix-modes");
    if (!box) return;
    box.innerHTML = "";
    for (const m of PRESET_MODES) {
        const b = document.createElement("button");
        b.className = "fix-mode" + (m.id === FIX_MODE ? " active" : "");
        b.type = "button";
        b.dataset.mode = m.id;
        b.textContent = m.name || m.id;
        b.addEventListener("click", () => setFixMode(m.id));
        box.appendChild(b);
    }
}
window.renderFixModes = renderFixModes;   // loadModes 完成后由 views.js 调用重渲染（ESM 循环规避）

(function initFixModule() {
    const input = document.getElementById("fix-input");
    const sendBtn = document.getElementById("fix-send-btn");
    if (input && sendBtn) {
        sendBtn.addEventListener("click", () => sendFixMessage(input.value));
        input.addEventListener("keydown", e => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFixMessage(input.value); }
        });
    }
    // 模式按钮由 loadModes 完成后桥调 window.renderFixModes() 渲染
    // （不可在此直接调：bundle 拼接顺序 fix 段在 views 段前，PRESET_MODES 此时 TDZ 未初始化）
    const histRefresh = document.getElementById("fix-chathist-refresh");
    if (histRefresh) histRefresh.addEventListener("click", loadFixChatHistory);})();


/* ── 来源：js/views.js ── */
// 视图切换：首页 / 聊天 / 轮播 / 主题 / 界面缩放 / 粒子与降级开关

// ═══ 入口收敛（A5 底部导航已于 0.8.1 移除）：聊天页全屏只留输入栏，
// 首页轮播图进入聊天、返回键/首页按钮回首页、设置走首页右上角 ⚙ / 汉堡菜单 ═══
function activateTab(tab) {
    // 兼容遗留调用（panels.js / 历史代码 window.btActivate）：导航已移除，空实现
}
window.btActivate = activateTab;

// ═══════════════════════════════════════════
// 界面大小调节（消息/头像/气泡缩放，设置面板滑条）
// ═══════════════════════════════════════════
function applyUiScale(percent) {
    document.body.style.setProperty("--ui-scale", (percent / 100).toFixed(2));
    const val = document.getElementById("ui-scale-value");
    if (val) val.textContent = percent + "%";
    const sum = document.getElementById("ui-scale-summary");   // 外观组头摘要（4.4）
    if (sum) sum.textContent = percent + "%";
}
const uiSlider = document.getElementById("ui-scale-slider");
if (uiSlider) {
    let saved = 100;
    try { saved = parseInt(localStorage.getItem("ui-scale")) || 100; } catch (e) {}
    uiSlider.value = saved;
    applyUiScale(saved);
    uiSlider.addEventListener("input", () => {
        const v = parseInt(uiSlider.value) || 100;
        applyUiScale(v);
        try { localStorage.setItem("ui-scale", String(v)); } catch (e) {}
    });
}

// ═══════════════════════════════════════════
// 配色切换（暗色 / 游戏亮色，首页右上角）
// ═══════════════════════════════════════════
function toggleTheme() {
    const light = document.body.classList.toggle("theme-light");
    try { localStorage.setItem("theme", light ? "light" : "dark"); } catch (e) {}
    const icon = document.getElementById("theme-icon");
    if (icon) icon.src = light ? "assets/theme_moon.png" : "assets/theme_sun.png";
}
window.toggleTheme = toggleTheme;
(function applyTheme() {
    let t = "dark";
    try { t = localStorage.getItem("theme") || "dark"; } catch (e) {}
    if (t === "light") {
        document.body.classList.add("theme-light");
        const icon = document.getElementById("theme-icon");
        if (icon) icon.src = "assets/theme_moon.png";
    }
})();

// ═══ 萤火粒子开关（5.1：设置 → 外观；关闭省电）+ 低端机降级（5.5）═══
(function initPerfToggles() {
    // 低端机探测：内存 ≤4GB 或 核数 ≤4 → body.low-end（CSS 关毛玻璃）
    try {
        const dm = navigator.deviceMemory || 8, hc = navigator.hardwareConcurrency || 8;
        if (dm <= 4 || hc <= 4) document.body.classList.add("low-end");
    } catch (e) {}
    // 粒子开关：window.__particlesOff 供 index.html 内联 canvas 脚本逐帧检查
    const canvas = document.getElementById("firefly-field");
    let off = false;
    try { off = localStorage.getItem("firefly_particles") === "0"; } catch (e) {}
    function applyParticles() {
        window.__particlesOff = off;
        if (canvas) canvas.style.display = off ? "none" : "";
    }
    applyParticles();
    const cb = document.getElementById("particles-enabled");
    if (cb) {
        cb.checked = !off;
        cb.addEventListener("change", () => {
            off = !cb.checked;
            try { localStorage.setItem("firefly_particles", off ? "0" : "1"); } catch (e) {}
            applyParticles();
        });
    }
})();

// ═══════════════════════════════════════════
// 首页：视图切换 / 滚动轮播 / 公告面板 / 模式入口
// ═══════════════════════════════════════════
const homeView = document.getElementById("home-view");
const appView = document.getElementById("app");

// 模式（仅两类）：story=故事/剧情模式；haruno=剧本模式（有旁白与环境描写）
// 角色预设化：模式清单来自后端注册表（GET /modes），前端不写死包列表
let CURRENT_MODE = "story";
let _lastMode = null;   // 上次进入聊天时的模式（切换时重载历史）
let _modeGen = 0;       // 模式代际：切换时递增，飞行中的异步渲染/历史加载任务作废丢弃
const MODE_NAMES = { story: "剧情模式", haruno: "剧本模式" };   // 默认两内置；loadModes 后按注册表刷新
const PRESET_MODES = [];   // /modes 清单（id/name/presentation/desc/tagline/cover/avatar/has_opening）
// 归档包清单（3.5）：归档 = 不进 PRESET_MODES（不在 /modes 的 modes 里），但数据与清单条目都在。
// 单独存一份是为了让首页能画出"已归档"区块——否则归档就等于把包弄丢：看不见、恢复不了。
const ARCHIVED_PACKS = [];
let _modesLoaded = false;

// ── 当前包持久化（F-6.3，2026-09-13）──
// 原状：localStorage 只存 ui-scale/theme/particles，当前包从不落盘，而 /modes 的
// `default` 字段前端也没读——于是切到 haruno 后一刷新就弹回 story，
// 用户以为"我的角色卡丢了"。
// 恢复顺序：last_mode → /modes.default → "story"；每个候选都必须在注册表里，否则继续回退。
const _MODE_KEY = "firefly_last_mode";
function _rememberMode(id) {
    try { if (id) localStorage.setItem(_MODE_KEY, id); } catch (e) {}
}
function _savedMode() {
    try { return localStorage.getItem(_MODE_KEY) || ""; } catch (e) { return ""; }
}
// 切包的唯一入口：校验 → 赋值 → 持久化。所有入口都走这里，
// 避免"某些入口记得存、某些不存"（那会让持久化时灵时不灵）。
function _applyMode(id) {
    if (!id || !PRESET_MODES.some(m => m.id === id)) return false;
    CURRENT_MODE = id;
    _rememberMode(id);
    return true;
}
// 面向用户的"进入某包"：非法/空 id 回退注册表首包，最后兜底 "story"
function _switchMode(id) {
    if (_applyMode(id)) return;
    if (_applyMode(PRESET_MODES[0] && PRESET_MODES[0].id)) return;
    CURRENT_MODE = "story";
}

function modeName(id) { return MODE_NAMES[id] || id; }
function currentPreset() {
    return PRESET_MODES.find(m => m.id === CURRENT_MODE) || PRESET_MODES[0] || null;
}

/** 当前角色卡的**角色名**（角色卡化铁律：框架任何地方都不许写角色名字面量）。
 *  取不到返回空串 —— 调用方自行决定降级（名字行不渲染 / 文案省略称呼），
 *  **不要**回落到某个具体角色名，否则自建卡会显示别人的名字。 */
function charName() {
    const p = currentPreset();
    return ((p && (p.char_name || p.name)) || "").toString().trim();
}

/** 当前角色卡的**用户称呼**（同上：取不到返回空串，不回落字面量）。 */
function userName() {
    const p = currentPreset();
    return ((p && p.user_name) || "").toString().trim();
}
function setCurrentMode(mode) {   // ESM 导出只读绑定，外部经此切换
    _applyMode(mode);
}

// 拉取预设包清单并渲染模式卡片（轮播 + PC 大卡）；失败兜底两内置包（与后端兜底一致）
async function loadModes() {
    if (_modesLoaded) return;
    _modesLoaded = true;
    let serverDefault = "";
    try {
        const resp = await fetch("/modes");
        const data = await resp.json();
        const list = Array.isArray(data.modes) ? data.modes : [];
        serverDefault = typeof data.default === "string" ? data.default : "";
        const arch = Array.isArray(data.archived) ? data.archived : [];
        ARCHIVED_PACKS.splice(0, ARCHIVED_PACKS.length, ...arch);
        if (list.length) {
            PRESET_MODES.splice(0, PRESET_MODES.length, ...list);
            for (const m of list) MODE_NAMES[m.id] = m.name || m.id;
        }
    } catch (e) {}
    if (!PRESET_MODES.length) {
        // 离线兜底：只给**渲染必需**的最小信息（id/模式名/演出形态/图）。
        // **不带 char_name / user_name** —— 那是角色卡数据，正常从 /modes 取；
        // 兜底路径下名字行为空（宁可不显示，也不在框架里写死某个角色名）。
        PRESET_MODES.push(
            {id: "story", name: "剧情模式", presentation: "sticker", desc: "", tagline: "",
             cover: "/assets/character/story/assets/cover.png", avatar: "/assets/character/story/assets/avatar.png", has_opening: false},
            {id: "haruno", name: "剧本模式", presentation: "narration", desc: "", tagline: "",
             cover: "/assets/character/haruno/assets/cover.png", avatar: "/assets/character/haruno/assets/avatar.png", has_opening: true});
    }
    // 启动恢复当前包（F-6.3）：候选逐个校验，全不合法才回退注册表首包
    const pick = [_savedMode(), serverDefault, "story"].find(id => id && PRESET_MODES.some(m => m.id === id))
        || (PRESET_MODES[0] && PRESET_MODES[0].id) || "story";
    _applyMode(pick);
    renderModeCards();
    applyModeBranding();
    try { window.renderFixModes && window.renderFixModes(); } catch (e) {}   // 纠错页模式按钮随注册表刷新
}

// 包资产编辑后强制重拉注册表（panels.js 角色包管理调用）
window.__modesReload = async () => {
    _modesLoaded = false;
    await loadModes();
};
// bundle 拼接单作用域下，panels.js 取注册表/切模式的桥（避免 import("./views.js")
// 动态导入产生第二份 ESM 模块实例）
window.__getPresets = () => PRESET_MODES;
window.__setCurrentMode = (mode) => setCurrentMode(mode);
// PC 三栏外壳（pc_shell.js，独立 classic script）读当前包用：它看不到 bundle 作用域里的
// CURRENT_MODE，所以给一个只读 getter（不要给它写入口，切包一律走 enterMode）。
window.__getCurrentMode = () => CURRENT_MODE;

// 进入某包的管理页（卡片角标/轮播角标入口）：切到该包 + 打开角色编辑页。
// （2026-09-14 修复：此处原有第二个同名 managePack 定义（openPackView 包装），函数声明提升下
//  后者覆盖前者，菜单里又没有 "pack" 这个 tab，旧定义实为死代码+误导——已删，统一走 openPackView。）
document.getElementById("carousel-manage-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const m = PRESET_MODES[carouselIndex];
    if (m) openPackView(m.id);
});

// ── 新建角色卡（阶段7，本地版）──
// 命名统一（2026-09-18）：角色名_模式名_创建时间（月日_时分），如「流萤_剧情_0918_0041」。
// 同角色允许建多个包（后端不校验 char_name 唯一），靠这个名字区分。
function _defaultPackName(charName, presentation) {
    const d = new Date();
    const p2 = n => String(n).padStart(2, "0");
    const label = presentation === "narration" ? "剧本" : "剧情";
    return `${charName || "角色"}_${label}_${p2(d.getMonth() + 1)}${p2(d.getDate())}_${p2(d.getHours())}${p2(d.getMinutes())}`;
}

function togglePackCreate(show) {
    const p = document.getElementById("pack-create-panel");
    if (!p) return;
    const visible = p.style.display !== "none";
    const target = (show === undefined) ? !visible : !!show;
    p.style.display = target ? "block" : "none";
    if (target) {
        const nameEl = document.getElementById("pc-name");
        // 名称留空时预填默认名（用户可改；留空提交后端也会按同规则生成）
        if (nameEl && !nameEl.value.trim()) {
            const cn = (document.getElementById("pc-char")?.value || "").trim();
            const pres = document.getElementById("pc-presentation")?.value || "sticker";
            nameEl.value = _defaultPackName(cn, pres);
        }
        nameEl?.focus();
    }
}
window.togglePackCreate = togglePackCreate;

document.getElementById("pc-submit")?.addEventListener("click", async () => {
    const name = document.getElementById("pc-name").value.trim();
    const charName = document.getElementById("pc-char").value.trim();
    const userName = document.getElementById("pc-user").value.trim();
    const presentation = document.getElementById("pc-presentation").value;
    // 名称可留空：后端按「角色名_模式名_月日_时分」生成（同角色多包靠它区分）
    if (!charName || !userName) { showToast("角色名、对方称呼都必填"); return; }
    try {
        const resp = await fetch("/pack-create", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name, char_name: charName, user_name: userName, presentation})});
        const data = await resp.json();
        if (data.ok) {
            showToast(`已创建「${data.name}」——点列表里的卡片进详情编辑人设`);
            togglePackCreate(false);
            await window.__modesReload();
            renderCardsList();
        } else {
            showToast("创建失败：" + (data.error || ""));
        }
    } catch (e) { showToast("网络错误"); }
});



// 轮播图上的角色信息条：跟随当前轮播位置
function _updateCarouselChar() {
    const c = document.getElementById("carousel-char");
    if (!c) return;
    const m = PRESET_MODES[carouselIndex];
    if (!m) { c.style.display = "none"; return; }
    c.style.display = "";
    const img = c.querySelector("img");
    _setImgSrc(img, m.avatar);   // F-5：无头像清掉旧 src（持久元素，否则沿用上一包的图）
    c.querySelector(".cc-name").textContent = m.char_name || "";
    c.querySelector(".cc-scene").textContent = m.name || "";
}

// 角色编辑页（独立全屏）：切换目标包 + 打开；_packFrom 记录来源（cards=列表页 / 其他=首页）
let _packFrom = "";
function openPackView(mode, from) {
    if (mode) _applyMode(mode);
    _packFrom = from || "";
    homeView.classList.remove("show");
    const cv = document.getElementById("cards-view");
    if (cv) cv.classList.remove("show");
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    const v = document.getElementById("pack-view");
    if (!v) return;
    v.classList.add("show");
    try { if (location.hash !== "#pack") history.pushState({pack: true}, "", "#pack"); } catch (e) {}
    try { window.loadPackView && window.loadPackView(); } catch (e) {}
}
window.openPackView = openPackView;
function closePackView() {
    const v = document.getElementById("pack-view");
    if (v) v.classList.remove("show");
    if (_packFrom === "cards") { showCardsView(); return; }   // 从列表来则回列表
    showHome();
    try { if (location.hash === "#pack") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closePackView = closePackView;

// ── 角色卡管理页（全屏：卡片列表 → 点卡进详情编辑）──
function openCardsView() {
    homeView.classList.remove("show");
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    const v = document.getElementById("cards-view");
    if (!v) return;
    v.classList.add("show");
    // 服务器版不做自定义整包：隐藏新建区
    const newBox = document.querySelector("#cards-view .cv-new");
    if (newBox) newBox.style.display = IS_SERVER ? "none" : "";
    try { if (location.hash !== "#cards") history.pushState({cards: true}, "", "#cards"); } catch (e) {}
    renderCardsList();
}
window.openCardsView = openCardsView;
function showCardsView() { openCardsView(); }
function closeCardsView() {
    const v = document.getElementById("cards-view");
    if (v) v.classList.remove("show");
    showHome();
    try { if (location.hash === "#cards") history.replaceState({}, "", location.pathname + location.search); } catch (e) {}
}
window.closeCardsView = closeCardsView;

// 卡片列表：每张卡 = 头像 + 角色名 + 剧本/形态，点击进详情编辑页
function renderCardsList() {
    const list = document.getElementById("cv-list");
    if (!list) return;
    list.innerHTML = "";
    const presLabel = {sticker: "短信+表情包", narration: "短信+旁白", none: "纯短信"};
    for (const m of PRESET_MODES) {
        const card = document.createElement("button");
        card.className = "cv-card";
        card.type = "button";
        card.onclick = () => openPackView(m.id, "cards");
        const img = _coverImg(m, "cv-thumb");
        img.alt = "";
        const mid = document.createElement("div");
        const cn = document.createElement("div");
        cn.className = "cv-cname";
        cn.textContent = (m.char_name || "") + (m.custom ? "" : "");
        const sub = document.createElement("div");
        sub.className = "cv-csub";
        // name 形如「流萤_剧情_0918_0041」；副标题去掉与大字重复的角色名，
        // 留下「模式_创建时间 · 演出形态」——同角色多包靠这段区分（不分组方案）
        const _nm = m.name || m.id;
        const _shown = (m.char_name && _nm.startsWith(m.char_name + "_"))
            ? _nm.slice(m.char_name.length + 1) : _nm;
        sub.textContent = _shown + " · " + (presLabel[m.presentation] || m.presentation);
        mid.append(cn, sub);
        const go = document.createElement("span");
        go.className = "cv-cgo";
        go.textContent = "›";
        card.append(img, mid, go);
        list.appendChild(card);
    }
}

// 卡片角标/轮播角标入口（保留 managePack 名兼容）：经 _applyMode 校验后开详情页
function managePack(mode) { if (_applyMode(mode)) openPackView(); }
window.managePack = managePack;

// F-5（2026-09-14）：无封面/头像的包给「首字占位块」，不再显示破图或沿用上一张图。
// 返回值是元素（img 或 div），尺寸由调用处既有 class 决定。
function _coverImg(m, cls) {
    if (m.cover) {
        const img = document.createElement("img");
        img.className = cls;
        img.src = m.cover;
        img.alt = m.name || m.id;
        return img;
    }
    const d = document.createElement("div");
    d.className = cls + " pack-noimg";
    d.textContent = (m.char_name || m.name || "?").slice(0, 1);
    return d;
}

// 持久 <img> 元素（详情页/品牌位/轮播信息条）：无图必须清掉旧 src，否则沿用上一包的图
function _setImgSrc(img, url) {
    if (!img) return;
    if (url) img.src = url;
    else img.removeAttribute("src");
}

// 按 PRESET_MODES 渲染角色卡（轮播 + PC 大卡）：卡的主角是角色（头像+角色名），
// 模式名（剧情模式/剧本模式）是小标签；卡上 ✎ 角标进角色编辑页
function renderModeCards() {
    carouselTrack.innerHTML = "";
    carouselDots.innerHTML = "";
    PRESET_MODES.forEach((m, i) => {
        carouselTrack.appendChild(_coverImg(m, "pack-noimg-abs"));
        const dot = document.createElement("span");
        if (i === 0) dot.classList.add("active");
        dot.addEventListener("click", () => goCarousel(i));
        carouselDots.appendChild(dot);
    });
    // 轮播图上叠加当前包的角色信息条（左下）
    _updateCarouselChar();
    carouselCount = PRESET_MODES.length;
    // 轮播初始位置跟随当前包（F-6.3 配套）：否则恢复成 haruno 后首页却显示 story 封面，
    // 用户点封面进入会被 enterCarouselAction 记成 story，刚恢复的记忆立刻被覆盖。
    if (carouselCount) {
        const i = PRESET_MODES.findIndex(m => m.id === CURRENT_MODE);
        goCarousel(i > 0 ? i : 0);
    }
    const hm = document.getElementById("home-modes");
    if (hm) {
        hm.innerHTML = "";
        for (const m of PRESET_MODES) {
            const btn = document.createElement("button");
            btn.className = "hm-card";
            btn.type = "button";
            btn.onclick = () => enterMode(m.id);
            const img = _coverImg(m, "hm-cover");
            // 编辑角标（毛玻璃，hover 卡面时显现）
            const edit = document.createElement("span");
            edit.className = "hm-edit";
            edit.title = `编辑「${m.char_name || m.name || m.id}」`;
            edit.textContent = "✎";
            edit.onclick = (e) => { e.stopPropagation(); openPackView(m.id); };
            // 角色信息叠加层：底部渐变压暗 + 头像 + 角色名 + 剧本标签
            const ov = document.createElement("div");
            ov.className = "hm-overlay";
            const av = _coverImg(m, "hm-avatar");
            const info = document.createElement("div");
            info.className = "hm-info";
            const cn = document.createElement("div");
            cn.className = "hm-charname";
            cn.textContent = m.char_name || m.name || m.id;
            const sc = document.createElement("div");
            sc.className = "hm-scene";
            sc.textContent = m.name || "";
            info.append(cn, sc);
            ov.append(av, info);
            btn.append(img, edit, ov);
            hm.appendChild(btn);
        }
        _renderArchivedPacks(hm);
    }
}

// 包生命周期动作（3.5）：归档 / 恢复 / 彻底删除 —— 首页归档区与包详情页**共用一套**调用与提示。
// 为什么收在一处：三个动作的端点、确认文案、失败提示一旦两处各写一遍就会漂移，
// 而漂移的后果是"按钮写着归档、实际调的是删除"这种灾难性不一致。
// 返回 true 表示动作成功（调用方据此刷新列表/关闭详情页）。
async function packLifecycle(action, id, label) {
    const paths = {archive: "/pack-archive", restore: "/pack-restore", erase: "/pack-delete"};
    const names = {archive: "归档", restore: "恢复", erase: "删除"};
    const path = paths[action];
    if (!path || !id) return false;
    if (action === "archive") {
        if (!confirm(`归档「${label}」？\n该包会从列表与聊天里隐藏，人设与聊天记录全部保留。`)) return false;
    } else if (action === "restore") {
        if (!confirm(`恢复「${label}」？`)) return false;
    } else {
        const typed = prompt(`彻底删除「${label}」后无法恢复（删除前会自动备份一份快照）。\n` +
            `请输入包名以确认：`);
        if (typed === null) return false;
        if (typed.trim() !== label) { showToast("包名不匹配，已取消"); return false; }
    }
    try {
        const r = await fetch(path, {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({id})});
        const d = await r.json();
        if (!d.ok) { showToast(names[action] + "失败：" + (d.error || "")); return false; }
        // 归档/删除后要重载（归档包不在清单里，留着旧视图只会显示别的包的数据）
        showToast(action === "archive" ? "已归档（数据保留）" :
            (action === "restore" ? "已恢复" : "已删除"));
        return true;
    } catch (e) { showToast("网络错误"); return false; }
}
window.__packLifecycle = packLifecycle;

// 已归档区块（3.5）：每个归档包给「恢复」与「彻底删除」两个出口。
// 彻底删除要**输入包名**确认（比 confirm 更难误触），后端还会在删除前强制打一份全量快照；
// 恢复只是把 state 改回 active（数据一直没动）。
function _renderArchivedPacks(container) {
    if (!container || !ARCHIVED_PACKS.length) return;
    const box = document.createElement("div");
    box.id = "archived-packs";
    box.style.cssText = "flex-basis:100%;margin-top:14px;padding:10px 12px;border:1px dashed #6666;" +
        "border-radius:10px;font-size:0.8em;color:var(--fg-muted, #999)";
    const title = document.createElement("div");
    title.style.cssText = "margin-bottom:8px;font-weight:600";
    title.textContent = `已归档（${ARCHIVED_PACKS.length}）—— 数据仍在，可随时恢复`;
    box.appendChild(title);
    for (const a of ARCHIVED_PACKS) {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:10px;padding:4px 0";
        const nm = document.createElement("span");
        nm.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
        nm.textContent = a.char_name || a.name || a.id;
        const back = document.createElement("button");
        back.type = "button";
        back.textContent = "恢复";
        back.onclick = async () => {
            if (await window.__packLifecycle("restore", a.id, a.name || a.id)) {
                await window.__modesReload();
            }
        };
        const kill = document.createElement("button");
        kill.type = "button";
        kill.textContent = "彻底删除";
        kill.onclick = async () => {
            if (await window.__packLifecycle("erase", a.id, a.name || a.id)) {
                await window.__modesReload();
            }
        };
        row.append(nm, back, kill);
        box.appendChild(row);
    }
    container.appendChild(box);
}

/** 把带 `data-tpl` 的静态文案按当前角色卡重填（角色卡化：HTML 里不写死角色名）。
 *
 *  用法：HTML 写通用默认文案（无 JS/首屏时不露角色名），标签带模板：
 *    `<span data-tpl="让{c}休息">让角色休息</span>`
 *    `<div data-tpl-ph="…让{c}休息…">` → 填 placeholder
 *  `{c}` = charName()，`{u}` = userName()；两者取不到时用中性词（角色／你）。
 *  覆盖了菜单「休息」按钮、手账标签与说明、休息/起床遮罩、用户形象弹窗标题等。 */
function applyTextTemplates() {
    const c = charName() || "角色";
    const u = userName() || "你";
    const fill = (s) => String(s).replace(/\{c\}/g, c).replace(/\{u\}/g, u);
    document.querySelectorAll("[data-tpl]").forEach(el => {
        el.textContent = fill(el.dataset.tpl || "");
    });
    document.querySelectorAll("[data-tpl-ph]").forEach(el => {
        el.setAttribute("placeholder", fill(el.dataset.tplPh || ""));
    });
}

// 聊天页/侧边栏的角色标识（头像/名字/签名）按当前包刷新
function applyModeBranding() {
    const p = currentPreset() || {};
    const cname = p.char_name || "";
    for (const id of ["chat-avatar", "pcs-avatar"]) {
        _setImgSrc(document.getElementById(id), p.avatar);   // F-5：无头像清旧 src
    }
    const pcsName = document.getElementById("pcs-name");
    if (pcsName) pcsName.textContent = cname;
    const chatName = document.getElementById("chat-char-name");
    if (chatName) chatName.textContent = cname;
    for (const id of ["pcs-status", "chat-status"]) {
        const el = document.getElementById(id);
        if (el) el.textContent = p.tagline || "";
    }
    applyTextTemplates();   // 静态文案里的角色名/称呼一并刷新（换卡后立刻生效）
}

function showHome() {
    const fixView = document.getElementById("fix-view");
    if (fixView) fixView.classList.remove("show");
    homeView.classList.add("show");
    appView.style.display = "none";     // 首页独立视图：真正隐藏聊天页（避免半透明透视）
    closeMenu();
    stopCarousel();
    goCarousel(0);   // 回到首页重置轮播位置
    startCarousel();   // 重新开始自动轮播
    try { activateTab("home"); } catch (e) {}
}
window.showHome = showHome;   // ESM 拆分后供 pc_nav.js（classic script）与内联 onclick 使用
async function showChat() {
    // A7 登录前置（本地版）：未登录 → 回首页展开登录表单（本地后端离线时放行）；
    // 已登录但离线宽限外（offline_ok===false）→ 显式 POST /auth/verify 一次再定夺：
    //   200 放行；401 回首页展开登录表单；网络/状态异常回首页提示联网验证。
    if (!IS_SERVER) {
        try {
            const st = await (await fetch("/auth/state")).json();
            if (st.logged_in === false) {
                showHome();
                showAuthModule();
                try { toggleAuthForms(); } catch (e) {}
                try { showToast("请先登录（登录后本地数据可云端同步，Key 仍只存本机）"); } catch (e) {}
                return;
            }
            if (st.logged_in && st.offline_ok === false) {
                let vResp = null;
                try { vResp = await fetch("/auth/verify", {method: "POST"}); } catch (e) { vResp = null; }
                if (vResp && vResp.status === 200) {
                    // verify 通过：放行
                } else if (vResp && vResp.status === 401) {
                    showHome();
                    showAuthModule();
                    try { toggleAuthForms(); } catch (e) {}
                    try { showToast("登录已过期，请重新登录"); } catch (e) {}
                    return;
                } else {
                    // 网络异常/意外状态码：本地登录态无法联网核验
                    showHome();
                    showAuthModule();
                    try { showToast("登录已过期，需联网验证一次"); } catch (e) {}
                    return;
                }
            }
        } catch (e) { /* 本地后端未就绪：放行（聊天不依赖账号） */ }
        // 进聊天页/模式切换成功后：补一次云端同步（不 await 不阻塞页面；节流期内自动跳过）
        try { window.autoSyncNow && window.autoSyncNow(); } catch (e) {}
    }
    const fixView = document.getElementById("fix-view");    if (fixView) fixView.classList.remove("show");
    homeView.classList.remove("show");
    appView.style.display = "flex";     // 恢复聊天页
    stopCarousel();   // 聊天页轮播不可见，停止自动轮播（避免返回首页时位置已乱）
    scrollToBottom();
    // 顶部显示当前模式名
    const modeTag = document.getElementById("chat-mode-tag");
    if (modeTag) modeTag.textContent = MODE_NAMES[CURRENT_MODE] || CURRENT_MODE;
    // 模式可能已切换：清空并重载当前模式历史（story/haruno 数据隔离）
    if (_lastMode !== CURRENT_MODE) {
        _lastMode = CURRENT_MODE;
        _modeGen++;   // 模式代际递增：作废所有飞行中的异步渲染任务（防止串模式显示）
        applyModeBranding();   // 头部头像/角色名/签名刷新为当前包
        initAssets(); // 模式切换：同步该模式资产（story/haruno 设定不同；未登录时静默失败）
        // 未提交的提交窗口作废：旧模式的 flush/hint/批检查器不跨模式触发（防串写历史）
        clearTimeout(S._flushTimer);
        S._flushTimer = null;
        clearTimeout(S._hintTimer);
        S._hintTimer = null;
        try { resetBatchWindow(); } catch (e) {}   // 0.8.1 批状态机作废（chat.js）
        try { _clearQuote(); } catch (e) {}   // F-6.4：引用条不跨包——否则 A 包引用的消息会随下一条发送写进 B 包历史
        messagesEl.innerHTML = "";
        S._hasMore = false;
        await loadHistory();   // 先加载历史（含已保存的开场）
        // 历史为空且当前包有开场脚本：触发开场生成一次并保存为对话内容。
        // 之后进入只走历史加载，不重复开场——与剧情模式行为一致。
        const _p = currentPreset();
        if (_p && _p.has_opening && messagesEl.children.length === 0) {
            await openModeOpening();
        }
    }
    try { if (location.hash !== "#chat") history.pushState({chat: true}, "", "#chat"); } catch (e) {}
    try { activateTab("chat"); } catch (e) {}
}
window.showChat = showChat;   // 同上：pc_nav.js / home-start 按钮 onclick



// haruno 模式开场：服务端幂等（无历史才生成），返回旁白+首条消息
async function openModeOpening() {
    const gen = _modeGen;   // 捕获发起时的模式代际
    try {
        const resp = await fetch("/open-mode", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃开场消息，防止渲染进新模式界面
        if (data.opened && data.messages && data.messages.length) {
            renderMessages(data.messages, "firefly", data);
        }
    } catch (e) {}
}

// 轮播图功能入口：进入当前轮播位置对应的模式
function enterCarouselAction() {
    const m = PRESET_MODES[carouselIndex];
    _switchMode(m && m.id);
    showChat();
}

// PC 桌面模式大卡入口（home-modes）：与轮播图入口同语义，直接进入指定模式
function enterMode(mode) {
    _switchMode(mode);
    showChat();
}
window.enterMode = enterMode;


// 返回键支持（PC 浏览器后退 / Android WebView goBack → popstate → 回首页）
window.addEventListener("popstate", () => {
    if (location.hash !== "#chat") showHome();
});
function toggleNotice() {
    const panel = document.getElementById("notice-panel");
    const open = panel.classList.toggle("show");
    document.getElementById("notice-arrow").textContent = open ? "▴" : "▾";
    // 打开时才联网刷新 + 延迟记已读（见 js/notice.js）。
    // 走 window 全局而不是 import：本文件在 bundle 里先于 notice 出现，
    // 调用期解析既能避免顶层顺序约束，也少了 views ↔ notice 的模块耦合。
    if (open && typeof window.noticeOnOpen === "function") {
        try { window.noticeOnOpen(); } catch (e) {}
    }
}
function closeNotice() {
    document.getElementById("notice-panel").classList.remove("show");
    document.getElementById("notice-arrow").textContent = "▾";
}
window.toggleNotice = toggleNotice;   // 内联 onclick（公告栏）
window.closeNotice = closeNotice;

// 滚动轮播（自动 + 触摸滑动）
const carouselTrack = document.getElementById("carousel-track");
const carouselDots = document.getElementById("carousel-dots");
let carouselIndex = 0;
let carouselTimer = null;
let carouselCount = 0;   // 由 renderModeCards 按 PRESET_MODES 设置（loadModes 后）

function goCarousel(i) {
    if (!carouselCount) return;
    carouselIndex = (i + carouselCount) % carouselCount;
    // 安卓 WebView bug：transform 移动后的 img 合成层光栅化模糊。
    _updateCarouselChar();   // 角色信息条跟随当前卡
    // 改用 opacity 淡入淡出切换（无 transform、无 display 硬切，过渡平滑）。
    [...carouselTrack.children].forEach((img, di) => {
        const active = di === carouselIndex;
        img.style.opacity = active ? "1" : "0";
        img.style.pointerEvents = active ? "auto" : "none";   // 隐藏层不挡点击
        img.style.zIndex = active ? "1" : "0";
    });
    [...carouselDots.children].forEach((d, di) => d.classList.toggle("active", di === carouselIndex));
}
function startCarousel() {
    stopCarousel();
    carouselTimer = setInterval(() => goCarousel(carouselIndex + 1), 10000);
}
function stopCarousel() { if (carouselTimer) { clearInterval(carouselTimer); carouselTimer = null; } }
// 手动滑动/点击后暂停自动轮播：用户主动浏览时不打扰（避免"滑不回来"的错觉）。
// 返回首页时 showHome 会 stopCarousel；再次进入聊天页不会自动轮播。
// 触摸滑动/点击：只绑定轮播图图片区（carouselTrack），其余区域不触发
let touchX = null;
carouselTrack.addEventListener("touchstart", (e) => {
    touchX = e.touches[0].clientX;
    stopCarousel();   // 手动触摸时暂停自动轮播
}, {passive: true});
carouselTrack.addEventListener("touchend", (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 40) goCarousel(carouselIndex + (dx < 0 ? 1 : -1));
    else enterCarouselAction();   // 触摸点击轮播图 → 按功能入口进入
    touchX = null;
    // 手动交互后不恢复自动轮播（用户已接管）
}, {passive: true});

// ── PC 鼠标支持：拖拽滑动 + 滚轮切换 + 点击进入对话 ──
let dragState = null;
carouselTrack.addEventListener("mousedown", (e) => {
    dragState = { startX: e.clientX, curX: e.clientX, moved: false };
    stopCarousel();
    e.preventDefault();
});
window.addEventListener("mousemove", (e) => {
    if (!dragState) return;
    dragState.curX = e.clientX;
    const dx = dragState.curX - dragState.startX;
    if (Math.abs(dx) > 5) dragState.moved = true;
});
window.addEventListener("mouseup", (e) => {
    if (!dragState) return;
    const dx = dragState.curX - dragState.startX;
    const moved = dragState.moved;
    dragState = null;
    if (Math.abs(dx) > 40) {
        goCarousel(carouselIndex + (dx < 0 ? 1 : -1));   // 拖拽切换
    } else if (!moved) {
        enterCarouselAction();   // 点击（未拖动）→ 按功能入口进入
    }
    startCarousel();
});
carouselTrack.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (e.deltaY > 0) goCarousel(carouselIndex + 1);
    else goCarousel(carouselIndex - 1);
}, {passive: false});

// 页面加载默认显示首页（若从对话页刷新则恢复对话页）
document.addEventListener("DOMContentLoaded", async () => {
    await loadModes();   // 先拉预设包清单（渲染轮播/大卡/角色标识），再决定进哪个视图
    if (location.hash === "#chat") showChat();
    else if (location.hash === "#fix") openFixView();
    // 桌面双栏（≥1100px）：默认直接进聊天，首页降级为侧栏「首页」视图
    else if (window.matchMedia && matchMedia("(min-width:1100px)").matches) showChat();
    else showHome();
    initAuth();   // 服务器版：轮播图下登录/用户模块
    uiSelectEnhance(document);   // 自绘下拉全站接管（原生 select 弹窗无法主题化；幂等有守卫）
});


/* ── 来源：js/proactive.js ── */
// 主动性轮询与后台主动消息

// ═══════════════════════════════════════════
// 主动性轮询 — 流萤在合适的时候主动找开拓者说话
// ═══════════════════════════════════════════
// 轮询纪律（避免冲突）：
// - 仅在聊天页可见且空闲时检查（不等待回复、不在打字、距离上次回复 > 2 分钟）
// - 服务端门控保证频率（主动式=轮次+概率；概率式=时间静默+前端概率），不通过则零成本返回空
// - 空闲判定（概率式硬性）：无输入、无提交、无处理中（S.waiting/pending/输入框非空）
const _PROACTIVE_INTERVAL = 10 * 1000;   // 轮询周期 10s
const _PROACTIVE_QUIET = 2 * 60 * 1000;  // 主动式：回复渲染后 2 分钟内不检查
const _PROB_QUIET = 10 * 60 * 1000;      // 概率式：距上次渲染 10 分钟内不检查（与服务端静默阈值一致）

function _chatVisible() {
    return appView && appView.style.display !== "none";
}

// 空闲判定：无输入 / 无请求在飞 / 无思考锁 / 无渲染动画
function _idleOk() {
    if (S.waiting) return false;
    if (S._rendering) return false;         // 主动消息渲染动画中
    if (_inflight > 0) return false;      // 发送请求在飞（等待回复）
    if (inputEl && inputEl.value.trim()) return false;
    return _chatVisible();
}

// 渲染主动消息：先锁定输入（思考 2~5s 模拟"想了想/想起什么"），期间禁止用户输入防竞态
async function _renderProactiveWithThink(data) {
    S._lastRenderTs = Date.now();
    S.waiting = true;
    inputEl.disabled = true; sendBtn.disabled = true;
    const statusEl = document.querySelector("#header .status");
    const defaultStatus = (currentPreset() || {}).tagline || "会找到的，属于我的梦...";   // 随当前角色包签名（原来是硬编码流萤签名）
    if (statusEl) statusEl.textContent = "对方正在输入...";
    const thinkMs = 2000 + Math.floor(Math.random() * 3000);
    await new Promise(r => setTimeout(r, thinkMs));
    if (statusEl) statusEl.textContent = defaultStatus;
    S.waiting = false;
    inputEl.disabled = false; sendBtn.disabled = false;
    renderMessages(data.messages, "firefly", data);
}

async function checkProactive() {
    if (!_idleOk()) return;
    const gen = _modeGen;   // 捕获发起时的模式代际
    try {
        // 轮询探测：不改任何前端状态（大部分概率未中，闪状态是错的）
        const resp = await fetch("/proactive-status", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ session_id: SESSION_ID, mode: CURRENT_MODE }),
        });
        const data = await resp.json();
        if (gen !== _modeGen) return;   // 模式已切换：丢弃旧模式主动消息（已写盘原模式）
        if (data.proactive && data.messages && data.messages.length) {
            // 确定要回复：才锁输入框 + 显示状态 + 思考延迟 + 渲染（全程锁防乱序）
            S._lastRenderTs = Date.now();
            S.waiting = true;
            inputEl.disabled = true; sendBtn.disabled = true;
            const statusEl = document.querySelector("#header .status");
            const defaultStatus = statusEl ? statusEl.textContent : "";
            if (statusEl) statusEl.textContent = "对方正在输入...";
            const thinkMs = 2000 + Math.floor(Math.random() * 3000);
            await new Promise(r => setTimeout(r, thinkMs));
            if (statusEl) statusEl.textContent = defaultStatus;
            S.waiting = false;
            inputEl.disabled = false; sendBtn.disabled = false;
            renderMessages(data.messages, "firefly", data);
            _notifyFirefly(data.messages);   // 后台触发的主动消息 → 状态栏通知（桥判断前台与否）
        }
        // 无消息：前端状态完全不动
    } catch (e) {
        // 网络异常：无状态变更，无需恢复（静默等下一轮）
    }
}
// ═══════════════════════════════════════════
// 服务器版后台主动（KeepAliveService 定时触发）
// ═══════════════════════════════════════════

function _notifyFirefly(messages) {
    // 消息渲染后提醒：FireflyJs 桥仅 App 不在前台时发状态栏通知（前台不打扰，
    // 复刻本地版 _notify_reply_if_background 语义）
    try {
        if (window.FireflyJs && window.FireflyJs.notify && Array.isArray(messages)) {
            const texts = messages.filter(m => m && m.type === "text" && m.content)
                                  .map(m => m.content);
            if (texts.length) window.FireflyJs.notify((charName() || "角色") + " · AI", texts.join("\n").slice(0, 200));
        }
    } catch (e) {}
}

window.__serverProactive = async function () {
    // KeepAliveService 后台触发：hidden 开关判断 + 主动式/概率式门控在服务端，
    // relay 代发由页面 relay 引擎完成（本函数跑在页面，Key 可直达）
    if (!S._hiddenEnabled) return;
    await checkProactive();
};

// 10s 轮询 + 抖动（2026-09-19 压测）：原来是固定 10s 的 setInterval。
// 固定周期会让所有客户端在同一秒对齐（惊群）；加 0-5s 抖动把负载摊平，
// 首次延迟也保持 10s 量级（避免刚进页面就判定"该主动了"）。
(function _proactiveLoop() {
    setTimeout(() => {
        checkProactive();
        _proactiveLoop();
    }, _PROACTIVE_INTERVAL + Math.random() * 5000);
})();


/* ── 来源：js/relay.js ── */
// 服务器版 relay 引擎与资产本地化

// ═══════════════════════════════════════
// 后端代理（relay）— 服务器不持有用户 Key 的完整链路
// ═══════════════════════════════════════
// 服务器流水线的 LLM 请求在服务器入队（资产用 __CORE__ 等占位符表示），
// 本页 1s 轮询取件 → 占位符填充（本地资产）→ 用户 Key 直连 api_base 代发 → 回传。
// 资产（知识库/核心设定/身份/短信样本，~500KB）下载后缓存 localStorage（5MB 上限绰绰有余）。
let _assets = { knowledge: "", core: "", identity: "", sms_samples: "" };
let _relayBusy = false;

function _assetStoreKey(name) { return "firefly_asset_" + name + "_" + CURRENT_MODE; }

async function initAssets() {
    // 资产本地化：清单指纹对比 → 差异下载 → 缓存。登录后/模式切换时调用。
    if (!IS_SERVER) return;   // 本地模式：知识库/设定由本地后端直接注入，无需本地化
    const mode = CURRENT_MODE;
    try {
        const idxResp = await fetch("/assets/index?mode=" + mode);
        const idx = await idxResp.json();
        const local = (() => { try { return JSON.parse(localStorage.getItem("firefly_assets_idx") || "{}"); } catch (e) { return {}; } })();
        const assetGroups = {
            knowledge: idx.knowledge, core: idx.character.core,
            identity: idx.character.identity, sms_samples: idx.character.sms_samples,
        };
        for (const [name, info] of Object.entries(assetGroups || {})) {
            const cacheKey = name + ":" + mode;
            const ver = (info && info.version) || "0";
            if ((local[cacheKey] || "") !== ver && ver !== "0") {
                try {
                    const rawResp = await fetch("/assets/raw?name=" + name + "&mode=" + mode);
                    const raw = await rawResp.json();
                    if (raw.content) {
                        localStorage.setItem(_assetStoreKey(name), raw.content);
                        local[cacheKey] = ver;
                    }
                } catch (e) { /* 单项失败不阻塞其余资产 */ }
            }
        }
        try { localStorage.setItem("firefly_assets_idx", JSON.stringify(local)); } catch (e) {}
    } catch (e) {
        // 同步失败（未登录/网络）：用已有缓存兜底（首次无缓存时占位符以空串填充，模型仍可聊天）
    }
    // 无论同步成败，从缓存装配当前模式资产
    for (const name of ["knowledge", "core", "identity", "sms_samples"]) {
        try { _assets[name] = localStorage.getItem(_assetStoreKey(name)) || ""; } catch (e) { _assets[name] = ""; }
    }
}

function fillPlaceholders(payload) {
    const msgs = payload && payload.messages;
    if (!Array.isArray(msgs)) return;
    for (const m of msgs) {
        if (typeof m.content === "string" && m.content.indexOf("__") >= 0) {
            m.content = m.content
                .replaceAll("__CORE__", _assets.core || "")
                .replaceAll("__IDENTITY__", _assets.identity || "")
                .replaceAll("__SMS_SAMPLES__", _assets.sms_samples || "")
                .replaceAll("__KNOWLEDGE__", _assets.knowledge || "");
        }
    }
}

// A8 能力探测（服务器版）：供应商拒绝 DeepSeek 私有参数（thinking/reasoning_effort）时
// 剥离后重试一次，并按结果缓存 caps 状态（后续 payload 直接剥离，避免每次探错）。
// 缓存按供应商隔离：key = firefly_no_thinking_<base_url>（换供应商后重新探测，互不污染）；
// 旧全局 key firefly_no_thinking 首次读取时迁移到当前供应商键并删除。
function _noThinkingKey(apiBase) {
    return "firefly_no_thinking_" + String(apiBase || "").replace(/\/+$/, "");
}
function _loadNoThinking(apiBase) {
    try {
        const old = localStorage.getItem("firefly_no_thinking");
        if (old !== null) {
            localStorage.setItem(_noThinkingKey(apiBase), old);
            localStorage.removeItem("firefly_no_thinking");
        }
        return localStorage.getItem(_noThinkingKey(apiBase)) === "1";
    } catch (e) { return false; }
}

function _stripThinking(payload) {
    if (!payload || typeof payload !== "object") return payload;
    try {
        const p = JSON.parse(JSON.stringify(payload));
        delete p.thinking;
        delete p.reasoning_effort;
        return p;
    } catch (e) { return payload; }
}

function _looksUnknownParam(text) {
    if (!text) return false;
    const low = String(text).toLowerCase();
    return low.indexOf("unknown") >= 0 || low.indexOf("unexpected parameter") >= 0
        || low.indexOf("not a valid parameter") >= 0 || low.indexOf("not supported") >= 0;
}

async function relayTick() {
    // 1s 轮询取件。用原始 fetch 手动带头：避开包装器的 401 toast（未登录/过期时静默）。
    if (_relayBusy) return;
    const token = (() => { try { return localStorage.getItem("firefly_token") || ""; } catch (e) { return ""; } })();
    if (!token) return;   // 未登录：服务器不会入队
    let pending = null;
    try {
        const resp = await _serverFetch(API_BASE + "/relay/pending", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
            body: "{}",
        });
        if (resp.status !== 200) return;
        pending = await resp.json();
    } catch (e) { return; }
    if (!pending || !pending.pending) return;
    _relayBusy = true;
    try {
        const apiBase = pending.api_base || "https://api.deepseek.com/v1";
        const key = getLocalApiKey();   // 单点读取：providers 优先 + legacy 兜底
        if (!key) throw new Error("no key");
        fillPlaceholders(pending.payload);
        // 中转降级：服务器用本请求 X-API-Key 头代发（Key 内存即弃不落盘），
        // call_id 必须匹配服务器队列中真实 pending 项（非开放代理），
        // 服务器回传时已唤醒流水线（带状态码做错误分类），前端无需再调 /relay/result
        const proxyFallback = async () => {
            const p = await fetch("/relay/proxy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ call_id: pending.call_id, payload: pending.payload }),
            });
            const pd = await p.json();
            if (!pd.ok || !pd.response) throw new Error(pd.error || "proxy failed");
        };
        let ds;
        try {
            // 用户 Key 直连代发（DeepSeek 官方端点支持浏览器 CORS）；已知不支持的供应商先剥离私有参数
            let directPayload = _loadNoThinking(apiBase) ? _stripThinking(pending.payload) : pending.payload;
            ds = await _serverFetch(apiBase + "/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json", "Authorization": "Bearer " + key },
                body: JSON.stringify(directPayload),
            });
            // 能力探测：4xx 未知参数 → 剥离后重试一次（成功后按该供应商缓存，后续直接剥离）
            if (!ds.ok && _loadNoThinking(apiBase) === false) {
                const errText = await ds.clone().text().catch(() => "");
                if (_looksUnknownParam(errText)) {
                    const stripped = _stripThinking(pending.payload);
                    ds = await _serverFetch(apiBase + "/chat/completions", {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + key },
                        body: JSON.stringify(stripped),
                    });
                    if (ds.ok) {
                        try { localStorage.setItem(_noThinkingKey(apiBase), "1"); } catch (e) {}
                    }
                }
            }
        } catch (e1) {
            // 直连失败（如 OpenCode Go 端点不支持 CORS）→ 中转降级
            await proxyFallback();
            _relayBusy = false;
            return;
        }
        let respData = null;
        if (ds.ok) {
            try { respData = await ds.json(); }
            catch (e2) {
                // 200 但响应体异常 → 降级重试
                await proxyFallback();
                _relayBusy = false;
                return;
            }
        } else {
            // API 错误响应（401 Key 无效 / 402 余额不足 / 429 限流…）：
            // 照常回传（带状态码），服务器转成分类错误唤醒流水线 → 前端人话提示
            try { respData = await ds.json(); } catch (e3) { respData = {}; }
        }
        await _serverFetch(API_BASE + "/relay/result", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
            body: JSON.stringify({ call_id: pending.call_id, response: respData, status: ds.status }),
        });
    } catch (e) {
        // 直连与中转都失败：不回传 → 服务器侧 120s 超时，流水线自动降级话术
    }
    _relayBusy = false;
}
// 由 main.js 在全部模块求值后调用（直接顶层 setInterval 会在 api↔relay 循环导入中撞 TDZ）
//
// 2026-09-19 压力测试后的调整：1s → 4s + 抖动。
// 为什么：1s 轮询 = **60 请求/分钟/用户**，是全站最大的流量来源。10× 并发（22 → 220 用户）时：
//   · 网关限流是"每 IP 300 请求/分钟"→ 同一出口 IP（手机 CGNAT / 家庭 WiFi / 办公室）
//     只够 ~4 个用户，第 5 个起全 429（实测：第 301 次请求返回 429）；
//   · 220 用户 × 70 请求/分钟 ≈ 257 请求/秒，同时逼近 3Mbps 上行。
// 降到 4s 后请求量变 1/4，代价只是"回复最多晚几秒到达"—— 而后端合并窗口本来就是 5 秒级的，
// 用户感知不到差别。**抖动**是避免所有客户端在同一秒对齐（惊群）。
function startRelay() {
    const tick = () => {
        relayTick();
        setTimeout(tick, 4000 + Math.random() * 1500);
    };
    setTimeout(tick, 1200 + Math.random() * 1200);   // 首次也错开
}


/* ── 来源：js/guide.js ── */
// 使用引导（基础 + 深度）

// ═══════════════════════════════════════════
// 使用引导（纯代码：高亮框 + 文字气泡 + CSS 呼吸边，不用任何图片）
// 基础引导 4 步；结束后可点「深入了解」进入详细引导（设置/菜单/纠错助手）。
// ═══════════════════════════════════════════
// 基础引导 5 步；结束后可点「深入了解」进入详细引导（设置/菜单/纠错助手）。
// ★ 0.9.0 把 KEY 从 v1 升到 v2：引导内容变了（新增"公告与更新说明""自动修复"两步），
//   不升 key 的话老用户永远看不到新步骤 —— 那等于没更新引导。
const GUIDE_KEY = "firefly_guide_v2_done";
const DEEP_GUIDE_KEY = "firefly_deep_guide_v2_done";
const GUIDE_STEPS = [
    { el: "#home-carousel", title: "从这里进入对话",
      text: "请实际操作：点任意一张角色卡片（如「剧情模式」），进入和角色的聊天页。\n操作成功会自动进入下一步；如果没反应，点「下一步」。",
      setup: () => { showHome(); },
      done: () => !document.getElementById("home-view").classList.contains("show") },
    { el: "#home-settings-btn", title: "先填 API Key",
      text: "请点右上角 ⚙ 打开设置，把 sk- 开头的 Key 粘进 API Key 输入框。\n没有 Key 之前，聊天只会提示你去设置。",
      setup: () => { showHome(); },
      done: () => document.getElementById("settings-panel").classList.contains("show") },
    { el: "#cards-manage-btn", title: "角色卡管理",
      text: "请点「角色卡管理」。每个角色卡的人设、用户设定、知识库、出厂记忆、表情包都在里面单独编辑（聊天产生的记忆与手账在菜单页「设定文件」里，清除历史会一并清空）。",
      setup: () => { closeSettings(); showHome(); },
      done: () => document.getElementById("cards-view").classList.contains("show") },
    { el: "#home-notice", title: "公告与更新说明",
      text: "请点首页上方的「公告 · 使用指南」。\n\n更新说明与临时提醒会自动更新到这里（有新内容时标题旁会亮一个小圆点）；万一拉不到，这里仍显示 App 内置的使用指南，功能不受影响。",
      setup: () => { showHome(); },
      done: () => document.getElementById("notice-panel").classList.contains("show") },
    { el: "#home-feedback-btn", title: "其他问题",
      text: "请点左上角「✉ 反馈」看看。功能建议、安装问题、联系开发者（GitHub / QQ 群 / 邮箱）都在这页。",
      setup: () => { showHome(); },
      done: () => document.getElementById("feedback-panel").classList.contains("show") },
];

function _guideOpenGroup(name) {
    try {
        const head = document.querySelector(`#settings-panel .set-head[data-group="${name}"]`);
        const group = head && head.closest(".set-group");
        if (group && !group.classList.contains("open")) head.click();
    } catch (e) {}
}

function _guideGroupOpen(name) {
    try {
        const head = document.querySelector(`#settings-panel .set-head[data-group="${name}"]`);
        const group = head && head.closest(".set-group");
        return !!(group && group.classList.contains("open"));
    } catch (e) { return false; }
}

function _guideStickerTabOpen() {
    const content = document.getElementById("tab-sticker");
    return !!(content && content.classList.contains("active"));
}

// 详细引导 = 实际操作教程：每一步让用户真的点对应功能，操作成功自动进下一步。
const DEEP_GUIDE_STEPS = [
    { el: "#home-settings-btn", title: "① 实际点开设置",
      text: "请点右上角 ⚙ 打开设置页（不要点“下一步”）。\n\n设置页有 4 组卡片：账号与连接 / 模型与速度 / 外观 / 数据；除 API Key 外，改动会自动保存。",
      setup: () => { showHome(); },
      done: () => document.getElementById("settings-panel").classList.contains("show") },
    { el: "#key-input", title: "② 试填 API Key",
      text: "请点 API Key 输入框，粘贴 sk- 开头的 Key；留空=保留原来的 Key。\n\n填完点「保存 Key 与连接设置」。需要换模型可在「模型与速度」里改 API 供应商。",
      setup: () => { showHome(); openSettings(); },
      done: () => document.activeElement && document.activeElement.id === "key-input" },
    { el: '#settings-panel .set-head[data-group="model"]', title: "③ 点开「模型与速度」",
      text: "请点「🧠 模型与速度」展开。\n\n默认真接 DeepSeek 官方（填 Key 即用）；展开「自定义」可分别调检索/分析/回复/组织四个阶段的模型与思考档位。",
      setup: () => { openSettings(); },
      done: () => _guideGroupOpen("model") },
    { el: '#settings-panel .set-head[data-group="system"]', title: "④ 点开「数据」",
      text: "请点「📦 数据」展开。\n\n版本更新、自动修复、数据快照（保存/恢复/导入 zip）、云端同步都在这里；主动消息则按角色卡配（角色卡管理 → 点角色卡 → 主动消息）。",
      setup: () => { openSettings(); },
      done: () => _guideGroupOpen("system") },
    { el: "#hotupdate-check-btn", title: "⑤ 看一眼「自动修复」",
      text: "请点「检查修复」。\n\n小 bug 修好后服务器会推一个小补丁，App 空闲时自动装上，不必重装；这里能开关、查看当前修复版本，有问题还能「回退修复」退回安装包自带的版本。",
      setup: () => { openSettings(); _guideOpenGroup("system"); _guideArmHotupdate(); },
      done: () => _guideHotTouched },
    { el: "#menu-btn", title: "⑥ 到聊天页打开菜单",
      text: "已经帮你切到聊天页：请点右上角 ☰ 打开菜单。\n\n菜单里是分组页签：与角色相关（收藏 / 表情包 / 设定文件）、与系统相关（请求记录 / 流程日志）。",
      setup: () => { closeSettings(); showChat(); },
      done: () => document.getElementById("menu-drawer").classList.contains("open") },
    { el: '.menu-tab[data-tab="sticker"]', title: "⑦ 点「表情包」页签",
      text: "请在菜单顶部点「表情包」。\n\n这一页能添加新表情、打开映射表逐个启用/停用；停用的表情不会出现在聊天面板，也不会被 AI 使用。",
      setup: () => { openMenu(); },
      done: () => _guideStickerTabOpen() },
    { el: "#sticker-manage-btn", title: "⑧ 展开映射表试开关",
      text: "请点「表情包映射表」。\n\n展开后可以试试点某张表情的「启用中 / 已停用」按钮，状态会立刻切换；改分类和描述后要点该卡片「保存」。内置默认表情的「删」是灰色保护。",
      setup: () => { openMenu(); try { document.querySelector('.menu-tab[data-tab="sticker"]')?.click(); } catch (e) {} },
      done: () => { const p = document.getElementById("sticker-manage-panel"); return !!(p && p.style.display !== "none" && p.style.display !== ""); } },
    { el: "#sticker-add-btn", title: "⑨ 看看添加表情包表单",
      text: "请点「+ 添加表情包」展开表单（不用真的上传）。\n\n流程是：选图 → 选分类（可爱/帅气）→ 写一句含义描述 → 保存。描述越清楚，AI 选图越准。",
      setup: () => { openMenu(); try { document.querySelector('.menu-tab[data-tab="sticker"]')?.click(); } catch (e) {} },
      done: () => { const f = document.getElementById("sticker-add-form"); return !!(f && f.style.display !== "none" && f.style.display !== ""); } },
    { el: "#home-feedback-btn", title: "⑩ 反馈页可随时重看",
      text: "最后请点左上角「✉ 反馈」。\n\n以后想复习：反馈页点「查看详细使用教程」即可重新开始这套实际操作教程；有问题可在 GitHub / QQ 群 / 邮箱反馈。",
      setup: () => { showHome(); },
      done: () => document.getElementById("feedback-panel").classList.contains("show") },
];

// 步骤⑤的判定：用户真的点了「检查修复」（而不是"这个按钮存在"——那不叫操作成功）。
// 监听是一次性的：进入该步时挂上，离开后自然失效（按钮不存在时直接算完成，不卡住流程）。
let _guideHotTouched = false;
function _guideArmHotupdate() {
    _guideHotTouched = false;
    const btn = document.getElementById("hotupdate-check-btn");
    if (!btn) { _guideHotTouched = true; return; }
    btn.addEventListener("click", () => { _guideHotTouched = true; }, { once: true });
}

let _guideIndex = 0;
let _guideSteps = GUIDE_STEPS;
let _guideKey = GUIDE_KEY;
let _guideMask = null, _guideSpot = null, _guideTip = null;
let _guideBlocks = null;
let _guideAdvanceTimer = null;

function _guideEnsureHome() {
    try { if (typeof closeFeedback === "function") closeFeedback(); } catch (e) {}
    if (typeof showHome === "function") showHome();
}

function _guideMarkDone(key) {
    try { localStorage.setItem(key, "1"); } catch (e) {}
}

function _guideOnUserClick(e) {
    if (!_guideMask || _guideIndex >= _guideSteps.length) return;
    // 教程气泡上的按钮（跳过/上一步/下一步）不走自动判定
    if (e.target && e.target.closest && e.target.closest("#guide-tip")) return;
    const idx = _guideIndex;
    const step = _guideSteps[idx];
    if (!step || typeof step.done !== "function") return;
    clearTimeout(_guideAdvanceTimer);
    _guideAdvanceTimer = setTimeout(() => {
        if (!_guideMask || _guideIndex !== idx) return;
        try {
            if (step.done()) {
                if (idx >= _guideSteps.length - 1) _guideClose();
                else _guideTo(idx + 1);
            }
        } catch (err) {}
    }, 350);
}

function _guideClose() {
    clearTimeout(_guideAdvanceTimer);
    document.removeEventListener("click", _guideOnUserClick, true);
    if (_guideMask) _guideMask.remove();
    _guideMask = _guideSpot = _guideTip = null;
    _guideBlocks = null;
    _guideMarkDone(_guideKey);
    try { closeSettings(); closeMenu(); showHome(); } catch (e) {}
}

function _guideCreateMask() {
    if (_guideMask) _guideMask.remove();
    _guideMask = document.createElement("div");
    _guideMask.id = "guide-mask";
    _guideSpot = document.createElement("div");
    _guideSpot.id = "guide-spot";
    _guideTip = document.createElement("div");
    _guideTip.id = "guide-tip";
    _guideBlocks = {};
    ["top", "bottom", "left", "right"].forEach(name => {
        const d = document.createElement("div");
        d.className = "guide-block";
        d.id = "guide-block-" + name;
        _guideBlocks[name] = d;
        _guideMask.appendChild(d);
    });
    _guideMask.appendChild(_guideSpot);
    _guideMask.appendChild(_guideTip);
    document.body.appendChild(_guideMask);
    document.addEventListener("click", _guideOnUserClick, true);
}

function _guideTo(i) {
    _guideIndex = Math.max(0, Math.min(i, _guideSteps.length - 1));
    const step = _guideSteps[_guideIndex];
    if (step.setup) { try { step.setup(); } catch (e) {} }
    const target = document.querySelector(step.el);
    if (!target) { _guideIndex++; if (_guideIndex >= _guideSteps.length) { _guideClose(); return; } _guideTo(_guideIndex); return; }

    const r = target.getBoundingClientRect();
    const pad = 6;
    Object.assign(_guideSpot.style, {
        left: (r.left - pad) + "px", top: (r.top - pad) + "px",
        width: (r.width + pad * 2) + "px", height: (r.height + pad * 2) + "px",
    });
    // 透明拦截片：盖住高亮目标以外的全部区域，其他按钮真的不可点；目标区域保持可点
    if (_guideBlocks) {
        const vw = document.documentElement.clientWidth || window.innerWidth;
        const vh = document.documentElement.clientHeight || window.innerHeight;
        const x0 = Math.max(0, r.left - pad), x1 = Math.min(vw, r.right + pad);
        const y0 = Math.max(0, r.top - pad), y1 = Math.min(vh, r.bottom + pad);
        Object.assign(_guideBlocks.top.style, { left: "0px", top: "0px", width: vw + "px", height: Math.max(0, y0) + "px" });
        Object.assign(_guideBlocks.bottom.style, { left: "0px", top: y1 + "px", width: vw + "px", height: Math.max(0, vh - y1) + "px" });
        Object.assign(_guideBlocks.left.style, { left: "0px", top: y0 + "px", width: Math.max(0, x0) + "px", height: Math.max(0, y1 - y0) + "px" });
        Object.assign(_guideBlocks.right.style, { left: x1 + "px", top: y0 + "px", width: Math.max(0, vw - x1) + "px", height: Math.max(0, y1 - y0) + "px" });
    }
    // 目标在屏幕下半部时，把讲解气泡放到顶部，避免气泡盖住要点击的目标
    const vh = window.innerHeight || document.documentElement.clientHeight || 800;
    _guideTip.classList.toggle("top", (r.top + r.height / 2) > vh * 0.55);
    const isBasic = _guideSteps === GUIDE_STEPS;
    const isLast = _guideIndex === _guideSteps.length - 1;
    _guideTip.innerHTML =
        `<div class="guide-step">${isBasic ? "基础引导" : "实际操作教程"} · ${_guideIndex + 1} / ${_guideSteps.length}</div>` +
        `<div class="guide-title">${escapeHtml(step.title)}</div>` +
        `<div class="guide-text">${escapeHtml(step.text)}</div>` +
        `<div class="guide-actions">` +
        `<button type="button" class="guide-btn skip" id="guide-skip">跳过教程</button>` +
        (_guideIndex > 0 ? `<button type="button" class="guide-btn prev" id="guide-prev">上一步</button>` : "") +
        (isBasic && isLast ? `<button type="button" class="guide-btn deep" id="guide-deep">深入了解</button>` : "") +
        `<button type="button" class="guide-btn next" id="guide-next">${isLast ? "完成" : "没反应？下一步"}</button>` +
        `</div>`;
    document.getElementById("guide-skip").onclick = _guideClose;
    const prev = document.getElementById("guide-prev");
    if (prev) prev.onclick = () => _guideTo(_guideIndex - 1);
    document.getElementById("guide-next").onclick = () => {
        if (isLast) _guideClose();
        else _guideTo(_guideIndex + 1);
    };
    const deep = document.getElementById("guide-deep");
    if (deep) deep.onclick = _startDeepGuide;
    try { target.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
}

function _guideStart(steps, key, showHomeFirst, force) {
    if (!force) { try { if (localStorage.getItem(key)) return; } catch (e) { return; } }
    if (_guideMask) return;
    if (showHomeFirst) _guideEnsureHome();
    _guideSteps = steps;
    _guideKey = key;
    _guideCreateMask();
    setTimeout(() => _guideTo(0), 150);
}

function _startBasicGuide() {
    _guideStart(GUIDE_STEPS, GUIDE_KEY, true, false);
}

function _startDeepGuide() {
    _guideMarkDone(GUIDE_KEY);   // 从「深入了解」进入时，基础引导视为已完成
    _guideClose();               // 移除旧气泡与点击监听（并回到首页）
    _guideStart(DEEP_GUIDE_STEPS, DEEP_GUIDE_KEY, true, true);
}
window.startDeepGuide = _startDeepGuide;

if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", () => setTimeout(_startBasicGuide, 900));
} else {
    setTimeout(_startBasicGuide, 900);
}


/* ── 来源：js/main.js ── */
// 启动编排：合并原文件两套启动路径（DOMContentLoaded 在 views.js）

// ═══════════════════════════════════════════
// 起床检查
// ═══════════════════════════════════════════
async function checkWake() {
    try {
        const resp = await fetch(`/wake-status?mode=${CURRENT_MODE}`);
        const data = await resp.json();
        if (data.interrupted) {
            document.getElementById("wake-overlay").style.display = "flex";
            document.getElementById("wake-text").textContent = (charName() || "角色") + "正在起床，记忆还在整理中…";
        }
    } catch(e) {}
}

// ═══════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════
checkWake();
checkKey().then(() => { loadHistory(); });
initAssets();   // 服务器模式：已有 token 时立即资产本地化（未登录静默失败，登录后 initAuth 会再触发）
loadFixStatus();   // 设定纠错助手：恢复多轮对齐/待确认方案（本地与服务器模式都可用）
// 公告通道**不在这里启动**：由 js/notice.js 自己挂 DOMContentLoaded（原因见该文件末尾的说明
// —— bundle 单作用域，从 main 顶层调进去会撞 TDZ，把 notice 整块静默打死）。
if (IS_SERVER) startRelay();   // relay 引擎仅服务器模式（本地为 direct 直发）


/* ── 来源：js/hotupdate.js ── */
// 热更新前端（见 docs/热更新规范.md 与 热更新/02_实现契约.md §五/§六/§七）
//
// 前端在这条链路上有四件不可省的事：
//   ① 上报「忙/闲」——后端据此决定什么时候允许 reload（规范 §5.2.1：绝不打断用户）
//   ② 首帧渲染完成后上报 boot-ok —— 这是"启动成功判据"，坏补丁靠它才敢确认（§5.5）
//   ③ reload 前把草稿存 sessionStorage —— 一个会吞掉用户正在打的字的"静默修复"比不修还糟（§5.2.2）
//   ④ 把运行版本三元组露给用户，并给一个手动回滚的出口（信任问题，不只是合规）
//
// 写成自包含 IIFE：bundle 是单作用域拼接，不污染其它模块的名字。
(function () {
    "use strict";

    var POLL_MS = 10000;          // 本地请求，代价可忽略；10s 内完成"立即生效"
    var DRAFT_KEY = "firefly_hu_draft";
    var _busy = false;
    var _busyWhy = "";
    var _last = null;

    function $(id) { return document.getElementById(id); }

    function post(path, obj) {
        try {
            return fetch(path, {
                method: "POST", cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(obj || {}),
            });
        } catch (e) { return Promise.resolve(null); }
    }

    // ── ① 忙/闲上报 ────────────────────────────────
    function setBusy(busy, why) {
        if (busy === _busy && why === _busyWhy) return;
        _busy = busy; _busyWhy = why;
        post("/hotupdate/activity", { busy: busy, why: why });
    }

    function computeBusy() {
        // 输入框有内容 = 用户正在打字（后端还会叠加"模型下载中/正在应用"两个自有判据）
        var inp = $("msg-input");
        if (inp && inp.value && inp.value.trim()) return "typing";
        if (typeof _voicePlaying !== "undefined" && _voicePlaying) return "playing";
        return "";
    }

    function watchActivity() {
        var inp = $("msg-input");
        if (inp) {
            ["input", "focus", "compositionstart"].forEach(function (ev) {
                inp.addEventListener(ev, function () { setBusy(!!computeBusy(), computeBusy()); });
            });
            ["blur", "compositionend"].forEach(function (ev) {
                inp.addEventListener(ev, function () {
                    var w = computeBusy();
                    setBusy(!!w, w);
                });
            });
        }
        setInterval(function () {
            var w = computeBusy();
            setBusy(!!w, w);
        }, 3000);
    }

    // ── ③ 草稿保留 ──────────────────────────────────
    function saveDraft() {
        try {
            var inp = $("msg-input");
            var msgs = $("messages");
            sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
                text: inp ? inp.value : "",
                scroll: msgs ? msgs.scrollTop : 0,
                at: Date.now(),
            }));
        } catch (e) { /* 存不下也不能拦着 reload */ }
    }

    function restoreDraft() {
        try {
            var raw = sessionStorage.getItem(DRAFT_KEY);
            if (!raw) return;
            sessionStorage.removeItem(DRAFT_KEY);
            var d = JSON.parse(raw);
            if (Date.now() - (d.at || 0) > 60000) return;   // 太久的草稿不要（可能是上次崩的）
            var inp = $("msg-input");
            if (inp && d.text) {
                inp.value = d.text;
                if (typeof setBusy === "function") setBusy(true, "typing");
            }
            var msgs = $("messages");
            if (msgs && d.scroll) msgs.scrollTop = d.scroll;
        } catch (e) { /* 恢复失败无所谓 */ }
    }

    // ── ④ 状态展示 + 操作 ───────────────────────────
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function render(st) {
        var box = $("hotupdate-box");
        if (!box || !st) return;
        var run = st.running || {};
        var lines = [];
        var ver = esc(run.base_version || "") +
            (run.hot_serial ? ' <b style="color:var(--fg-accent)">hot.' + run.hot_serial + "</b>" : "");
        lines.push('<div>运行版本：' + ver +
            (run.patch_hash ? ' <span style="opacity:.6">' + esc(run.patch_hash) + "</span>" : "") +
            "</div>");
        if (!st.enabled) {
            lines.push('<div style="opacity:.75">热更新已关闭（只保留安全吊销）</div>');
        } else if (st.available) {
            lines.push('<div>可更新：修复 ' + st.available.serial + " · " +
                esc(st.available.note || "") +
                (st.available.layer === "py" || st.available.layer === "web+py"
                    ? ' <span style="opacity:.6">（含程序层，安装后需重启）</span>' : "") +
                "</div>");
        } else if (st.overlay_files) {
            lines.push('<div style="opacity:.75">已应用 ' + st.overlay_files + " 个文件" +
                (st.applied_layer ? "（" + esc(st.applied_layer) + "）" : "") + "</div>");
        } else {
            lines.push('<div style="opacity:.6">没有待安装的修复</div>');
        }
        // ★ v2：含 py 层的补丁必须重启进程才生效 —— 由壳负责重启，前端只如实告知，
        //   并明确"不会吞掉你正在打的内容"（与服务端空闲判据同一条承诺）
        if (st.restart_pending) {
            lines.push('<div style="color:var(--fg-accent)">修复已就绪，空闲时会自动重启以生效</div>');
        }
        if (st.restart_note) {
            lines.push('<div style="opacity:.7">' + esc(st.restart_note) + "</div>");
        }
        if (st.rolled_back_reason) {
            lines.push('<div style="color:#e0a05c">上次修复已回退：' + esc(st.rolled_back_reason) +
                ' <button id="hotupdate-dismiss-btn" type="button" ' +
                'style="font-size:0.9em;padding:0 6px;margin-left:4px">知道了</button></div>');
        }
        if (st.last_error) {
            lines.push('<div style="opacity:.7">' + esc(st.last_error) +
                ' <button id="hotupdate-dismiss-btn2" type="button" ' +
                'style="font-size:0.9em;padding:0 6px;margin-left:4px">知道了</button></div>');
        }
        box.innerHTML = lines.join("");
        // 「知道了」：清掉"上次修复已回退/上次检查失败"的提示。
        // 为什么必要：这两行原本**只写不清**，用户看一次之后再也消不掉，
        // 只能一直怀疑"是不是又坏了"（清的是提示，不是状态）。
        ["hotupdate-dismiss-btn", "hotupdate-dismiss-btn2"].forEach(function (id) {
            var b = $(id);
            if (b) {
                b.addEventListener("click", function () {
                    post("/hotupdate/action", { action: "clear_note" }).then(poll);
                });
            }
        });
        var rb = $("hotupdate-rollback-btn");
        if (rb) rb.style.display = st.applied_serial ? "" : "none";
        var ap = $("hotupdate-apply-btn");
        if (ap) ap.style.display = (st.enabled && st.available) ? "" : "none";
        var tg = $("hotupdate-toggle");
        if (tg) tg.checked = !!st.enabled;
    }

    function poll() {
        fetch("/hotupdate/status", { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (st) {
                _last = st;
                render(st);
                // ★ 生效：后端说可以刷新了，而且此刻确实空闲 → 存草稿后刷新。
                //   `restart_pending` 时不刷新：页面 reload **换不掉已 import 的 Python 模块**，
                //   只会白白闪一次屏（真重启由壳用 AlarmManager 自拉起）。
                if (st.reload_pending && !st.restart_pending && st.idle) {
                    saveDraft();
                    location.reload();
                }
            })
            .catch(function () { /* 后端在重启/不可用：下一轮再试 */ });
    }

    function wire() {
        var tg = $("hotupdate-toggle");
        if (tg) {
            tg.addEventListener("change", function () {
                post("/hotupdate/action", { action: "set_enabled", enabled: tg.checked })
                    .then(poll);
            });
        }
        var ck = $("hotupdate-check-btn");
        if (ck) {
            ck.addEventListener("click", function () {
                var m = $("hotupdate-msg");
                if (m) m.textContent = "检查中…";
                post("/hotupdate/action", { action: "check" }).then(function (r) {
                    return r ? r.json() : null;
                }).then(function (d) {
                    if (m) m.textContent = (d && d.ok) ? "已检查" : ("检查失败：" + ((d && d.error) || "网络不可达"));
                    poll();
                }).catch(function () { if (m) m.textContent = "检查失败"; });
            });
        }
        var ap = $("hotupdate-apply-btn");
        if (ap) {
            ap.addEventListener("click", function () {
                var m = $("hotupdate-msg");
                if (m) m.textContent = "下载并安装中…";
                post("/hotupdate/action", { action: "apply" }).then(function (r) {
                    return r ? r.json() : null;
                }).then(function (d) {
                    if (m) {
                        m.textContent = (d && d.ok)
                            ? (d.restart ? "已安装，空闲时会自动重启以生效" : "已安装，即将生效")
                            : ("安装失败：" + ((d && d.error) || ""));
                    }
                    poll();
                }).catch(function () { if (m) m.textContent = "安装失败"; });
            });
        }
        var rb = $("hotupdate-rollback-btn");
        if (rb) {
            rb.addEventListener("click", function () {
                if (!confirm("回退到当前安装包的版本？")) return;
                post("/hotupdate/action", { action: "rollback" }).then(poll);
            });
        }
    }

    function boot() {
        restoreDraft();
        wire();
        // ★ 启动成功判据：首帧渲染完成（这里就是）→ 告诉后端"补丁是好的"
        post("/hotupdate/boot-ok", {});
        poll();
        setInterval(poll, POLL_MS);
        watchActivity();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();


/* ── 来源：js/notice.js ── */
// 公告 / 更新说明通道（服务端下发 + 本地缓存 + 静态兜底）
// 见 docs/公告通道规范.md。
//
// 三条设计约束（都不是"顺手写的"，是有原因的）：
//  ① **不阻塞**：先渲染 localStorage 里的上次结果，再后台联网刷新。
//     公告是"顺带看一眼"的东西，绝不能让人打开面板时卡在网络上。
//  ② **不拼 HTML**：服务端文本一律 createTextNode/textContent 写入。
//     这样即便签名密钥泄露、服务端被投毒，也**注入不进来** —— 结构上不存在 XSS 面。
//  ③ **不依赖网络**：拿不到就什么都不加，「使用指南」Tab 里的内置指南照常显示。
//
// 2026-09-24 重构（用户报障："公告没做好上下滚动和内容区分，0.9.0 的公告
// 还是塞在一个使用手册里"）：
//   · 拆两个 Tab：「公告」（服务端下发）与「使用指南」（内置常驻）。
//   · 公告按 level 分档筛选（全部 / 更新 / 提醒 / 重要），按日期分组 + sticky 组头。
//   · 三态：有内容 / 空 / 拉取失败——**失败必须说出来**，否则空面板会被当成"功能没做"。
//   · 滚动位置按 Tab 记住（切回来还在原处）。
//   · 面板头徽标**只计未读公告**，不含使用指南（否则点掉也不灭，等于失效）。

const CACHE_KEY = "firefly_notice_cache";     // 最近一次成功的 payload（含已读态镜像）
const SEEN_OPEN_KEY = "firefly_notice_opened"; // 上次打开面板的时间（节流刷新用）
const TAB_KEY = "firefly_notice_tab";         // 上次停留的 Tab（下次打开还回到那儿）
const REFRESH_MS = 30 * 60 * 1000;            // 打开面板时最多 30 分钟联网一次

let _payload = null;
let _readTimer = null;
let _tab = "entries";                         // "entries"（公告）| "guide"（使用指南）
let _filter = "";                             // ""（全部）| info | warn | critical
let _scroll = { entries: 0, guide: 0 };       // 两个 Tab 各自的滚动位置
let _fetchState = "idle";                     // idle | loading | ok | fail

// 没有日期、或日期格式异常时归到这里，永远排最后
const _NO_DATE = "更早";

function _ls(key, val) {
    try {
        if (val === undefined) return localStorage.getItem(key);
        localStorage.setItem(key, val);
    } catch (e) { /* 隐私模式/配额满：静默降级为"不缓存" */ }
    return null;
}

function _readCache() {
    try {
        const raw = _ls(CACHE_KEY);
        const d = raw ? JSON.parse(raw) : null;
        return (d && typeof d === "object" && Array.isArray(d.entries)) ? d : null;
    } catch (e) { return null; }
}

function _writeCache(p) {
    try { _ls(CACHE_KEY, JSON.stringify(p)); } catch (e) {}
}

// ── 渲染（只用 DOM API + textContent；本文件刻意不出现任何标记字符串接口）─────
function _el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.appendChild(document.createTextNode(String(text)));
    return n;
}

const _BLOCK_TAG = { p: "p", h: "h4", li: "li", tip: "p" };

function _renderBlock(b) {
    if (b.t === "img") {
        const fig = _el("div", "notice-fig");
        const img = document.createElement("img");
        // 图片走独立端点：内容在服务端已按清单 sha256 校验过，文件名是白名单形态
        img.src = API_BASE + "/notice-image?name=" + encodeURIComponent(b.name);
        img.alt = b.alt || "";
        img.loading = "lazy";
        fig.appendChild(img);
        if (b.alt) fig.appendChild(_el("div", "notice-fig-cap", b.alt));
        return fig;
    }
    const tag = _BLOCK_TAG[b.t] || "p";
    return _el(tag, "notice-b-" + b.t, b.text);
}

// 哪几条是展开的（按 id 记）。★ 必须记在模块级而不是 DOM 上：
//   `_paintEntries()` 会整块重建 DOM（筛选/刷新/标已读都会触发），
//   状态只存在 DOM 里的话，用户点开一条、后台刷新一次就又折回去了。
const _open = new Set();

function _renderEntry(e) {
    const box = _el("div", "notice-entry notice-lv-" + (e.level || "info"));
    if (_open.has(e.id)) box.classList.add("notice-open");

    // ★ 折叠开关 = 整行标题（2026-09-25 改：列表默认折叠，点开看详情）
    const toggle = _el("button", "notice-ent-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", _open.has(e.id) ? "true" : "false");
    toggle.appendChild(_el("span", "notice-ent-arrow", "▾"));
    if (e.unread) toggle.appendChild(_el("span", "notice-ent-dot"));
    toggle.appendChild(_el("span", "notice-ent-title", e.title));
    if (e.date) toggle.appendChild(_el("span", "notice-ent-date", e.date));
    if (e.unread) toggle.appendChild(_el("span", "notice-ent-new", "新"));
    // 收起时提示"点开有多少内容"，省得逐条试点
    const n = (e.blocks || []).length;
    toggle.appendChild(_el("span", "notice-ent-hint", n ? n + " 段" : ""));
    toggle.addEventListener("click", () => {
        const open = !box.classList.contains("notice-open");
        box.classList.toggle("notice-open", open);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) _open.add(e.id); else _open.delete(e.id);
    });
    box.appendChild(toggle);

    const body = _el("div", "notice-ent-body");
    for (const b of (e.blocks || [])) body.appendChild(_renderBlock(b));
    box.appendChild(body);
    return box;
}

// 日期分组：服务端 date 是 "YYYY-MM-DD"。今天/昨天用人话，其余原样。
function _groupLabel(date) {
    if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return _NO_DATE;
    const d = new Date(date + "T00:00:00");
    if (isNaN(d.getTime())) return _NO_DATE;
    const now = new Date();
    const day = (x) => Math.floor((x.getTime() - x.getTimezoneOffset() * 60000) / 86400000);
    const diff = day(now) - day(d);
    if (diff === 0) return "今天";
    if (diff === 1) return "昨天";
    if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + " 月 " + d.getDate() + " 日";
    return date;
}

function _filtered() {
    const all = (_payload && _payload.entries) || [];
    return _filter ? all.filter(e => (e.level || "info") === _filter) : all;
}

function _paintEmpty(host, icon, title, sub) {
    const box = _el("div", "notice-empty");
    box.appendChild(_el("span", "notice-empty-ico", icon));
    box.appendChild(_el("div", null, title));
    if (sub) box.appendChild(_el("div", "notice-empty-sub", sub));
    host.appendChild(box);
}

// 公告 Tab 的正文
function _paintEntries() {
    const host = document.getElementById("notice-server");
    if (!host) return;
    host.textContent = "";                       // 清空（只走 textContent，全程不碰标记字符串）

    // 缓存过、但联网失败过 → 先说清楚"你看到的可能不是最新的"
    if (_fetchState === "fail") {
        host.appendChild(_el("div", "notice-stale",
            "暂时连不上公告服务，以下为上次同步的内容。"));
    }

    const list = _filtered();
    if (!list.length) {
        const total = ((_payload && _payload.entries) || []).length;
        if (!total) {
            // 真的没有公告。给一句人话，别让空面板看起来像坏了
            if (_fetchState === "loading") {
                _paintEmpty(host, "⏳", "正在获取公告…");
            } else if (_fetchState === "fail") {
                _paintEmpty(host, "📡", "暂时连不上公告服务",
                    "你的网络可能不稳定：切到「使用指南」仍可正常阅读。");
            } else {
                _paintEmpty(host, "📢", "暂无新公告",
                    "更新说明与临时提醒会出现在这里。");
            }
        } else {
            // 有公告，只是这个筛选档下没有
            _paintEmpty(host, "🔍", "这个分类下暂无公告", "换一个筛选条件看看。");
        }
        return;
    }

    // 分组：按日期倒序（服务端已排过，这里只做分组不做重排）。
    // ★ 2026-09-25 去掉"置顶"：公告是**时间流**，谁都不该抢第一位；
    //   之前给 0.9.0 更新说明打了 pinned:true，结果列表头永远挂一个"置顶"组。
    let lastGroup = null;
    for (const e of list) {
        const label = _groupLabel(e.date);
        if (label !== lastGroup) {
            lastGroup = label;
            host.appendChild(_el("div", "notice-group", label));
        }
        host.appendChild(_renderEntry(e));
    }
}

// 徽标与 chips 计数：**只算公告，不算使用指南**
function _paintBadges() {
    const entries = (_payload && _payload.entries) || [];
    const unread = entries.filter(e => e.unread).length;

    const badge = document.getElementById("notice-badge");
    if (badge) {
        badge.hidden = !unread;
        badge.textContent = unread > 9 ? "9+" : String(unread || "");
        badge.title = unread ? `有 ${unread} 条新公告` : "";
    }
    const tab = document.getElementById("notice-tab-entries");
    if (tab) {
        tab.textContent = "公告";
        if (unread) {
            const b = _el("span", "notice-badge", unread > 9 ? "9+" : String(unread));
            tab.appendChild(b);
        }
    }
    // 首页公告栏的小圆点（结构在 index.html，样式 .notice-dot）
    const dot = document.getElementById("notice-dot");
    if (dot) {
        dot.hidden = !unread;
        dot.textContent = unread > 9 ? "9+" : String(unread || "");
        dot.title = unread ? `有 ${unread} 条新公告` : "";
    }
    // chips 上的计数
    const counts = { "": entries.length, info: 0, warn: 0, critical: 0 };
    for (const e of entries) {
        const lv = e.level || "info";
        if (counts[lv] !== undefined) counts[lv]++;
    }
    const chips = document.querySelectorAll("#notice-chips .notice-chip");
    for (const c of chips) {
        const lv = c.getAttribute("data-lv") || "";
        const n = counts[lv] || 0;
        // 计数值重建（避免重复追加）：先清掉上次追加的计数节点
        const old = c.querySelector(".notice-chip-n");
        if (old) old.remove();
        const base = { "": "全部", info: "更新", warn: "提醒", critical: "重要" }[lv] || lv;
        c.textContent = base;
        if (n) c.appendChild(_el("span", "notice-chip-n", " " + n));
        c.hidden = (lv !== "" && n === 0);      // 空档位直接收起，别留一排没用的按钮
    }
}

function _paint(p) {
    if (p) _payload = p;
    _paintEntries();
    _paintBadges();
}

// ── Tab / 筛选 / 滚动位置 ────────────────────────
function noticeTab(name) {
    if (name !== "entries" && name !== "guide") return;
    _scroll[_tab] = _bodyScroll();
    _tab = name;
    _ls(TAB_KEY, name);

    const entries = document.getElementById("notice-pane-entries");
    const guide = document.getElementById("notice-pane-guide");
    const tE = document.getElementById("notice-tab-entries");
    const tG = document.getElementById("notice-tab-guide");
    const chips = document.getElementById("notice-chips");
    if (entries) entries.hidden = name !== "entries";
    if (guide) guide.hidden = name !== "guide";
    if (tE) tE.setAttribute("aria-selected", name === "entries" ? "true" : "false");
    if (tG) tG.setAttribute("aria-selected", name === "guide" ? "true" : "false");
    if (chips) chips.hidden = name !== "entries";

    _setBodyScroll(_scroll[name] || 0);
}

function noticeFilter(lv) {
    _filter = lv || "";
    const chips = document.querySelectorAll("#notice-chips .notice-chip");
    for (const c of chips) {
        c.setAttribute("aria-pressed", (c.getAttribute("data-lv") || "") === _filter ? "true" : "false");
    }
    _paintEntries();
    _scroll[_tab] = 0;                        // 换筛选回到顶部，并把记录一起归零
    _setBodyScroll(0);
}

// 滚动的是 .notice-body（两个 Tab 共用它，所以必须自己记位置）
function _bodyEl() { return document.querySelector("#notice-panel .notice-body"); }
function _bodyScroll() { const b = _bodyEl(); return b ? b.scrollTop : 0; }

// 恢复滚动位置。★ 必须"先取消上一次待执行的 rAF"：
//   切 Tab 是"存旧位置 → 恢复新位置"两步，若用户连点两个 Tab（或代码里连续调两次），
//   两个 rAF 都会在下一帧排队执行，**先入队的那个会后跑**的概率存在 ⇒ 恢复成错的 Tab 的位置。
//   2026-09-24 实测 G1 用例就是这么失败的（连续 noticeTab × 2，最终 scrollTop 被盖回 0）。
let _scrollRaf = 0;
function _setBodyScroll(v) {
    const b = _bodyEl();
    if (!b) return;
    if (_scrollRaf) cancelAnimationFrame(_scrollRaf);
    _scrollRaf = requestAnimationFrame(() => {
        _scrollRaf = 0;
        try { b.scrollTop = v; } catch (e) {}
    });
}

// ── 联网（失败一律静默：公告不配打断用户）────────────
async function refreshNotice(force) {
    try {
        const url = force ? "/notice/action" : "/notice";
        const opts = force
            ? { method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "check" }) }
            : {};
        const r = await fetch(url, opts);
        if (!r.ok) { _fetchState = "fail"; _paintEntries(); return null; }
        const d = await r.json();
        const p = force ? (d.payload || null) : d;
        if (p && p.ok) {
            _payload = p;
            _writeCache(p);
            // 注意顺序：先落状态再 paint，否则空态文案会慢一帧
            _fetchState = "ok";
            _paint(p);
            // ★ 后端是「先回缓存、后台联网刷新」（见 app/notice.py::payload 的说明）：
            //   首次安装 / 缓存过期时，这一次拿到的就是**空的**，而刷新结果要几百毫秒后才有。
            //   不跟一次的话：首页永远不亮小圆点、面板永远只有内置指南，
            //   直到用户下次再打开面板 —— 表现为"公告功能像没做"。
            //   （2026-09-19 真机实测抓到的：后端 serial=1、前端渲染 0 条。）
            if (p.refreshing) { _fetchState = "loading"; _scheduleRetry(); }
            else { _retry = 0; }
            return p;
        }
        _fetchState = "fail";
        _paintEntries();
        return p;
    } catch (e) {
        _fetchState = "fail";                    // 离线/服务端没开：说清楚，别装作没公告
        _paintEntries();
        return null;
    }
}

// 后台刷新还没落地 → 过几秒自己再拉一次（最多 3 次，之后交给下次打开面板）
let _retry = 0;
function _scheduleRetry() {
    if (_retry >= 3) return;
    _retry++;
    setTimeout(() => { refreshNotice(false); }, 4000);
}

// 标记已读（面板打开 1.5 秒后）——延迟是为了让"新"标记真的被眼睛扫到
function _markReadSoon() {
    clearTimeout(_readTimer);
    _readTimer = setTimeout(async () => {
        const p = _payload;
        const ids = ((p && p.entries) || []).filter(e => e.unread).map(e => e.id);
        if (!ids.length) return;
        try {
            await fetch("/notice/action", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "read", ids }),
            });
        } catch (e) { /* 没记上也没关系：下次打开还会提示 */ }
        const cached = _readCache();
        if (cached) {
            cached.entries = (cached.entries || []).map(e => Object.assign({}, e, { unread: false }));
            cached.unread = 0;
            _writeCache(cached);
        }
        if (_payload) {
            _payload.entries = (_payload.entries || []).map(e => Object.assign({}, e, { unread: false }));
            _payload.unread = 0;
        }
        _paint(_payload);
    }, 1500);
}

// 面板打开时调用（views.js 的 toggleNotice 会调）
function noticeOnOpen() {
    // 回到上次停留的 Tab（第一次打开默认"公告"）
    const saved = _ls(TAB_KEY);
    noticeTab(saved === "guide" ? "guide" : "entries");
    _markReadSoon();
    let last = 0;
    try { last = parseInt(_ls(SEEN_OPEN_KEY) || "0", 10) || 0; } catch (e) {}
    if (Date.now() - last < REFRESH_MS) return;
    _ls(SEEN_OPEN_KEY, String(Date.now()));
    refreshNotice(false);
}

// 启动：先画缓存（瞬时），再后台刷新（不 await）
function initNotice() {
    _payload = _readCache();
    if (_payload) {
        _paint(_payload);
    } else {
        _paintBadges();                          // 无缓存也要把 chips/徽标置成干净的初始态
    }
    // 冷启动先让首页把首帧画完，别跟聊天/历史的启动请求抢带宽
    setTimeout(() => { refreshNotice(false); }, 2500);
}

// ★ 自启动，**不要**让 main.js 在顶层调 initNotice()（2026-09-19 真机踩到，见 docs/错误总结.md #15）：
//   bundle 是**单作用域**的拼接产物，`main` 排在 `notice` 前面 —— 从 main 的顶层调进本模块时，
//   本模块的顶层 `let _payload` 还没执行 ⇒ **TDZ ReferenceError** ⇒ main 顶层从这里中断，
//   连本文件末尾的 `window.noticeOnOpen = ...` 都没跑到 ⇒ 公告功能整体静默失效
//   （后端一切正常、界面什么都没有，52 项单测全绿）。
//   挂在 DOMContentLoaded 上还有第二个好处：DOM 一定就绪，首帧就能画。
window.noticeOnOpen = noticeOnOpen;
window.noticeTab = noticeTab;         // 面板内联 onclick
window.noticeFilter = noticeFilter;

function _bootNotice() { try { initNotice(); } catch (e) { /* 公告不配拖垮启动 */ } }
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bootNotice);
} else {
    _bootNotice();
}


/* ── 来源：js/diag.js ── */
// 诊断包导出（本地导出，不经服务器）—— 见 app/api/diag.py 与 docs/未完成事项清单.md P1-3
//
// 用户 2026-09-19 的要求：导出成文件/压缩包，**并且导出后给一个跳转 QQ 的选项**
//（"不然怕用户在 QQ 里找不到文件"）。
//
// 三件事：
//   ① 导出：**安卓壳优先**（FireflyJs.sendDiagnostics：壳自己取包 → 写缓存 →
//      ACTION_SEND 系统分享面板，附件已带好）；没有桥才退回"隐藏链接下载"；
//      （window.open 在部分 WebView 会被拦，项目里已有先例，见 chat.js 导出 zip 的写法）；
//   ② 导出后**就地**给下一步：打开 QQ（用户自己发给开发者）/ 复制群号兜底；
//   ③ 跳 QQ 用系统 scheme `mqqapi://`。安卓壳的 shouldOverrideUrlLoading 会把非内部 URL
//      交给系统 ACTION_VIEW（MainActivity.kt:477），所以这里 location.href 即可；
//      PC 浏览器上没装 QQ 时不会报错，只是没反应 —— 所以**同时**给"复制群号"兜底。

const QQ_GROUP = "1097936258";   // 仅作"复制群号"兜底（用户找不到开发者时用）
// ★ 直接启动 QQ 主界面（不指定群、不指定联系人）：用户自己决定发给谁。
//   2026-09-19 用户明确要求："直接跳转QQ就行，这样用户会自己发我"。
const QQ_SCHEME = "mqq://";

/** 壳的回执出口（Kotlin 里 diagResult() 会调它）——成功/失败都要让用户看见。 */
window.__diagResult = function (text, ok) {
    _msg(text, ok);
    try { showToast(String(text)); } catch (e) {}
};

function _msg(text, ok) {
    const el = document.getElementById("diag-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = ok ? "var(--fg-accent)" : "var(--fg-muted)";
}

function _shell() {
    // 安卓壳注入的桥：有它就能"带附件直接分享"（比让用户自己找文件靠谱得多）
    try {
        return (window.FireflyJs && typeof window.FireflyJs.sendDiagnostics === "function")
            ? window.FireflyJs : null;
    } catch (e) { return null; }
}

/** 导出诊断包：有壳桥就**带附件分享**（QQ 在分享面板里），否则退回下载到本机。 */
function exportDiagnostics() {
    if (IS_SERVER) {
        // 服务器版没有这个端点（后端 403）；界面也不该显示按钮，这里只做兜底提示
        _msg("服务器版不提供诊断包（诊断包只在你自己设备上生成）");
        return;
    }
    const sh = _shell();
    if (sh) {
        let ok = false;
        try {
            // 把页面 origin 传进壳：桥线程不能读 WebView（会抛），所以由这边告诉它地址
            ok = sh.sendDiagnostics(location.origin || "");
        } catch (e) { ok = false; }
        if (ok) {
            _msg("正在生成诊断包 …（稍后会弹出分享面板）");
            return;
        }
        _msg("壳内分享不可用，改为导出到本机（下面还有打开 QQ）");
    }
    try {
        const a = document.createElement("a");
        a.href = API_BASE + "/export-diagnostics";
        a.style.display = "none";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { try { a.remove(); } catch (e) {} }, 0);
    } catch (e) {
        _msg("导出失败：" + (e && e.message ? e.message : e));
        return;
    }
    _msg("已保存到本机下载目录，正在打开 QQ —— 把它发给我即可");
    try { showToast("诊断包已导出，发给我即可"); } catch (e) {}
    openQQGroup();          // 按钮就叫"导出并发送"，PC 上发送=打开 QQ（文件在本机，得自己拖进去）
}

/** 打开 QQ（安卓壳交给系统；PC 没装 QQ 就无反应 → 提示用复制群号兜底） */
function openQQGroup() {
    try {
        // 用隐藏 iframe 触发 scheme：比 location.href 更不容易把当前页顶掉
        const f = document.createElement("iframe");
        f.style.display = "none";
        f.src = QQ_SCHEME;
        document.body.appendChild(f);
        setTimeout(() => { try { f.remove(); } catch (e) {} }, 1500);
    } catch (e) {
        try { window.location.href = QQ_SCHEME; } catch (e2) {}
    }
    _msg("已尝试打开 QQ —— 把刚导出的 zip 发给我即可（没跳转就点「复制群号」）");
}

/** 复制群号（兜底：PC 上没装 QQ、或用户想自己在 QQ 里搜） */
async function copyQQGroup() {
    try {
        await navigator.clipboard.writeText(QQ_GROUP);
        _msg("群号已复制：" + QQ_GROUP, true);
    } catch (e) {
        // 老 WebView / 非安全上下文没有 clipboard API → 退回选中提示
        _msg("复制失败，请手动记下群号：" + QQ_GROUP);
    }
}

window.exportDiagnostics = exportDiagnostics;
window.openQQGroup = openQQGroup;
window.copyQQGroup = copyQQGroup;


/** 一键清理已导出的诊断包。
 *  安卓：壳真删（缓存 + Download 目录 + DownloadManager 记录），返回人话摘要；
 *  PC：文件在浏览器下载目录，应用删不到 —— 如实告知，不假装成功。 */
function clearDiagnostics() {
    const sh = _shell();
    if (sh) {
        let msg = "";
        try {
            msg = String(sh.clearDiagnostics());
        } catch (e) {
            msg = "清理失败：" + (e && e.message ? e.message : e);
        }
        if (!msg) msg = "清理完成";
        _msg(msg, true);
        try { showToast(msg); } catch (e) {}   // 小字容易看不见 → 再浮层提示一次
        return;
    }
    const tip = "PC 上诊断包在浏览器下载目录里（应用删不到），请到那里删除";
    _msg(tip);
    try { showToast(tip); } catch (e) {}
}

window.clearDiagnostics = clearDiagnostics;
