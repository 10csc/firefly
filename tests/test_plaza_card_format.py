# -*- coding: utf-8 -*-
"""角色卡归档格式回归 —— 共创平台 M1（docs/设计/角色卡共创平台/01）

钉死的事（每条错误码至少一例）：
- 白名单外的文件**整卡拒绝**，不静默忽略；
- 路径穿越 / 绝对路径 / 顶层多余项 → CARD_BAD_PATH / CARD_UNKNOWN_FILE；
- 体积（zip / 单文件 / 总量 / 图片）+ 压缩比炸弹 + 图片像素；
- preset.json 交给权威解析器（字段缺失、id 与卡片不一致）；
- 哈希与 digest 一致性；
- 未知分类回退「其他」（前向兼容）；
- build_card_zip 产出的卡**必定**能通过 parse_card_zip（自校验）。

沙箱纪律：只在本进程临时目录里造数据，绝不触碰真实 user_data/。
"""
import base64
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import modules.app_config as cfg

_tmp = Path(tempfile.mkdtemp(prefix="firefly_test_plaza_card_"))
cfg.USER_DIR = _tmp
cfg.CONFIG_FILE = _tmp / "config.json"

from plaza import card_format as cf   # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


def expect_code(desc, code, fn):
    """断言 fn() 抛 CardError 且码等于 code。"""
    global PASS, FAIL
    try:
        fn()
    except cf.CardError as e:
        if e.code == code:
            PASS += 1
            print(f"  ok   {desc} [{code}]")
        else:
            FAIL += 1
            print(f"  FAIL {desc} —— 期望 {code}，实际 {e.code}（{e.message}）")
    except Exception as e:
        FAIL += 1
        print(f"  FAIL {desc} —— 期望 CardError({code})，实际 {type(e).__name__}: {e}")
    else:
        FAIL += 1
        print(f"  FAIL {desc} —— 期望 {code}，但没抛异常")


# 1×1 PNG（真实可解析头部，无 Pillow 依赖）
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ── 造"指定宽高"的图片头部（服务端只解析头部；这些样本正好用来测宽高复核）──
def png(w, h):
    """PNG：签名 + IHDR（校验器只读 b[16:20]/b[20:24] 的宽高）。"""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def jpg(w, h):
    """JPEG：SOI + SOF0（含 16bit 高/宽）。"""
    return (b"\xff\xd8\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03\x00" * 6)


def webp_vp8x(w, h):
    """WebP 扩展格式：VP8X chunk（24bit 画布宽高，减一存储）。**chunk 长度字段必须写对**。"""
    b = bytearray(30)
    b[0:4], b[8:12], b[12:16] = b"RIFF", b"WEBP", b"VP8X"
    b[16:20] = (10).to_bytes(4, "little")            # VP8X payload = 10 字节
    b[24:27] = (w - 1).to_bytes(3, "little")
    b[27:30] = (h - 1).to_bytes(3, "little")
    return bytes(b)


def webp_vp8l(w, h):
    """WebP 无损：VP8L chunk（14bit 宽/高打包在一个小端整数里）。"""
    b = bytearray(30)
    b[0:4], b[8:12], b[12:16], b[20] = b"RIFF", b"WEBP", b"VP8L", 0x2F
    b[16:20] = (5).to_bytes(4, "little")             # VP8L payload = 5 字节
    v = ((w - 1) & 0x3FFF) | (((h - 1) & 0x3FFF) << 14)
    b[21:25] = v.to_bytes(4, "little")
    return bytes(b)


def webp_vp8(w, h):
    """WebP 有损：VP8 chunk（起始码 `9d 01 2a` 后跟 14bit 宽/高）。"""
    b = bytearray(50)
    b[0:4], b[8:12], b[12:16] = b"RIFF", b"WEBP", b"VP8 "
    b[16:20] = (10).to_bytes(4, "little")            # VP8 payload = 10 字节
    b[20:23] = b"\x9d\x01\x2a"
    b[23:25] = (w & 0x3FFF).to_bytes(2, "little")
    b[25:27] = (h & 0x3FFF).to_bytes(2, "little")
    return bytes(b)


def _chunk(fourcc, payload):
    """拼一个 RIFF chunk（长度字段 + 奇数补齐），供"容器套帧"的对抗样本用。"""
    pad = b"\0" if len(payload) % 2 else b""
    return fourcc + len(payload).to_bytes(4, "little") + payload + pad


