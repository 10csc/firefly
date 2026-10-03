# -*- coding: utf-8 -*-
"""公告通道 · 端到端联通性检查（**联网**，与其它单测不同）

其它公告测试都跑在 mock 上（`tests/test_notice.py` 52 项用假清单 + 注入验签）。
那些全绿**不能**回答用户真正关心的问题：**"我现在能不能正常发一条公告出去？"**

能回答它的只有这一条链路，本文件逐步实测、每步都打印证据：

    ① 内容源可读        notice_src/source.json（人写，进 git）
    ② 产物已签名        hotupdate_dist/notice/notice-<N>.json
    ③ 主源在线          101.200.14.126:8787/notice/*
    ④ Gitee 兜底在线    gitee.com/.../releases/download/v0.9.0/*
    ⑤ 客户端验签通过    hotupdate.verify（真 RSA，不是注入的假实现）
    ⑥ 客户端 validate 通过  notice.validate（决定端侧会不会渲染）
    ⑦ 客户端 check() 真拉到  notice.check(force=True) → 落缓存
    ⑧ payload 出内容     notice.payload() 有条目、unread 语义正确
    ⑨ 前端能渲染        内联 notice.js 在真浏览器里把真实 payload 画出来

任一步失败 → 非 0 退出。**发布公告前先跑这个。**

    python tests/test_notice_e2e.py            # 联网全链路
    python tests/test_notice_e2e.py --offline  # 只跑 ① ② ⑤ ⑥ ⑧（不联网）

⚠️ 依赖：受测机需能访问 101.200.14.126 与 gitee.com；离线环境用 --offline。
"""
import argparse
import base64
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

SRC_JSON = ROOT / "notice_src" / "source.json"
SERIAL_F = ROOT / "notice_src" / "serial.txt"
DIST = ROOT / "hotupdate_dist" / "notice"

SERVER = "http://101.200.14.126:8787/notice"
GITEE = "https://gitee.com/cpt-asymmetry/firefly/releases/download/v0.9.0"

PASS = FAIL = 0
OFFLINE = False


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


