// 聊天媒体：发图片（选图/压缩/上传/描述）/ 表情包面板与发送
import { S, inputEl, messagesEl } from "./state.js";
import { _toast, escapeHtml } from "./util.js";
import { compressImage } from "./imgzip.js";
import { API_BASE, IS_SERVER } from "./api.js";
import { addImage, addSticker } from "./chat_render.js";
import { _batchArmSubmit, _batchRefreshTimer, _chatSend, _clearQuote, _mediaBusy, _quoteTarget } from "./chat.js";
import { CURRENT_MODE } from "./views.js";

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
        let imgId = "", desc = "";
        try {
            const fd = new FormData();
            fd.append("file", blob, "upload." + ext);   // 压缩产物是匿名 Blob：补文件名让后端拿到正确扩展名
            fd.append("mode", CURRENT_MODE);
            const resp = await fetch("/upload-image", {method: "POST", body: fd});
            const data = await resp.json();
            if (!data.ok) { _toast("图片上传失败：" + (data.error || "")); endMedia(false); return; }   // 配额满等错误直接透传后端文案
            imgId = data.img_id || "";
            desc = data.desc || "";
            if (data.need_desc) desc = "";
        } catch (e) { _toast("网络错误，图片未发送"); endMedia(false); return; }
        if (!desc) {
            // 描述缺失（vision 不支持/失败）：让用户填一句（可为空 → [图片] 占位）
            desc = (window.prompt("流萤还没有识图能力，这幅图是什么？（可留空）", "") || "").trim().slice(0, 300);
        }
        const q = _quoteTarget;
        addImage({img_id: imgId, desc}, "user");
        const msg = {type: "image", img_id: imgId};
        if (desc) msg.desc = desc;
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
    if (!stickerGrid.dataset.loaded) {
        try {
            const resp = await fetch("/stickers?enabled=1");
            const data = await resp.json();
            const list = data.stickers || [];
            stickerGrid.innerHTML = list.map(s =>
                `<img src="${IS_SERVER ? API_BASE : ""}/assets/${escapeHtml(s.file)}" alt="${escapeHtml(s.label)}" data-label="${escapeHtml(s.label)}" data-file="${escapeHtml(s.file)}">`).join("");
            stickerGrid.dataset.loaded = "1";
            stickerGrid.querySelectorAll("img").forEach(img => {
                img.addEventListener("click", () => {
                    _mediaBusy = false;   // 选中即结束媒体状态（sendStickerMessage 内 _batchArmSubmit 重新计时）
                    _stickerPanelClose();
                    sendStickerMessage(img.dataset.label, img.dataset.file);
                });
            });
        } catch (e) { /* 静默 */ }
    }
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
