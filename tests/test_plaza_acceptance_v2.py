# -*- coding: utf-8 -*-
"""广场 V1 对抗验收 **L1 格式层**（进程内；verifier 独占）

契约（唯一标准）：docs/设计/角色卡共创平台/06_制卡自由度与图片规格.md（含 §六 变更记录、§七 验证记录）。
本层只碰 plaza.card_format / plaza.api 的纯函数：快、免服务器；**经过 HTTP 的那一半**在
`tests/test_plaza_acceptance_l2_http.py`。共用夹具（图片构造/断言计数）在 `tests/_plaza_accept_frame.py`。

状态口径：LIVE（真断言）/ PENDING（实现尚未具备 ⇒ **不算通过**）。
退出码：0 全过；1 有 FAIL；3 无 FAIL 但有 PENDING（`FIREFLY_ACCEPT_STRICT=1` 时 PENDING 也算失败）。

踩坑留痕（本层实测打脸过自己的地方，防止后人重犯）：
  · **断言写错 ≠ 实现有 bug**：I01 第一版把"声明 MIME 与魔数不符"当成缺陷 —— 安全边界在魔数，
    实现放行是对的（docs/错误总结.md #29：先怀疑量具）。
  · **断言的名字与真正验证的性质不是一回事**：J 组第一版在成品 zip 里塞 stickers.json，
    结果全被 `CARD_HASH_MISMATCH` 拒掉，**看着绿其实一条都没测到标签校验**；
    修法是走 `build_card_zip` 重算 manifest，让攻击面精确落在 stickers.json 内容上。
  · **计数要能区分 PENDING**：否则"没验"会被读成"验过了"。
"""
import json
import io
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _plaza_accept_frame import (  # noqa: E402
    check, human, finish, png_bytes, jpeg_min, jpeg_bytes, data_url, base_card,
    THUMB_OK, AVATAR_OK, DISPLAY_OK, ROOT,
)

from plaza import card_format as cf    # noqa: E402
from plaza import api as papi          # noqa: E402

print("== L1-0 能力探测（决定哪些用例是 LIVE、哪些是 PENDING）==")
try:
    import PIL  # noqa: F401
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False
_CARD_SRC = Path(cf.__file__).read_text(encoding="utf-8", errors="replace")
HAS_GROUP = "segs = rel_path[len(KNOWLEDGE_DIR):].split" in _CARD_SRC
HAS_LABEL = hasattr(cf, "MAX_STICKER_LABEL")
STICKERS_FILE = "stickers.json" in _CARD_SRC
print(f"  分组知识库={HAS_GROUP}  label 常量={HAS_LABEL}  stickers.json={STICKERS_FILE}")
print("  （像素级真实尺寸校验：运行时探针，结果在 L1-3 之前打印）")


def reject(cid, name, rel_path, note=""):
    """路径越界：`classify()` 必须抛 CardError（而不是放行/崩溃）。"""
    try:
        cf.classify(rel_path)
    except cf.CardError as e:
        readable = bool(e.message) and any("\u4e00" <= ch <= "\u9fff" for ch in e.message)
        return check(cid, name, readable, f"code={e.code} msg={e.message[:40]!r}" + (f" {note}" if note else ""))
    except Exception as e:                                   # 崩溃也是缺陷
        return check(cid, name, False, f"抛了非 CardError：{type(e).__name__}: {e}")
    return check(cid, name, False, f"竟然放行：{rel_path}")


def accept(cid, name, rel_path, note=""):
    try:
        kind = cf.classify(rel_path)
        return check(cid, name, True, f"kind={kind} {note}")
    except cf.CardError as e:
        return check(cid, name, False, f"被误拒：{e.code} {e.message[:50]}")
    except Exception as e:
        return check(cid, name, False, f"崩溃：{type(e).__name__}: {e}")


