# -*- coding: utf-8 -*-
"""PC 语音引擎 —— **独立子进程 worker**。

形态与理由（照搬 `FireflyVoiceResearch/_proto/engine_worker.py` 的结论）：
  ① 单线程 HTTP 下推理会阻塞整个 API 十几秒；② 多线程 HTTP 下原生推理可能崩溃；
  ③ 代码里 release 掉 session，RSS 只降几 MB —— **进程杀掉才是真的把内存还给系统**。
  所以引擎放独立执行体：空闲自退、崩溃隔离、内存真归还。

协议：stdin 收一行 JSON、stdout 回一行 JSON。
★ stdout 被严格隔离：**任何模块的 print 都走 stderr**，协议输出走 dup 出来的原始 fd ——
  否则 onnxruntime / numpy 的底层打印会污染协议流。

配置：由宿主（`pc_host.py`）通过环境变量 `FIREFLY_VOICE_CFG`（JSON）注入
  {models_dir, assets_dir, mood_lib_dir, moods[], bert_file,
   steps, intra_op_threads, inter_op_threads, ort_sequential}。
  （后四项为 V4 提速杠杆，省略时分别取 4 / 0 / 0 / False = 与手机同档的基线）。
  子进程**不 import `voice` 包**（自包含），只 import 同目录的 `pc_engine`。
"""
import base64
import json
import os
import sys
import threading
import time
import traceback

# ── stdout 隔离（必须在 import 任何会打印的模块之前做）──────────────
_REAL_STDOUT_FD = os.dup(1)          # 保存真正的 stdout
os.dup2(2, 1)                        # fd1 → stderr：所有 print 进 stderr
PROTO = os.fdopen(_REAL_STDOUT_FD, "w", encoding="utf-8", buffering=1)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_START = time.time()


# ── 自身内存三口径（psapi，与 docs/设计/PC端语音/04 §1 同口径；无需 psutil）──
def _make_self_mem():
    """返回一个 `-> {priv_mb, ws_mb, peak_ws_mb}` 的读取器（非 Windows 返回空 dict）。

    ★ 为什么由 worker **自报**而不是父进程跨进程读：实测本机跨进程
      `OpenProcess + GetProcessMemoryInfo` 读到的是错的（子进程已触碰 400MB，父进程只读到 6MB），
      而**进程内** `GetCurrentProcess` 读取正确（同款代码在进程内读到 429MB）。故自报。
    """
    if os.name != "nt":
        return lambda: {}
    try:
        import ctypes
        import ctypes.wintypes as _w

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = _w.HANDLE
        psapi = ctypes.WinDLL("psapi")

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", _w.DWORD), ("PageFaultCount", _w.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        psapi.GetProcessMemoryInfo.argtypes = [_w.HANDLE, ctypes.POINTER(_PMC), _w.DWORD]
        psapi.GetProcessMemoryInfo.restype = _w.BOOL

        def read():
            c = _PMC()
            c.cb = ctypes.sizeof(c)
            if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return {}
            return {"priv_mb": round(c.PagefileUsage / 1048576, 1),
                    "ws_mb": round(c.WorkingSetSize / 1048576, 1),
                    "peak_ws_mb": round(c.PeakWorkingSetSize / 1048576, 1)}
        return read
    except Exception:
        return lambda: {}


_self_mem = _make_self_mem()
_PEAK_MEM = {"priv_mb": 0.0, "ws_mb": 0.0, "peak_ws_mb": 0.0}
_stop_sampler = threading.Event()


def _sampler():
    while not _stop_sampler.is_set():
        m = _self_mem()
        for k in _PEAK_MEM:
            _PEAK_MEM[k] = max(_PEAK_MEM[k], float(m.get(k, 0.0)))
        time.sleep(0.1)


def _mem_report() -> dict:
    return {"now": _self_mem(), "peak": {k: round(v, 1) for k, v in _PEAK_MEM.items()}}



def _send(obj: dict) -> None:
    PROTO.write(json.dumps(obj, ensure_ascii=False) + "\n")
    PROTO.flush()


def _log(msg: str) -> None:
    print(f"[voice-worker {os.getpid()}] {msg}", file=sys.stderr, flush=True)


def _cfg() -> dict:
    raw = os.environ.get("FIREFLY_VOICE_CFG") or "{}"
    c = json.loads(raw)
    c.setdefault("moods", ["happy", "sad"])
    c.setdefault("bert_file", "firefly_bert_int8.onnx")
    c.setdefault("steps", 4)                # V4 L1：CFM 欧拉步数（默认 4 = 与手机同档）
    c.setdefault("intra_op_threads", 0)     # V4 L2：0 = ORT 默认（不改）
    c.setdefault("inter_op_threads", 0)
    c.setdefault("ort_sequential", False)
    # W1 零质量代价杠杆（默认值 = 与 V1 行为一致：arena/图优化档不覆盖；ref 缓存开；不预热）
    c.setdefault("arena", None)
    c.setdefault("graph_opt", None)
    c.setdefault("bert_graph_opt", None)
    c.setdefault("ref_cache", True)
    c.setdefault("warmup", False)
    return c