def webp_vp8x_container(canvas, frame, frame_kind=b"VP8 "):
    """真编码器会产出的结构：VP8X 容器 + 内层真实帧（两者尺寸可以**故意不一致**）。

    `verifier` 的 W02 就是这个形状：画布 64×64、内层 VP8 帧 128×128。
    "只读第一个 chunk"的实现会返回画布尺寸 —— 那是漏洞（契约 §3.4：按更严的判）。
    """
    vp8x = bytearray(10)
    vp8x[4:7] = (canvas[0] - 1).to_bytes(3, "little")
    vp8x[7:10] = (canvas[1] - 1).to_bytes(3, "little")
    if frame_kind == b"VP8L":
        inner = bytearray(5)
        inner[0] = 0x2F
        inner[1:5] = (((frame[0] - 1) & 0x3FFF)
                      | (((frame[1] - 1) & 0x3FFF) << 14)).to_bytes(4, "little")
        inner = bytes(inner)
    else:
        inner = bytearray(10)
        inner[0:3] = b"\x9d\x01\x2a"
        inner[3:5] = (frame[0] & 0x3FFF).to_bytes(2, "little")
        inner[5:7] = (frame[1] & 0x3FFF).to_bytes(2, "little")
        inner = bytes(inner)
    body = _chunk(b"VP8X", bytes(vp8x)) + _chunk(frame_kind, inner)
    return b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WEBP" + body

PRESET = {
    "id": "testcard", "name": "测试卡", "char_name": "小萤", "user_name": "你",
    "presentation": "sticker", "desc": "一张用于测试的卡", "tagline": "副标题", "schema": 1,
}


def good_files():
    return {
        "preset.json": json.dumps(PRESET, ensure_ascii=False).encode("utf-8"),
        "core.md": "# 核心设定\n测试".encode("utf-8"),
        "assets/cover.png": PNG_1X1,
    }


def good_manifest(**over):
    m = {
        "format": cf.CARD_FORMAT, "pack_schema": 1, "id": "testcard", "name": "测试卡",
        "char_name": "小萤", "user_name": "你", "presentation": "sticker",
        "desc": "一张用于测试的卡", "tagline": "副标题", "category": "陪伴",
        "tags": ["治愈", "日常"], "created_at": 1759290000,
        "author": {"uid_hash": "a" * 64, "display": "官方", "official": True},
    }
    m.update(over)
    return m


def raw_zip(entries, compress=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as zf:
        for name, blob in entries.items():
            zf.writestr(name, blob)
    return buf.getvalue()


def with_manifest(manifest, files):
    """按给出 manifest 原样打包（不做 files[]/digest 补齐，用于构造坏卡）。"""
    entries = {"manifest.json": json.dumps(manifest, ensure_ascii=False).encode("utf-8")}
    for rel, blob in files.items():
        entries["character/" + rel] = blob
    return raw_zip(entries)


def _mk_dirty_dir():
    """造一个含白名单外文件的 character/ 目录（模拟"本地老包发广场"）。"""
    d = _tmp / "dirty_character"
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "preset.json").write_bytes(json.dumps(PRESET, ensure_ascii=False).encode())
    (d / "core.md").write_bytes("# 核心设定".encode())
    (d / "prompts" / "polisher.md").write_bytes(b"x")
    return d