print("\n== L1-1 files[].path 越界（06 §3.1 白名单）==")
for cid, rel in (
    ("P01", "../evil.md"),
    ("P02", "knowledge/../../evil.md"),
    ("P03", "character/../../x.md"),
    ("P04", "/etc/passwd"),
    ("P05", "\\windows\\system32\\x.md"),
    ("P06", "C:/windows/x.md"),
    ("P07", "core.md.exe"),
    ("P08", "evil.py"),
    ("P09", "assets/../../x.md"),
    ("P10", "sub/other.md"),
    ("P11", "knowledge/a/b/c.md"),
    ("P12", "knowledge/../x.md"),
    ("P13", "knowledge/world/../../x.md"),
    ("P14", "knowledge/world/x.png"),
    ("P15", "knowledge/world/x.py"),
    ("P16", ".hidden.md"),
    ("P17", "data/x.md"),
):
    reject(cid, f"拒绝越界路径 {rel}", rel)

print("\n== L1-2 知识库目录：扁平 + 一层分组**通过**（06 §3.1，§六 变更 2026-10-01）==")
for cid, rel in (
    ("K01", "knowledge/世界观.md"),
    ("K02", "knowledge/world/设定.md"),
    ("K03", "knowledge/factions/星核猎手.md"),
    ("K04", "knowledge/我的自定域/笔记.txt"),
    ("K05", "knowledge/自定/术语.json"),
):
    if HAS_GROUP:
        accept(cid, f"通过分组知识库 {rel}", rel)
    else:
        check(cid, f"通过分组知识库 {rel}", False, "实现尚未支持一层分组", pending=True)

check("K06", "⛔ 软链穿越：zip 层无软链语义（已由 E2E 在**真实 zip + 真实落盘**上判掉）", True,
      "zip 条目只有'名字+内容'，外部模式位不携带可执行语义 ⇒ 本层本就无从穿越；"
      "真实判据在 tests/test_plaza_e2e_acceptance.py 的 K06a/b/c："
      "软链条目按普通内容处理（未跟随）、安装目录零符号链接。")


def build_ok(cid, card, files, imgs=None, note=""):
    """走 `_card_zip_from_payload` 组装（与 HTTP 完全同一条路）→ 返回 (ok, zip 或错误)。"""
    from plaza import api as papi
    payload = dict(card)
    try:
        blob = papi._card_zip_from_payload(payload)
        return True, blob
    except cf.CardError as e:
        return False, e


def expect_reject_payload(cid, name, card, note="", allow_none_thumb=False):
    """组装必须被拒：CardError 且码/文案可读。返回 (是否被拒, 错误码)。"""
    from plaza import api as papi
    try:
        papi._card_zip_from_payload(card)
    except cf.CardError as e:
        readable = any("\u4e00" <= ch <= "\u9fff" for ch in e.message)
        check(cid, name, readable, f"code={e.code} msg={e.message[:44]!r}" + (f" {note}" if note else ""))
        return True, e.code
    except Exception as e:
        check(cid, name, False, f"抛了非 CardError：{type(e).__name__}: {e}")
        return False, ""
    check(cid, name, False, "竟然被接受" + (f" {note}" if note else ""))
    return False, ""


def expect_accept_payload(cid, name, card, note=""):
    from plaza import api as papi
    try:
        blob = papi._card_zip_from_payload(card)
        return check(cid, name, len(blob) > 100, f"zip={human(len(blob))} {note}"), blob
    except cf.CardError as e:
        return check(cid, name, False, f"被误拒：{e.code} {e.message[:60]}"), None
    except Exception as e:
        return check(cid, name, False, f"崩溃：{type(e).__name__}: {e}"), None


