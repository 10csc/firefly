// 流萤聊天 App — 前端逻辑

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const keyPanel = document.getElementById("key-panel");
const keyInput = document.getElementById("key-input");
const keySave = document.getElementById("key-save");
const configBtn = document.getElementById("config-btn");
const configClose = document.getElementById("config-close");
const replyModelSelect = document.getElementById("reply-model-select");
const replyEffortSelect = document.getElementById("reply-effort-select");
const replyTempSlider = document.getElementById("reply-temp-slider");
const tempValue = document.getElementById("temp-value");
const configMsg = document.getElementById("config-msg");
const stateBtn = document.getElementById("state-btn");
const restBtn = document.getElementById("rest-btn");
const drawer = document.getElementById("state-drawer");
const overlay = document.getElementById("drawer-overlay");
const drawerClose = document.getElementById("drawer-close");
const restOverlay = document.getElementById("rest-overlay");
const wakeOverlay = document.getElementById("wake-overlay");
const SESSION_ID = "firefly-" + Date.now();
let waiting = false;

// ── 抽屉开关 ──────────────────────────────────
if (stateBtn) {
    stateBtn.addEventListener("click", () => {
        drawer.classList.add("open");
        overlay.classList.add("show");
    });
}
function closeDrawer() {
    drawer.classList.remove("open");
    overlay.classList.remove("show");
}
if (drawerClose) drawerClose.addEventListener("click", closeDrawer);
if (overlay) overlay.addEventListener("click", closeDrawer);

// ── 配置管理（API Key + 回复器模型 + 思考等级 + 温度）────────
async function loadConfig() {
    try {
        const resp = await fetch("/config");
        const data = await resp.json();
        if (data.reply_model && replyModelSelect) replyModelSelect.value = data.reply_model;
        if (data.reply_effort && replyEffortSelect) replyEffortSelect.value = data.reply_effort;
        if (data.reply_temperature != null && replyTempSlider) {
            replyTempSlider.value = data.reply_temperature;
            if (tempValue) tempValue.textContent = Number(data.reply_temperature).toFixed(1);
        }
        if (configMsg) {
            configMsg.textContent = data.has_key
                ? "当前 Key：" + (data.key_prefix || "已设置")
                : "尚未设置 API Key";
        }
        // 有 key 时提示留空保留，避免用户误以为没设
        if (keyInput) {
            keyInput.placeholder = data.has_key ? "已设置，留空则保留原 Key" : "sk-...";
            keyInput.value = "";
        }
        return data;
    } catch (e) { return {has_key: false}; }
}

// 温度滑块实时显示数值
if (replyTempSlider) {
    replyTempSlider.addEventListener("input", () => {
        if (tempValue) tempValue.textContent = Number(replyTempSlider.value).toFixed(1);
    });
}

async function checkKey() {
    const data = await loadConfig();
    if (!data.has_key) {
        keyPanel.style.display = "flex";
    } else {
        keyPanel.style.display = "none";
    }
}

function openConfig() { keyPanel.style.display = "flex"; }
function closeConfig() { keyPanel.style.display = "none"; }

if (configBtn) configBtn.addEventListener("click", openConfig);
if (configClose) configClose.addEventListener("click", closeConfig);

keySave.addEventListener("click", async () => {
    const k = keyInput.value.trim();
    const model = replyModelSelect ? replyModelSelect.value : "deepseek-v4-flash";
    const effort = replyEffortSelect ? replyEffortSelect.value : "high";
    const temp = replyTempSlider ? parseFloat(replyTempSlider.value) : 0.5;
    // key 为空时保留旧值（只改模型/思考等级/温度）
    const payload = { reply_model: model, reply_effort: effort, reply_temperature: temp };
    if (k) payload.api_key = k;
    if (configMsg) configMsg.textContent = "保存中…";
    try {
        const resp = await fetch("/set-config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.ok) {
            keyPanel.style.display = "none";
            keyInput.value = "";
            if (configMsg) configMsg.textContent = "已保存";
        } else {
            if (configMsg) configMsg.textContent = "保存失败";
        }
    } catch (e) {
        if (configMsg) configMsg.textContent = "网络错误";
    }
});

