# -*- coding: utf-8 -*-
"""广场 V1 对抗验收的**共用夹具**（verifier 独占）—— 图片构造 + 断言计数 + 沙箱环境。

为什么单独一个模块：`test_plaza_acceptance_v2.py` 到 L1/L2 拆开后共用这份夹具，
避免"同一段伪造图片的代码抄两份"（抄两份 = 迟早分叉，docs/错误总结.md #10 同族）。
本模块**不含任何断言用例**，只是工具；用例在：
  · `tests/test_plaza_acceptance_v2.py`      —— L1 格式层（进程内，快）
  · `tests/test_plaza_acceptance_l2_http.py` —— L2 HTTP 层（真实服务器进程）

设计口径（防"量具自己骗人"，错误总结 #23/#29）：
  · 所有图片都由**本模块现场生成**（PNG 手搓 zlib；JPEG 手写最小编码器），
    不依赖 Pillow —— 且能构造"头部声明与真实像素不一致"的样本；
  · 需要"和实现一致"的常量一律**从实现读**（如 STICKER_SLOTS），不抄字面量（错误总结 #12）。
"""
import base64
import io
import json
import os
import socket as _sk
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRICT = (os.environ.get("FIREFLY_ACCEPT_STRICT") or "").strip() == "1"

# ⚠ 环境**必须先摆好再 import 实现**：app/core/paths.py 在 import 期就按 FIREFLY_* 定数据根
SANDBOX = Path(tempfile.mkdtemp(prefix="ff_accept_v2_"))
USER_DIR = SANDBOX / "user_data"
PLAZA_DIR = SANDBOX / "plaza"
USER_DIR.mkdir(parents=True, exist_ok=True)
PLAZA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "FIREFLY_ANDROID": "1",
    "FIREFLY_DATA_DIR": str(SANDBOX),
    "FIREFLY_PLAZA_DIR": str(PLAZA_DIR),
    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
})
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "server"))

PASS = FAIL = PENDING = 0
RESULTS = []            # {layer, id, name, status, note}


def check(cid, name, cond, note="", pending=False, layer="L1"):
    """记录一条断言。pending=True ⇒ 只计 PENDING（"实现尚未具备，不是通过"）。"""
    global PASS, FAIL, PENDING
    if pending:
        PENDING += 1
        print(f"  PEND {cid:<6}{name}" + (f"  [{note}]" if note else ""), flush=True)
        RESULTS.append({"layer": layer, "id": cid, "name": name, "status": "PENDING", "note": note})
        return False
    if cond:
        PASS += 1
        print(f"  ok   {cid:<6}{name}" + (f"  [{note}]" if note else ""), flush=True)
        RESULTS.append({"layer": layer, "id": cid, "name": name, "status": "PASS", "note": note})
    else:
        FAIL += 1
        print(f"  FAIL {cid:<6}{name}  {note}", flush=True)
        RESULTS.append({"layer": layer, "id": cid, "name": name, "status": "FAIL", "note": note})
    return bool(cond)


def human(n):
    return f"{n/1024:.1f}KB" if n >= 1024 else f"{n}B"


def free_port():
    """端口一律 `bind(("127.0.0.1", 0))` 再读回（协作记忆 #18：别碰 Windows 保留段）。"""
    s = _sk.socket()
    s.settimeout(0.3)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ── 图片样本（纯 stdlib 现场生成）────────────────────────────────────