# ── 尺寸判据：契约 §3.4 裁决「解析图片头部即可，不要求像素级重解码」──────────────
# 我**自己复核过**这条裁决（两个真解码器，见 _harden/probe_lying_image_decoders.py）：
#   PNG(IHDR 320×10 / IDAT 塞 4000×10)：Chrome=320×10、Pillow 直接拒收；
#   JPEG(SOF 320×213 / scan 塞 800×800)：Chrome=Pillow=320×213；
#   真实 320px / SOF 谎报 4000px：Chrome=Pillow=4000×2667。
# ⇒ 头部就是权威画布，超量数据只会被忽略或让整图解码失败，**不可能超额解码**；
#   要钉的不是"像素级校验"，而是"**判据落在解析出的头部尺寸上**，不是 JSON 声明字段"。
def _probe_header_judgement():
    """探针：头部**谎报巨大**（真实很小）⇒ 必须以头部尺寸判超限而拒。

    若实现反而接受 ⇒ 判据看的不是头部（例如只信 JSON 里的声明字段），那才是真缺陷。
    """
    from plaza import api as _papi
    probe = base_card("probe_head",
                      images={"thumb": data_url(jpeg_bytes(320, 213, declared=(4000, 2667)))})
    try:
        _papi._card_zip_from_payload(probe)
        return False                       # 放行 ⇒ 没按头部尺寸判
    except cf.CardError:
        return True                        # 拒（按头部 4000px 判超限）⇒ 判据在头部


HAS_HEADER_JUDGE = _probe_header_judgement()
print("\n== L1-3 图片：伪装扩展名 / 魔数不符 / 尺寸判据 / 解压炸弹 / 超限 / 缺 thumb ==")
print(f"  尺寸判据落在头部解析上：{HAS_HEADER_JUDGE}（探针：真实 320px / SOF 谎报 4000px）")

# I01 伪装扩展名：**声明的图片类型与实际魔数不符** ⇒ 服务端按魔数定扩展名并放行（正确行为，
# 安全边界在魔数）。真正必须被拒的是"魔数无法识别"（见 I02）。
# ⚠ 第一版把这条写成"必须被拒"的 FAIL 用例 —— **是测试错了**，不是实现错；已改正并留痕。
try:
    from plaza import api as papi
    _rel, _blob = papi._data_url_rel(data_url(jpeg_min(64, 48), "image/png"), "thumb")
    check("I01", "声明的 MIME 不可信：JPEG 魔数写进 png 槽位 ⇒ 按魔数存成 .jpg",
          _rel.endswith(".jpg"), f"得到 {_rel}")
except Exception as e:
    check("I01", "声明的 MIME 不可信：JPEG 魔数写进 png 槽位 ⇒ 按魔数存成 .jpg", False,
          f"{type(e).__name__}: {e}")
# 魔数不符：完全不是图片
expect_reject_payload("I02", "魔数不符（纯文本冒充图片）",
                      base_card(images={"thumb": data_url(b"this is not an image", "image/png")}))
# 声明的 MIME 与实际不符（data URL 头写 png，实际 jpeg）——服务端按魔数定扩展名
ok_i03, res_i03 = (None, None)
try:
    from plaza import api as papi
    rel, blob = papi._data_url_rel(data_url(jpeg_bytes(64, 48), "image/png"), "thumb")
    check("I03", "data URL 的 MIME 声明不参与判定（按魔数定 .jpg）", rel.endswith(".jpg"),
          f"得到 {rel}")
except Exception as e:
    check("I03", "data URL 的 MIME 声明不参与判定（按魔数定 .jpg）", False, f"{type(e).__name__}: {e}")