// 启动时加载历史（在 checkKey 之后，避免与配置面板冲突）
// checkKey + loadHistory 在文件末尾统一调用

// ── 渲染状态（侧边抽屉）───────────────────────
function renderState(state) {
    if (!state) return;
    const list = document.getElementById("state-list");
    const items = [
        { label: "心情", value: formatMood(state.mood) },
        { label: "好感度", value: state.affection?.toFixed(1), max: 100, pct: state.affection },
        { label: "紧张度", value: state.tension?.toFixed(1), max: 100, pct: Math.min(state.tension, 100) },
        { label: "主动性", value: state.initiative?.toFixed(1), max: 100, pct: state.initiative },
    ];
    list.innerHTML = items.map(it => `
        <div class="state-item">
            <div class="state-label">${it.label}</div>
            <div class="state-bar"><div class="state-bar-fill" style="width:${it.pct||0}%"></div></div>
            <div class="state-value">${it.value}</div>
        </div>`).join("");
}
function formatMood(moods) {
    if (!Array.isArray(moods)) return "—";
    return moods.map(m => `${m.label}${m.intensity}`).join("、");
}

// ── 添加消息（文本）───────────────────────────
function addTextMessage(text, who, timeStr, prepend = false, seq = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;

    const time = document.createElement("div");
    time.className = "time";
    time.textContent = timeStr || new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });

    row.appendChild(time);
    row.appendChild(bubble);
    if (prepend) {
        messagesEl.insertBefore(row, messagesEl.firstChild);
    } else {
        messagesEl.appendChild(row);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    return row;
}

// ── 添加表情包 ────────────────────────────────
function addSticker(stickerPath, who, timeStr, prepend = false, seq = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;

    const img = document.createElement("img");
    img.className = "sticker-img";
    img.src = "/assets/" + stickerPath;

    const time = document.createElement("div");
    time.className = "time";
    time.textContent = timeStr || new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });

    row.appendChild(time);
    row.appendChild(img);
    if (prepend) {
        messagesEl.insertBefore(row, messagesEl.firstChild);
    } else {
        messagesEl.appendChild(row);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    return row;
}

// ── 逐条渲染消息数组（支持 type: text|sticker）────
function renderMessages(messages, who, data) {
    if (!messages || messages.length === 0) return;

    let delay = 0;
    messages.forEach((msg, i) => {
        setTimeout(() => {
            // 用服务器返回的 time（HH:MM:SS），截取 HH:MM 显示
            const ts = msg.time ? msg.time.slice(11, 16) : null;
            if (msg.type === "sticker") {
                addSticker(msg.path, who, ts);
            } else {
                addTextMessage(msg.content, who, ts);
            }
        }, delay);
        delay += 400;
    });

    // 气泡切换
    if (data && data.bubble) {
        setTimeout(() => { switchBubble(data.bubble); }, delay + 300);
    }
}

// ── 气泡切换 ────────────────────────────────
const BUBBLE_ASSETS = {
    "bubble_rabbit":  "/assets/bubbles/bubbleStyle2.svg",
    "bubble_trotter": "/assets/bubbles/bubbleStyle3.svg",
    "bubble_culture": "/assets/bubbles/bubbleStyle1.svg",
    "bubble_tavern":  "/assets/bubbles/bubbleStyle4.svg",
    "bubble_cinema":  "/assets/bubbles/bubbleStyle5.svg",
    "bubble_warmth":  "/assets/bubbles/bubbleStyle6/main.png",
};

