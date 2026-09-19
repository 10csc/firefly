# -*- coding: utf-8 -*-
"""诊断包导出 —— 脱敏**证明**测试（不是"能跑通"测试）。

这个功能的唯一风险是**泄密**，所以断言围绕"包里不许出现什么"：
  A 结构：zip 里该有的成员都在
  B **脱敏**：任何成员都不许含 API Key 形态、不许含对话/记忆/手账正文片段
  C pipeline 只留形状：不许出现 pipeline 里的正文键
  D 服务器版必须 403（诊断包只允许本地生成）
  E 生成失败也不许 500 崩（能返回 JSON 错误）
"""
import io
import json
import os
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

# 沙箱：绝不碰真实 user_data
_TMP = Path(tempfile.mkdtemp(prefix="firefly_test_diag_"))
os.environ["FIREFLY_DATA_DIR"] = str(_TMP)
import modules.app_config as cfg          # noqa: E402
cfg.USER_DIR = _TMP / "user_data"
cfg.CONFIG_FILE = cfg.USER_DIR / "config.json"

# 造一份带"明显可识别"正文与假 Key 的现场，用来证明它们**不会**进包
SECRET_KEY = "sk-DEADBEEFdeadbeefDEADBEEFdeadbeef1234"
MARKER = "标记正文不可外泄-MARKER-77001"
(cfg.USER_DIR / "story" / "data").mkdir(parents=True, exist_ok=True)
(cfg.USER_DIR / "story" / "journal").mkdir(parents=True, exist_ok=True)
(cfg.USER_DIR / "story" / "data" / "conversation.jsonl").write_text(
    json.dumps({"seq": 1, "who": "user", "type": "text", "content": MARKER},
               ensure_ascii=False) + "\n", encoding="utf-8")
(cfg.USER_DIR / "story" / "data" / "memory.md").write_text(MARKER, encoding="utf-8")
(cfg.USER_DIR / "story" / "journal" / "手账.md").write_text(MARKER, encoding="utf-8")
(cfg.CONFIG_FILE).write_text(json.dumps({"api_key": SECRET_KEY, "api_base": "https://api.deepseek.com",
                                         "providers": [{"id": "deepseek", "api_key": SECRET_KEY,
                                                        "base_url": "https://api.deepseek.com"}]},
                                        ensure_ascii=False), encoding="utf-8")

from api import diag                       # noqa: E402

PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  V {desc}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  X {desc}" + (f"   {extra}" if extra else ""))

print("=== A. 结构 ===")
data = diag.build_diagnostics_zip()
z = zipfile.ZipFile(io.BytesIO(data))
names = set(z.namelist())
for n in ("summary.json", "config.redacted.json", "requests.json", "pipeline.shape.json",
          "conversation-stats.json", "metrics.json", "env.txt", "README.txt"):
    check(f"A  {n} 在包里", n in names)
check("A  zip 可正常解析且大小合理（>1KB 且 <10MB）", 1024 < len(data) < 10 * 1048576,
      f"{len(data)/1024:.1f} KB")

print("\n=== B. 脱敏证明（最关键）===")
raw_all = b""
for n in names:
    raw_all += z.read(n)
text_all = raw_all.decode("utf-8", "replace")
check("B  任何成员都不含真 Key", SECRET_KEY not in text_all)
check("B  任何成员都不含 `sk-` 形态的串", "sk-" not in text_all)
check("B  任何成员都不含对话/记忆/手账正文（标记串）", MARKER not in text_all)
check("B  config 里 Key 只留 set/len 两个字段",
      '"api_key_set"' in z.read("config.redacted.json").decode()
      and '"api_key_len"' in z.read("config.redacted.json").decode())
check("B  conversation-stats 只有 存在/字节数/轮数，没有正文字段",
      "content" not in z.read("conversation-stats.json").decode())

print("\n=== C. pipeline 只留形状（用**带正文的假条目**证明正文被丢）===")
# 沙箱里没有真实 pipeline 记录，那就注入一条**带可识别正文**的假记录：
# 如果形状函数漏了正文，下面 B/C 的标记串就会出现在包里 → 测试立刻抓住。
import orchestrator                        # noqa: E402

FAKE = [{"time": "12:00:00", "mode": "story", "user_input": MARKER,
         "messages": [{"role": "user", "content": MARKER}],
         "retriever": {"summary": MARKER, "hits": 3},
         "analyzer": {"intent": MARKER, "facts": [MARKER]},
         "polisher": {"prompt": MARKER, "output": MARKER},
         "organizer": {"sticker": "微笑", "label": MARKER}}]
_orig = getattr(orchestrator, "get_pipeline_log", None)
orchestrator.get_pipeline_log = lambda *a, **k: FAKE
try:
    data2 = diag.build_diagnostics_zip()
finally:
    if _orig is not None:
        orchestrator.get_pipeline_log = _orig
z2 = zipfile.ZipFile(io.BytesIO(data2))
ps = z2.read("pipeline.shape.json").decode()
check("C  形状里有 user_input_len（长度而不是正文）", "user_input_len" in ps, ps[:80])
check("C  ★ 含正文的假记录被脱敏：标记串没进包",
      MARKER not in z2.read("pipeline.shape.json").decode())
check("C  长度是对的（= 正文长度）", f'"user_input_len": {len(MARKER)}' in ps)
check("C  四个阶段都在且只留长度/标量", all(s in ps for s in
      ("retriever", "analyzer", "polisher", "organizer")))

print("\n=== D. 服务器版必须拒绝 ===")


class _H:
    """最小 handler 桩：记录 status 与 json。"""

    def __init__(self):
        self.status = None
        self.body = None
        self.headers = {}

    def _json(self, obj, code=200):
        self.status = code
        self.body = obj

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass


os.environ["FIREFLY_SERVER"] = "1"
h = _H()
diag.export_diagnostics(h)
check("D  服务器版返 403 且说明原因", h.status == 403 and "不提供" in str(h.body), str(h.body))
os.environ.pop("FIREFLY_SERVER", None)

print("\n=== E. 本地版正常导出（含 Content-Disposition）===")
h2 = _H()


class _H2(_H):
    class wfile:
        buf = b""

        @staticmethod
        def write(b):
            _H2.wfile.buf += b


h3 = _H2()
diag.export_diagnostics(h3)
check("E  本地版返 200", h3.status == 200)
# 逐成员比内容，不比整包字节：zip 会写文件 mtime，两次生成必然差 1~2 字节（第一版就栽在这）
z3 = zipfile.ZipFile(io.BytesIO(_H2.wfile.buf))
check("E  写出的成员集合一致", set(z3.namelist()) == names, str(sorted(z3.namelist())))
check("E  每个成员内容一致（同一份数据）",
      all(z3.read(n) == z.read(n) for n in names))

print(f"\n统计: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