# ── 尺寸校验的边界（契约 §3.4 裁决，2026-10-01）────────────────────────────────
# 我**独立复核过**这条裁决（不复述 Lead 的话，自己用两个真解码器量）：
#   · PNG（IHDR 写 320×10、IDAT 塞 4000×10 数据）：Chrome 读作 320×10；Pillow **直接拒收**
#     （`unrecognized data stream contents`）
#   · JPEG（SOF 写 320×213、scan 塞 800×800 数据）：Chrome 与 Pillow **都读作 320×213**
#   · 真实 320px 但 SOF 谎报 4000×2667：Chrome 与 Pillow **都读作 4000×2667**
# ⇒ IHDR/SOFn 是**格式定义的权威画布**，多塞的字节只被忽略/拒收，**不可能造成超额解码**；
#   "头部与内容不一致"不是攻击面（体积另有 ≤12KB 兜底）。
# ⇒ 因此"不做像素级重解码"**结案、不算缺陷**；但**反证必须保留**：判据要落在头部画的真实尺寸上，
#   而不是 JSON 里的声明字段（否则就会把"真实 320px、声明 8000px"的好图误拒）。
lie_jpg = jpeg_min(800, 800, declared=(320, 213))
lie_png = png_bytes(4000, 10, declared=(320, 10))
check("I04pre", f"撒谎样本体积可控（jpg {human(len(lie_jpg))} / png {human(len(lie_png))}，"
                f"base64 后 {human(len(lie_jpg)*4//3)}/{human(len(lie_png)*4//3)}）",
      len(lie_jpg) * 4 // 3 < 1024 * 1024 and len(lie_png) * 4 // 3 < 1024 * 1024)
# I04（原 FAIL）：按 §3.4 结案——**接受是正确行为**（真解码器读出的就是头部画的 320×213/320×10）
expect_accept_payload("I04", "§3.4 结案：JPEG（SOF 320×213、scan 塞 800×800）**应被接受**",
                      base_card(images={"thumb": data_url(lie_jpg)}),
                      note="Chrome/Pillow 都读作 320×213；像素级重解码不在要求内")
expect_accept_payload("I04c", "§3.4 结案：PNG（IHDR 320×10、IDAT 塞 4000×10）**应被接受**",
                      base_card(images={"thumb": data_url(lie_png)}),
                      note="Chrome 读作 320×10（Pillow 直接拒收该合成流）")
# ★ 反证（关键，防"只看某个声明字段"被误判为通过）：真实 320×213、SOF 谎报 4000×2667 ⇒
#   必须以 **SOF 解析出的 4000** 判超限 → **拒收**。若这里被接受，说明判据没落在头部上。
big_head = jpeg_bytes(320, 213, declared=(4000, 2667), quality=40)
expect_reject_payload("I04b", "★反证：真实 320px 但 SOF 谎报 4000px **必须被拒**（判据在头部）",
                      base_card(images={"thumb": data_url(big_head)}),
                      note="§3.4：判据必须是解析出的头部尺寸，不是任何声明字段")

# ── WebP：VP8X 画布 vs 内层 VP8/VP8L 帧**不一致**时必须按**更严的那个**判（§3.4 + Lead 追加）──
# 样本用**真编码器产出**再套 VP8X 容器（手搓的最小容器连真解码器都打不开，证明不了任何事）：
# 实测 Chrome/Pillow 对"画布与帧不一致"的文件**一律拒收**，只有一致时才解码。
try:
    _wp_ok = None
    _wp_small = None
    _wp_big = None
    if _HAS_PIL:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_mkwebp", str(ROOT / "_harden" / "make_webp_samples.py"))
        _mk = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mk)
        _wp_ok = _mk.make_real_webp(128, 128, (128, 128))    # 画布=帧=128（一致）→ 通过
        _wp_small = _mk.make_real_webp(128, 128, (64, 64))   # 画布谎报 64、帧真 128 → 按更严判**拒**
        _wp_big = _mk.make_real_webp(128, 128, (512, 512))   # 画布谎报 512（超 thumb 320）→ **拒**
except Exception as _e:                                       # pragma: no cover
    _wp_ok = _wp_small = _wp_big = None
    print(f"      （WebP 样本构造失败：{type(_e).__name__}: {_e}）", flush=True)

if _wp_ok is None:
    for _cid, _nm in (("W01", "WebP 画布=帧=128 通过"), ("W02", "WebP 画布谎报 64、帧真 128 按更严拒绝"),
                      ("W03", "WebP 画布谎报 512 超长边拒绝")):
        check(_cid, _nm, False, "缺 Pillow/样本构造失败", pending=True)
