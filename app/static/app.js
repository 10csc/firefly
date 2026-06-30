// 流萤聊天 App — 前端逻辑

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const keyPanel = document.getElementById("key-panel");
const keyInput = document.getElementById("key-input");
const keySave = document.getElementById("key-save");
const SESSION_ID = "firefly-" + Date.now();
let waiting = false;

// ── Key 管理 ─────────────────────────────────
async function checkKey() {
    const resp = await fetch("/check-key");
    const data = await resp.json();
    if (!data.has_key) keyPanel.style.display = "flex";
    else keyPanel.style.display = "none";
}

keySave.addEventListener("click", async () => {
    const k = keyInput.value.trim();
    if (!k) return;
    await fetch("/set-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: k }),
    });
    keyPanel.style.display = "none";
    keyInput.value = "";
});

// 启动时检查
checkKey();

// ── 添加消息 ────────────────────────────────
function addMessage(text, who, timeStr) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;

    const time = document.createElement("div");
    time.className = "time";
    time.textContent = timeStr || new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });

    row.appendChild(time);
    row.appendChild(bubble);
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return row;
}

// ── 发送消息 ────────────────────────────────
async function send() {
    const text = inputEl.value.trim();
    if (!text || waiting) return;

    waiting = true;
    inputEl.value = "";
    inputEl.disabled = true;
    sendBtn.disabled = true;

    addMessage(text, "user");

    // 打字占位
    const typingRow = addMessage("...", "firefly");
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
        } else if (data.reply) {
            addMessage(data.reply, "firefly");
        }
    } catch (e) {
        typingRow.remove();
        addMessage("嗯…信号不太好，等会儿再试试？", "firefly");
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
