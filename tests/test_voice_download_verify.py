# -*- coding: utf-8 -*-
"""语音模型下载器的 sha256 对账（2026-09-18 加）。

为什么加：下载链路原来只探 `Content-Length`，抓不到「半截文件 / 被中间设备篡改 /
服务端返回错误页却当成模型存下来」——872 MB 下完才发现是坏的最亏。
ModelScope 的仓库清单接口**免费返回每个文件的 Sha256**，所以下完就能逐个比对。

本测试全部**离线**：起一个本地 http.server 假冒仓库（清单 + 文件），把
`downloader.API_BASE` 指过去，跑真实的 `_run()` 全链路，不碰外网。
（真机外网那一段另有一次一次性实测：官方 Qwen 仓库 config.json 的 sha256 与 API 完全一致。）
"""
import hashlib
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# 沙箱：plugin_root() 走 FIREFLY_DATA_DIR → 临时目录（绝不碰真实 voice/ 与 user_data/）
_TMP = Path(tempfile.mkdtemp(prefix="firefly_test_voice_dl_"))
os.environ["FIREFLY_DATA_DIR"] = str(_TMP)

import modules.app_config as cfg          # noqa: E402
cfg.USER_DIR = _TMP / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

from voice import downloader as D         # noqa: E402
from voice import plugin as P             # noqa: E402

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))


# ── 本地假仓库 ─────────────────────────────────────
PAYLOAD = {f"m{i}.bin": bytes([i]) * (3000 + i) for i in range(1, 4)}
SHA = {n: hashlib.sha256(b).hexdigest() for n, b in PAYLOAD.items()}
STATE = {"tamper": False}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        if self.path.startswith("/api/v1/models/"):
            name = self.path.split("FilePath=")[-1]
            body = PAYLOAD.get(name, b"")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if "/repo/files" in self.path:
            data = {"Code": 200, "Data": {"Files": [
                {"Name": n, "Path": n, "Size": len(b), "Sha256": SHA[n], "IsLFS": False}
                for n, b in PAYLOAD.items()]}}
            raw = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if "FilePath=" in self.path:
            name = self.path.split("FilePath=")[-1]
            body = PAYLOAD.get(name)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            if STATE["tamper"]:
                body = body[:-1] + bytes([body[-1] ^ 0xFF])   # 内容改一个字节
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
D.API_BASE = f"http://127.0.0.1:{PORT}"
TPL = f"http://127.0.0.1:{PORT}/api/v1/models/ns/name/repo?Revision=master&FilePath={{file}}"

print("=== A. 清单 URL 推导 ===")
check("A1 标准模板 → 清单端点（Revision 沿用）",
      D._manifest_url(TPL) == f"http://127.0.0.1:{PORT}/api/v1/models/ns/name/repo/files?Revision=master",
      str(D._manifest_url(TPL)))
check("A2 模板带 Revision=v1 → 清单也带 v1",
      (D._manifest_url("https://x/api/v1/models/ns/name/repo?Revision=v1&FilePath={file}") or "")
      .endswith("Revision=v1"))
check("A3 非 ModelScope 形式的地址 → None（跳过校验，不阻断）",
      D._manifest_url("https://example.com/files/{file}") is None,
      str(D._manifest_url("https://example.com/files/{file}")))
check("A4 _url_for 支持 {file} 占位与裸目录两种写法",
      D._url_for(TPL, "a.bin").endswith("FilePath=a.bin")
      and D._url_for("https://h/dir/", "b.bin") == "https://h/dir/b.bin")
check("A5 ★ origin 从模板自身取（自建镜像不会跑去魔搭取清单）",
      (D._manifest_url("https://mirror.internal/api/v1/models/ns/name/repo?FilePath={file}") or "")
      .startswith("https://mirror.internal/"),
      str(D._manifest_url("https://mirror.internal/api/v1/models/ns/name/repo?FilePath={file}")))

print("=== B. 清单解析与容错 ===")
man = D._manifest(TPL)
check("B1 解析出全部文件的 sha256", {k: v["sha256"] for k, v in man.items()} == SHA,
      f"{len(man)} 项")
check("B2 sha256 已是小写十六进制",
      all(len(v["sha256"]) == 64 for v in man.values()))
check("B3 ★ 清单里同时带回 size（总大小靠它，不靠 HEAD —— 真机实测 HEAD 不给 Content-Length）",
      all(v["size"] == len(PAYLOAD[n]) for n, v in man.items()),
      str({n: v["size"] for n, v in man.items()}))