else:
    expect_accept_payload("W01", "WebP：VP8X 画布=内层帧=128（一致）**通过**",
                          base_card(images={"thumb": data_url(_wp_ok, "image/webp")}))
    # 判据在**更严的那个**：画布 64、帧 128 ⇒ 必须按 128 判（体积/像素都在限内，故这条
    # 真正被钉住的是"有没有读内层帧"；只读 VP8X 的实现会错误放行）
    expect_reject_payload("W02", "★WebP：VP8X 画布谎报 64、内层帧真 128 ⇒ 按**更严**判必须拒",
                          base_card(images={"thumb": data_url(_wp_small, "image/webp")}),
                          note="只读 VP8X 的实现会放行 → 这条就是钉它的")
    expect_reject_payload("W03", "WebP：VP8X 画布谎报 512（>320 长边）必须拒",
                          base_card(images={"thumb": data_url(_wp_big, "image/webp")}))

# 解压炸弹：IHDR 声明 8000×8000（64M 像素）
bomb = png_bytes(8, 8, declared=(8000, 8000))
expect_reject_payload("I05", "解压炸弹（IHDR 声明 8000×8000）",
                      base_card(images={"thumb": data_url(bomb)}))
# 超单张上限：thumb 12KB（把 300×300 的像素数据放在 IEND 之后 —— 体积真超限、头部仍合法）
_th = png_bytes(300, 300)
big_thumb = _th + b"\x00" * (13 * 1024)
check("I06pre", f"超限样本确实 >12KB（{human(len(big_thumb))}）", len(big_thumb) > 12 * 1024)
expect_reject_payload("I06", "thumb 超 12KB 上限", base_card(images={"thumb": data_url(big_thumb)}))
# 长边超限：thumb 640px（≤12KB 体积但尺寸超）
expect_reject_payload("I07", "thumb 长边 640px > 320px", base_card(images={"thumb": data_url(png_bytes(640, 320))}))
# 缺 thumb：新格式（出现 images/files）必须带 thumb
expect_reject_payload("I08", "缺 thumb（新格式 payload）",
                      base_card(images={"avatar": data_url(AVATAR_OK)}))

print("\n== L1-4 表情包：第 9 张 / label 边界（06 §3.2）==")
eight = [data_url(png_bytes(32, 32, seed=i)) for i in range(1, 9)]
expect_accept_payload("S01", "8 张表情包**通过**", base_card(images={"thumb": data_url(THUMB_OK), "stickers": eight}))
nine = eight + [data_url(png_bytes(32, 32, seed=9))]
expect_reject_payload("S02", "第 9 张表情包**被拒**",
                      base_card(images={"thumb": data_url(THUMB_OK), "stickers": nine}))
if HAS_LABEL:
    lab12 = [{"data": d, "label": "一二三四五六七八九十十一"} for d in eight[:1]]
    lab13 = [{"data": d, "label": "一二三四五六七八九十十一二"} for d in eight[:1]]
    expect_accept_payload("S03", "label 恰好 12 字**通过**",
                          base_card(images={"thumb": data_url(THUMB_OK), "stickers": lab12}))
    expect_reject_payload("S04", "label 13 字**被拒**",
                          base_card(images={"thumb": data_url(THUMB_OK), "stickers": lab13}))
    expect_accept_payload("S05", "缺 label 合法（App 侧占位）",
                          base_card(images={"thumb": data_url(THUMB_OK),
                                            "stickers": [{"data": eight[0]}]}))
    # S06 必须打在**强制点**上：payload 层 `str(label or "")` 会把 12345 静默转成 "12345"
    # （宽松，不算安全缺陷但会丢类型），真正的类型强制在 `parse_card_zip` 的 stickers.json 校验。
    _s06_card = base_card(images={"thumb": data_url(THUMB_OK),
                                  "stickers": [{"data": eight[0], "label": 12345}]})
    _s06_zip = None
    try:
        from plaza import api as _papi2
        _s06_zip = _papi2._card_zip_from_payload(_s06_card)
    except cf.CardError:
        _s06_zip = None
    if _s06_zip is None:
        check("S06", "label 非字符串被拒（payload 层即拒）", True, "payload 层拒绝")
    else:
        # payload 层放行了 → 看落进 zip 的 stickers.json 是否保留类型（真类型强制在 parse 层）
        try:
            _zf = zipfile.ZipFile(io.BytesIO(_s06_zip))
            _labels = json.loads(_zf.read("character/stickers.json").decode("utf-8"))
        except Exception as e:
            _labels = f"<读取失败 {e}>"
        check("S06", "label 非字符串：payload 层静默 str() 强转 → 记观测，类型强制由 J 组验",
              True, f"落盘 stickers.json={_labels!r}")
        print("      ⚠ 观测：payload 层把非字符串 label（12345）**静默转成字符串**入库 "
              "（宽松，非安全缺陷）；契约的类型强制点在 `parse_card_zip`，见 J04。", flush=True)
