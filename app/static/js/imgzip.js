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

export async function compressImage(file) {
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