check("B4 清单拿不到（端口不通）→ 返回 {}（不抛异常、不阻断）",
      D._manifest("http://127.0.0.1:1/api/v1/models/ns/name/repo?FilePath={file}") == {})
check("B5 非 ModelScope 地址 → 直接 {} 且不发请求",
      D._manifest("https://example.com/dir/{file}") == {})

print("=== C. 全链路：校验通过 ===")
P.download_reset()
D._run(TPL, sorted(PAYLOAD))
md = D.paths.plugin_root() / "models"
st = P.download_status()
check("C1 三个文件都落盘", all((md / n).exists() for n in PAYLOAD), str(sorted(p.name for p in md.glob("*"))))
check("C2 内容与源一致", all((md / n).read_bytes() == b for n, b in PAYLOAD.items()))
check("C3 无 .part 残留", not list(md.glob("*.part")))
check("C4 状态：verify=on 且 3 个文件校验计数", st.get("verify") == "on" and st.get("verified") == 3,
      f"verify={st.get('verify')} verified={st.get('verified')}")
check("C5 ★ 总大小来自清单（非 0，且等于三文件之和）",
      st.get("total") == sum(len(b) for b in PAYLOAD.values()),
      f"total={st.get('total')} 期望={sum(len(b) for b in PAYLOAD.values())}")
check("C6 无错误、百分比 100", not st.get("error") and st.get("percent") == 100, str(st.get("error"))[:80])

print("=== C'. 百分比永不越界（真机 bug 回归）===")
# 真机现象：total=0 时原来传 `total or 1` → percent = 已下字节×100 → 出现 31435628400%
class _SpyDict(dict):
    """记录每次 update 后的快照（dict.update 不能 monkeypatch，只能换掉整个对象）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen = []

    def update(self, *a, **kw):           # noqa: D102
        super().update(*a, **kw)
        self.seen.append(dict(self))


for p in md.glob("*"):
    p.unlink()
P.download_reset()
_spy = _SpyDict(P._DL)
P._DL = _spy
try:
    D._run(TPL, sorted(PAYLOAD))
finally:
    P._DL = dict(_spy)
bad = [s.get("percent") for s in _spy.seen
       if isinstance(s.get("percent"), int) and not (0 <= s["percent"] <= 100)]
check("C'1 整个过程 percent 始终落在 0–100", not bad, f"越界值: {bad[:5]}")
check("C'2 total 一旦写入就是清单里的真值（不是 0 也不是 1）",
      any(s.get("total") for s in _spy.seen),
      str([s.get("total") for s in _spy.seen if "total" in s][:3]))

print("=== D. 全链路：内容被篡改 → 必须拒绝落盘 ===")
for p in md.glob("*"):
    p.unlink()
STATE["tamper"] = True
P.download_reset()
D._run(TPL, sorted(PAYLOAD))
st2 = P.download_status()
check("D1 报错里明确指出校验失败", "校验失败" in (st2.get("error") or ""), str(st2.get("error"))[:120])
check("D2 坏文件**没有**被写成正式文件", not any((md / n).exists() for n in PAYLOAD),
      str(sorted(p.name for p in md.glob("*"))))
check("D3 半截 .part 也被清掉（不留下次误当续传起点）", not list(md.glob("*.part")))
check("D4 verified 计数不增加", int(st2.get("verified") or 0) == 0, str(st2.get("verified")))

print("=== E. 全链路：已就绪文件跳过重下 ===")
STATE["tamper"] = False
for p in md.glob("*"):
    p.unlink()
for n, b in PAYLOAD.items():
    (md / n).write_bytes(b)          # 预先放好且内容正确
P.download_reset()
D._run(TPL, sorted(PAYLOAD))
st3 = P.download_status()
check("E1 内容正确 → 跳过下载并计入 verified", int(st3.get("verified") or 0) == len(PAYLOAD),
      str(st3.get("verified")))
check("E2 无错误", not st3.get("error"), str(st3.get("error"))[:80])

print("=== F. 全链路：已存在但内容错 → 删掉重下 ===")
(md / "m1.bin").write_bytes(b"corrupted")
P.download_reset()
D._run(TPL, ["m1.bin"])
check("F1 内容错的文件被重下成正确内容", (md / "m1.bin").read_bytes() == PAYLOAD["m1.bin"])
check("F2 无错误", not P.download_status().get("error"), str(P.download_status().get("error"))[:80])

srv.shutdown()
print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