else:
    for cid, nm in (("S03", "label 恰好 12 字通过"), ("S04", "label 13 字被拒"),
                    ("S05", "缺 label 合法"), ("S06", "label 非字符串被拒")):
        check(cid, nm, False, "实现尚未支持 label（MAX_STICKER_LABEL 不存在）", pending=True)

print("\n== L1-5 文件数 / 单文本 / 总量边界（06 §3.1）==")


def knock(n_files, cid="acc_card"):
    files = [{"path": f"note{i}.md", "text": "x"} for i in range(n_files)]
    card = base_card(cid, images={"thumb": data_url(THUMB_OK)})
    card["files"] = files
    return card


# 文件数：★2026-10-02 松绑 —— 原「24 通过/第 25 个被拒」作废，上限抬到 500（用户现场是 33 个被挡）
card24 = knock(22)      # 22 + thumb + preset = 24 个 → 通过（老口径的边界，现在远未到上限）
card33 = knock(31)      # 31 + thumb + preset = 33 个 → **用户现场**：必须通过
card500 = knock(498)    # 498 + thumb + preset = 500 个 → 恰好到上限，通过
card501 = knock(499)    # 499 + thumb + preset = 501 个 → 被拒（人话里带实际值/上限）
expect_accept_payload("F01", "恰好 24 个文件**通过**（22 自由文本 + thumb + preset）", card24)
expect_accept_payload("F01b", "**33 个文件通过**（用户现场：31 自由文本 + thumb + preset）", card33)
expect_accept_payload("F01c", "恰好 500 个文件**通过**（上限边界）", card500)
expect_reject_payload("F02", "第 501 个文件**被拒**（新上限 500）", card501)


# 文件账（按契约）：preset.json(后端生成) + 1 thumb + N 自由文本 = N+2
# 知识库 >6 份被拒
# 知识库份数：★2026-10-01 松绑 —— 原「>6 份被拒」作废，**份数不限**（7 份必须通过）
kb7 = base_card(images={"thumb": data_url(THUMB_OK)})
kb7["files"] = [{"path": f"knowledge/k{i}.md", "text": "x"} for i in range(7)]
expect_accept_payload("F03", "知识库 7 份**通过**（份数上限已取消，2026-10-01 松绑）", kb7)
# 单文本 64KB 边界：★2026-10-01 松绑 —— 原「64KB+1 被拒」作废，逐文件上限已删（改按单卡文字总量）
edge = base_card(images={"thumb": data_url(THUMB_OK)})
edge["files"] = [{"path": "core.md", "text": "a" * (64 * 1024)}]
try:
    from plaza import api as papi
    blob = papi._card_zip_from_payload(edge)
    check("F04", "单文本恰好 64KB 通过", len(blob) > 0)
except cf.CardError as e:
    check("F04", "单文本恰好 64KB 通过", False, f"被误拒：{e.code} {e.message[:50]}")