def png_bytes(w, h, *, declared=None, seed=7):
    """造合法 PNG。`declared=(dw,dh)` ⇒ IHDR 写假尺寸，而 IDAT 真按 (w,h) 编码
    —— 专治"只信客户端声明的尺寸"（契约 §3.1）。"""
    dw, dh = declared or (w, h)
    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # 每行 filter=0 + RGB
        for x in range(w):
            raw += bytes(((x * 3 + seed) % 256, (y * 5 + seed) % 256, 128))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", dw, dh, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def jpeg_min(w, h, *, declared=None):
    """最小**合法**灰度 baseline JPEG（手写编码器，不依赖 Pillow）。

    为什么需要它：Pillow 造不出"真实 800×800 但 SOF 谎报 320×213"这种**体积很小**的样本
    （真 4000px 的图 base64 后 1.6MB，会先撞服务端 1MB 请求体上限，测到的是"请求太大"）。
    这里逐 MCU 编码，体积 ~10KB 却带真实尺寸；`declared` 只改 SOF 两个字节，扫描数据不动。
    """
    dw, dh = declared or (w, h)
    W = ((w + 7) // 8) * 8
    H = ((h + 7) // 8) * 8

    def seg(marker, payload):
        return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + bytes(payload)

    bits = bytearray()
    for _ in range((W // 8) * (H // 8)):
        bits += bytes([0xC9])               # 5 bit：DC 码 11001 + 幅值 100（类别 3 → 值 4）
    while len(bits) % 8:
        bits.append(0)
    scan = bytes(bits).replace(b"\xff", b"\xff\x00")

    app0 = seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    dqt = seg(0xDB, b"\x00" + bytes([16] * 64))
    sof = seg(0xC0, b"\x08" + struct.pack(">HH", dh, dw) + b"\x01\x01\x11\x00")
    dht_dc = seg(0xC4, b"\x00" + bytes([0, 1] + [0] * 14) + b"\x03")
    dht_ac = seg(0xC4, b"\x10" + bytes([0, 1] + [0] * 14) + b"\x00")
    sos = seg(0xDA, b"\x01\x01\x00\x00\x3F\x00")
    return b"\xff\xd8" + app0 + dqt + sof + dht_dc + dht_ac + sos + scan + b"\xff\xd9"


def jpeg_bytes(w, h, *, declared=None, quality=60):
    """造合法 JPEG：优先 Pillow（真实感更强），不可用/太大时回退 `jpeg_min`。
    `declared=(dw,dh)`：SOF 写假宽高、像素真按 (w,h) 编码。"""
    dw, dh = declared or (w, h)
    data = None
    try:
        from PIL import Image
        if w * h <= 4_000_000:
            img = Image.new("RGB", (w, h))
            px = img.load()
            for y in range(0, h, 2):
                for x in range(0, w, 2):
                    v = (x * 3 + y * 5) % 256
                    for dx in range(2):
                        for dy in range(2):
                            if x + dx < w and y + dy < h:
                                px[x + dx, y + dy] = (v, (v * 2) % 256, (v * 3) % 256)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            data = bytearray(buf.getvalue())
    except Exception:
        data = None
    if data is None:
        return jpeg_min(w, h, declared=declared)
    if not declared:
        return bytes(data)
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            data[i + 5:i + 7] = struct.pack(">H", dh)
            data[i + 7:i + 9] = struct.pack(">H", dw)
            return bytes(data)
        if seg_len <= 0:
            break
        i += 2 + seg_len
    return bytes(data)


def data_url(blob, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


# ── WebP（手搓容器；§3.4 要求 VP8X 画布与内层 VP8/VP8L 帧**都**解析、按更严的判）──
def webp_vp8x(canvas, frame):
    """RIFF/WEBP：VP8X 画布 + 内层 VP8 帧，两者可给**不同**尺寸。

    - VP8X 的 24bit 画布宽高是**减一存储**；
    - VP8 帧头 = 3 字节 frame tag（bit0=0 关键帧、version=0、show_frame=1）+ 起始码 `9d 01 2a`
      + 16bit 宽 + 16bit 高（低 14 位有效）。
    """
    cw, ch = canvas
    fw, fh = frame
    vp8x = (b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
            + struct.pack("<I", 0)
            + (cw - 1).to_bytes(3, "little") + (ch - 1).to_bytes(3, "little"))
    frame_tag = bytes([0x01, 0x00, 0x00])          # 关键帧 · version 0 · show_frame=1
    vp8_payload = (frame_tag + b"\x9d\x01\x2a"
                   + struct.pack("<H", fw & 0x3FFF) + struct.pack("<H", fh & 0x3FFF)
                   + b"\x00" * 16)
    vp8 = b"VP8 " + struct.pack("<I", len(vp8_payload)) + vp8_payload
    if len(vp8_payload) % 2:
        vp8 += b"\x00"                      # RIFF 块要 2 字节对齐
    body = b"WEBP" + vp8x + vp8
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_bytes(canvas=(64, 64), frame=None):
    """对外统一入口（`frame=None` ⇒ 与画布一致）。"""
    return webp_vp8x(canvas, frame or canvas)


THUMB_OK = png_bytes(64, 48)
AVATAR_OK = png_bytes(64, 64, seed=11)
DISPLAY_OK = png_bytes(128, 96, seed=13)
STICKER_OK = png_bytes(32, 32, seed=17)


def base_card(cid="acc_card", **over):
    c = {"id": cid, "name": "对抗验收卡", "char_name": "小萤", "user_name": "你",
         "presentation": "sticker", "desc": "独立验收用", "category": "陪伴"}
    c.update(over)
    return c


# ── 落地：写报告 + 退出码 ────────────────────────────────────────────
def finish(extra=None):
    import shutil
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pass": PASS, "fail": FAIL, "pending": PENDING, "strict": STRICT,
        "results": RESULTS,
        "sandbox": str(SANDBOX),
        "baseline_reference": str(ROOT / "_harden" / "baseline_plaza_traffic.json"),
    }
    if extra:
        summary.update(extra)
    out = Path(os.environ.get("FIREFLY_ACCEPT_REPORT")
               or (ROOT / "tests" / f"_acceptance_v2_report_{(extra or {}).get('layer', 'all')}.json"))
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    print(f"\n  汇总：PASS={PASS} FAIL={FAIL} PENDING={PENDING}")
    print(f"  报告：{out}")
    if FAIL:
        print("  仍有 FAIL：" + ", ".join(r["id"] for r in RESULTS if r["status"] == "FAIL"))
    if PENDING:
        print("  未验证（PENDING，实现就绪后必须转 LIVE）："
              + ", ".join(r["id"] for r in RESULTS if r["status"] == "PENDING"))
    if STRICT and PENDING:
        print("\n结果（严格模式）：PENDING 视为失败")
        return 1
    return 1 if FAIL else (3 if PENDING else 0)
