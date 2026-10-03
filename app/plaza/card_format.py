# -*- coding: utf-8 -*-
"""角色卡归档 card.zip 的构建与校验（共创平台 M1）

契约：docs/设计/角色卡共创平台/01_角色卡格式与校验.md
铁律：卡 = `character/` 子树；**永不包含用户数据**（data/ journal/ images/）。

复用（不重造轮子，防口径漂移）：
- `_zip_safe_entries`（infra/sync/restore.py:63）—— zip slip 防御
- `_pack_digest`（infra/sync/manifest.py:34）—— 与快照完全相同的哈希口径
- `_parse_preset`（core/preset_parse.py:71）—— preset.json 的**唯一**权威解析器

设计要点（踩坑预防）：
1. 白名单外的文件**整卡拒绝**，不静默忽略——否则用户"传了却没生效"无法自查。
2. 体积以**实际读出的字节数**为准，不信 zip 中央目录声明的 file_size。
3. 压缩比 + 像素上限双管，防 zip 炸弹与图片解压炸弹。
4. 图片只做**魔数 + 头部尺寸解析**（stdlib，不引 Pillow；requirements.txt 只有 requests）。
5. 未知分类回退「其他」而不是报错：服务器将来加分类，旧客户端不能崩。
"""

import base64
import hashlib
import io
import json
import logging
import re
import tempfile
import zipfile
from pathlib import Path

from core.preset_parse import PRESET_SCHEMA, _parse_preset
from infra.sync.manifest import _pack_digest
from infra.sync.restore import _zip_safe_entries

logger = logging.getLogger(__name__)

# ── 格式版本与上限（改这些值必须同步改 01 文档）──────────────
CARD_FORMAT = 1
MANIFEST_NAME = "manifest.json"
CARD_ROOT = "character/"

MAX_ZIP_BYTES = 10 * 1024 * 1024      # zip 本体（文字总量 8MB + 图片 ≤约 0.5MB + 余量）
MAX_TOTAL_BYTES = 10 * 1024 * 1024    # 解压后总量（与 zip 本体同口径）
# 管理员（`is_admin=True`）：上限**抬高**而不是真"无界" —— 防手滑上传一个几百 MB 的东西
# 把服务器磁盘/内存打满（"不限"也要有个安全天花板，这是 DoS 防护不是配额）。
ADMIN_MAX_ZIP_BYTES = 64 * 1024 * 1024
ADMIN_MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 500                      # character/ 下文件数上限
# ★ 2026-10-02 松绑（用户现场：33 个文本文件被原"上限 24"挡住 ⇒ 发不出去）：文件**数量**不该卡人，
#   真正的闸门是**文字总量 8MB**（MAX_TEXT_TOTAL_BYTES）。保留 500 只为挡"一万个空文件"这类滥用。
# ★ 2026-10-01 松绑（用户要求："为什么知识库还限制份数""不要限制那么多，只是图片限制"）：
#   · 删除 MAX_KNOWLEDGE_FILES（原 6 份）—— **知识库份数不限**；
#   · 删除 MAX_TEXT_BYTES（原单文件 64KB）—— **不再逐文件限**；
#   · 文字改按**单卡总量**管：`MAX_TEXT_TOTAL_BYTES`（普通账号 8MB；管理员不限）；
#   · 图片**仍按槽位规格**限（thumb ≤12KB / avatar ≤30KB / display ≤300KB / sticker ≤20KB×8）。
MAX_TEXT_TOTAL_BYTES = 8 * 1024 * 1024   # 单卡**文字总量**（知识库 + core/identity 等之和）
MAX_DEPTH = 3                        # character/ 之后的路径深度
MAX_RATIO = 20                       # 压缩比上限（配合下面的最小体积阈值）
RATIO_MIN_BYTES = 100 * 1024         # 小于此体积的文件不做压缩比判定
MAX_PIXELS = 2048 * 2048             # 单图解码像素上限（防解压炸弹）

# 图片槽位 → (体积上限字节, 长边上限像素；None = 只看 MAX_PIXELS)
# V1 契约（06 §3.1/§3.3）：`thumb` 是**列表唯一**用图（≤320px/≤12KB）、`display` 详情用
# （≤1024px/≤300KB）、`sticker-1..8` 表情包（≤160px/≤20KB）、`avatar` ≤256px/≤30KB。
# ⚠ `cover` 是 **M1 老槽位**：V1 起列表不再用它，但**继续接受**——线上存量卡、老客户端与
# 老草稿都带 cover，直接拒收等于把已有数据判死（"老客户端与老草稿不能坏"）。它的上限沿用旧值。
STICKER_SLOTS = tuple("sticker-%d" % i for i in range(1, 9))
IMAGE_SPECS = {
    "thumb": (12 * 1024, 320),
    "avatar": (30 * 1024, 256),
    "display": (300 * 1024, 1024),
    "cover": (300 * 1024, None),
}
IMAGE_SPECS.update({s: (20 * 1024, 160) for s in STICKER_SLOTS})
IMAGE_SLOTS = ("thumb", "avatar", "display") + STICKER_SLOTS + ("cover",)
IMAGE_MAX_BYTES = {k: v[0] for k, v in IMAGE_SPECS.items()}   # 兼容既有引用（=体积上限）
LIST_SLOT = "thumb"                  # 列表投影只发这一个槽位（06 §3.3）