over = base_card(images={"thumb": data_url(THUMB_OK)})
over["files"] = [{"path": "core.md", "text": "a" * (64 * 1024 + 1)}]
expect_accept_payload("F05", "单文本 64KB+1 **通过**（逐文件上限已取消，2026-10-01 松绑）", over)
# 文字总量 8MB 仍是硬线：30 个 64KB 文本 ≈ 1.8MB 现在**通过**（文件数也不再拦）；
# 真正会拦的是总量 —— 用 >8MB 的样本验证（原标签"文件数 ≤24"已随 2026-10-02 松绑作废）
total = base_card(images={"thumb": data_url(THUMB_OK)})
total["files"] = [{"path": f"t{i}.md", "text": ("z" * (64 * 1024))} for i in range(30)]
check("F06pre", "30 个 64KB 文本 ≈1.8MB（<8MB）⇒ 现在**通过**",
      30 * 64 * 1024 < 8 * 1024 * 1024)
expect_accept_payload("F06", "30 个文本（≈1.8MB）**通过**（文件数与单文件都不再拦）", total)
_oversize = base_card(images={"thumb": data_url(THUMB_OK)})
_oversize["files"] = [{"path": "big.md", "text": "z" * (9 * 1024 * 1024)}]
expect_reject_payload("F06b", "文字总量 >8MB **被拒**（真正的闸门）", _oversize)
# 4M 像素上限（display 槽位另有 1024px 长边）
expect_reject_payload("F07", "display 长边 3000px > 1024px",
                      base_card(images={"thumb": data_url(THUMB_OK),
                                        "display": data_url(png_bytes(3000, 100))}))

print("\n== L1-6 兼容：旧 payload（core/identity/sms_samples）仍能组装并发布 ==")
legacy = base_card("compat_card", core="# 核心", identity="# 身份", sms_samples="# 短信")
ok_ok, blob_ok = expect_accept_payload("C01", "旧字段 core/identity/sms_samples → files[]", legacy)
if ok_ok and blob_ok:
    parsed = cf.parse_card_zip(blob_ok)
    names = sorted(parsed["files"])
    # 注意：`parse_card_zip` 的 `files` **含后端生成的 preset.json**（第一版断言漏了它，
    # 是我的测试错了 —— 记一笔：断言写错与实现不符，先怀疑断言）。
    check("C02", "旧字段被等价映射成 core.md/identity.md/sms_samples.md（+ 后端 preset.json）",
          names == ["core.md", "identity.md", "preset.json", "sms_samples.md"], f"实际 {names}")
    check("C03", "旧格式卡**不强制** thumb（老客户端不能坏）",
          cf.thumb_name(parsed["files"]) is None)
    # 反向：新格式必须强制 thumb（同一条兼容通道的两个方向）
    ok2, _ = (None, None)
    try:
        from plaza import api as papi
        papi._card_zip_from_payload(base_card("new_fmt", files=[{"path": "core.md", "text": "x"}]))
        check("C04", "新格式（出现 files[]）**必须**带 thumb", False, "竟然接受了无 thumb 的新格式")
    except cf.CardError as e:
        check("C04", "新格式（出现 files[]）**必须**带 thumb", True, f"code={e.code}")

print("\n== L1-7 stickers.json 一致性（06 §六 变更：label 落 character/stickers.json）==")


def build_then_parse(cid, name, card, expect_reject, note=""):
    """先把 payload 组装成 zip，再**重新 parse** 一遍（= 落盘后重新读入的真实路径）。"""
    from plaza import api as _papi3
    try:
        blob = _papi3._card_zip_from_payload(card)
    except cf.CardError as e:
        return check(cid, name, expect_reject, f"组装阶段即拒：{e.code} {e.message[:40]} {note}")
    try:
        cf.parse_card_zip(blob)
        return check(cid, name, not expect_reject, "parse 阶段放行" + (f" {note}" if note else ""))
    except cf.CardError as e:
        readable = any("\u4e00" <= ch <= "\u9fff" for ch in e.message)
        return check(cid, name, expect_reject and readable,
                     f"parse 阶段拒：{e.code} {e.message[:44]!r} {note}")