function switchBubble(bubbleKey) {
    const url = BUBBLE_ASSETS[bubbleKey];
    if (!url) return;

    // 动态注入 style，避免 CSS 静态文件中的路径问题
    const styleId = "bubble-style";
    const old = document.getElementById(styleId);
    if (old) old.remove();

    const s = document.createElement("style");
    s.id = styleId;
    s.textContent = ".msg-row.firefly .bubble {" +
        "border-image: url('" + url + "') 30 30 30 30 fill stretch !important;" +
        "border-width: 16px !important; border-style: solid !important;" +
        "border-color: transparent !important; padding: 8px !important;" +
        "background: none !important; }";
    document.head.appendChild(s);
}

// 加载默认气泡
document.addEventListener("DOMContentLoaded", () => switchBubble("bubble_culture"));

// ── 发送消息 ────────────────────────────────
async function send() {
    const text = inputEl.value.trim();
    if (!text || waiting) return;

    waiting = true;
    inputEl.value = "";
    inputEl.disabled = true;
    sendBtn.disabled = true;

    addTextMessage(text, "user");

    // 打字占位
    const typingRow = addTextMessage("...", "firefly");
    typingRow.classList.add("typing");

    try {
        const resp = await fetch("/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text, session_id: SESSION_ID }),
        });
        const data = await resp.json();

        // 移除占位
        typingRow.remove();

        if (data.need_key) {
            keyPanel.style.display = "flex";
        } else if (data.messages) {
            renderMessages(data.messages, "firefly", data);
            if (data.state) renderState(data.state);
        } else if (data.reply) {
            // 兼容旧格式
            addTextMessage(data.reply, "firefly");
        }
    } catch (e) {
        typingRow.remove();
        addTextMessage("嗯…信号不太好，等会儿再试试？", "firefly");
    }

    waiting = false;
    inputEl.disabled = false;
    sendBtn.disabled = false;
    inputEl.focus();
}

// ── 事件 ─────────────────────────────────────
sendBtn.addEventListener("click", send);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
    }
});

// ── 休息按钮 ────────────────────────────────────
if (restBtn) {
    restBtn.addEventListener("click", async () => {
        if (!confirm("让流萤去休息吗？她会整理这段对话的记忆。")) return;
        restOverlay.style.display = "flex";
        document.getElementById("rest-text").textContent = "流萤正在整理记忆…";
        try {
            const resp = await fetch("/rest", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ session_id: SESSION_ID }),
            });
            const data = await resp.json();
            if (data.ok) {
                document.getElementById("rest-text").textContent =
                    `流萤已休息。新增记忆 ${data.added} 条，解决 ${data.resolved} 条。下次见。`;
            } else {
                document.getElementById("rest-text").textContent = "整理出了点问题：" + (data.error || "未知");
            }
        } catch (e) {
            document.getElementById("rest-text").textContent = "信号不好，等会儿再试。";
        }
    });
}

// ── 起床状态检查 ────────────────────────────────
async function checkWake() {
    try {
        const resp = await fetch("/wake-status");
        const data = await resp.json();
        if (data.interrupted) {
            wakeOverlay.style.display = "flex";
            document.getElementById("wake-text").textContent =
                "流萤正在起床，记忆还在整理中，请稍候片刻再刷新…";
        }
    } catch (e) {}
}
checkWake();

// ── 添加表情包 ────────────────────────────────────
const stickerAddBtn = document.getElementById("sticker-add-btn");
const stickerAddForm = document.getElementById("sticker-add-form");
const stickerFile = document.getElementById("sticker-file");
const stickerCategory = document.getElementById("sticker-category");
const stickerLabel = document.getElementById("sticker-label");
const stickerSubmit = document.getElementById("sticker-submit");
const stickerAddMsg = document.getElementById("sticker-add-msg");

if (stickerAddBtn) {
    stickerAddBtn.addEventListener("click", () => {
        stickerAddForm.style.display = stickerAddForm.style.display === "none" ? "flex" : "none";
    });
}

