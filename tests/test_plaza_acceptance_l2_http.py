# -*- coding: utf-8 -*-
"""广场 V1 对抗验收 **L2 HTTP 层**（真实服务器进程；verifier 独占）

契约（唯一标准）：docs/设计/角色卡共创平台/06_制卡自由度与图片规格.md（含 §六/§七）。
本层证明三件事，全部走真 HTTP：
  ① **经过 HTTP 也照样被拒，且拒绝原因是中文可读的**（不是 500、不是静默成功）；
  ② **合法新格式能存草稿、能发布；旧 payload（core/identity/sms_samples）同样能存能发**；
  ③ **列表口径与流量**：列表 20 张 ≤60KB、只给 thumb 地址、单张 thumb ≤12KB、304 生效。

沙箱：临时数据根 + `server_app.py` 真进程 + 真账号 token（直接建库造会话，不走邮件流程）。
**不连外网、不碰真实 user_data、零模型费用** —— 需要"审核通过"才能发布的路径，按真实形状
把结论写进沙箱草稿（与 tests/test_plaza_server_e2e.py 同一手法，**产品闸门一行不改**）。

退出码：0 全过；1 有 FAIL；2 服务器没起来；3 无 FAIL 但有 PENDING；4 端口/环境异常。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _plaza_accept_frame import (  # noqa: E402
    check, human, finish, free_port, data_url, base_card, png_bytes, jpeg_min,
    THUMB_OK, AVATAR_OK, DISPLAY_OK, SANDBOX, USER_DIR, PLAZA_DIR, ROOT,
)
import gzip                                                                   # noqa: E402
import subprocess                                                             # noqa: E402

import db as srv_db                                                           # noqa: E402
from plaza import card_format as cf                                           # noqa: E402

# L1 里用到的撒谎样本在这里**用同一个构造器**重建（口径必须一致，别抄两份）
LIE_JPG = jpeg_min(800, 800, declared=(320, 213))
LIE_PNG = png_bytes(4000, 10, declared=(320, 10))

srv_db.init_db(USER_DIR / "firefly.db")
EXP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7 * 86400))
UID = srv_db.create_user("acc@qq.com", "h", "s", install_id="inst-" + "c" * 32, role="user")
TOK = "tok-acc"
srv_db.create_session(TOK, UID, "accept", EXP)

PORT = free_port()
check("L2-0", f"拿到空闲端口（bind(0)）{PORT}", PORT > 0, f"port={PORT}", layer="L2")
env = dict(os.environ)
env.update({"FIREFLY_PORT": str(PORT), "PYTHONUNBUFFERED": "1", "FIREFLY_NO_BROWSER": "1"})
log = open(SANDBOX / "server.log", "wb")
proc = subprocess.Popen([sys.executable, "server_app.py"], cwd=str(ROOT / "server"),
                        env=env, stdout=log, stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, headers=None):
    h = {"Authorization": "Bearer " + TOK}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:
        # 连接被中止等：**不要吞掉整轮验收**（第一版就崩在这里，后面一条都没验到）
        return 0, {}, f"<{type(e).__name__}: {e}>".encode("utf-8", "replace")


def knock(n_files, cid="acc_card"):
    """撑文件数的卡：n_files 个根目录自由文本 + thumb + 后端 preset。"""
    card = base_card(cid, images={"thumb": data_url(THUMB_OK)})
    card["files"] = [{"path": f"note{i}.md", "text": "x"} for i in range(n_files)]
    return card


try:
    ready = False
    for _ in range(80):
        s, _h, _b = req("GET", "/health")
        if s == 200:
            ready = True
            break
        time.sleep(0.5)
    check("L2-1", "真实服务器起来并可访问 /health", ready, f"port={PORT}", layer="L2")
    if not ready:
        print((SANDBOX / "server.log").read_text("utf-8", "replace")[-1500:], flush=True)
        sys.exit(2)

    print("\n== L2-2 走 HTTP：越界/超限照样 400 + 中文可读原因 ==")
    http_cases = [
        ("H01", "files[].path 越界 ../evil.md",
         base_card("h01", files=[{"path": "../evil.md", "text": "x"}]), True),
        ("H02", "缺 thumb（新格式）",
         base_card("h02", images={"avatar": data_url(AVATAR_OK)}), True),
        # ★ H03/H03b：**契约 §3.4 已裁决**（2026-10-01）—— IHDR/SOFn 是格式定义的**权威画布**，
        #   真解码器（Chrome + Pillow，我独立复跑过）读出的就是头部尺寸；超量数据只会被忽略，
        #   或让整图解码失败 ⇒ **不可能超额解码** ⇒ "只解析头部、不做像素级重解码"是正确行为。
        #   故这里改成**必须接受**（不是缺陷）；同时保留下方 H03c 的反证职责。
        ("H03", "§3.4 结案：JPEG（SOF 320、scan 塞 800×800 数据）**应被接受**",
         base_card("h03", images={"thumb": data_url(LIE_JPG)}), False),
        ("H03b", "§3.4 结案：PNG（IHDR 320×10、IDAT 塞 4000×10）**应被接受**",
         base_card("h03b", images={"thumb": data_url(LIE_PNG)}), False),
        # ★ 反证（保留）：真实 320px 但**头部**谎报 4000px ⇒ 判据落在头部解析上 ⇒ 必须拒。
        #   若这条被接受，说明判据看的不是头部尺寸（那才是真缺陷）。
        ("H03c", "★反证：真实 320px、头部谎报 4000px **必须被拒**（判据在头部）",
         base_card("h03c", images={"thumb": data_url(jpeg_min(320, 213, declared=(4000, 2667)))}),
         True),
        ("H04", "第 9 张表情包",
         base_card("h04", images={"thumb": data_url(THUMB_OK),
                                  "stickers": [data_url(png_bytes(32, 32, seed=i)) for i in range(9)]}),
         True),
        # ★2026-10-02 松绑：文件数上限 24 → 500 ⇒ 25 个现在**必须被接受**（用户现场是 33 个被旧值挡）
        ("H05", "25 个文件**通过**（上限已抬到 500）", knock(23, "h05"), False),
        ("H05b", "33 个文件**通过**（用户现场）", knock(31, "h05b"), False),
        ("H05c", "501 个文件被拒（新上限 500）", knock(499, "h05c"), True),
        ("H06", "知识库两层（knowledge/a/b/c.md）",
         base_card("h06", files=[{"path": "knowledge/a/b/c.md", "text": "x"}],
                   images={"thumb": data_url(THUMB_OK)}), True),
    ]
    for cid, name, card, want_reject in http_cases:
        s, _h, b = req("POST", "/plaza/api/draft", {"card": card})
        txt = b.decode("utf-8", "replace")
        if not want_reject:
            # 期望被**接受**（§3.4 结案项）：被拒就是矫枉过正，同样要看得见
            check(cid, f"HTTP 必须接受：{name}", s == 200,
                  f"status={s} body={txt[:100]!r}", layer="L2")
            continue
        readable = any(k in txt for k in ("请", "上限", "非法", "校验", "不支持", "缺少", "超过"))
        check(cid, f"HTTP 拒绝且原因可读：{name}", s == 400 and readable,
              f"status={s} body={txt[:90]!r}", layer="L2")

    print("\n== L2-3 走 HTTP：合法新格式能存草稿；旧 payload 能存能发布 ==")
    good_card = base_card("acc_good",
                          files=[{"path": "core.md", "text": "# 核心\n对抗验收"},
                                 {"path": "knowledge/世界观.md", "text": "# 世界观"},
                                 {"path": "knowledge/world/设定.md", "text": "# 分组知识库"}],
                          images={"thumb": data_url(THUMB_OK), "display": data_url(DISPLAY_OK),
                                  "avatar": data_url(AVATAR_OK),
                                  "stickers": [{"data": data_url(png_bytes(32, 32, seed=1)),
                                                "label": "开心"}]})
    s, _h, b = req("POST", "/plaza/api/draft", {"card": good_card})
    check("H07", "合法新格式卡（含分组知识库 + 带 label 表情包）能存草稿", s == 200,
          f"status={s} {b[:120].decode('utf-8', 'replace')!r}", layer="L2")

    s, _h, b = req("POST", "/plaza/api/draft",
                   {"card": base_card("acc_legacy", core="# 核心", identity="# 身份")})
    check("H08", "旧 payload（core/identity）能存草稿", s == 200,
          f"status={s} {b[:120].decode('utf-8', 'replace')!r}", layer="L2")

    # 让草稿"通过审核"：按**真实形状**把结论写进沙箱草稿（产品闸门一行不改）
    patched = 0
    for meta_fp in (PLAZA_DIR / "drafts").rglob("*.json"):
        zip_fp = meta_fp.with_suffix(".zip")
        if not zip_fp.is_file():
            continue
        m = json.loads(meta_fp.read_text(encoding="utf-8"))
        if m.get("id") not in ("acc_legacy", "acc_good"):
            continue
        digest = cf.parse_card_zip(zip_fp.read_bytes())["digest"]
        m["review"] = {"verdict": "pass", "risk": "low", "reasons": ["桩：合规"],
                       "model": "deepseek-stub", "digest": digest}
        m["status"] = "reviewed"
        meta_fp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        patched += 1
    check("H09", "按真实形状把审核结论写进沙箱草稿（2 张）", patched == 2,
          f"patched={patched}", layer="L2")

    s, _h, b = req("POST", "/plaza/api/publish", {"id": "acc_legacy"})
    check("H10", "旧格式草稿能**发布**", s == 200,
          f"status={s} {b[:120].decode('utf-8', 'replace')!r}", layer="L2")
    s, _h, b = req("POST", "/plaza/api/publish", {"id": "acc_good"})
    check("H11", "新格式草稿能**发布**", s == 200,
          f"status={s} {b[:120].decode('utf-8', 'replace')!r}", layer="L2")

    print("\n== L2-4 列表口径与流量（契约 §3.3 + §四 验收）==")
    s, h, b = req("GET", "/plaza/api/list?page=1&size=20")
    list_bytes = len(b)
    check("T01", f"列表 20 张响应 ≤60KB（实测 {human(list_bytes)}）",
          s == 200 and list_bytes <= 60 * 1024, f"status={s} bytes={list_bytes}", layer="L2")
    body_txt = b.decode("utf-8", "replace")
    bad_slots = [slot for slot in ("display", "sticker-1", "sticker-2", "cover")
                 if f'"slot={slot}"' in body_txt or f'"slot": "{slot}"' in body_txt]
    check("T02", "列表响应里**没有** display/sticker/cover 图片地址", not bad_slots,
          f"命中 {bad_slots}", layer="L2")
    items = (json.loads(body_txt).get("items") or []) if s == 200 else []
    leaked = [k for k in ("cover", "display") if any(it.get(k) for it in items)]
    check("T03", "列表条目不含非空 cover/display 字段（只给 thumb）", not leaked,
          f"命中字段 {leaked}", layer="L2")
    check("T04", "列表条目**含** thumb 地址（列表离不开它）",
          bool(items) and any(it.get("thumb") for it in items),
          f"items={len(items)} thumbs={[it.get('thumb') for it in items]}", layer="L2")
    no_thumb = [it["id"] for it in items if not it.get("thumb")]
    if no_thumb:
        print(f"      注：{len(no_thumb)} 张老卡在列表里没有 thumb 地址（{no_thumb}）"
              f" —— 老数据可见但无缩略图，这是兼容通道的设计取舍", flush=True)

    cid = next((it["id"] for it in items if it.get("thumb")), "acc_good")
    s, h, b = req("GET", f"/plaza/api/asset?id={cid}&slot=thumb")
    thumb_bytes = len(b)
    check("T05", f"单张 thumb ≤12KB（实测 {human(thumb_bytes)}）",
          s == 200 and 0 < thumb_bytes <= 12 * 1024, f"status={s} bytes={thumb_bytes}", layer="L2")
    check("T06", "thumb 带长缓存与 ETag（翻回上一页不重复花钱）",
          bool(h.get("ETag")) and "max-age" in (h.get("Cache-Control") or ""),
          f"etag={h.get('ETag')!r} cc={h.get('Cache-Control')!r}", layer="L2")
    s, _h, b = req("GET", f"/plaza/api/asset?id={cid}&slot=thumb",
                   headers={"If-None-Match": h.get("ETag") or ""})
    check("T07", "thumb 二次请求 304、正文 0 字节", s == 304 and len(b) == 0,
          f"status={s} bytes={len(b)}", layer="L2")

    s, _h, b = req("GET", f"/plaza/api/card?id={cid}")
    detail = json.loads(b.decode("utf-8")) if s == 200 else {}
    check("T08", "详情页才返回 display 地址（大图只在详情）",
          bool((detail.get("card") or {}).get("display")),
          f"card keys={sorted((detail.get('card') or {}).keys())[:14]}", layer="L2")
    check("T08b", "详情页返回 thumb（列表与详情同源）",
          bool((detail.get("card") or {}).get("thumb")),
          f"thumb={(detail.get('card') or {}).get('thumb')!r}", layer="L2")

    print("\n== L2-5 一屏 20 张的真实流量 ==")
    s, _h, b = req("GET", "/plaza/api/list?page=1&size=20")
    gz = len(gzip.compress(b, 6))
    check("T09", f"列表 20 张 gzip 后 {human(gz)}（浏览器实收口径）", gz <= 60 * 1024,
          f"{gz}B", layer="L2")
    one_screen = list_bytes + 20 * thumb_bytes
    check("T10", f"一屏 20 张合计（列表 + 20×thumb）{human(one_screen)}",
          one_screen <= 60 * 1024 + 20 * 12 * 1024, f"{one_screen}B", layer="L2")
    # 与 task-10 基线对比（基线 JSON 由 _harden/baseline_plaza_traffic.py 产出）
    base_fp = ROOT / "_harden" / "baseline_plaza_traffic.json"
    if base_fp.is_file():
        bl = json.loads(base_fp.read_text(encoding="utf-8"))
        before = bl.get("screen_cover_20")
        if before:
            check("T11", f"一屏图片流量相对改造前基线下降 "
                         f"{100 * (1 - (20 * thumb_bytes) / before):.1f}%"
                         f"（基线 {human(before)} → 实测 {human(20 * thumb_bytes)}）",
                  (20 * thumb_bytes) < before, f"baseline={before}", layer="L2")
    else:
        check("T11", "与 task-10 基线对比", False, f"缺基线文件 {base_fp}", pending=True, layer="L2")

    print("\n== L2-6 未登录闸门（写端点也不能裸奔）==")
    r = urllib.request.Request(BASE + "/plaza/api/list", headers={})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            anon = resp.status
    except urllib.error.HTTPError as e:
        anon = e.code
    except Exception as e:
        anon = f"<{type(e).__name__}>"
    check("T12", f"未登录读列表 → 401（实际 {anon}）", anon == 401, f"{anon}", layer="L2")

finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log.close()

sys.exit(finish({"layer": "L2"}))