def _build_engine(cfg: dict):
    """延迟 import onnxruntime/numpy（装载失败要能作为 JSON 错误返回，而不是把进程弄死）。"""
    try:
        from pc_engine import PcVoiceEngine
    except Exception:
        # 极端兜底：相对导入（被当作包模块 dispatch 时）
        from .pc_engine import PcVoiceEngine      # type: ignore
    arena = cfg.get("arena", None)
    return PcVoiceEngine(
        models_dir=cfg["models_dir"], assets_dir=cfg["assets_dir"],
        mood_lib_dir=cfg["mood_lib_dir"], moods=cfg["moods"],
        bert_file=cfg["bert_file"], steps=int(cfg.get("steps", 4)),
        intra_op_threads=int(cfg.get("intra_op_threads", 0)),
        inter_op_threads=int(cfg.get("inter_op_threads", 0)),
        ort_sequential=bool(cfg.get("ort_sequential", False)),
        arena=(None if arena is None else bool(arena)),
        graph_opt=cfg.get("graph_opt", None),
        bert_graph_opt=cfg.get("bert_graph_opt", None),
        ref_cache=bool(cfg.get("ref_cache", True)),
        warmup=bool(cfg.get("warmup", False)))


def _echo(eng) -> dict:
    """回显引擎当前生效的杠杆取值 + 缓存/预热计数（供宿主与 W1 实测脚本确认"无变量漂移"）。"""
    out = {}
    try:
        out.update(eng.opts())
    except Exception:
        out["steps"] = getattr(eng, "steps", 4)
    out["warmup_seconds"] = getattr(eng, "warmup_seconds", 0.0)
    out["ref_cache_hits"] = getattr(eng, "ref_cache_hits", 0)
    out["ref_cache_misses"] = getattr(eng, "ref_cache_misses", 0)
    out["stage_calls"] = getattr(eng, "stage_calls", 0)
    return out


def main() -> int:
    _log("启动，等待请求")
    threading.Thread(target=_sampler, daemon=True, name="voice-mem-sampler").start()
    cfg = _cfg()
    eng = None                     # 懒加载
    load_error = ""

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        t0 = time.time()
        try:
            req = json.loads(line)
        except Exception as e:
            _send({"ok": False, "error": f"JSON 解析失败: {e}"})
            continue
        cmd = req.get("cmd", "")
        try:
            if cmd == "ping":
                _send({"ok": True, "pid": os.getpid(), "loaded": bool(eng and eng.loaded),
                       "uptime_s": round(time.time() - _START), "mem": _mem_report()})

            elif cmd == "status":
                if eng is None:
                    eng = _build_engine(cfg)
                ok, why = eng.available()
                _send({"ok": True, "pid": os.getpid(), "available": ok,
                       "reason": ("" if ok else why), "loaded": bool(eng.loaded),
                       "load_seconds": (eng.load_seconds if eng.loaded else 0.0),
                       "steps": getattr(eng, "steps", 4),
                       "moods": eng.moods, "load_error": load_error,
                       "uptime_s": round(time.time() - _START), "mem": _mem_report()})

            elif cmd == "load":
                if eng is None:
                    eng = _build_engine(cfg)
                if eng.loaded:
                    _send({"ok": True, "load_seconds": eng.load_seconds, "cached": True,
                           "steps": getattr(eng, "steps", 4),
                           "moods": eng.moods, "opts": _echo(eng), "mem": _mem_report()})
                else:
                    secs = eng.load()
                    _send({"ok": True, "load_seconds": secs, "cached": False,
                           "steps": getattr(eng, "steps", 4),
                           "moods": eng.moods, "opts": _echo(eng), "mem": _mem_report()})

            elif cmd == "synthesize":
                if eng is None:
                    eng = _build_engine(cfg)
                wav, info = eng.synthesize(req.get("text", ""), req.get("mood", ""))
                if wav is None:
                    _send({"ok": False, "error": info.get("error", "合成失败"),
                           "tokens": info.get("tokens"), "guard": info.get("guard", ""),
                           "mem": _mem_report()})
                else:
                    _send({"ok": True, "wav_b64": base64.b64encode(wav).decode("ascii"),
                           "bytes": len(wav), "dur": info.get("dur"),
                           "seconds": info.get("seconds"),
                           "worker_ms": int((time.time() - t0) * 1000),
                           "tokens": info.get("tokens"), "phones": info.get("phones"),
                           "steps": info.get("steps"), "stages": info.get("stages"),
                           "vits_ref_s": getattr(eng, "last_vits_ref_s", None),
                           "opts": _echo(eng),
                           "mem": _mem_report()})

            elif cmd == "quit":
                _send({"ok": True, "bye": True, "mem": _mem_report()})
                _log("收到 quit，退出")
                return 0

            else:
                _send({"ok": False, "error": f"未知命令: {cmd!r}"})

        except Exception as e:
            load_error = f"{type(e).__name__}: {e}"
            _log(f"请求异常: {e}\n{traceback.format_exc()}")
            _send({"ok": False, "error": load_error,
                   "traceback": traceback.format_exc()[-800:]})

    _log("stdin 关闭，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