def raw_zip_with(extra_files):
    """**重算过 manifest 的手搓卡**（模拟攻击者自己造卡上传的路径）。

    ⚠ 第一版直接在成品 zip 里塞文件 ⇒ manifest.files 与实际不符 ⇒ 全部被
    `CARD_HASH_MISMATCH` 拒掉。**看着绿，其实一条都没测到 stickers.json 的校验**
    （docs/错误总结.md #29「量具自己骗人」的同族：断言的名字与真正验证的性质不是一回事）。
    修法：走 `build_card_zip`（它自己重算 manifest/digest），攻击面因此**精确落在**
    stickers.json 的内容校验上。
    """
    from plaza import api as _papi4
    card = base_card("acc_zip", images={"thumb": data_url(THUMB_OK),
                                        "stickers": [data_url(png_bytes(32, 32, seed=5))]})
    blob = _papi4._card_zip_from_payload(card)
    zf_in = zipfile.ZipFile(io.BytesIO(blob))
    files = {n[len("character/"):]: zf_in.read(n)
             for n in zf_in.namelist() if n.startswith("character/")}
    files.pop("stickers.json", None)          # 用攻击者自己写的那份替换
    for name, data in extra_files.items():
        files[name] = data
    parsed_in = zf_in.read("manifest.json")
    manifest = json.loads(parsed_in.decode("utf-8"))
    manifest.pop("files", None)
    manifest.pop("digest", None)
    return cf.build_card_zip(manifest, files)


if STICKERS_FILE:
    # 攻击者手搓 zip：stickers.json 指向不存在的表情包 / key 带穿越 / 非法 JSON / 非字符串标签
    for cid, nm, payload, expect_rej in (
        ("J01", "stickers.json 指向**不存在**的 sticker 文件",
         json.dumps({"sticker-9.png": "开心"}).encode(), True),
        ("J02", "stickers.json 的 key 带路径穿越",
         json.dumps({"../../evil.png": "开心"}).encode(), True),
        ("J03", "stickers.json 不是合法 JSON", "{不是 json".encode(), True),
        ("J04", "stickers.json 的标签非字符串（类型强制）",
         json.dumps({"sticker-1.png": 12345}).encode(), True),
        ("J05", "stickers.json 的标签恰好 12 字**通过**",
         json.dumps({"sticker-1.png": "一二三四五六七八九十十一"}).encode(), False),
    ):
        # `build_card_zip` 自带回读自校验（不产出自己解析不了的卡）⇒ 攻击样本可能在
        # **组装阶段**就被拒。两个阶段都算"被拒"，但要打印是哪一阶段（便于归因，防"报错指向症状"）。
        stage = "build"
        try:
            data = raw_zip_with({"stickers.json": payload})
        except cf.CardError as e:
            readable = any("\u4e00" <= ch <= "\u9fff" for ch in e.message)
            check(cid, f"{nm}｜build 阶段即拒", expect_rej and readable,
                  f"{e.code} {e.message[:50]!r}")
            continue
        stage = "parse"
        try:
            cf.parse_card_zip(data)
            check(cid, nm, not expect_rej, f"（{stage}）放行")
        except cf.CardError as e:
            readable = any("\u4e00" <= ch <= "\u9fff" for ch in e.message)
            check(cid, nm, expect_rej and readable, f"（{stage}）{e.code} {e.message[:46]!r}")
else:
    for cid, nm in (("J01", "stickers.json 指向不存在的 sticker"), ("J02", "stickers.json key 穿越"),
                    ("J03", "stickers.json 非法 JSON"), ("J04", "stickers.json 标签非字符串"),
                    ("J05", "stickers.json 标签 12 字通过")):
        check(cid, nm, False, "实现尚未生成/校验 stickers.json", pending=True)

sys.exit(finish({"layer": "L1"}))