# 广场一级分类（前端同一份；未知值回退 DEFAULT_CATEGORY）
CATEGORIES = ("陪伴", "剧情", "日常", "战斗", "治愈", "搞笑", "其他")
DEFAULT_CATEGORY = "其他"

FIELD_MAX = {"name": 40, "char_name": 20, "user_name": 20,
             "desc": 120, "tagline": 40, "category": 16}
MAX_TAGS = 20                        # ★2026-10-02：5 → 20（标签是元数据，5 个太紧；仍能用于筛选）
MAX_TAG_LEN = 12
MAX_STICKER_LABEL = 12               # 表情包标签 ≤12 字（06 §3.2；落 character/stickers.json）

# character/ 下的文本白名单（06 §3.1）：根目录固定 5 个 + 根/knowledge/ 下的**自由命名**文本。
# 自由命名是"制卡自由度"的关键（用户要能自己加删文本文件），但必须挡住路径穿越与伪扩展名。
TEXT_EXT = (".md", ".txt", ".json")
KNOWLEDGE_DIR = "knowledge/"
_TEXT_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,40}\.(?:md|txt|json)$")
# 根目录固定文本名（**只用于白名单判定**；值不再是体积上限 —— 逐文件上限已取消，见 MAX_TEXT_TOTAL_BYTES）
TEXT_FILE_NAMES = frozenset({"preset.json", "core.md", "identity.md", "sms_samples.md", "opening.json"})
IMAGE_EXT = {".webp", ".jpg", ".jpeg", ".png"}

# 错误码（01 文档 §三）
E_TOO_BIG = "CARD_TOO_BIG"
E_BAD_PATH = "CARD_BAD_PATH"
E_UNKNOWN_FILE = "CARD_UNKNOWN_FILE"
E_BAD_PRESET = "CARD_BAD_PRESET"
E_BAD_MANIFEST = "CARD_BAD_MANIFEST"
E_HASH_MISMATCH = "CARD_HASH_MISMATCH"
E_BAD_IMAGE = "CARD_BAD_IMAGE"
E_BOMB = "CARD_BOMB"
E_NOT_ZIP = "CARD_NOT_ZIP"
E_FORMAT_UNSUPPORTED = "CARD_FORMAT_UNSUPPORTED"
# V1 新增：列表离不开 thumb（06 §3.3）；只有**新格式提交**（payload 带 files/images）才强制，
# 存量卡与老草稿没有 thumb，走兼容通道（否则老数据全部无法安装/继续编辑）。
E_MISSING_THUMB = "CARD_MISSING_THUMB"


