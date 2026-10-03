# -*- coding: utf-8 -*-
"""PC 语音引擎 —— **主进程侧宿主**（管理独立子进程）。

职责（照搬 `FireflyVoiceResearch/_proto/engine_host.py` 的形态）：
  · 拉起 / 保活 / 重启 子进程（`pc_worker.py`）；
  · 请求-响应（带超时；**超时即杀进程**，避免响应错位）；
  · **空闲自动退出** → 内存真正归还；
  · 崩溃隔离：子进程死了主进程无感，下次调用自动重拉。

Windows 打包注意：子进程用 `CREATE_NO_WINDOW`（主 exe 是 `console=False`，否则会弹黑窗）。

子进程解释器解析顺序：
  1. 构造参数 python_exe（最高优先）；
  2. 环境变量 `FIREFLY_ENGINE_PYTHON`（原型既有约定；本机指向上面的 venv 解释器）；
  3. frozen → `sys.executable --voice-worker`（同一 exe 复用，见 `app/server.py` 顶部分发）；
     非 frozen → `sys.executable <pc_worker.py>`。

主进程**从不 import** onnxruntime/numpy（依赖只在子进程里）—— `available()` 的依赖检查
走 `importlib.util.find_spec`（不触发导入）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SENTINEL = object()


class PcHostError(Exception):
    """宿主层错误（写失败/超时/子进程意外退出）。"""


class PcEngineHost:
    def __init__(self, models_dir, assets_dir, mood_lib_dir, moods, bert_file,
                 python_exe: str | None = None, idle_timeout: float = 300.0,
                 call_timeout: float = 240.0, log_path: Path | None = None,
                 steps: int = 4, intra_op_threads: int = 0,
                 inter_op_threads: int = 0, ort_sequential: bool = False,
                 arena: bool | None = None, graph_opt: str | None = None,
                 bert_graph_opt: str | None = None, ref_cache: bool = True,
                 warmup: bool = False):
        self.models_dir = str(models_dir)
        self.assets_dir = str(assets_dir)
        self.mood_lib_dir = str(mood_lib_dir)
        self.moods = list(moods)
        self.bert_file = bert_file
        # V4 提速杠杆（默认 = 与手机同档的基线；由 `_child_env` 透传给子进程）
        self.steps = int(steps)
        self.intra_op_threads = int(intra_op_threads)
        self.inter_op_threads = int(inter_op_threads)
        self.ort_sequential = bool(ort_sequential)
        # W1 零质量代价杠杆（默认 = 与 V1 行为一致：不覆盖 arena/图优化档、ref 缓存开、不预热）
        self.arena = (None if arena is None else bool(arena))
        self.graph_opt = graph_opt
        self.bert_graph_opt = bert_graph_opt
        self.ref_cache = bool(ref_cache)
        self.warmup = bool(warmup)
        self.python_exe = python_exe
        self.idle_timeout = idle_timeout            # 秒；0 = 不自动退出
        self.call_timeout = call_timeout
        here = Path(__file__).resolve().parent
        self.log_path = Path(log_path) if log_path else (here / "_worker.log")
        self._worker_py = here / "pc_worker.py"

        self._proc: subprocess.Popen | None = None
        self._q: "queue.Queue" = queue.Queue()
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._starts = 0
        self._idle_kills = 0
        self._timeouts = 0
        self._errfh = None
        self._stopping = threading.Event()
        if self.idle_timeout > 0:
            threading.Thread(target=self._reap_loop, daemon=True,
                             name="pc-voice-idle-reaper").start()

    # ── 解释器与启动命令 ──────────────────────────────────────────
    def _resolve_launch(self) -> tuple[list[str], str | None]:
        env_py = self.python_exe or os.environ.get("FIREFLY_ENGINE_PYTHON")
        if env_py and Path(env_py).exists():
            return [env_py, str(self._worker_py)], str(self._worker_py.parent)
        if getattr(sys, "frozen", False):
            # 冻结态：复用同一个 exe，标记参数让它把自己当 worker 跑（app/server.py 顶部分发）
            return [sys.executable, "--voice-worker"], None
        return [sys.executable, str(self._worker_py)], str(self._worker_py.parent)

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("FIREFLY_NO_ARENA", "1")
        env["FIREFLY_VOICE_CFG"] = json.dumps({
            "models_dir": self.models_dir, "assets_dir": self.assets_dir,
            "mood_lib_dir": self.mood_lib_dir, "moods": self.moods,
            "bert_file": self.bert_file,
            "steps": self.steps, "intra_op_threads": self.intra_op_threads,
            "inter_op_threads": self.inter_op_threads, "ort_sequential": self.ort_sequential,
            "arena": self.arena, "graph_opt": self.graph_opt,
            "bert_graph_opt": self.bert_graph_opt, "ref_cache": self.ref_cache,
            "warmup": self.warmup,
        }, ensure_ascii=False)
        # 让子进程无论 cwd 在哪都能 `import pc_engine`
        pp = str(self._worker_py.parent)
        env["PYTHONPATH"] = pp + os.pathsep + env.get("PYTHONPATH", "")
        return env

    # ── 状态 ──────────────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.alive else None

    def stats(self) -> dict:
        return {"alive": self.alive, "pid": self.pid, "starts": self._starts,
                "idle_kills": self._idle_kills, "timeouts": self._timeouts,
                "idle_timeout": self.idle_timeout,
                "idle_for_s": round(time.time() - self._last_used, 1) if self._last_used else 0}

    # ── 生命周期 ──────────────────────────────────────────────────
    def _spawn_unlocked(self) -> None:
        self._teardown_unlocked()
        argv, cwd = self._resolve_launch()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._errfh = open(self.log_path, "a", encoding="utf-8")
        except OSError:
            self._errfh = None
        if self._errfh:
            self._errfh.write(f"\n===== pc voice worker 拉起 {time.strftime('%H:%M:%S')} "
                              f"argv={argv} =====\n")
            self._errfh.flush()
        kw: dict = {}
        if _CREATE_NO_WINDOW:
            kw["creationflags"] = _CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._errfh,
            text=True, encoding="utf-8", bufsize=1, env=self._child_env(), cwd=cwd, **kw)
        self._q = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._q),
                         daemon=True, name="pc-voice-stdout-pump").start()
        self._starts += 1
        self._last_used = time.time()

    def _pump(self, proc, q) -> None:
        try:
            for line in proc.stdout:
                q.put(line.rstrip("\n"))
        except Exception:
            pass
        finally:
            q.put(_SENTINEL)

    def _teardown_unlocked(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=8)
                    except Exception:
                        p.kill()
            except Exception:
                pass
            for stream in (p.stdin, p.stdout):
                try:
                    stream and stream.close()
                except Exception:
                    pass
        if self._errfh:
            try:
                self._errfh.close()
            except Exception:
                pass
            self._errfh = None

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            self._teardown_unlocked()

    def _reap_loop(self) -> None:
        while not self._stopping.wait(5.0):
            with self._lock:
                if not self.alive or not self._last_used:
                    continue
                if time.time() - self._last_used > self.idle_timeout:
                    self._idle_kills += 1
                    self._teardown_unlocked()

    # ── 请求 ──────────────────────────────────────────────────────
    def call(self, obj: dict, timeout: float | None = None) -> dict:
        timeout = timeout or self.call_timeout
        with self._lock:
            if not self.alive:
                self._spawn_unlocked()
            self._last_used = time.time()
            try:
                self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self._teardown_unlocked()
                logger.warning("写入语音子进程失败: %s", e)
                raise PcHostError("语音引擎通信失败，请重启程序后重试")
            try:
                line = self._q.get(timeout=timeout)
            except queue.Empty:
                self._timeouts += 1
                self._teardown_unlocked()          # 超时即杀，杜绝响应错位
                logger.warning("语音子进程响应超时（%ss），已终止它", timeout)
                raise PcHostError("语音生成超时了，请重试（若反复超时，可先关闭其它占用较大的程序）")
            self._last_used = time.time()
            if line is _SENTINEL:
                rc = self._proc.poll() if self._proc else None
                self._teardown_unlocked()
                logger.warning("语音子进程意外退出（returncode=%s）", rc)
                raise PcHostError("语音引擎意外退出，请重试")
            try:
                return json.loads(line)
            except Exception as e:
                logger.warning("语音子进程响应非法: %s: %s", e, str(line)[:200])
                raise PcHostError("语音引擎返回异常，请重试")

    # ── 高层接口 ──────────────────────────────────────────────────
    def status(self) -> dict:
        try:
            r = self.call({"cmd": "status"}, timeout=60)
        except PcHostError as e:
            return {"ok": False, "error": str(e), **self.stats()}
        return {**r, **self.stats()}

    def ensure_loaded(self, timeout: float | None = None) -> dict:
        """装载引擎（幂等）；子进程崩溃/超时自动重拉一次。"""
        try:
            return self.call({"cmd": "load"}, timeout=timeout or max(self.call_timeout, 300.0))
        except PcHostError as e1:
            try:
                r = self.call({"cmd": "load"}, timeout=timeout or max(self.call_timeout, 300.0))
                r["retried"] = True
                return r
            except PcHostError as e2:
                logger.warning("语音引擎装载接连失败: %s / %s", e1, e2)
                return {"ok": False, "error": "语音引擎接连失败，请重启程序后重试"}

    def synthesize(self, text: str, mood: str, timeout: float | None = None) -> dict:
        """合成；返回含 `wav`(bytes) 的 dict，或 {"ok": False, "error": ...}。"""
        req = {"cmd": "synthesize", "text": text, "mood": mood}
        try:
            r = self.call(req, timeout=timeout)
        except PcHostError as e1:
            try:
                r = self.call(req, timeout=timeout)
                r["retried"] = True
            except PcHostError as e2:
                logger.warning("语音合成接连失败: %s / %s", e1, e2)
                return {"ok": False, "error": "语音引擎接连失败，请重启程序后重试"}
        if r.get("ok") and r.get("wav_b64"):
            try:
                r["wav"] = base64.b64decode(r["wav_b64"])
            except Exception as e:
                return {"ok": False, "error": f"音频解码失败: {e}"}
        return r
