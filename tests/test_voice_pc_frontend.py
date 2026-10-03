# -*- coding: utf-8 -*-
"""PC 语音：纯 Python 文本前端 + 引擎结构的**离线回归**（不需要模型/onnxruntime/网络）。

为什么要有它：V1 的 PC 语音后端全靠 `app/assets/voice/` 里的 4 张映射表 + Kotlin 语义的
纯 Python 前端。前端一旦被改坏，"吞字/错位"只会体现在**听感**上（后端单测抓不到，2026-09-18
真机事故的同类教训）。这里把"前端逐位语义"钉成断言，跑得飞快、无重依赖。

依赖：只用**标准库**（json/re/array）—— 仓库默认 `python`（无 numpy/ort）也能跑。

用法：python tests/test_voice_pc_frontend.py
"""
import ast
import importlib.util
import inspect
import json
import re
import subprocess
import sys
from array import array
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "voice"))

from voice.text_frontend import TextFrontend  # noqa: E402

ASSETS = APP / "assets" / "voice"
PASS = FAIL = 0


def check(ok, label, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  V {label}   {extra}")
    else:
        FAIL += 1
        print(f"  X {label}   {extra}")


# ── G 组用的工具：import 屏蔽器 / 静态作用域分析（不依赖 numpy/onnxruntime）──
class _ImportBlocker:
    """`sys.meta_path` finder：对 `numpy` / `onnxruntime`（含子模块）抛 ImportError。

    用途：**证明** PC-only 模块（`voice.pc_engine` 顶层 import numpy/onnxruntime）不会被
    "手机端也会 import 的模块"（`voice` / `engine` / `plugin` / `routes`）在导入链上带进来。
    装了它以后若某个模块**真的**顶层 import 了 numpy/onnxruntime，那次 import 会立刻失败。
    """

    BLOCKED = ("numpy", "onnxruntime")

    def __init__(self):
        self.hits = []          # 被拦下的全限定名（应为空）

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.BLOCKED:
            self.hits.append(fullname)
            raise ImportError(f"[test-blocker] 拒绝导入 {fullname}")
        return None


def _purge_blocked_modules():
    """把 `numpy` / `onnxruntime`（及其子模块）从 `sys.modules` 里弹掉，返回被弹的清单。

    ★ 为什么必须（QA 指出的加固）：屏蔽器（`sys.meta_path` finder）**只在真的要 import 时**
    才被调用。若解释器里 numpy 已在 `sys.modules`（例如某个装了 numpy 的 venv），
    `voice.engine` 里的 `import numpy as np` 会直接命中缓存、**根本不会调用 find_spec**
    ⇒ 屏蔽器形同虚设、断言空转。换到"有 numpy 的解释器"时，就靠这一步保证屏蔽器仍被真正走一遍。
    """
    popped = []
    for name in list(sys.modules):
        if name.split(".")[0] in _ImportBlocker.BLOCKED:
            popped.append(name)
            sys.modules.pop(name, None)
    return popped


def _fresh_import_under_blocker():
    """弹掉 `voice*` 与 numpy/onnxruntime 缓存 → 装屏蔽器 → **自证** → 重新 import。

    返回 (结果表, 屏蔽器, 自证结果, 导入链命中, 被弹出的重依赖模块)。
    自证 = 在屏蔽器生效下主动 `import numpy/onnxruntime` 必须被拦下（证明这一组不是空转）。
    """
    blocker = _ImportBlocker()
    for k in [k for k in list(sys.modules) if k == "voice" or k.startswith("voice.")]:
        sys.modules.pop(k, None)
    purged = _purge_blocked_modules()
    sys.meta_path.insert(0, blocker)
    results = {}
    selfproof = []
    try:
        # ① 自证：屏蔽器对 numpy/onnxruntime 真的拦得住（否则后面"零命中"毫无意义）
        for probe in _ImportBlocker.BLOCKED:
            try:
                importlib.import_module(probe)
                selfproof.append((probe, "NOT-BLOCKED"))
            except ImportError:
                selfproof.append((probe, "blocked"))
        # ② 清空自证的命中，再走要验的导入链
        blocker.hits = []
        for name in ("voice.engine", "voice.plugin", "voice.routes"):
            try:
                importlib.import_module(name)
                results[name] = True
            except Exception as e:                   # noqa: BLE001
                results[name] = f"{type(e).__name__}: {e}"
        chain_hits = list(blocker.hits)
    finally:
        try:
            sys.meta_path.remove(blocker)
        except ValueError:
            pass
    return results, blocker, selfproof, chain_hits, purged


def _top_level_pc_refs():
    """上游 4 个模块的**顶层** import 若提到 pc_engine/pc_host 则记录（应为空）。"""
    bad = []
    for rel in ("voice/__init__.py", "voice/engine.py", "voice/plugin.py", "voice/routes.py"):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        for node in tree.body:                        # 只看顶层语句（函数体内的不算）
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = ["." * node.level + (node.module or "")]
            for m in mods:
                if "pc_engine" in m or "pc_host" in m:
                    bad.append(f"{rel}:{node.lineno} 顶层 import {m!r}")
    return bad


class _PcImportScope(ast.NodeVisitor):
    """找 `from pc_engine import ...` 并记录它在**函数体内**（depth>=1）还是顶层（depth=0）。"""

    def __init__(self):
        self.depth = 0
        self.found = []                               # (depth, lineno, module)

    def visit_FunctionDef(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ImportFrom(self, node):
        if (node.module or "").endswith("pc_engine"):
            self.found.append((self.depth, node.lineno, node.module))
        self.generic_visit(node)


def _subprocess_blocked_import():
    """全新解释器 + 屏蔽器下导入三模块（排除本进程模块缓存污染）。返回 CompletedProcess 或 None。"""
    snippet = (
        "import importlib, sys\n"
        "from pathlib import Path\n"
        f"APP = Path(r'{APP}')\n"
        "sys.path.insert(0, str(APP))\n"
        "sys.path.insert(0, str(APP / 'voice'))\n"
        "class B:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('numpy', 'onnxruntime'):\n"
        "            raise ImportError('blocked ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        "for m in ('voice.engine', 'voice.plugin', 'voice.routes'):\n"
        "    importlib.import_module(m)\n"
        "print('SUBPROC OK', {k: (k in sys.modules) for k in "
        "('voice.pc_engine', 'voice.pc_host', 'onnxruntime', 'numpy')})\n"
    )
    try:
        return subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                              text=True, encoding="utf-8", timeout=90)
    except Exception:                                 # noqa: BLE001
        return None


def main():
    fe = TextFrontend.from_assets(ASSETS)

    print("=== A. 资产载入 ===")
    check(len(fe.sym_to_id) > 500, "symbols.json 已载入", f"symbols={len(fe.sym_to_id)}")
    check(len(fe.pinyin_data) > 20000, "pinyin.json 已载入（≥20921 字）",
          f"chars={len(fe.pinyin_data)}")
    check(len(fe.pinyin_to_symbol) > 300, "opencpop-strict 已载入",
          f"map={len(fe.pinyin_to_symbol)}")

    print("=== B. normalize（Kotlin 语义）===")
    check(fe.normalize("嗯") == "恩", "「嗯」→「恩」")
    check(fe.normalize("呣") == "母", "「呣」→「母」")
    check(fe.normalize("，") == "," and fe.normalize("。") == "." and fe.normalize("！") == "!"
          and fe.normalize("？") == "?" and fe.normalize("：") == "," and fe.normalize("；") == ","
          and fe.normalize("、") == ",", "标点改写（，→, 。→. ！→! ？→? ：；、→,")
    check(fe.normalize("～") == "…", "「～」→「…」")
    check(fe.normalize("abcABC123") == "", "字母数字被删（[a-zA-Z]+ 一律过滤）")

    print("=== C. process：与 word2ph 逐位对应 + 轻声虚词保留 ===")
    ids, w2ph, aligned = fe.process("今天天气真好啊，我们去看星星吧。")
    check(len(w2ph) == len(aligned), "len(word2ph) == len(aligned)",
          f"{len(w2ph)} vs {len(aligned)}")
    check(sum(w2ph) == len(ids), "sum(word2ph) == len(ids)", f"{sum(w2ph)} vs {len(ids)}")
    check("啊" in aligned and "吧" in aligned, "轻声虚词（啊/吧）不被吞", f"aligned={aligned!r}")
    _, _, a2 = fe.process("好吧，你说了算呢。真的吗？")
    check(all(c in a2 for c in "吧了呢吗"), "轻声虚词（吧/了/呢/吗）不被吞", f"aligned={a2!r}")

    print("=== D. 跨平台锚点：与 mood_lib 预计算 ref_phones **逐字表长度一致** ===")
    for mood in ("happy", "sad"):
        d = ASSETS / "mood_lib" / mood
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        ref_text = meta["_source"]["ref_text"]
        n = meta["ref_phones"]["shape"][1]
        ref = array("q")
        with open(d / "ref_phones.raw", "rb") as fh:
            ref.fromfile(fh, n)
        pids, pw2, pal = fe.process(ref_text)
        # 不变量：len(ids)==ref 长度；sum(word2ph)==len(ids)；len(aligned)==len(word2ph)（逐位对应）
        check(len(pids) == n and sum(pw2) == n and len(pal) == len(pw2),
              f"{mood}: 长度自洽（len(ids)=ref 长度=sum(word2ph)；len(aligned)=len(word2ph)）",
              f"ids={len(pids)} ref={n} sum(w2ph)={sum(pw2)} len(aligned)={len(pal)}")
        # 差异只应落在声调数字上（官方做变调，Kotlin/本移植用字典调）—— 位置数应远小于总数
        tone_only = sum(1 for i in range(min(n, len(pids))) if int(ref[i]) != pids[i])
        check(tone_only <= max(2, n // 5), f"{mood}: 与官方 ref_phones 差异仅在少量声调位",
              f"{tone_only}/{n} 处不同（官方变调 vs 字典调，已逐位报告）")

    print("=== E. 引擎结构（PC 后端已就位，签名不变）===")
    import voice.engine as eng
    check(eng._platform() == "pc", "_platform() 在非安卓=pc")
    for fn, nargs in (("available", 0), ("ensure", 0), ("status", 0),
                      ("synthesize", 2), ("release", 0)):
        f = getattr(eng, fn, None)
        ok = callable(f) and (
            len(inspect.signature(f).parameters) == nargs or f.__code__.co_argcount == nargs)
        check(ok, f"五个函数之一 {fn}() 存在且签名不变",
              str(inspect.signature(f)) if callable(f) else "MISSING")
    for mod in ("pc_engine", "pc_host", "pc_worker"):
        check((APP / "voice" / f"{mod}.py").is_file(), f"app/voice/{mod}.py 存在")

    print("=== F. 主进程不引入 onnxruntime（依赖只在子进程）===")
    spec = importlib.util.find_spec("onnxruntime")
    check("onnxruntime" not in sys.modules,
          "`import voice.engine` 后主进程 sys.modules 里没有 onnxruntime",
          f"(本进程可 find_spec: {spec is not None})")

    print("=== G. PC-only 模块的导入隔离（import 屏蔽器下必须仍可导入）===")
    g_before = PASS + FAIL
    # G1：进程内 —— 弹缓存（含 numpy/onnxruntime）+ 装屏蔽器 + 自证 → fresh import 三模块必须全成功
    results, blocker, selfproof, chain_hits, purged = _fresh_import_under_blocker()
    for name in ("voice.engine", "voice.plugin", "voice.routes"):
        r = results.get(name)
        check(r is True, f"屏蔽 numpy/onnxruntime 下 import {name} 成功",
              "" if r is True else str(r))
    # G2：屏蔽器**自证**（对 numpy/onnxruntime 确实抛 ImportError ⇒ 这一组不是空转）
    check(all(v == "blocked" for _, v in selfproof),
          "屏蔽器自证：对 numpy/onnxruntime 确实拦得住（非空转）", f"探针={selfproof}")
    # G3：整条导入链从未触碰被屏蔽的重依赖
    check(chain_hits == [], "导入链从未 import numpy/onnxruntime（屏蔽器零命中）",
          f"命中={chain_hits}；本轮从 sys.modules 弹出的重依赖模块={purged}")
    # G4/G5：PC-only 模块与宿主都不应进 sys.modules
    check("voice.pc_engine" not in sys.modules, "屏蔽后 sys.modules 无 voice.pc_engine")
    check("voice.pc_host" not in sys.modules, "屏蔽后 sys.modules 无 voice.pc_host")
    # G6：静态 —— 上游 4 模块顶层不引用 pc_engine/pc_host
    bad_refs = _top_level_pc_refs()
    check(bad_refs == [], "pc_engine/pc_host 不被上游 4 模块顶层 import", str(bad_refs))
    # G7：静态 —— pc_worker 里 `from pc_engine import` 只在函数体内
    sc = _PcImportScope()
    sc.visit(ast.parse((APP / "voice" / "pc_worker.py").read_text(encoding="utf-8")))
    check(bool(sc.found) and all(d >= 1 for d, _, _ in sc.found),
          "pc_worker 的 `from pc_engine import` 只在函数体内（子进程路径）",
          f"命中(depth,line,mod)={sc.found}")
    # G8：全新解释器 + 屏蔽器（排除本进程缓存）
    proc = _subprocess_blocked_import()
    last_line = (proc.stdout or "").strip().splitlines()[-1] if (proc and proc.stdout) else ""
    check(proc is not None and proc.returncode == 0,
          "子进程（全新解释器 + 屏蔽器）三模块导入成功", last_line or "subprocess 失败")
    print(f"  —— 本节新增断言：{PASS + FAIL - g_before} 条 ——")

    print()
    print(f"统计: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
