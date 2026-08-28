// 聊天渲染：消息行（文本/表情包/图片/旁白）/ 引用小卡片 / 时间分割 / 打字机占位 / 逐条渲染动画
import { S, messagesEl } from "./state.js";
import { stickerSrc } from "./util.js";
import { API_BASE, IS_SERVER } from "./api.js";
import { TB_AVATARS, openAvatarPicker, tbChoice } from "./panels.js";
import { CURRENT_MODE, _modeGen } from "./views.js";

// 消息渲染
// ═══════════════════════════════════════════
// ═══════════════════════════════════════════
// 滚动到底部（rAF 延迟：等 DOM 更新/键盘 resize 后再滚，QQ/微信式自动拉底）
// ═══════════════════════════════════════════
export function scrollToBottom() {
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

function _addAvatar(row, who) {    const img = document.createElement("img");
    img.className = "msg-avatar";
    if (who === "user") {
        img.src = TB_AVATARS[tbChoice];
        img.classList.add("tb-toggle");
        img.title = "点击切换开拓者";
        img.addEventListener("click", openAvatarPicker);
        img.classList.add("tb-avatar");
    } else {
        img.src = "流萤_头像.png";
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

export function addTextMessage(text, who, prepend = false, seq = null, quote = null) {
    const row = document.createElement("div");
    row.className = "msg-row " + (who === "user" ? "user" : "firefly");
    if (seq !== null) row.dataset.seq = seq;
    if (!prepend) row.classList.add("float-in");   // 新消息从下方浮现（历史加载不带动画）
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    if (quote) {
        // 带引用的消息：气泡上方加引用小卡片（QQ 式）
        const col = document.createElement("div");
        col.className = "msg-col";
        col.appendChild(_buildQuotePreview(quote));
        col.appendChild(bubble);
        row.appendChild(col);
    } else {
        row.appendChild(bubble);
    }
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    return row;
}

/** 引用快照 → 气泡上方的小卡片（谁 + 内容摘要） */
export function _quoteContentText(q) {
    if (!q) return "";
    if (q.type === "sticker") return "[表情包：" + (q.label || "") + "]";
    if (q.type === "image") return "[图片：" + (q.desc || "（无描述）") + "]";
    if (q.type === "narration") return q.text || "";
    return q.content || "";
}
export function _quoteWhoName(q) { return q && q.who === "user" ? "我" : "流萤"; }
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

export async function addSticker(stickerPath, who, prepend = false, seq = null, label = null, quote = null) {
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
            row.replaceChild(span, img);
        };
    } else {
        const span = document.createElement("span");
        span.className = "sticker-fallback";
        span.textContent = "（表情包已失效）";
        img = span;
    }
    if (quote) {
        const col = document.createElement("div");
        col.className = "msg-col";
        col.appendChild(_buildQuotePreview(quote));
        col.appendChild(img);
        row.appendChild(col);
    } else {
        row.appendChild(img);
    }
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { _insertRow(row, seq); scrollToBottom(); }
    return row;
}

/** A9 图片消息渲染：两端统一走后端 /image?id=&mode= 取字节（服务器版也上传落盘，IndexedDB 图片存储已退役）。
 *  服务器版 <img> 标签带不了 Bearer：经鉴权 fetch 拿字节转 objectURL；本地版同源直接 <img src>。
 *  缺失/失败 → 显示 desc 文字占位。 */
export async function addImage(msg, who, prepend = false, seq = null, quote = null) {
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
            row.replaceChild(span, img);
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
    if (quote) {
        const col = document.createElement("div");
        col.className = "msg-col";
        col.appendChild(_buildQuotePreview(quote));
        col.appendChild(content);
        row.appendChild(col);
    } else {
        row.appendChild(content);
    }
    _addAvatar(row, who);
    if (prepend) { messagesEl.insertBefore(row, messagesEl.firstChild); }
    else { messagesEl.appendChild(row); scrollToBottom(); }
    return row;
}

export function addNarration(text, style, prepend = false, seq = null) {
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

export function addTimeDivider(timeStr) {
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

export function renderMessages(messages, who, data) {
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
