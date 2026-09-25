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
export const SESSION_PUBKEY = {
    n_b64: "9KNHioGtk7fZxARe7cAaIUpdz/mDq33ZYbpGw0OusocCLL3EDmDVi2viOTOBmBVy3pDQSP1Jw4ZnKaj8Tav+fsQGsa+h8p1m88qrQf4mIEZTaJv7l7Q9+gh4P4unl4FioI4hF6MZitELRYKoJuuZMZ8WO81kVgembPFRRFyaqcTRzvrutCdBExqNoq0mn5YFSdMfB4jdBmI0T2DzbCKkmP0PtrnSbQredP5OBSZj6A6PJj6kaoPnBMvZwKuSYtbJp5W1oifuB5OqG+b45KbkWVZj5CzPx5c7HdxmSMJ1PLfIREtBz58S+zIkKM6nLrUZWt2CwMB64aGr1ozU0DpSZQ==",
    e: 65537,
};
export const ENC_HEADER = "X-Firefly-Enc";
// 只有这几个头值得加密（值里有凭证或用户自填地址）；其余头保持明文，
// 免得把每个请求都拖进 RSA 运算（一次 BigInt 模幂 ~1ms，够用但没必要人人有份）
export const SECRET_HEADERS = ["Authorization", "X-API-Key", "X-API-Base"];

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
export async function encHead(headerPairs) {
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
