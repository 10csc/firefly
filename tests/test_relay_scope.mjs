// Phase 2 relay 资产作用域回归：直接执行 relay.js 源码（仅剥离 import/export 语法）。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(here, "../app/static/js/relay.js");
const source = readFileSync(sourcePath, "utf8")
    .split("\n")
    .filter((line) => !line.trim().startsWith("import ") || !line.trim().endsWith('";'))
    .map((line) => line.replace(/^export (?=(?:async\s+)?function|const|let|var)/, ""))
    .join("\n");

let pass = 0;
let fail = 0;
function check(name, condition, detail = "") {
    if (condition) {
        pass++;
        console.log("  V", name);
    } else {
        fail++;
        console.log("  X", name, detail);
    }
}

class MemoryStorage {
    constructor(initial = {}) { this.data = new Map(Object.entries(initial)); }
    getItem(key) { return this.data.has(key) ? this.data.get(key) : null; }
    setItem(key, value) { this.data.set(key, String(value)); }
    removeItem(key) { this.data.delete(key); }
}

function response(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

function makeHarness({ localStorage, fetch }) {
    const API_BASE = "https://server.example";
    const IS_SERVER = true;
    const _serverFetch = fetch;
    let CURRENT_MODE = "story";
    const getLocalApiKey = () => "test-key";
    const factory = new Function(
        "API_BASE", "IS_SERVER", "_serverFetch", "CURRENT_MODE", "getLocalApiKey",
        "localStorage", "fetch", "setTimeout",
        `${source}
return {
    initAssets,
    fillPlaceholders,
    _assetStoreKey,
    _assetsCurrent,
    _assetsSnapshot: () => _assets,
    _setMode: (value) => { CURRENT_MODE = value; },
    _setAccount: (value) => { localStorage.setItem("firefly_account", value); },
};`,
    );
    return factory(
        API_BASE, IS_SERVER, _serverFetch, CURRENT_MODE, getLocalApiKey,
        localStorage, fetch, setTimeout,
    );
}

async function main() {
    console.log("=== A. 缓存键包含 server/account/mode ===");
    {
        const storage = new MemoryStorage({
            firefly_token: "token-a",
            firefly_account: "alice@example.com",
        });
        const fetch = async () => response({});
        const api = makeHarness({ localStorage: storage, fetch });
        const storyKey = api._assetStoreKey("core");
        api._setMode("haruno");
        const harunoKey = api._assetStoreKey("core");
        api._setMode("story");
        api._setAccount("bob@example.com");
        const bobKey = api._assetStoreKey("core");
        check("缓存键包含服务器地址", storyKey.includes(encodeURIComponent("https://server.example")), storyKey);
        check("缓存键包含账号", storyKey.includes(encodeURIComponent("alice@example.com")), storyKey);
        check("缓存键包含 mode", storyKey.includes(encodeURIComponent("story")), storyKey);
        check("切包得到不同缓存键", storyKey !== harunoKey);
        check("切账号得到不同缓存键", storyKey !== bobKey);
    }

    console.log("=== B. 捕获 mode，旧请求晚到不覆盖新包快照 ===");
    {
        const storage = new MemoryStorage({
            firefly_token: "token-a",
            firefly_account: "alice@example.com",
        });
        let releaseStory;
        const storyGate = new Promise((resolveGate) => { releaseStory = resolveGate; });
        const fetch = async (url) => {
            const target = String(url);
            if (target.includes("/assets/index?mode=story")) {
                await storyGate;
                return response({
                    knowledge: { version: "1" },
                    character: {
                        core: { version: "1" },
                        identity: { version: "1" },
                        sms_samples: { version: "1" },
                    },
                });
            }
            if (target.includes("/assets/index?mode=haruno")) {
                return response({
                    knowledge: { version: "2" },
                    character: {
                        core: { version: "2" },
                        identity: { version: "2" },
                        sms_samples: { version: "2" },
                    },
                });
            }
            if (target.includes("mode=story")) return response({ content: "STORY-ASSET" });
            if (target.includes("mode=haruno")) return response({ content: "HARUNO-ASSET" });
            throw new Error("unexpected URL: " + target);
        };
        const api = makeHarness({ localStorage: storage, fetch });

        const storyCoreKey = api._assetStoreKey("core");
        const oldLoad = api.initAssets();
        api._setMode("haruno");
        await api.initAssets();
        check("新包快照先就绪", api._assetsSnapshot().core === "HARUNO-ASSET",
            JSON.stringify(api._assetsSnapshot()));

        releaseStory();
        await oldLoad;
        check("旧包晚到仍不覆盖新包内存快照", api._assetsSnapshot().core === "HARUNO-ASSET",
            JSON.stringify(api._assetsSnapshot()));
        check("旧包资产仍可写入旧包独立缓存",
            storage.getItem(storyCoreKey) === "STORY-ASSET");
    }

    console.log("=== C. 未就绪快照拒绝填充占位符 ===");
    {
        const storage = new MemoryStorage({
            firefly_token: "token-a",
            firefly_account: "alice@example.com",
        });
        const api = makeHarness({ localStorage: storage, fetch: async () => response({}) });
        api._setMode("story");
        const payload = { messages: [{ role: "system", content: "__CORE__" }] };
        const filled = api.fillPlaceholders(payload);
        check("没有当前作用域快照时不填充", filled === false);
        check("占位符保持原样等待下一轮", payload.messages[0].content === "__CORE__");
    }

    console.log(`\n=== relay scope: PASS=${pass} FAIL=${fail} ===`);
    process.exitCode = fail === 0 ? 0 : 1;
}

main().catch((error) => {
    fail++;
    console.error("  X 未捕获异常", error && error.stack ? error.stack : error);
    console.log(`\n=== relay scope: PASS=${pass} FAIL=${fail} ===`);
    process.exitCode = 1;
});