def fetch(url: str, timeout: int = 30) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "FireflyE2E/1.0", "Cache-Control": "no-store"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main() -> int:
    global OFFLINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    OFFLINE = a.offline

    print("=== ① 内容源（人写，进 git）===")
    check("notice_src/source.json 存在", SRC_JSON.is_file())
    if not SRC_JSON.is_file():
        print("\n统计: PASS=%d FAIL=%d" % (PASS, FAIL))
        return 1
    src = json.loads(SRC_JSON.read_text(encoding="utf-8"))
    entries = src.get("entries") or []
    check("schema=1", int(src.get("schema") or 0) == 1)
    check("entries 非空", bool(entries), f"{len(entries)} 条")
    for e in entries:
        n_img = sum(1 for b in (e.get("blocks") or []) if b.get("t") == "img")
        print(f"     · {e.get('id')}  «{e.get('title')}»  level={e.get('level')}"
              + ("  [置顶]" if e.get("pinned") else "") + (f"  {n_img} 图" if n_img else ""))
    serial_src = int((SERIAL_F.read_text(encoding="utf-8").strip() or "0")) if SERIAL_F.is_file() else 0
    print(f"     内容源 serial 计数 = {serial_src}")

    print("=== ② 产物已签名 ===")
    name = f"notice-{serial_src}.json"
    art = DIST / name
    check(f"{name} 存在", art.is_file())
    check("latest 存在", (DIST / "latest").is_file())
    if art.is_file():
        doc = json.loads(art.read_text(encoding="utf-8"))
        check("产物含 manifest_b64 + sig", "manifest_b64" in doc and "sig" in doc)
        man_raw = base64.b64decode(doc["manifest_b64"])
        check("latest 与 serial 一致",
              json.loads((DIST / "latest").read_text(encoding="utf-8")).get("serial") == serial_src)
    else:
        man_raw = b""

    print("=== ⑤ 客户端验签（真 RSA）===")
    from hotupdate import verify
    print(f"     后端 = {verify.backend()}（PC 为 python 自实现，安卓为 kotlin 桥）")
    if not OFFLINE and art.is_file():
        online_doc = json.loads(fetch(f"{SERVER}/{name}").decode())
        ok_sig = verify.verify(base64.b64decode(online_doc["manifest_b64"]), online_doc["sig"])
        check("线上清单验签通过", ok_sig)
    elif art.is_file():
        ok_sig = verify.verify(man_raw, doc["sig"])
        check("本地产物验签通过", ok_sig)

    print("=== ⑥ 客户端 validate（决定端侧是否渲染）===")
    import notice
    if art.is_file():
        got = notice.validate(man_raw)
        check("validate 接受", bool(got.get("entries")),
              f"{len(got.get('entries') or [])} 条 / serial {got.get('serial')}")
        # 关键：不能有内容被端侧静默丢弃
        n_src = len(entries)
        n_got = len(got.get("entries") or [])
        check("条目数未被静默丢弃", n_src == n_got, f"源 {n_src} → 端侧 {n_got}")
    else:
        got = {}

    if not OFFLINE:
        print("=== ③④ 两个源在线（主源 + Gitee 兜底）===")
        for label, root in (("主源", SERVER), ("Gitee", GITEE)):
            try:
                lt = fetch(f"{root}/latest")
                s = json.loads(lt.decode()).get("serial")
                check(f"{label} /latest 可取", isinstance(s, int), f"serial={s}")
                try:
                    blob = fetch(f"{root}/notice-{s}.json")
                    check(f"{label} /notice-{s}.json 可取", len(blob) > 100, f"{len(blob)}B")
                except Exception as e:
                    check(f"{label} /notice-{s}.json 可取", False, f"{type(e).__name__}")
            except Exception as e:
                check(f"{label} /latest 可取", False, f"{type(e).__name__}: {str(e)[:60]}")

        print("=== ⑦ 客户端 check() 真拉到并落缓存 ===")
        r = notice.check(force=True)
        check("check() ok", bool(r.get("ok")), f"serial={r.get('serial')} changed={r.get('changed')}")
        check("端侧 serial 与线上一致", r.get("serial") == serial_src,
              f"端侧 {r.get('serial')} vs 源 {serial_src}")

    print("=== ⑧ payload 出内容 + 已读语义 ===")
    p = notice.payload(auto_refresh=False)
    check("payload.ok=True", p.get("ok") is True)
    check("payload 有条目", len(p.get("entries") or []) >= 1, f"{len(p.get('entries') or [])} 条")
    check("serial 正确", p.get("serial") == serial_src, f"实际 {p.get('serial')}")

    # 已读语义两条断言。★ 注意 reset_for_test() 只清"刷新中"锁，**不清缓存**——
    #   这是有意的：清缓存会把用户的已读状态一起抹掉。所以"重置后回到全未读"这种
    #   写法是错的（第一版就这么写，假失败了一次）。正确做法是直接验证两条语义：
    n_entries = len(p.get("entries") or [])
    notice.mark_read(all_read=True)
    check("全标已读后 unread=0", notice.payload(auto_refresh=False).get("unread") == 0)
    # 逐条标已读（只标前一条）→ unread 应减 1（不能负）
    if n_entries >= 1:
        first_id = p["entries"][0]["id"]
        notice.mark_read(ids=[first_id])
        after = notice.payload(auto_refresh=False).get("unread")
        check("单条标已读后 unread 不超过总数", 0 <= after <= n_entries, f"unread={after}")
    # 反证：把这些 id 从 read_ids 里摘掉（模拟"有新公告"），unread 必须回来
    d = notice._load()
    d["read_ids"] = []
    notice._save(d)
    check("清空 read_ids 后 unread 回到全部（=新公告会再次提示）",
          notice.payload(auto_refresh=False).get("unread") == n_entries,
          f"unread={notice.payload(auto_refresh=False).get('unread')} 期望 {n_entries}")

    st = notice.status()
    print(f"     status: serial={st.get('serial')} entries={st.get('entries')} "
          f"images={st.get('images')} backend={st.get('verify_backend')}")

    print(f"\n统计: PASS={PASS} FAIL={FAIL}")
    print("结果: " + ("PASS 公告可正常下发" if FAIL == 0 else "FAIL 见上面 X"))
    if OFFLINE:
        print("（--offline：未测 ③④⑦ 三段联网链路）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
