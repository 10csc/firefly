// 敏感头加密 · 真跑产物（Node + 极简浏览器桩）—— 见 app/static/js/session_crypto.js
//
// 与 tests/test_frontend_boot.py 同一思路：**把真源码真跑一遍**，而不是断言"文件里有某行"。
// 这里验的是那条最容易"两边都改错还互相自洽"的跨语言契约：
//   JS 加密出来的信封 → Python 必须能解开（并且明文逐字节相同）。
//
// 两条路径都要跑：
//   ① WebCrypto 路径（Android WebView 的 http(s) 页面走这条）
//   ② 纯 JS 兜底路径（file:// 页面没有 crypto.subtle —— 安卓服务器模式就是这个形态）
//
// 用法：node tests/js/test_session_crypto.mjs <out.json>
//   输出 {"cases":[{"name":…,"env":…}], "pub":{"n_b64":…}}，由 Python 侧解开并断言。
import { readFileSync, writeFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ROOT = process.env.FIREFLY_ROOT || process.cwd();
const SRC = `${ROOT}/app/static/js/session_crypto.js`;
const OUT = process.argv[2];

// ── 浏览器桩 ──────────────────────────────────────
globalThis.btoa = (s) => Buffer.from(s, "binary").toString("base64");
globalThis.atob = (s) => Buffer.from(s, "base64").toString("binary");
globalThis.TextEncoder = TextEncoder;
// Node 24 的 globalThis.crypto 是只读 getter（直接赋值会 TypeError）⇒ 用 defineProperty 影子替换，
// 这样才能模拟"没有 crypto.subtle 的 file:// WebView"那条兜底路径。
const _nodeCrypto = globalThis.crypto;
function setCrypto(obj) {
    Object.defineProperty(globalThis, "crypto", {
        value: obj, configurable: true, writable: true,
    });
}
setCrypto(webcrypto);

const mod = await import("file://" + SRC);
if (mod.SESSION_PUBKEY.n_b64 === "__SESSION_N_B64__" || !mod.SESSION_PUBKEY.n_b64) {
    console.error("X 公钥常量还是占位符 —— 先跑 tools/build_session_keys.py --gen");
    process.exit(3);
}

const cases = [];
function record(name, env) { cases.push({ name, env }); }

// ① WebCrypto 路径
record("webcrypto-中文与空格", (await mod.encHead([
    ["Authorization", "Bearer 0f8a1b2c3d4e5f60718293a4b5c6d7e8"],
    ["X-API-Key", "sk-abcdefghijklmnopqrstuvwxyz0123456789"],
    ["X-API-Base", "https://api.deepseek.com/v1"],
])).enc);
record("webcrypto-短值", (await mod.encHead([["Authorization", "Bearer x"]])).enc);
// 4096 字符的长值：确认流式 keystream 分块正确（>1 个 HMAC 块）
record("webcrypto-长值(4096)", (await mod.encHead([
    ["X-API-Key", "K".repeat(4096)]])).enc);

// ② 纯 JS 兜底路径（把 crypto.subtle 摘掉，模拟 file:// 页面）
setCrypto({ getRandomValues: (a) => _nodeCrypto.getRandomValues(a) });   // 只有随机源
record("no-subtle-纯JS兜底", (await mod.encHead([
    ["Authorization", "Bearer 0123456789abcdef0123456789abcdef"],
    ["X-API-Key", "sk-plain-js-path"],
])).enc);
setCrypto(webcrypto);

// ③ 不该加密的头必须留在明文里（否则普通请求会被拖进 RSA 运算）
const r = await mod.encHead([["Content-Type", "application/json"], ["X-API-Mode", "proxy"]]);
if (r.enc !== "" || r.rest["Content-Type"] !== "application/json" || r.rest["X-API-Mode"] !== "proxy") {
    console.error("X 非敏感头被误加密/丢失:", JSON.stringify(r));
    process.exit(4);
}

// ④ 信封格式自检：enc.v1.<b64u key>.<b64u iv>.<b64u ct>
// ⚠️ 用 split(".", 4)：前缀里自带一个点（"enc.v1"），不加极限会在前缀上先切坏
//    （Python 侧 decrypt_envelope 用 split(".", 4)，两边必须一致）
for (const c of cases) {
    const p = String(c.env || "").split(".", 4);
    if (p.length !== 4 || p[0] !== "enc" || p[1] !== "v1") {
        console.error("X 信封格式不对:", c.name, String(c.env).slice(0, 40));
        process.exit(5);
    }
}

writeFileSync(OUT, JSON.stringify({ cases, pub: mod.SESSION_PUBKEY }, null, 1), "utf8");
console.log(`OK 生成 ${cases.length} 条信封 → ${OUT}`);