if (stickerSubmit) {
    stickerSubmit.addEventListener("click", async () => {
        const file = stickerFile.files[0];
        const category = stickerCategory.value;
        const label = stickerLabel.value.trim();
        if (!file) { stickerAddMsg.textContent = "请先选择图片文件"; return; }
        if (!label) { stickerAddMsg.textContent = "请填写含义描述"; return; }

        const formData = new FormData();
        formData.append("file", file);
        formData.append("category", category);
        formData.append("label", label);

        stickerSubmit.disabled = true;
        stickerAddMsg.textContent = "保存中…";
        try {
            const resp = await fetch("/add-sticker", { method: "POST", body: formData });
            const data = await resp.json();
            if (data.ok) {
                stickerAddMsg.textContent = "已添加：" + data.label + "（" + category + "）";
                stickerFile.value = "";
                stickerLabel.value = "";
                // 若管理表打开，刷新列表
                if (stickerManagePanel && stickerManagePanel.style.display !== "none") loadStickerList();
            } else {
                stickerAddMsg.textContent = "失败：" + (data.error || "未知");
            }
        } catch (e) {
            stickerAddMsg.textContent = "网络错误，等会儿再试";
        }
        stickerSubmit.disabled = false;
    });
}

// ── 表情包映射表（查看 / 改 label / 删除）──────────────
const stickerManageBtn = document.getElementById("sticker-manage-btn");
const stickerManagePanel = document.getElementById("sticker-manage-panel");
const stickerListEl = document.getElementById("sticker-list");
const stickerManageMsg = document.getElementById("sticker-manage-msg");

if (stickerManageBtn) {
    stickerManageBtn.addEventListener("click", () => {
        const shown = stickerManagePanel.style.display !== "none";
        stickerManagePanel.style.display = shown ? "none" : "flex";
        if (!shown) loadStickerList();
    });
}

async function loadStickerList() {
    if (stickerManageMsg) stickerManageMsg.textContent = "加载中…";
    try {
        const resp = await fetch("/stickers");
        const data = await resp.json();
        renderStickerList(data.stickers || []);
        if (stickerManageMsg) stickerManageMsg.textContent = `共 ${(data.stickers||[]).length} 个`;
    } catch (e) {
        if (stickerManageMsg) stickerManageMsg.textContent = "加载失败，等会儿再试";
    }
}

function renderStickerList(stickers) {
    if (!stickerListEl) return;
    if (stickers.length === 0) {
        stickerListEl.innerHTML = '<div style="color:#8a8a8a;font-size:0.85em;padding:8px 0">还没有表情包</div>';
        return;
    }
    stickerListEl.innerHTML = stickers.map(s => `
        <div class="sticker-row" data-id="${s.id}">
            <img class="stk-thumb" src="/assets/${s.file}" loading="lazy" onerror="this.style.opacity=0.2">
            <div class="stk-cat">${s.category === "帅气" ? "帅" : "爱"}</div>
            <input class="stk-label-input" type="text" value="${s.label}" maxlength="20" placeholder="含义">
            <button class="stk-save" type="button" disabled>保存</button>
            <button class="stk-del" type="button" ${s.is_default ? "disabled title=\"默认表情包不可删\"" : ""}>删</button>
        </div>`).join("");

    // 绑定每行交互
    stickerListEl.querySelectorAll(".sticker-row").forEach(row => {
        const id = row.dataset.id;
        const input = row.querySelector(".stk-label-input");
        const saveBtn = row.querySelector(".stk-save");
        const delBtn = row.querySelector(".stk-del");
        const original = input.value;

        // 输入有变化才启用保存按钮
        input.addEventListener("input", () => {
            saveBtn.disabled = input.value.trim() === original.trim() || !input.value.trim();
        });

        saveBtn.addEventListener("click", async () => {
            const label = input.value.trim();
            if (!label) return;
            saveBtn.disabled = true;
            saveBtn.textContent = "…";
            try {
                const resp = await fetch("/sticker-update", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({id, label}),
                });
                const data = await resp.json();
                if (data.ok) {
                    input.value = data.label;
                    saveBtn.textContent = "已存";
                    if (stickerManageMsg) stickerManageMsg.textContent = `已更新：${data.label}`;
                    setTimeout(() => { saveBtn.textContent = "保存"; saveBtn.disabled = true; }, 1200);
                } else {
                    saveBtn.textContent = "保存";
                    saveBtn.disabled = false;
                    if (stickerManageMsg) stickerManageMsg.textContent = "失败：" + (data.error || "未知");
                }
            } catch (e) {
                saveBtn.textContent = "保存";
                saveBtn.disabled = false;
                if (stickerManageMsg) stickerManageMsg.textContent = "网络错误，等会儿再试";
            }
        });

        delBtn.addEventListener("click", async () => {
            if (!confirm(`确认删除这个表情包？\n（图片文件会保留，只是从映射表移除）`)) return;
            delBtn.disabled = true;
            delBtn.textContent = "…";
            try {
                const resp = await fetch("/sticker-delete", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({id}),
                });
                const data = await resp.json();
                if (data.ok) {
                    row.remove();
                    if (stickerManageMsg) stickerManageMsg.textContent = "已删除";
                } else {
                    delBtn.disabled = false;
                    delBtn.textContent = "删";
                    if (stickerManageMsg) stickerManageMsg.textContent = "失败：" + (data.error || "未知");
                }
            } catch (e) {
                delBtn.disabled = false;
                delBtn.textContent = "删";
                if (stickerManageMsg) stickerManageMsg.textContent = "网络错误，等会儿再试";
            }
        });
    });
}