class CardError(Exception):
    """角色卡校验/构建失败。`code` 是稳定机读码，`message` 是给人看的中文。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── 摘要与元数据 ──────────────────────────────────────
def card_digest(files: dict) -> str:
    """整卡摘要：与快照 `_pack_digest` **完全相同**的口径（路径参与哈希、排序无关）。"""
    return _pack_digest((path, hashlib.sha256(blob).hexdigest()) for path, blob in files.items())


def file_entries(files: dict) -> list:
    """manifest.files[]：按路径排序的 {path, size, sha256}。"""
    return [{"path": p, "size": len(files[p]), "sha256": hashlib.sha256(files[p]).hexdigest()}
            for p in sorted(files)]


def normalize_category(raw) -> str:
    """未知/空分类回退「其他」（前向兼容：服务器加分类不能让旧客户端崩）。"""
    s = str(raw or "").strip()
    return s if s in CATEGORIES else DEFAULT_CATEGORY


# ── 图片：魔数 + 头部尺寸（纯 stdlib）────────────────────
def _png_size(b: bytes):
    if len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    return (int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big"))


def _jpeg_size(b: bytes):
    if len(b) < 4 or b[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(b[i + 2:i + 4], "big")
        # SOF0-3 / SOF5-7 / SOF9-11 / SOF13-15 携带尺寸
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(b[i + 5:i + 7], "big")
            w = int.from_bytes(b[i + 7:i + 9], "big")
            return (w, h)
        if seg <= 0:
            return None
        i += 2 + seg
    return None


def _webp_chunks(b: bytes):
    """按 RIFF 规则遍历 WebP 的 chunk，产出 `(fourcc, payload 起点, payload 长度)`。

    为什么要**自己走 chunk 表**：VP8X 只是**容器**，真实帧在后面的 VP8/VP8L chunk 里，
    两者尺寸可以不一致（verifier 的 W02：画布 64×64 + 内层 VP8 帧 128×128）。
    结构性错误（chunk 长度越界）直接停止遍历 —— 剩下的字节不可信，调用方会拿到"解不出尺寸"。
    """
    i, n = 12, len(b)
    while i + 8 <= n:
        fourcc = b[i:i + 4]
        size = int.from_bytes(b[i + 4:i + 8], "little")
        if i + 8 + size > n:
            break
        yield fourcc, i + 8, size
        i += 8 + size + (size & 1)          # RIFF：奇数长度补 1 字节对齐


def _webp_chunk_size(fourcc: bytes, p: bytes):
    """单个 chunk 的尺寸；不是认识的格式 / 结构不对 → None。"""
    if fourcc == b"VP8X":                    # 扩展格式：24bit 画布宽高（减一存储）
        if len(p) < 10:
            return None
        return (int.from_bytes(p[4:7], "little") + 1,
                int.from_bytes(p[7:10], "little") + 1)
    if fourcc == b"VP8 ":                    # 有损：帧头 16bit 宽高（低 14 位有效）
        i = p.find(b"\x9d\x01\x2a", 0, 16)   # 关键帧起始码（前面还有 3 字节 frame tag）
        if i < 0 or i + 7 > len(p):
            return None
        return (int.from_bytes(p[i + 3:i + 5], "little") & 0x3FFF,
                int.from_bytes(p[i + 5:i + 7], "little") & 0x3FFF)
    if fourcc == b"VP8L":                    # 无损：14bit 宽/高打包在一个小端整数里
        if len(p) < 5 or p[0] != 0x2F:
            return None
        bits = int.from_bytes(p[1:5], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _webp_parts(b: bytes):
    """拆出 `(VP8X 画布尺寸 or None, [内层帧尺寸…])`；结构不可信时给 `(None, [])`。"""
    if len(b) < 21 or b[:4] != b"RIFF" or b[8:12] != b"WEBP":
        return None, []
    canvas, frames = None, []
    for fourcc, off, size in _webp_chunks(b):
        s = _webp_chunk_size(fourcc, b[off:off + size])
        if not s or s[0] <= 0 or s[1] <= 0:
            continue
        if fourcc == b"VP8X":
            canvas = s
        elif fourcc in (b"VP8 ", b"VP8L"):
            frames.append(s)
    return canvas, frames


def _webp_canvas_conflict(b: bytes) -> bool:
    """画布容不下内层帧（文件自相矛盾）？—— 给**错误文案**用：这种文件真解码器也打不开。"""
    canvas, frames = _webp_parts(b)
    return bool(canvas and frames
                and any(f[0] > canvas[0] or f[1] > canvas[1] for f in frames))


def _webp_size(b: bytes):
    """WebP 尺寸：**VP8X 画布与内层 VP8/VP8L 帧都解析，按更严的判**（06 §3.4）。

    只读第一个 chunk（VP8X 画布）就 return 是**漏洞**：verifier 用真编码器造出
    「VP8X 画布 = 64×64 / 内层 VP8 帧 = 128×128」，只信画布就会把这种文件放行（W02 FAIL）。

    判法（按"最保守优先"）：
      1. **画布比任何内层帧都小 ⇒ 返回 None（拒绝）**。VP8X 规范要求画布必须容得下帧；
         实测这种文件连 Pillow 都打不开（`解码失败(OSError)`）—— 自相矛盾的文件不能信，
         更不能挑小的那个尺寸放行。
      2. 否则**取更大者**：画布 ≥ 帧时用画布（它才是呈现尺寸，也更严），
         只有裸 VP8/VP8L（无 VP8X）时用帧尺寸。
      3. 完全解析不出尺寸 ⇒ None（不信客户端）。
    """
    canvas, frames = _webp_parts(b)
    if frames:
        if canvas is not None:
            if any(f[0] > canvas[0] or f[1] > canvas[1] for f in frames):
                return None                    # 画布容不下帧 = 自相矛盾
            return canvas                      # 画布 ≥ 帧：画布更大，取更严的
        return max(frames, key=lambda s: (max(s), s[0] * s[1]))
    return canvas


def image_size(blob: bytes):
    """返回 (w, h)；无法识别返回 None。"""
    for fn in (_png_size, _jpeg_size, _webp_size):
        try:
            size = fn(blob)
        except Exception:      # 畸形头部不能让校验崩
            return None
        if size:
            return size
    return None


def _is_free_text(rel_path: str) -> bool:
    """自由命名文本：`character/` 根、`character/knowledge/` 或 `character/knowledge/<域>/` 下的
    `.md/.txt/.json`（06 §3.1：允许**一层分组** —— App 编辑器写的就是 `knowledge/<域>/x.md`）。

    兼容壳：只回 True/False（不抛）。要**指名道姓**的错误请用 `_free_text_rel()`。
    """
    try:
        return _free_text_rel(rel_path)
    except CardError:
        return False


# 每段（文件名 / 分组名）长度上限（★2026-10-02：原 40 → 64；用户载入自己的卡时被文件名规则误拒）
_TEXT_SEG_MAX = 64
_TEXT_EXT_RE = re.compile(r"^(?P<stem>.+)\.(?P<ext>md|txt|json)$", re.IGNORECASE)
# 说明：**不做严格白名单**，只做安全黑名单 —— 典型可用字符是
#   中文 / 字母 / 数字 / `-` / `_` / 空格 / `.` / `·` / `（）`（以及其它可打印字符）。
# 为什么不白名单：现在能用的名字（`Q&A.md`、`世界观：设定.md`、`v1.2 设定.md`…）不该被新规则
# 反过来拒掉 —— 用户这轮就是被"规则太紧"挡住的（2026-10-02 载入自己的官方卡时撞上）。
_ILLEGAL_SEG_CHARS = set("/\\")      # 段内不允许的分隔符（路径分隔只能用 `/`，且已按段切开）


def _check_seg(seg: str, what: str) -> None:
    """段级校验：不合法就抛**指名道姓**的人话（哪一段、哪个字符、当前长度）。"""
    if not seg:
        raise CardError(E_BAD_PATH, f"{what}为空（路径里有连续斜杠或结尾斜杠）")
    if len(seg) > _TEXT_SEG_MAX:
        raise CardError(E_BAD_PATH,
                        f"{what}过长：当前 {len(seg)} 字，上限 {_TEXT_SEG_MAX} 字（{seg[:24]}…）")
    for ch in seg:
        if ord(ch) < 32 or ord(ch) == 0x7F:
            raise CardError(E_BAD_PATH, f"{what}含控制字符 U+{ord(ch):04X}：{seg!r}")
        if ch in _ILLEGAL_SEG_CHARS:
            raise CardError(E_BAD_PATH,
                            f"{what}含非法字符 {ch!r}（路径分隔只能用 `/`，反斜杠不允许）：{seg}")
    if seg in (".", ".."):
        raise CardError(E_BAD_PATH, f"{what}不能是 {seg!r}（路径穿越）")
    if seg.startswith("."):
        raise CardError(E_BAD_PATH, f"{what}不能以 `.` 开头（隐藏文件/目录）：{seg}")
    if seg != seg.strip() or seg.endswith("."):
        raise CardError(E_BAD_PATH,
                        f"{what}首尾不能有空白、结尾不能是 `.`（Windows 会静默去掉 ⇒ 路径对不上）："
                        f"{seg!r}")


def _free_text_rel(rel_path: str) -> bool:
    """自由文本路径判定（**按段校验**，会抛指名道姓的 `CardError`）。

    - True  ⇒ 合法文本路径（根、`knowledge/<名>`、`knowledge/<域>/<名>`，扩展名 md/txt/json）；
    - False ⇒ **明显不是文本路径**（如 `assets/…`）⇒ 交给调用方走图片分支；
    - 看着像文本但不合法 ⇒ 抛错（深层分组/其它目录/伪扩展名/穿越/控制字符/超长…）。
    """
    if rel_path.startswith("assets/"):
        return False
    if rel_path.startswith(KNOWLEDGE_DIR):
        segs = rel_path[len(KNOWLEDGE_DIR):].split("/")
        # 穿越/空段**优先**报（否则深层路径会先撞"最多一层分组"，看不出真正的毛病）
        for _i, _s in enumerate(segs):
            if _s in (".", ".."):
                raise CardError(E_BAD_PATH, f"知识库路径第 {_i + 1} 段是 {_s!r}（路径穿越不允许）：{rel_path}")
            if not _s:
                raise CardError(E_BAD_PATH, f"知识库路径第 {_i + 1} 段为空（连续斜杠或结尾斜杠）：{rel_path}")
        if len(segs) > 2:
            raise CardError(E_BAD_PATH,
                            f"知识库最多一层分组：{rel_path}（只允许 knowledge/<名>.md 或 "
                            f"knowledge/<域>/<名>.md，当前 {len(segs)} 段）")
        if len(segs) == 2:
            _check_seg(segs[0], "知识库分组名")
        name = segs[-1]
        _check_seg(name, "知识库文件名")
    elif "/" in rel_path:
        raise CardError(E_UNKNOWN_FILE,
                        f"文本只能放在 character/ 根或 character/knowledge/ 下（可一层分组）：{rel_path}")
    else:
        name = rel_path
        _check_seg(name, "文本文件名")
    m = _TEXT_EXT_RE.match(name)
    if not m:
        raise CardError(E_UNKNOWN_FILE,
                        f"文本扩展名只允许 .md/.txt/.json（大小写不敏感）：{name}")
    stem = m.group("stem")
    if not stem or stem[0].isspace() or stem.startswith("."):
        raise CardError(E_BAD_PATH, f"文本名不能以空白或 `.` 开头：{name}")
    return True


def _classify_rel(rel_path: str) -> tuple:
    """白名单判定：返回 `(kind, 体积上限字节或 None)`；不在白名单抛 `CardError`。

    - 文本：根目录固定 5 个 + 根/knowledge/ 下自由命名的 `.md/.txt/.json` —— **不设逐文件上限**
      （2026-10-01 松绑：文字只受"单卡文字总量"约束，见 `MAX_TEXT_TOTAL_BYTES`）；
    - 图片：`assets/<槽位>.<ext>`，槽位见 `IMAGE_SPECS`（含老槽位 `cover`）—— **仍逐张限**。
    拒绝理由一律**中文可读**（前端直接透传给作者，verifier 也据此判断"正确拒绝"还是"崩了"）。
    """
    if rel_path in TEXT_FILE_NAMES or _free_text_rel(rel_path):
        return "text", None
    if rel_path.startswith("assets/"):
        parts = rel_path.split("/")
        if len(parts) != 2 or not parts[1]:
            raise CardError(E_UNKNOWN_FILE, f"图片只能直接放在 character/assets/ 下：{rel_path}")
        stem, ext = Path(parts[1]).stem, Path(parts[1]).suffix.lower()
        if ext not in IMAGE_EXT:
            raise CardError(E_BAD_IMAGE,
                            f"不支持的图片格式（只允许 png/jpg/jpeg/webp）：{rel_path}")
        if stem not in IMAGE_SPECS:
            # 表情包越界单独给"人话"：sticker-9 / sticker-0 与"随便一个槽位名"不是一回事
            m = re.fullmatch(r"sticker-(\d+)", stem)
            if m:
                raise CardError(E_TOO_BIG,
                                f"表情包槽位越界：{rel_path}"
                                f"（只允许 sticker-1 .. sticker-{len(STICKER_SLOTS)}）")
            raise CardError(E_UNKNOWN_FILE,
                            f"未知图片槽位：{rel_path}（可用槽位：{'、'.join(IMAGE_SLOTS)}）")
        return "image", IMAGE_SPECS[stem][0]
    raise CardError(E_UNKNOWN_FILE, f"角色卡不接受该文件：character/{rel_path}")


def classify(rel_path: str) -> str:
    """返回 'text' / 'image'；不在白名单内抛 CardError。"""
    return _classify_rel(rel_path)[0]


def thumb_name(files: dict) -> str | None:
    """卡里的 thumb 相对路径（如 `assets/thumb.webp`）；没有 thumb 返回 None。"""
    for rel in files:
        p = Path(rel)
        if rel.startswith("assets/") and p.stem == LIST_SLOT and p.suffix.lower() in IMAGE_EXT:
            return rel
    return None


def _check_image(rel_path: str, blob: bytes) -> None:
    """`assets/<槽位>.<ext>`：体积 + 魔数 + **服务端解析出的宽高** + 槽位尺寸上限。

    宽高一律从**图片头部**解析（PNG IHDR / JPEG SOFn / WebP VP8X|VP8|VP8L），
    **不信客户端声明**（06 §3.1）——客户端只负责压，服务端负责复核（06 §3.3）。
    """
    kind, limit = _classify_rel(rel_path)
    if kind != "image":
        raise CardError(E_UNKNOWN_FILE, f"不是图片槽位：{rel_path}")
    slot = Path(rel_path.split("/")[1]).stem
    if len(blob) > limit:
        raise CardError(E_TOO_BIG, f"{rel_path} 超过 {limit // 1024}KB 上限")
    size = image_size(blob)
    if not size:
        # 文案要能一眼看出是"画布与帧矛盾"（verifier 的 W02 形状），别一律报"魔数不对"
        if _webp_canvas_conflict(blob):
            raise CardError(E_BAD_IMAGE,
                            f"{rel_path} 的 WebP 画布小于内层帧（画布与帧矛盾，真解码器也打不开）")
        raise CardError(E_BAD_IMAGE, f"{rel_path} 不是可识别的图片（魔数/头部解析失败）")
    w, h = size
    if w <= 0 or h <= 0:
        raise CardError(E_BAD_IMAGE, f"{rel_path} 图片尺寸非法：{w}x{h}")
    if w * h > MAX_PIXELS:
        raise CardError(E_BAD_IMAGE, f"{rel_path} 像素数过大（{w}x{h}）")
    max_edge = IMAGE_SPECS[slot][1]
    if max_edge and max(w, h) > max_edge:
        raise CardError(E_BAD_IMAGE,
                        f"{rel_path} 尺寸 {w}x{h} 超过上限（长边 ≤{max_edge}px）")


def _check_manifest(m: dict) -> dict:
    """字段级校验与归一化；返回清洗后的 manifest（不改动入参）。"""
    if not isinstance(m, dict):
        raise CardError(E_BAD_MANIFEST, "manifest 不是 JSON 对象")
    try:
        fmt = int(m.get("format", 0))
    except (TypeError, ValueError):
        raise CardError(E_BAD_MANIFEST, "format 必须是整数")
    if fmt != CARD_FORMAT:
        raise CardError(E_FORMAT_UNSUPPORTED, f"不支持的卡格式版本：{fmt}（本端支持 {CARD_FORMAT}）")

    out = {"format": CARD_FORMAT}
    for key, limit in FIELD_MAX.items():
        if key == "category":
            continue
        val = str(m.get(key, "") or "").strip()
        if key in ("name", "char_name", "user_name", "desc") and not val:
            raise CardError(E_BAD_MANIFEST, f"缺必填字段：{key}")
        if len(val) > limit:
            raise CardError(E_BAD_MANIFEST, f"{key} 超过 {limit} 字上限")
        out[key] = val

    pid = str(m.get("id", "") or "").strip()
    from core.preset_parse import _PRESET_ID_RE
    if not _PRESET_ID_RE.fullmatch(pid):
        raise CardError(E_BAD_MANIFEST, f"id 非法（须匹配 ^[a-z0-9_-]{{1,32}}$）：{pid!r}")
    out["id"] = pid

    presentation = str(m.get("presentation", "") or "").strip()
    out["presentation"] = presentation if presentation in ("sticker", "narration", "none") else "sticker"
    out["category"] = normalize_category(m.get("category"))

    tags = m.get("tags") or []
    if not isinstance(tags, list):
        raise CardError(E_BAD_MANIFEST, "tags 必须是数组")
    clean_tags = []
    for t in tags:
        s = str(t or "").strip()
        if not s:
            continue
        if len(s) > MAX_TAG_LEN:
            raise CardError(E_BAD_MANIFEST, f"标签过长（≤{MAX_TAG_LEN} 字）：{s}")
        if s not in clean_tags:
            clean_tags.append(s)
    if len(clean_tags) > MAX_TAGS:
        raise CardError(E_BAD_MANIFEST, f"标签最多 {MAX_TAGS} 个（当前 {len(clean_tags)} 个）")
    out["tags"] = clean_tags

    author = m.get("author") if isinstance(m.get("author"), dict) else {}
    out["author"] = {
        "uid_hash": str(author.get("uid_hash", "") or "")[:64],
        "display": str(author.get("display", "") or "").strip()[:40],
        "official": bool(author.get("official")),
    }
    try:
        out["created_at"] = int(m.get("created_at") or 0)
    except (TypeError, ValueError):
        raise CardError(E_BAD_MANIFEST, "created_at 必须是整数")
    try:
        out["pack_schema"] = int(m.get("pack_schema", PRESET_SCHEMA))
    except (TypeError, ValueError):
        out["pack_schema"] = PRESET_SCHEMA
    return out


# ── 解析（接收方入口）──────────────────────────────────
def parse_card_zip(data: bytes, *, max_zip_bytes: int | None = None,
                   max_total_bytes: int | None = None,
                   max_text_total: int | None = MAX_TEXT_TOTAL_BYTES,
                   require_thumb: bool = False, is_admin: bool = False) -> dict:
    """校验并解析 card.zip。

    返回 {"manifest": dict, "files": {zip内路径: bytes}, "digest": str, "preset": dict}。
    任何不合规都抛 CardError（码见本模块顶部 E_*）。**不做任何落盘**（preset 校验用临时文件）。

    `require_thumb=True`：把"缺 thumb"判为不合规（06 §3.3）。**默认 False** —— 存量卡与老草稿
    没有 thumb，默认路径必须继续能解析（老数据不能坏）；新格式提交走
    `_card_zip_from_payload(..., require_thumb=True)` / `build_card_zip(require_thumb=True)`。

    `is_admin=True`（2026-10-01 管理员松绑）：**文字总量不设限**，且 zip/解压总量上限抬到
    `ADMIN_MAX_*`（"不限"的安全天花板，见常量注释）。显式传入 `max_*` 参数时以参数为准
    （`max_text_total=None` 亦表示不限）。
    """
    if max_zip_bytes is None:
        max_zip_bytes = ADMIN_MAX_ZIP_BYTES if is_admin else MAX_ZIP_BYTES
    if max_total_bytes is None:
        max_total_bytes = ADMIN_MAX_TOTAL_BYTES if is_admin else MAX_TOTAL_BYTES
    if is_admin:
        max_text_total = None
    if not isinstance(data, (bytes, bytearray)):
        raise CardError(E_NOT_ZIP, "数据不是字节流")
    data = bytes(data)
    if len(data) > max_zip_bytes:
        raise CardError(E_TOO_BIG, f"压缩包超过 {max_zip_bytes // 1024}KB 上限")
    if data[:4] not in (b"PK\x03\x04", b"PK\x05\x06"):
        raise CardError(E_NOT_ZIP, "不是 zip 压缩包")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise CardError(E_NOT_ZIP, "zip 已损坏或无法解析")

    # 第一遍：条目级检查（体积/数量/深度/压缩比）——先给出**准确**的错误码，
    # 再交给 _zip_safe_entries 做 zip slip 判定（它的 20MB/100MB 上限此时已不可能触发）。
    infos = [i for i in zf.infolist() if not i.is_dir()]
    total = 0
    text_total = 0
    card_files = 0
    seen = set()
    for info in infos:
        name = info.filename.replace("\\", "/")
        # 同名条目：ZipFile 按名取**首条**，重复条目能让"校验通过的内容"与"取出的内容"不一致，
        # 直接拒绝（白名单文件数本来就少，不存在合法的重复场景）。
        if name in seen:
            raise CardError(E_BAD_PATH, f"压缩包内含重复条目：{name}")
        seen.add(name)
        if name == MANIFEST_NAME:
            # manifest 本身不再单独限体积（2026-10-01 松绑）：它计入文字总量。
            total += info.file_size
            text_total += info.file_size
            continue
        # 路径安全前置判定（与 _zip_safe_entries 同语义，但先报出**准确**的错误码）：
        # 否则 `character/../../x` 会先撞白名单，报成"不接受该文件"，掩盖穿越事实。
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":") or ".." in name.split("/"):
            raise CardError(E_BAD_PATH, f"压缩包内含非法路径：{name}")
        if not name.startswith(CARD_ROOT):
            raise CardError(E_UNKNOWN_FILE, f"压缩包顶层只允许 {MANIFEST_NAME} 与 {CARD_ROOT}：{name}")
        rel = name[len(CARD_ROOT):]
        if not rel:
            continue
        # 空路径段（`character//x` → rel 以 / 开头）：rel 会被下游按路径拼接，必须拦在入口。
        if rel.startswith("/") or any(seg == "" for seg in rel.split("/")):
            raise CardError(E_BAD_PATH, f"路径含空段或绝对路径：{name}")
        card_files += 1
        if card_files > MAX_FILES:
            raise CardError(E_TOO_BIG,
                            f"卡内文件过多：当前 {card_files} 个，上限 {MAX_FILES} 个"
                            f"（文本总量另有 {MAX_TEXT_TOTAL_BYTES // 1024 // 1024}MB 上限）")
        if len([p for p in rel.split("/") if p]) > MAX_DEPTH:
            raise CardError(E_BAD_PATH, f"路径层级过深：{rel}")
        kind, limit = _classify_rel(rel)   # 不在白名单 → 这里就抛中文可读的错
        # 知识库**份数不限**（2026-10-01 松绑：原 MAX_KNOWLEDGE_FILES=6 已删）
        if limit is not None and info.file_size > limit:
            raise CardError(E_TOO_BIG, f"{rel} 超过 {limit // 1024}KB 上限")
        # 压缩比守卫**只对非文本**（图片/manifest）：文本已经受"单卡文字总量"约束 ⇒ 里面
        # 出现高压缩比是**正常现象**（markdown 表格/重复标题/批量样例轻松 >20:1），
        # 拿 zip 炸弹去拒它是**误杀**（2026-10-01 松绑实测：200KB 重复样本就是这么被拒的）。
        if kind != "text" and info.file_size > RATIO_MIN_BYTES and info.compress_size > 0:
            if info.file_size / info.compress_size > MAX_RATIO:
                raise CardError(E_BOMB, f"{rel} 压缩比异常（疑似 zip 炸弹）")
        total += info.file_size
        if kind == "text":
            text_total += info.file_size
            if max_text_total is not None and text_total > max_text_total:
                raise CardError(
                    E_TOO_BIG,
                    f"卡片文字总量超过上限：当前约 {text_total // 1024}KB，上限 "
                    f"{max_text_total // 1024}KB（知识库**份数与单文件大小不限**，只限总量；"
                    f"图片另有槽位上限）")
        if total > max_total_bytes:
            raise CardError(E_TOO_BIG, f"解压总量超过 {max_total_bytes // 1024}KB 上限")

    try:
        entries = _zip_safe_entries(zf)
    except ValueError as e:
        raise CardError(E_BAD_PATH, str(e))

    # 第二遍：真实读出（以实际字节数为准，不信中央目录声明）
    files: dict = {}
    actual = 0
    for name, info in entries:
        blob = zf.read(info)
        if len(blob) != info.file_size:
            raise CardError(E_BOMB, f"{name} 实际大小与声明不符（疑似 zip 炸弹）")
        actual += len(blob)
        if actual > max_total_bytes:
            raise CardError(E_TOO_BIG, f"解压总量超过 {max_total_bytes // 1024}KB 上限")
        if name != MANIFEST_NAME:
            files[name[len(CARD_ROOT):]] = blob

    if MANIFEST_NAME not in [n for n, _ in entries]:
        raise CardError(E_BAD_MANIFEST, f"缺少 {MANIFEST_NAME}")
    try:
        raw_manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except Exception as e:
        raise CardError(E_BAD_MANIFEST, f"{MANIFEST_NAME} 不是合法 UTF-8 JSON：{e}")
    manifest = _check_manifest(raw_manifest)

    # 内容校验：preset.json 交给**唯一权威解析器**
    preset_blob = files.get("preset.json")
    if preset_blob is None:
        raise CardError(E_BAD_PRESET, "缺少 character/preset.json")
    preset = _parse_preset_text(preset_blob, manifest["id"])
    if preset is None:
        raise CardError(E_BAD_PRESET, "preset.json 未通过解析（字段缺失或 id 与卡片 id 不一致）")

    # opening.json 必须是合法 JSON（既有读取端解析失败会静默降级为无开场，
    # 上传时必须拦住——否则作者以为发出去了）
    op = files.get("opening.json")
    if op is not None:
        try:
            json.loads(op.decode("utf-8"))
        except Exception as e:
            raise CardError(E_BAD_MANIFEST, f"opening.json 不是合法 JSON：{e}")

    # stickers.json：表情包标签映射（06 §3.1/§3.2）——key 必须是**本卡真实存在**的表情包文件名。
    # 拦住：指向不存在的文件、路径穿越（`../x` 找不到对应表情包自然被拒）、超长标签、非字符串。
    st_blob = files.get("stickers.json")
    if st_blob is not None:
        try:
            labels = json.loads(st_blob.decode("utf-8"))
        except Exception as e:
            raise CardError(E_BAD_MANIFEST, f"stickers.json 不是合法 UTF-8 JSON：{e}")
        if not isinstance(labels, dict):
            raise CardError(E_BAD_MANIFEST, "stickers.json 必须是 {文件名: 标签} 对象")
        sticker_names = {Path(rel).name for rel in files
                         if rel.startswith("assets/") and Path(rel).stem in STICKER_SLOTS}
        for key, val in labels.items():
            if key not in sticker_names:
                raise CardError(E_UNKNOWN_FILE,
                                f"stickers.json 指向不存在的表情包：{key}"
                                f"（本卡表情包：{'、'.join(sorted(sticker_names)) or '无'}）")
            if not isinstance(val, str):
                raise CardError(E_BAD_MANIFEST, f"stickers.json 的标签必须是字符串：{key}")
            if len(val.strip()) > MAX_STICKER_LABEL:
                raise CardError(E_BAD_MANIFEST,
                                f"表情包标签超过 {MAX_STICKER_LABEL} 字：{key} = {val.strip()[:20]!r}")

    # 逐文件校验（文本编码 / 图片魔数与尺寸）
    for rel, blob in files.items():
        kind = classify(rel)
        if kind == "image":
            _check_image(rel, blob)
        else:
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError:
                raise CardError(E_BAD_MANIFEST, f"{rel} 不是 UTF-8 文本")

    # 列表离不开 thumb（06 §3.3）：只有新格式提交才强制，存量卡/老草稿走兼容通道
    if require_thumb and thumb_name(files) is None:
        raise CardError(E_MISSING_THUMB,
                        "缺少列表缩略图 thumb（列表只发 thumb：请用 ≤320px、≤12KB 的 webp/jpg/png）")

    # 哈希一致性：manifest.files[] 与 digest 都必须对得上
    declared = raw_manifest.get("files")
    if declared is not None:
        if not isinstance(declared, list):
            raise CardError(E_BAD_MANIFEST, "files 必须是数组")
        got = {e["path"]: (e["size"], e["sha256"]) for e in file_entries(files)}
        if len(declared) != len(got):
            raise CardError(E_HASH_MISMATCH, "manifest.files 条目数与实际文件数不一致")
        for item in declared:
            if not isinstance(item, dict):
                raise CardError(E_BAD_MANIFEST, "files[] 元素必须是对象")
            p = str(item.get("path", ""))
            if p not in got:
                raise CardError(E_HASH_MISMATCH, f"manifest.files 列出的文件不存在：{p}")
            if int(item.get("size", -1)) != got[p][0] or str(item.get("sha256", "")) != got[p][1]:
                raise CardError(E_HASH_MISMATCH, f"文件哈希不一致：{p}")
    d = card_digest(files)
    if raw_manifest.get("digest") and str(raw_manifest["digest"]) != d:
        raise CardError(E_HASH_MISMATCH, "整卡摘要与内容不一致")

    return {"manifest": manifest, "files": files, "digest": d, "preset": preset}


def _parse_preset_text(blob: bytes, expect_id: str):
    """把 preset.json 字节交给 core.preset_parse 的权威解析器（它只接受 Path）。"""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
            f.write(blob)
            tmp = f.name
        return _parse_preset(Path(tmp), expect_id)
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


# ── 构建（发送方入口）──────────────────────────────────
def build_card_zip(manifest: dict, files: dict, *, now: int | None = None,
                   require_thumb: bool = False, is_admin: bool = False) -> bytes:
    """构建 card.zip。`files` 的键是 **character/ 相对路径**（如 'core.md'、'assets/cover.png'）。

    先补齐 manifest 的 files[]/digest，再打包，最后**回读一遍自校验**——
    绝不产出一张自己都解析不了的卡。
    `require_thumb=True`：新格式（V1 制卡）必须带列表缩略图；默认 False 兼容存量调用方。
    `is_admin=True`：管理员发布 ⇒ 文字总量不限（见 `parse_card_zip`）。
    """
    import time as _time
    files = {str(k).lstrip("/"): bytes(v) for k, v in files.items()}
    m = dict(manifest)
    m.setdefault("format", CARD_FORMAT)
    m.setdefault("pack_schema", PRESET_SCHEMA)
    m.setdefault("created_at", int(now if now is not None else _time.time()))
    m["files"] = file_entries(files)
    m["digest"] = card_digest(files)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(m, ensure_ascii=False, indent=1).encode("utf-8"))
        for rel in sorted(files):
            zf.writestr(CARD_ROOT + rel, files[rel])
    data = buf.getvalue()
    # 自校验：不合规直接抛，宁可不产出（管理员同一口径 —— 豁免只放宽"文字总量"这一项）
    parse_card_zip(data, require_thumb=require_thumb, is_admin=is_admin)
    return data


def collect_card_files(character_dir: Path, *, strict: bool = True) -> dict:
    """从磁盘上的 `character/` 目录收集卡文件。

    strict=True：白名单外有文件 → 抛 CARD_UNKNOWN_FILE（站内表单产出的目录必须干净）。
    strict=False：只挑白名单内的文件（用于"把已有本地包发到广场"时忽略 `prompts/`、`data/`
    等运行期目录 —— 注意 `knowledge/` 自 V1 起**在白名单内**，会被一起带上）。"""
    out: dict = {}
    root = Path(character_dir)
    for fp in sorted(root.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(root).as_posix()
        try:
            classify(rel)
        except CardError:
            if strict:
                raise
            continue
        out[rel] = fp.read_bytes()
    return out


def decode_data_url(s: str) -> bytes:
    """data:image/...;base64,xxx → 字节（前端表单直传封面时用）。"""
    if not isinstance(s, str) or not s.startswith("data:") or "," not in s:
        raise CardError(E_BAD_IMAGE, "图片数据不是合法的 data URL")
    head, _, payload = s.partition(",")
    if "base64" not in head:
        raise CardError(E_BAD_IMAGE, "图片 data URL 必须是 base64")
    try:
        return base64.b64decode(payload, validate=True)
    except Exception:
        raise CardError(E_BAD_IMAGE, "图片 base64 解码失败")