def _dup_zip():
    """同名条目：ZipFile 按名取首条，重复条目会让"校验的"与"取出的"不一致。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(good_manifest(), ensure_ascii=False).encode())
        zf.writestr("character/core.md", b"first")
        zf.writestr("character/core.md", b"second")
    return buf.getvalue()


def _bomb_zip():
    """高压缩比载荷：200KB 全零压成几百字节（>RATIO_MIN_BYTES 且比值远超 20）。"""
    entries = {
        "manifest.json": json.dumps(good_manifest(), ensure_ascii=False).encode(),
        "character/assets/cover.png": b"\0" * (200 * 1024),
    }
    return raw_zip(entries)


print("== 1. 正常往返 ==")
card = cf.build_card_zip(good_manifest(), good_files())
parsed = cf.parse_card_zip(card)
check("parse 成功并回出 manifest", parsed["manifest"]["id"] == "testcard")
check("digest 与实际内容一致", parsed["digest"] == cf.card_digest(good_files()))
check("文件集合一致", set(parsed["files"]) == {"preset.json", "core.md", "assets/cover.png"})
check("preset 走权威解析器", parsed["preset"]["char_name"] == "小萤")
check("author.official 保留", parsed["manifest"]["author"]["official"] is True)
check("体积远小于上限", len(card) < cf.MAX_ZIP_BYTES)

print("== 2. 白名单与路径 ==")
files = good_files()
files["prompts/polisher.md"] = b"x"
expect_code("白名单外的 prompts/ 文件被拒", cf.E_UNKNOWN_FILE,
            lambda: cf.build_card_zip(good_manifest(), files))
expect_code("顶层多余项被拒", cf.E_UNKNOWN_FILE,
            lambda: cf.parse_card_zip(raw_zip({"manifest.json": b"{}", "evil.txt": b"x"})))
expect_code("路径穿越被拒", cf.E_BAD_PATH,
            lambda: cf.parse_card_zip(raw_zip({
                "manifest.json": json.dumps(good_manifest()).encode(),
                "character/../../evil.md": b"x"})))
expect_code("绝对路径被拒", cf.E_BAD_PATH,
            lambda: cf.parse_card_zip(raw_zip({
                "manifest.json": json.dumps(good_manifest()).encode(),
                "character//etc/passwd": b"x"})))
expect_code("未知图片槽位被拒", cf.E_UNKNOWN_FILE,
            lambda: cf.build_card_zip(good_manifest(), {**good_files(), "assets/banner.png": PNG_1X1}))
expect_code("非图片扩展名被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), {**good_files(), "assets/cover.gif": b"GIF89a"}))
expect_code("collect_card_files(strict=True) 拒绝脏目录", cf.E_UNKNOWN_FILE,
            lambda: cf.collect_card_files(_mk_dirty_dir()))

print("== 3. 体积与炸弹 ==")
expect_code("zip 本体超限被拒", cf.E_TOO_BIG,
            lambda: cf.parse_card_zip(b"PK\x03\x04" + b"\0" * (cf.MAX_ZIP_BYTES + 1)))
expect_code("封面超 300KB 被拒", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), {**good_files(), "assets/cover.png": b"\0" * (300 * 1024 + 1)}))
# ★ 2026-10-01 松绑：单文件文本**不再限 64KB**（改按"单卡文字总量"管，见 §13）
check("文本 64KB 不再被拒（旧逐文件上限已取消）",
      len(cf.build_card_zip(good_manifest(), {**good_files(),
                                              "core.md": b"a" * (64 * 1024 + 1)})) > 0)
expect_code("重复条目被拒", cf.E_BAD_PATH, lambda: cf.parse_card_zip(_dup_zip()))
expect_code("压缩比异常判为炸弹", cf.E_BOMB,
            lambda: cf.parse_card_zip(_bomb_zip()))
check("1×1 PNG 能解析出尺寸", cf.image_size(PNG_1X1) == (1, 1))
check("垃圾字节被判定为非图片", cf.image_size(b"not an image") is None)

print("== 4. 元数据与 preset ==")
expect_code("format 不支持被拒", cf.E_FORMAT_UNSUPPORTED,
            lambda: cf.parse_card_zip(with_manifest(good_manifest(format=2), good_files())))
expect_code("缺 manifest 被拒", cf.E_BAD_MANIFEST,
            lambda: cf.parse_card_zip(raw_zip({"character/core.md": b"x"})))
expect_code("manifest 非 JSON 对象被拒", cf.E_BAD_MANIFEST,
            lambda: cf.parse_card_zip(raw_zip({"manifest.json": b"[]"})))
expect_code("缺必填字段被拒", cf.E_BAD_MANIFEST,
            lambda: cf.build_card_zip(good_manifest(name=""), good_files()))
# ★2026-10-02 松绑：标签上限 5 → 20（原"6 个标签被拒"作废）
check("6 个标签不再被拒（上限已抬到 20）",
      len(cf.build_card_zip(good_manifest(tags=["a", "b", "c", "d", "e", "f"]), good_files())) > 0)
expect_code("标签超过 20 个被拒", cf.E_BAD_MANIFEST,
            lambda: cf.build_card_zip(good_manifest(tags=[f"t{i}" for i in range(21)]), good_files()))
expect_code("id 非法被拒", cf.E_BAD_MANIFEST,
            lambda: cf.build_card_zip(good_manifest(id="Bad ID"), good_files()))
expect_code("preset 缺字段被拒", cf.E_BAD_PRESET,
            lambda: cf.parse_card_zip(with_manifest(good_manifest(), {
                **good_files(),
                "preset.json": json.dumps({k: v for k, v in PRESET.items() if k != "char_name"},
                                          ensure_ascii=False).encode()})))
expect_code("preset.id 与卡片 id 不一致被拒", cf.E_BAD_PRESET,
            lambda: cf.parse_card_zip(with_manifest(good_manifest(id="other"),
                                                    {**good_files(),
                                                     "preset.json": json.dumps({**PRESET, "id": "testcard"},
                                                                               ensure_ascii=False).encode()})))
expect_code("缺 preset.json 被拒", cf.E_BAD_PRESET,
            lambda: cf.parse_card_zip(with_manifest(good_manifest(), {"core.md": b"x"})))
expect_code("opening.json 非法 JSON 被拒", cf.E_BAD_MANIFEST,
            lambda: cf.build_card_zip(good_manifest(), {**good_files(), "opening.json": "{不是 json".encode("utf-8")}))

print("== 5. 哈希一致性 ==")
bad = good_manifest()
bad["files"] = [{"path": "core.md", "size": 1, "sha256": "0" * 64}]
expect_code("files[] 哈希不符被拒", cf.E_HASH_MISMATCH,
            lambda: cf.parse_card_zip(with_manifest(bad, good_files())))
bad2 = good_manifest(digest="f" * 64)
expect_code("整卡 digest 不符被拒", cf.E_HASH_MISMATCH,
            lambda: cf.parse_card_zip(with_manifest(bad2, good_files())))

print("== 6. 前向兼容与工具函数 ==")
check("未知分类回退其他", cf.normalize_category("外星分类") == cf.DEFAULT_CATEGORY)
check("空分类回退其他", cf.normalize_category(None) == cf.DEFAULT_CATEGORY)
check("已知分类原样保留", cf.normalize_category("治愈") == "治愈")
parsed2 = cf.parse_card_zip(cf.build_card_zip(good_manifest(category="外星分类"), good_files()))
check("解析后分类被归一化", parsed2["manifest"]["category"] == cf.DEFAULT_CATEGORY)
check("data URL 解码", cf.decode_data_url(
    "data:image/png;base64," + base64.b64encode(PNG_1X1).decode()) == PNG_1X1)
expect_code("非法 data URL 被拒", cf.E_BAD_IMAGE, lambda: cf.decode_data_url("http://x/y.png"))
check("collect_card_files(strict=False) 跳过白名单外文件",
      set(cf.collect_card_files(_mk_dirty_dir(), strict=False)) == {"core.md", "preset.json"})


# ══════════════════════════════════════════════════════════════════
# V1（06 契约）：新槽位 / 自由文本与分组知识库 / 宽高复核 / thumb 必填
# ══════════════════════════════════════════════════════════════════
def v1_files(**over):
    """一张"什么都带"的 V1 卡：分组知识库 + 表情包标签 + thumb/avatar/display。"""
    f = {
        "preset.json": json.dumps(PRESET, ensure_ascii=False).encode("utf-8"),
        "core.md": "# 核心设定".encode("utf-8"),
        "notes.txt": "自由文本".encode("utf-8"),
        "knowledge/世界观.md": "扁平知识库".encode("utf-8"),
        "knowledge/world/地理.md": "一层分组知识库".encode("utf-8"),
        "assets/thumb.webp": webp_vp8x(320, 200),
        "assets/avatar.png": png(256, 256),
        "assets/display.jpg": jpg(1024, 768),
        "assets/sticker-1.webp": webp_vp8l(160, 160),
        "assets/sticker-2.webp": webp_vp8(64, 64),
        "stickers.json": json.dumps({"sticker-1.webp": "开心", "sticker-2.webp": "疑惑"},
                                     ensure_ascii=False).encode("utf-8"),
    }
    f.update(over)
    return f


print("== 7. V1 新槽位与自由文本（全部通过）==")
v1 = cf.parse_card_zip(cf.build_card_zip(good_manifest(), v1_files()))
check("V1 卡能解析", v1["manifest"]["id"] == "testcard")
check("分组知识库在文件集里", "knowledge/world/地理.md" in v1["files"])
check("根目录自定文本在文件集里（notes.txt）", v1["files"]["notes.txt"] == "自由文本".encode())
check("thumb_name 找得到 thumb", cf.thumb_name(v1["files"]) == "assets/thumb.webp")
check("IMAGE_SLOTS 含 8 个表情包槽位", len(cf.STICKER_SLOTS) == 8
      and all(s in cf.IMAGE_SLOTS for s in cf.STICKER_SLOTS))
check("列表槽位就是 thumb", cf.LIST_SLOT == "thumb")
check("老槽位 cover 仍被接受（存量卡兼容）",
      cf.classify("assets/cover.png") == "image")

print("== 8. 服务端自己解析宽高（PNG/JPEG/WebP 三种，不信客户端声明）==")
check("PNG IHDR 宽高", cf.image_size(png(321, 200)) == (321, 200))
check("JPEG SOF0 宽高", cf.image_size(jpg(640, 480)) == (640, 480))
check("WebP VP8X 宽高", cf.image_size(webp_vp8x(300, 200)) == (300, 200))
check("WebP VP8L 宽高", cf.image_size(webp_vp8l(160, 90)) == (160, 90))
check("WebP VP8(有损) 宽高", cf.image_size(webp_vp8(128, 96)) == (128, 96))
check("头部撒谎也照样被读出来（声明 1×1 实为 5000×5000 的 PNG）",
      cf.image_size(png(5000, 5000)) == (5000, 5000))

print("== 8b. WebP 容器套帧：画布容不下帧就拒、否则取更严（verifier W02 回归）==")


def _w02_msg():
    """跑一次 W02 形状的拒绝，把中文原因取出来（文案要能一眼看出"画布与帧矛盾"）。"""
    try:
        cf.build_card_zip(good_manifest(), v1_files(**{
            "assets/sticker-1.webp": webp_vp8x_container((64, 64), (128, 128))}))
    except cf.CardError as e:
        return e.code, e.message
    return "", ""


_w02_code, _w02_text = _w02_msg()
check(f"W02a 画布 64 容不下内层 VP8 帧 128 → **拒绝**（{_w02_code}）", _w02_code == cf.E_BAD_IMAGE)
check(f"W02a' 文案点明「画布与帧矛盾」（{_w02_text}）",
      "画布" in _w02_text and "矛盾" in _w02_text)
expect_code("W02b 画布 64 容不下内层 VP8L 帧 200×100 → 拒绝", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{
                "assets/sticker-1.webp": webp_vp8x_container((64, 64), (200, 100), b"VP8L")})))
check("W02c 画布 320 ≥ 帧 64 → 取画布 320（画布更大 = 更严）",
      cf.image_size(webp_vp8x_container((320, 200), (64, 64))) == (320, 200))
check("W02d 画布 = 帧 → 取该尺寸",
      cf.image_size(webp_vp8x_container((300, 200), (300, 200))) == (300, 200))
expect_code("W02e 画布 512 谎报（帧只有 128）→ 超 thumb 320 上限被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{
                "assets/thumb.webp": webp_vp8x_container((512, 512), (128, 128))})))
check("W02f 结构损坏（chunk 长度越界）→ 解不出尺寸（宁可拒绝）", cf.image_size(
    b"RIFF" + (100).to_bytes(4, "little") + b"WEBP" + b"VP8X"
    + (999).to_bytes(4, "little")) is None)
check("W02g 裸 VP8（无 VP8X）仍按帧尺寸", cf.image_size(webp_vp8(128, 96)) == (128, 96))

print("== 8c. 真编码器交叉验证（Pillow 可选；没装就跳过）==")
try:
    import io as _io
    import struct as _struct

    from PIL import Image as _Image

    def _wrap_vp8x(raw, canvas):
        """把真编码的 WebP 码流套进 VP8X 容器（照 _harden/make_webp_samples.py 的做法）。"""
        off, inner = 12, b""
        while off + 8 <= len(raw):
            tag = raw[off:off + 4]
            size = _struct.unpack("<I", raw[off + 4:off + 8])[0]
            payload = raw[off + 8:off + 8 + size]
            if tag in (b"VP8 ", b"VP8L", b"ALPH"):
                inner += tag + _struct.pack("<I", len(payload)) + payload
                if len(payload) % 2:
                    inner += b"\0"
            off += 8 + size + (size % 2)
        vp8x = bytes([0]) + b"\0\0\0" + (canvas[0] - 1).to_bytes(3, "little") \
            + (canvas[1] - 1).to_bytes(3, "little")
        body = b"WEBP" + b"VP8X" + _struct.pack("<I", 10) + vp8x + inner
        return b"RIFF" + _struct.pack("<I", len(body)) + body

    _img = _Image.new("RGBA", (137, 91))
    _b1 = _io.BytesIO()
    _img.save(_b1, "WEBP", lossless=False)          # RGBA 有损 → VP8X 容器（+ALPH）
    _real1 = _b1.getvalue()
    check(f"真 WebP 有损（{len(_real1)}B，fourcc={_real1[12:16].decode('ascii', 'replace')}）解析出 137×91",
          cf.image_size(_real1) == (137, 91))
    _b2 = _io.BytesIO()
    _img.convert("RGB").save(_b2, "WEBP", lossless=True)
    _real2 = _b2.getvalue()
    check(f"真 WebP 无损（{len(_real2)}B，fourcc={_real2[12:16].decode('ascii', 'replace')}）解析出 137×91",
          cf.image_size(_real2) == (137, 91))
    # ★ 用**真编码器码流 + 只改画布**造 W02 的形状，并用真解码器交叉验证"这种文件确实打不开"
    _b3 = _io.BytesIO()
    _Image.new("RGB", (128, 128)).convert("RGB").save(_b3, "WEBP", quality=70)
    _lying = _wrap_vp8x(_b3.getvalue(), (64, 64))   # 帧真 128、画布谎报 64
    check("★ 真码流 W02：画布谎报 64 / 帧真 128 → 我们拒绝（返回 None）",
          cf.image_size(_lying) is None)
    try:
        _Image.open(_io.BytesIO(_lying)).load()     # 真解码器打不开 ⇒ 拒绝是对的
        _decodable = True
    except Exception:
        _decodable = False
    check("★ 同一份谎言样本：真解码器（Pillow）也打不开 ⇒ 拒绝有据", _decodable is False)
except ImportError:
    print("  skip 未安装 Pillow", flush=True)

print("== 9. 槽位体积/尺寸上限（06 §3.3）==")
expect_code("thumb 长边 >320px 被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/thumb.webp": webp_vp8x(321, 200)})))
expect_code("thumb >12KB 被拒", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/thumb.webp": webp_vp8x(320, 200) + b"\0" * (12 * 1024)})))
expect_code("avatar 长边 >256px 被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/avatar.png": png(257, 100)})))
expect_code("avatar >30KB 被拒", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/avatar.png": png(256, 256) + b"\0" * (30 * 1024)})))
expect_code("display 长边 >1024px 被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/display.jpg": jpg(1025, 768)})))
expect_code("display >300KB 被拒", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/display.jpg": jpg(1024, 768) + b"\0" * (300 * 1024)})))
expect_code("表情包长边 >160px 被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/sticker-1.webp": webp_vp8l(161, 160)})))
expect_code("表情包 >20KB 被拒", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/sticker-1.webp": webp_vp8l(160, 160) + b"\0" * (20 * 1024)})))
expect_code("第 9 张表情包被拒（sticker-9 越界）", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/sticker-9.webp": webp_vp8l(160, 160)})))
expect_code("图片像素总量超限被拒（防解压炸弹）", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/display.jpg": jpg(2048, 2048)})))
expect_code("伪装扩展名（thumb.png.exe）被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/thumb.png.exe": png(64, 64)})))
expect_code("魔数不对（thumb.webp 里塞文本）被拒", cf.E_BAD_IMAGE,
            lambda: cf.build_card_zip(good_manifest(), v1_files(**{"assets/thumb.webp": b"not an image at all"})))

print("== 10. 自由文本与分组知识库的边界 ==")
def _rel(changes):
    """按 V1 底卡改文件集，返回 build 调用（用于断言各类路径拒绝）。"""
    return lambda: cf.build_card_zip(good_manifest(), changes)


check("根目录世界设定.json 接受", cf.classify("世界设定.json") == "text")
check("knowledge/world/地理.md（一层分组）接受", cf.classify("knowledge/world/地理.md") == "text")
# ★2026-10-02 放宽：允许 空格 / · / （） / . / 大小写扩展名，每段 ≤64 字（用户载入自己的官方卡时
#   被"文件名规则太紧"误拒 —— 服务端与前端必须同口径：**分组名按段校验**，别按扁平名校验）
for _p in ("knowledge/世界 设定/角色 · 卡（v1.2）.md", "knowledge/世界 设定.md",
           "knowledge/版本 1.0/设定.md", "Q&A.md", "世界观：设定.MD",
           "knowledge/" + "长" * 60 + ".md"):
    try:
        check(f"合法名通过：{_p[:34]}…", cf.classify(_p) == "text")
    except cf.CardError as e:
        check(f"合法名通过：{_p[:34]}…（实际被拒 {e.code}：{e.message}）", False)
expect_code("knowledge 深层（a/b/c.md）被拒（层级过深）", cf.E_BAD_PATH,
            _rel({**v1_files(), "knowledge/a/b/c.md": b"x"}))
# 隐藏分组现在是**更准确的** CARD_BAD_PATH（原文案是泛泛的"不接受该文件"）
expect_code("knowledge 隐藏分组（.git/x.md）被拒", cf.E_BAD_PATH,
            _rel({**v1_files(), "knowledge/.git/x.md": b"x"}))
expect_code("knowledge 之外的目录（prompts/x.md）被拒", cf.E_UNKNOWN_FILE,
            _rel({**v1_files(), "prompts/x.md": b"x"}))
expect_code("伪扩展名文本（evil.md.exe）被拒", cf.E_UNKNOWN_FILE,
            _rel({**v1_files(), "evil.md.exe": b"x"}))
expect_code("路径穿越（knowledge/../../x.md）被拒", cf.E_BAD_PATH,
            _rel({**v1_files(), "knowledge/../../x.md": b"x"}))
# ★2026-10-02：非法名必须**指名道姓**（哪一段/哪个字符/当前长度）—— 别再让用户猜
for _bad, _kw in (("knowledge/" + "长" * 65 + ".md", "过长"),
                  ("knowledge/坏\x01名.md", "控制字符"),
                  ("knowledge/名/../x.md", "路径穿越"),
                  ("knowledge/世界/名.exe", "扩展名"),
                  ("a\\b.md", "非法字符")):
    try:
        cf.classify(_bad)
        check(f"非法名被拒且指名道姓：{_bad[:26]}…", False, "竟然通过")
    except cf.CardError as e:
        check(f"非法名被拒且指名道姓（{e.code}）：{e.message[:56]}…", _kw in e.message)
# ★ 2026-10-01 松绑：知识库**份数不限**（旧上限 6 份已删）—— 7 份必须通过，改由"文件总数 ≤24 / 文字总量"约束
check("知识库 7 份不再被拒（份数上限已取消）",
      cf.parse_card_zip(_rel({**v1_files(), **{"knowledge/第%d份.md" % i: b"x" for i in range(1, 8)}})())
      ["manifest"]["id"] == "testcard")
check("文本 64KB 不再被拒（新槽位同样不受逐文件上限约束）",
      len(cf.parse_card_zip(_rel({**v1_files(), "knowledge/巨长.md": b"a" * (64 * 1024 + 1)})())) > 0)
# ★ 2026-10-02 松绑：文件**数量**上限 24 → 500（用户现场：33 个文本文件被"上限 24"挡住发不出去）
_N0 = len(v1_files())                     # 底卡已有几个文件（preset/thumb/若干知识库与表情包…）
check(f"文件数 25 个不再被拒（上限已抬到 {cf.MAX_FILES}）",
      len(cf.parse_card_zip(_rel({**v1_files(), **{"notes-%02d.txt" % i: b"x" for i in range(20)}})())) > 0)
check(f"33 个文件通过（**用户现场**；底卡 {_N0} + 28 个文本）",
      len(cf.parse_card_zip(_rel({**v1_files(), **{"k%02d.md" % i: b"x" for i in range(33 - _N0)}})())) > 0)
check("恰好 500 个文件通过（上限边界）",
      len(cf.parse_card_zip(_rel({**v1_files(),
                                  **{"n%03d.md" % i: b"x" for i in range(500 - _N0)}})())) > 0)
try:
    cf.build_card_zip(good_manifest(), {**v1_files(),
                                        **{"m%03d.md" % i: b"x" for i in range(501 - _N0)}})
    check("501 个文件被拒（人话带实际值/上限）", False)
except cf.CardError as e:
    check(f"501 个文件被拒且人话：{e.message}",
          e.code == cf.E_TOO_BIG and "文件过多" in e.message and "500" in e.message)
check("MAX_FILES 常量 = 500（钉住，别被改回去）", cf.MAX_FILES == 500)

print("\n== 14. 标签上限（★2026-10-02：5 → 20）==")
check(f"MAX_TAGS 常量 = {cf.MAX_TAGS}（钉住）", cf.MAX_TAGS == 20)
check("20 个标签通过",
      cf.parse_card_zip(cf.build_card_zip(good_manifest(tags=[f"t{i}" for i in range(20)]),
                                          v1_files()))["manifest"]["id"] == "testcard")
try:
    cf.build_card_zip(good_manifest(tags=[f"t{i}" for i in range(21)]), v1_files())
    check("21 个标签被拒且人话（带实际值/上限）", False)
except cf.CardError as e:
    check(f"21 个标签被拒且人话：{e.message}",
          e.code == cf.E_BAD_MANIFEST and "标签最多 20 个" in e.message and "当前 21 个" in e.message)
check("5 个标签仍通过（老卡不受影响）",
      cf.parse_card_zip(cf.build_card_zip(good_manifest(tags=["治愈", "日常", "陪伴", "剧情", "助手"]),
                                          v1_files()))["manifest"]["id"] == "testcard")

print("== 11. stickers.json 标签映射校验 ==")
check("带标签的卡解析成功", cf.parse_card_zip(
    cf.build_card_zip(good_manifest(), v1_files()))["files"]["stickers.json"] ==
    json.dumps({"sticker-1.webp": "开心", "sticker-2.webp": "疑惑"}, ensure_ascii=False).encode("utf-8"))
expect_code("stickers.json 指向不存在的表情包被拒", cf.E_UNKNOWN_FILE,
            _rel({**v1_files(), "stickers.json": json.dumps({"sticker-7.webp": "有"},
                                                            ensure_ascii=False).encode()}))
expect_code("stickers.json 键路径穿越被拒", cf.E_UNKNOWN_FILE,
            _rel({**v1_files(), "stickers.json": json.dumps({"../x.webp": "有"},
                                                            ensure_ascii=False).encode()}))
expect_code("标签超 12 字被拒", cf.E_BAD_MANIFEST,
            _rel({**v1_files(), "stickers.json": json.dumps({"sticker-1.webp": "这是一个超过十二个字的标签"},
                                                            ensure_ascii=False).encode()}))
expect_code("标签不是字符串被拒", cf.E_BAD_MANIFEST,
            _rel({**v1_files(), "stickers.json": json.dumps({"sticker-1.webp": 5}).encode()}))
expect_code("stickers.json 不是对象被拒", cf.E_BAD_MANIFEST,
            _rel({**v1_files(), "stickers.json": b'["a"]'}))
expect_code("stickers.json 非法 JSON 被拒", cf.E_BAD_MANIFEST,
            _rel({**v1_files(), "stickers.json": b"{not json"}))

print("== 12. thumb 必填只对新格式生效（老卡/老草稿不坏）==")
check("默认 parse 接受无 thumb 的老卡", cf.parse_card_zip(
    cf.build_card_zip(good_manifest(), good_files()))["manifest"]["id"] == "testcard")
expect_code("require_thumb=True 时缺 thumb 被拒（build）", cf.E_MISSING_THUMB,
            lambda: cf.build_card_zip(good_manifest(), good_files(), require_thumb=True))
expect_code("require_thumb=True 时缺 thumb 被拒（parse）", cf.E_MISSING_THUMB,
            lambda: cf.parse_card_zip(cf.build_card_zip(good_manifest(), good_files()), require_thumb=True))
check("带 thumb 时 require_thumb=True 通过", cf.parse_card_zip(
    cf.build_card_zip(good_manifest(), v1_files()), require_thumb=True)["manifest"]["id"] == "testcard")

print("\n== 13. 松绑后的限制口径（2026-10-01 用户要求：别再限份数，只限图片）==")
check("① 常量钉住：文字总量 8MB；份数/单文件上限的常量已删除",
      cf.MAX_TEXT_TOTAL_BYTES == 8 * 1024 * 1024
      and not hasattr(cf, "MAX_KNOWLEDGE_FILES") and not hasattr(cf, "MAX_TEXT_BYTES"))
check("①b 卡体上限抬到 10MB（文字 8MB + 图片余量）；管理员天花板 64MB",
      cf.MAX_ZIP_BYTES == 10 * 1024 * 1024 and cf.MAX_TOTAL_BYTES == 10 * 1024 * 1024
      and cf.ADMIN_MAX_ZIP_BYTES == 64 * 1024 * 1024)
# 10 份知识库 + 单份 ~200KB 文本 ⇒ 通过（原来会被"知识库最多 6 份"与"单文件 64KB"双杀）
_ZH = "知识".encode("utf-8")
_f10 = {"preset.json": json.dumps(PRESET, ensure_ascii=False).encode("utf-8"),
        "core.md": "# 核心".encode("utf-8"), "assets/thumb.png": png(320, 200)}
for i in range(1, 11):
    _f10[f"knowledge/k{i}.md"] = (b"# knowledge\n" + _ZH * 34000) if i == 1 \
        else f"# 第{i}份\n".encode("utf-8")
try:
    _z10 = cf.build_card_zip(good_manifest(), _f10)
    _p10 = cf.parse_card_zip(_z10)
    _kn = sum(1 for k in _p10["files"] if k.startswith("knowledge/"))
    _big = len(_p10["files"]["knowledge/k1.md"])
    check(f"② 10 份知识库 + 单份 {_big // 1024}KB 文本 ⇒ **通过**（知识 {_kn} 份，zip {len(_z10) // 1024}KB）",
          _kn == 10 and _big > 64 * 1024)
except cf.CardError as e:
    check(f"② 10 份知识库 + 200KB 文本 ⇒ 通过（实际被拒：{e.code} {e.message}）", False)

# 超过文字总量（普通账号 8MB）⇒ 拒且**人话**（含实际值与上限）
_over = dict(_f10)
_over["knowledge/huge.md"] = _ZH * 1600000                      # ≈9.2MB
try:
    cf.build_card_zip(good_manifest(), _over)
    check("③ 超过文字总量 ⇒ 被拒", False)
except cf.CardError as e:
    check(f"③ 超过文字总量被拒且人话（{e.code}）：{e.message}",
          e.code == cf.E_TOO_BIG and "文字总量" in e.message
          and str(cf.MAX_TEXT_TOTAL_BYTES // 1024) in e.message)
# 管理员：同一张超量卡 ⇒ 通过（文字总量不限）
try:
    _za = cf.build_card_zip(good_manifest(), _over, is_admin=True)
    check(f"④ 管理员同一张超量卡 ⇒ 通过（zip {len(_za) // 1024}KB，文字 ≈9.2MB）", len(_za) > 0)
except cf.CardError as e:
    check(f"④ 管理员超量卡通过（实际被拒：{e.code} {e.message}）", False)
# 图片仍**逐张**限（这条不许松）
_bad_img = dict(_f10)
_bad_img["assets/display.png"] = png(1024, 768) + b"\x00" * (300 * 1024)
expect_code("⑤ 图片超槽位规格仍被拒（display > 300KB）", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), _bad_img))
_bad_img2 = dict(_f10)
_bad_img2["assets/sticker-1.png"] = png(160, 160) + b"\x00" * (20 * 1024)
expect_code("⑤b 表情包超 20KB 仍被拒（管理员也一样）", cf.E_TOO_BIG,
            lambda: cf.build_card_zip(good_manifest(), _bad_img2, is_admin=True))

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