// ── 历史加载 + 分层滚动 ────────────────────────────
let _hasMoreHistory = false;
let _loadingHistory = false;

function renderHistoryMessage(m, prepend = false) {
    const ts = m.time ? m.time.slice(11, 16) : null;
    if (m.type === "sticker") {
        addSticker(m.path, m.who, ts, prepend, m.seq);
    } else {
        addTextMessage(m.content, m.who, ts, prepend, m.seq);
    }
}

async function loadHistory(beforeSeq = null) {
    if (_loadingHistory) return;
    _loadingHistory = true;
    const url = beforeSeq
        ? `/history?limit=150&before_seq=${beforeSeq}`
        : `/history?limit=150`;
    try {
        const resp = await fetch(url);
        const data = await resp.json();
        if (!data.messages || data.messages.length === 0) {
            _hasMoreHistory = false;
            return;
        }
        if (beforeSeq === null) {
            // 首次加载：append 到末尾
            data.messages.forEach(m => renderHistoryMessage(m, false));
            messagesEl.scrollTop = messagesEl.scrollHeight;
        } else {
            // 向上翻页：prepend 到顶部，保持滚动位置
            const prevHeight = messagesEl.scrollHeight;
            const prevScroll = messagesEl.scrollTop;
            // 反序插入，使顶部到下保持时间正序
            data.messages.slice().reverse().forEach(m => renderHistoryMessage(m, true));
            // 滚动条停留在原来位置（新内容在上方插入）
            messagesEl.scrollTop = prevScroll + (messagesEl.scrollHeight - prevHeight);
        }
        _hasMoreHistory = !!data.has_more;
    } catch (e) {
        console.error("加载历史失败", e);
    } finally {
        _loadingHistory = false;
    }
}

// 向上滚动到顶 → 加载更早历史
messagesEl.addEventListener("scroll", () => {
    if (messagesEl.scrollTop === 0 && _hasMoreHistory && !_loadingHistory) {
        const firstRow = messagesEl.firstChild;
        const firstSeq = firstRow ? parseInt(firstRow.dataset.seq) : null;
        if (firstSeq) loadHistory(firstSeq);
    }
});

// 启动时加载历史（在 checkKey 之后，避免与配置面板冲突）
checkKey().then(() => loadHistory());
