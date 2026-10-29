// 敏感头跨语言契约：真实运行 app/static/js/session_crypto.js，
// 生成信封后交给 tools/check_hotupdate.py 用 Python 生产解密器回读。
import { webcrypto } from "node:crypto";
import { writeFileSync } from "node:fs";

const ROOT = process.env.FIREFLY_ROOT || process.cwd();
const SRC = `${ROOT}/app/static/js/session_crypto.js`;
const OUT = process.argv[2];

globalThis.btoa = (s) => Buffer.from(s, "binary").toString("base64");
globalThis.atob = (s) => Buffer.from(s, "base64").toString("binary");

const _nodeCrypto = globalThis.crypto;
function setCrypto(obj) {
    Object.defineProperty(globalThis, "crypto", {
        value: obj, configurable: true, writable: true,
    });
}
setCrypto(webcrypto);

const mod = await import("file://" + SRC);
if (mod.SESSION_PUBKEY.n_b64 === "__SESSION_N_B64__" || !mod.SESSION_PUBKEY.n_b64) {
    console.error("X 公钥常量还是占位符");
    process.exit(3);
}

const cases = [];
function record(name, env) {
    cases.push({ name, env });
}

record("webcrypto-多敏感头", (await mod.encHead([
    ["Authorization", "Bearer 0f8a1b2c3d4e5f60718293a4b5c6d7e8"],
    ["X-API-Key", "sk-FAKEefghijklmnopqrstuvwxyz0123456789"],
    ["X-API-Base", "https://api.deepseek.com/v1"],
])).enc);
record("webcrypto-短值", (await mod.encHead([["Authorization", "Bearer x"]])).enc);
record("webcrypto-长值", (await mod.encHead([
    ["X-API-Key", "K".repeat(4096)]])).enc);

setCrypto({ getRandomValues: (a) => _nodeCrypto.getRandomValues(a) });
record("no-subtle-纯JS兜底", (await mod.encHead([
    ["Authorization", "Bearer 0123456789abcdef0123456789abcdef"],
    ["X-API-Key", "sk-FAKEn-js-path"],
])).enc);
setCrypto(webcrypto);

const plain = await mod.encHead([
    ["Content-Type", "application/json"],
    ["X-API-Mode", "proxy"],
]);
if (plain.enc !== "" || plain.rest["Content-Type"] !== "application/json"
        || plain.rest["X-API-Mode"] !== "proxy") {
    console.error("X 非敏感头被误加密或丢失");
    process.exit(4);
}

for (const c of cases) {
    const p = String(c.env || "").split(".", 4);
    if (p.length !== 4 || p[0] !== "enc" || p[1] !== "v1") {
        console.error("X 信封格式不对:", c.name);
        process.exit(5);
    }
}

writeFileSync(OUT, JSON.stringify({ cases, pub: mod.SESSION_PUBKEY }, null, 1), "utf8");
console.log(`OK 生成 ${cases.length} 条信封`);
