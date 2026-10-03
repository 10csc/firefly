# -*- coding: utf-8 -*-
"""PC 语音引擎（纯 ONNX Runtime + numpy，**无 torch**）—— 子进程内执行。

移植来源：`android/.../TtsEngine.kt`（`synthesize` / `runBert` / `loadMoodFromDir`）。
Kotlin 那份是"真机验证过"的端侧实现（zcr 0.039 / ASR 回读正确），本文件**逐分支照抄**其
数值逻辑，只把 Java 数组/缓冲换成 numpy、把 ORT 的 Java API 换成 Python API。

为何不用 `_proto/voice_engine.py`：那个原型复用官方 `infer_onnx_v4.OnnxTTS.synthesize`，
**仍依赖 torch**（只把文本前端换成 ONNX）。本任务是把它彻底换成**纯 ONNX**（Kotlin 版形态），
从而砍掉 torch/transformers 的 +1164 MB 提交峰值。CFM 分块循环 Kotlin 版已自带（不依赖
`_cfm_common.cfm_sample_chunked` 的 torch 实现），直接照抄即可。

关键点（全部来自 Kotlin 真机踩坑注释，勿改）：
  · 7 个 session 会话级常驻，**不可每句重建**（重建会让 AR 从 25s 变 115s）；
  · ORT `enable_cpu_mem_arena=False`（arena 只增不减，连发会涨到被杀后台，E7）；
  · BERT 用 **BASIC 级**优化（ALL 级会把 GELU 融成 contrib 算子，fp16 档在部分 EP 上无内核）；
  · CFM 是**行主序 [c*T+t]** 手写循环（Kotlin 平铺索引曾出 bug）；
  · 参考侧 6 个量（prompt_semantic / ssl_content / refer_spec / mel2 / ref_phones / ref_bert）
    全部**预计算**好了（mood_lib），运行时零推理、也不需要 377MB 的 `firefly_ssl.onnx`。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

try:                                             # 开发/脚本：同目录模块（PYTHONPATH 含 app/voice）
    from text_frontend import TextFrontend
except Exception:                                # frozen：作为 voice 包模块被 dispatch 时
    from .text_frontend import TextFrontend      # type: ignore

logger = logging.getLogger(__name__)

SR = 48000
EOS = 1024
MIN_TOKENS = 12
MAX_TOK_PER_PHONE = 8
PAD_HEAD = int(SR * 0.15)      # 头部 0.15s 静音（Kotlin 原文）
PAD_TAIL = SR // 2             # 尾部 0.5s 余韵（Kotlin 原文）
SPEC_MIN, SPEC_MAX = -12.0, 2.0
CFM_STEPS = 4                  # 任务单：steps=4（V4 提速项才动它）
T_REF = 500
T_CHUNK = 1000

_COMPONENTS = {
    "s_encoder": "firefly_t2s_encoder.onnx",
    "s_fsdec": "firefly_t2s_fsdec_int8.onnx",
    "s_sdec": "firefly_t2s_sdec_int8.onnx",
    "s_vits": "firefly_vits_int8.onnx",
    "s_cfm": "firefly_cfm_estimator_int8.onnx",
    "s_vocoder": "firefly_vocoder.onnx",
}
_BERT_DEFAULT = "firefly_bert_int8.onnx"


class PcVoiceError(Exception):
    """引擎不可用（缺依赖 / 缺模型 / 缺资产 / 装载失败）。"""


# ── 语气参考特征：读打包资产里的 .raw（照抄 Kotlin `loadMoodFromDir`）──────
def _read_raw(path: Path, dtype: str, count: int) -> np.ndarray:
    a = np.fromfile(str(path), dtype=dtype)
    if a.size != count:
        raise PcVoiceError(f"{path.name} 大小不符：{a.size} != {count}")
    return a


def load_mood(dir_path: Path) -> dict:
    """读一本语气库（`mood_lib/<mood>/`：meta.json + 6 个裸字节 .raw）。"""
    dir_path = Path(dir_path)
    meta = json.loads((dir_path / "meta.json").read_text(encoding="utf-8"))

    def shape(k: str) -> list[int]:
        return list(meta[k]["shape"])

    sh_phones = shape("ref_phones")          # [1, N]
    sh_bert = shape("ref_bert")              # [N, 1024]（导出时已 .T 存储）
    sh_ssl = shape("ssl_content")            # [1, 768, T]
    sh_prompt = shape("prompt_semantic")     # [T]
    sh_spec = shape("refer_spec")            # [1, 1025, T]
    sh_mel2 = shape("mel2")                  # [1, 100, T]

    ref_phones = _read_raw(dir_path / "ref_phones.raw", "<i8", sh_phones[1])            # (N,)
    ref_bert = _read_raw(dir_path / "ref_bert.raw", "<f4",
                         sh_bert[0] * sh_bert[1]).reshape(sh_bert[0], sh_bert[1])       # (L,1024)
    ssl = _read_raw(dir_path / "ssl_content.raw", "<f4",
                    768 * sh_ssl[2]).reshape(768, sh_ssl[2])                            # (768,T)
    prompt_sem = _read_raw(dir_path / "prompt_semantic.raw", "<i8", sh_prompt[0])       # (T,)
    refer_spec = _read_raw(dir_path / "refer_spec.raw", "<f4",
                           1025 * sh_spec[2]).reshape(1025, sh_spec[2])                  # (1025,T)
    mel2 = _read_raw(dir_path / "mel2.raw", "<f4",
                     100 * sh_mel2[2]).reshape(100, sh_mel2[2])                          # (100,T)
    return {"ref_phones": ref_phones, "ref_bert": ref_bert, "ssl": ssl,
            "prompt_sem": prompt_sem, "refer_spec": refer_spec, "mel2": mel2, "meta": meta}


def _argmax_first_n(flat: np.ndarray, n: int) -> int:
    """Kotlin `argmaxFloat(buf, size)`：在前 size 个元素里取 argmax。"""
    seg = flat[:n]
    return int(np.argmax(seg)) if seg.size else -1


def _apply_thread_opts(so: ort.SessionOptions, intra: int, inter: int,
                       sequential: bool) -> None:
    """按需显式设定 ORT 线程（0/False = **保持 ORT 默认**，保证默认行为与 V1 逐位一致）。

    这是 V4 的 L2 杠杆：`intra_op_num_threads=物理核数`、`inter_op_num_threads=1`、
    `execution_mode=ORT_SEQUENTIAL`（见 `docs/设计/PC端语音/04_验证方法.md` §M1）。
    ★ 默认（不传）时**一个字段都不写**，避免与「手机同档」的基线分叉。
    """
    if intra:
        so.intra_op_num_threads = int(intra)
    if inter:
        so.inter_op_num_threads = int(inter)
    if sequential:
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL


def _apply_opt_opts(so: ort.SessionOptions, arena: bool | None,
                    graph_opt: str | None) -> None:
    """W1（零质量代价）的 ORT 会话选项杠杆。

    · `arena=None` ⇒ **保持调用方已设的值**（主会话现状是 `False`，E7 的设计取值）；
      传 `True/False` 才显式覆盖 —— **仅用于实测对比**：arena 只增不减（E7），默认不启用。
    · `graph_opt=None` ⇒ 保持调用方已设的档位；否则按 `all/extended/basic/disable` 覆盖。
    ★ 这两项都**不触碰模型权重与数值路径**，只影响 ORT 的执行计划与内存池 ⇒ 与音质无关。
    """
    if arena is not None:
        so.enable_cpu_mem_arena = bool(arena)
    if graph_opt is not None:
        lvl = {
            "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
            "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
            "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
            "disable": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
        }.get(str(graph_opt).lower())
        if lvl is None:
            raise PcVoiceError(f"未知 graph_opt: {graph_opt}（可选 all/extended/basic/disable）")
        so.graph_optimization_level = lvl


class PcVoiceEngine:
    """PC 端语音引擎。线程不安全（worker 单线程串行使用即可）。"""

    def __init__(self, models_dir: Path, assets_dir: Path, mood_lib_dir: Path,
                 moods: list[str], bert_file: str = _BERT_DEFAULT,
                 steps: int = CFM_STEPS, intra_op_threads: int = 0,
                 inter_op_threads: int = 0, ort_sequential: bool = False,
                 arena: bool | None = None, graph_opt: str | None = None,
                 bert_graph_opt: str | None = None, ref_cache: bool = True,
                 warmup: bool = False):
        self.models_dir = Path(models_dir)
        self.assets_dir = Path(assets_dir)
        self.mood_lib_dir = Path(mood_lib_dir)
        self.moods = list(moods)
        self.bert_file = bert_file
        # ── V4 提速杠杆参数（默认值 = 与手机同档/与 V1 完全一致，改动即「显式一个变量」）──
        #    steps：CFM 欧拉步数（默认 4；L1 试 3）；合法下限 1。
        #    *_threads / ort_sequential：0/False = 交给 ORT 默认（L2 才显式设定）。
        self.steps = max(1, int(steps))
        self.intra_op_threads = int(intra_op_threads)
        self.inter_op_threads = int(inter_op_threads)
        self.ort_sequential = bool(ort_sequential)
        # ── W1 零质量代价杠杆（详见 `docs/设计/PC端语音/06_W1速度口径收口.md`）──
        #    arena：None = 保持现状（False）；graph_opt / bert_graph_opt：None = 保持现状。
        #    ref_cache：**默认 True** —— 参考侧 vits 只依赖语气、与输入文本无关，缓存后逐位等价。
        #    warmup：装载后是否跑一次预热（消掉「首次请求更慢」的体感）。
        self.arena = arena
        self.graph_opt = graph_opt
        self.bert_graph_opt = bert_graph_opt
        self.ref_cache = bool(ref_cache)
        self.warmup_enabled = bool(warmup)
        self.loaded = False
        self.load_seconds = 0.0
        self.warmup_seconds = 0.0
        self.last_guard = ""
        self.last_stages: dict[str, float] = {}     # 最近一次 _run_chain 的阶段耗时（秒）
        self.stage_calls = 0                        # 已发生的 _run_chain 次数（阶段表归一用）
        self.ref_cache_hits = 0
        self.ref_cache_misses = 0
        self._sessions: dict = {}
        self._refs: dict = {}
        self._ref_vits: dict = {}                   # mood -> 参考侧 vits 输出（只依赖语气）
        self._frontend: TextFrontend | None = None
        self._char2id: dict = {}

    # ── 资源就绪探测（不触发装载）────────────────────────────────
    def available(self) -> tuple[bool, str]:
        if not self.models_dir.is_dir():
            return False, f"模型目录不存在: {self.models_dir}"
        for fn in list(_COMPONENTS.values()) + [self.bert_file]:
            if not (self.models_dir / fn).exists():
                return False, f"缺模型文件: {fn}"
        for fn in ("symbols.json", "pinyin.json", "opencpop-strict.txt", "char2id.json"):
            if not (self.assets_dir / fn).exists():
                return False, f"缺文本前端资产: {self.assets_dir / fn}"
        for m in self.moods:
            if not (self.mood_lib_dir / m / "meta.json").exists():
                return False, f"缺语气库: {self.mood_lib_dir / m}"
        return True, "ok"

    # ── 装载（一次性）────────────────────────────────────────────
    def load(self) -> float:
        if self.loaded:
            return self.load_seconds
        ok, why = self.available()
        if not ok:
            raise PcVoiceError(why)
        t0 = time.time()

        # ① 文本前端 + BERT 字表（读打包资产）
        self._frontend = TextFrontend.from_assets(self.assets_dir)
        self._char2id = json.loads((self.assets_dir / "char2id.json").read_text(encoding="utf-8"))

        # ② 6 个生成链 session + BERT（会话级常驻）
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.enable_cpu_mem_arena = False             # E7：arena 内存永不归还
        so.log_severity_level = 3
        _apply_thread_opts(so, self.intra_op_threads, self.inter_op_threads, self.ort_sequential)
        _apply_opt_opts(so, self.arena, self.graph_opt)
        for attr, fn in _COMPONENTS.items():
            self._sessions[attr] = ort.InferenceSession(
                str(self.models_dir / fn), so, providers=["CPUExecutionProvider"])

        bert_so = ort.SessionOptions()
        bert_so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC  # 见模块头注释
        bert_so.enable_cpu_mem_arena = False
        bert_so.log_severity_level = 3
        _apply_thread_opts(bert_so, self.intra_op_threads, self.inter_op_threads, self.ort_sequential)
        # ★ BERT 默认保持 BASIC（模块头注释里有理由），只有显式传 bert_graph_opt 才覆盖。
        #   W1 实测项之一：bert 提到 ALL/EXTENDED 是否更快（不触碰权重 ⇒ 与音质无关）。
        _apply_opt_opts(bert_so, self.arena, self.bert_graph_opt)
        self._sessions["s_bert"] = ort.InferenceSession(
            str(self.models_dir / self.bert_file), bert_so, providers=["CPUExecutionProvider"])

        # ③ 语气参考特征（预计算，零推理）
        for m in self.moods:
            self._refs[m] = load_mood(self.mood_lib_dir / m)

        self.loaded = True
        self.load_seconds = round(time.time() - t0, 1)
        if self.warmup_enabled:
            self.warmup()
        return self.load_seconds

    def warmup(self, mood: str | None = None) -> float:
        """装载后跑一次预热（**不落盘、不返回音频**），消掉「首次请求明显更慢」。

        W1 零质量代价项：预热只把 ORT 的算子/内存池捂热，**不改数值结果**。
        耗时单独记在 `warmup_seconds`，不计入 `load_seconds`。
        """
        m = mood or (self.moods[0] if self.moods else "")
        if not m:
            return 0.0
        t0 = time.time()
        try:
            self.synthesize("预热。", m)
        except Exception:
            pass                                    # 预热失败不影响可用性（真失败会在正式请求暴露）
        self.warmup_seconds = round(time.time() - t0, 2)
        return self.warmup_seconds

    def opts(self) -> dict:
        """当前生效的杠杆取值（回显给宿主/实测脚本，便于"确认无变量漂移"）。"""
        return {
            "steps": self.steps, "intra_op_threads": self.intra_op_threads,
            "inter_op_threads": self.inter_op_threads, "ort_sequential": self.ort_sequential,
            "arena": self.arena, "graph_opt": self.graph_opt,
            "bert_graph_opt": self.bert_graph_opt, "ref_cache": self.ref_cache,
            "warmup": self.warmup_enabled,
        }

    def close(self) -> None:
        self._sessions.clear()
        self._refs.clear()
        self._ref_vits.clear()
        self._frontend = None
        self.loaded = False

    # ── BERT（照抄 Kotlin `runBert`）──────────────────────────────
    def run_bert(self, aligned: str, word2ph: list[int]) -> np.ndarray:
        ids = [101]                                     # [CLS]
        for c in aligned:
            ids.append(int(self._char2id.get(c, 100)))  # [UNK]=100
        ids.append(102)                                 # [SEP]
        n = len(ids)
        input_ids = np.asarray(ids, dtype=np.int64)[None, :]
        attn = np.ones((1, n), dtype=np.int64)
        type_ids = np.zeros((1, n), dtype=np.int64)
        w2ph = np.asarray(word2ph, dtype=np.int64)
        out = self._sessions["s_bert"].run(None, {
            "input_ids": input_ids, "attention_mask": attn,
            "token_type_ids": type_ids, "word2ph": w2ph,
        })[0]
        return out                                       # (N, 1024)

    # ── 合成主链（照抄 Kotlin `synthesize`）───────────────────────
    def synthesize(self, text: str, mood: str) -> tuple[bytes | None, dict]:
        """返回 (wav_bytes, info)。失败返回 (None, info)，info 含 error。"""
        info: dict = {"text": text, "mood": mood}
        if not self.loaded:
            self.load()
        fe = self._frontend
        ref = self._refs.get(mood)
        if ref is None:
            logger.warning("语音合成收到未知语气: %s", mood)
            return None, {**info, "error": "这条语音的语气暂不支持，请换一种语气再试"}
        t = (text or "").strip()
        if not t:
            return None, {**info, "error": "空文本"}

        ids, w2ph, aligned = fe.process(t)
        if not ids or not w2ph:
            return None, {**info, "error": "这段文字里可能含暂不支持的符号，换个说法或去掉特殊符号再试"}
        bert = self.run_bert(aligned, w2ph)
        text_ids = np.asarray(ids, dtype=np.int64)
        info["phones"] = len(ids)
        info["steps"] = self.steps

        t0 = time.time()
        wav, guard = self._run_chain(text_ids, bert, ref, mood)
        info["seconds"] = round(time.time() - t0, 1)
        info["tokens"] = guard.get("tokens")
        info["guard"] = guard.get("reason", "")
        info["stages"] = dict(self.last_stages)         # 阶段耗时（encoder/fsdec/ar/vits/cfm/vocoder）
        if wav is None:
            logger.warning("语音采样被守卫拦下: %s", guard.get("reason", ""))
            return None, {**info, "error": "这段文字念出来效果不稳，已跳过；换一句再试"}
        info["dur"] = round(len(wav) / 2 / SR, 3)       # PCM16 mono
        return wav, info

    # ── 生成链（encoder → fsdec → AR → vits → CFM → vocoder）───────
    def _run_chain(self, text_ids: np.ndarray, text_bert: np.ndarray, ref: dict,
                   mood: str | None = None):
        S = self._sessions
        ref_phones = ref["ref_phones"]
        stage: dict[str, float] = {}                        # 阶段耗时（V4 阶段表用）
        self.stage_calls += 1
        _t = time.perf_counter()

        # ---- T2S encoder ----
        x, prompts = S["s_encoder"].run(None, {
            "ref_seq": ref_phones[None, :],
            "text_seq": text_ids[None, :],
            "ref_bert": ref["ref_bert"],                       # (L,1024)
            "text_bert": np.asarray(text_bert, dtype=np.float32),   # (Lt,1024)
            "ssl_content": ref["ssl"][None, :, :],             # [1,768,T]
        })
        prefix_len = int(prompts.shape[1])
        stage["encoder"] = time.perf_counter() - _t
        _t = time.perf_counter()

        # ---- fsdec ----
        y, k, v, y_emb, x_example = S["s_fsdec"].run(None, {"x": x, "prompts": prompts})
        n_x = int(x_example.reshape(-1).shape[0])
        stage["fsdec"] = time.perf_counter() - _t
        _t = time.perf_counter()

        # ---- AR 循环（每步 run；numpy 天然零泄漏）----
        cap = max(MIN_TOKENS, len(text_ids) * MAX_TOK_PER_PHONE)
        y_list = [int(y[0, prefix_len])]
        idx = 0
        stopped = False
        while idx < cap:
            ln = prefix_len + 1 + idx
            y, k, v, y_emb, logits, samples = S["s_sdec"].run(None, {
                "iy": y[:, :ln],
                "ik": k.reshape(24, -1, 1, 512),
                "iv": v.reshape(24, -1, 1, 512),
                "iy_emb": y_emb.reshape(1, -1, 512),
                "ix_example": x_example.reshape(1, n_x),
            })
            idx += 1
            y_list.append(int(y[0, ln]))
            flat = logits.reshape(-1)
            s_first = int(samples.reshape(-1)[0]) if samples.size else -1
            if _argmax_first_n(flat, 1025) == EOS or s_first == EOS:
                stopped = True
                break
        stage["ar"] = time.perf_counter() - _t
        _t = time.perf_counter()

        # ---- 输出侧守卫（Kotlin：只判 cap/过早收敛；MIN_TOK_PER_PHONE 未在端侧启用，故不引入）----
        if not stopped:
            reason = f"未在 {cap} token 内收敛（疑似复读参考句/跑飞）"
        elif idx < MIN_TOKENS:
            reason = f"过早收敛（{idx} token < 下限 {MIN_TOKENS}），疑似截断"
        else:
            reason = ""
        self.last_guard = reason
        guard = {"ok": not reason, "tokens": idx, "cap": cap, "reason": reason}
        # 记录到此为止的阶段耗时（失败也留痕，便于判断失败发生在哪一段）
        self.last_stages = {k: round(v, 2) for k, v in stage.items()}
        if reason:
            return None, guard
        tok_arr = np.asarray([x for x in y_list if x < 1024], dtype=np.int64)
        if tok_arr.size == 0:
            return None, {**guard, "reason": "语义 token 全被过滤（只剩 EOS）"}

        # ---- vits（目标 fea + 参考 fea）----
        fea = S["s_vits"].run(None, {
            "codes": tok_arr.reshape(1, 1, -1),
            "text": text_ids[None, :],
            "refer": ref["refer_spec"][None, :, :],
        })[0]
        fea_len = int(tok_arr.size) * 4
        # ★ W1 零质量代价项：**参考侧 vits 只依赖语气，与输入文本无关** ⇒ 同语气下每次请求
        #   都在重算同一份结果。它的输入（ref_phones / prompt_sem / refer_spec）全部来自 `_refs[mood]`，
        #   vits 图内没有任何随机采样 ⇒ 缓存命中与重算**逐位等价**（不触碰音质）。
        #   缓存的是原始输出 (1,512,T_ref)；下游 `fea_ref_2d[:, :t_min].copy()` 会复制后再改写，
        #   不会回写缓存（见下方 CFM 分块循环里的 fea_ref_v 赋值）。
        key = mood if mood is not None else id(ref)
        _tr = time.perf_counter()
        fea_ref = self._ref_vits.get(key) if self.ref_cache else None
        if fea_ref is None:
            self.ref_cache_misses += 1
            fea_ref = S["s_vits"].run(None, {
                "codes": ref["prompt_sem"][None, None, :],
                "text": ref_phones[None, :],
                "refer": ref["refer_spec"][None, :, :],
            })[0]                                            # (1,512,T_ref)
            if self.ref_cache:
                self._ref_vits[key] = fea_ref
        else:
            self.ref_cache_hits += 1
        self.last_vits_ref_s = round(time.perf_counter() - _tr, 3)   # 命中时≈0，用于阶段归因
        stage["vits"] = time.perf_counter() - _t
        _t = time.perf_counter()

        # ---- CFM 分块欧拉采样（行主序，照抄 Kotlin）----
        mel2 = ref["mel2"]                                   # (100, T)
        fea_ref_2d = fea_ref[0]                              # (512, T_ref)
        fea_2d = fea[0]                                      # (512, feaLen)
        t_min = min(mel2.shape[1], fea_ref_2d.shape[1])
        mel2v = mel2[:, :t_min].copy()
        fea_ref_v = fea_ref_2d[:, :t_min].copy()
        if t_min > T_REF:
            mel2v = mel2v[:, -T_REF:].copy()
            fea_ref_v = fea_ref_v[:, -T_REF:].copy()
            t_min = T_REF
        chunk_len = T_CHUNK - t_min
        d_step = 1.0 / self.steps                            # V4 L1：steps 可调（默认 4）

        parts: list[np.ndarray] = []
        pos = 0
        cur_prompt = t_min
        while pos < fea_len:
            take = min(chunk_len, fea_len - pos)
            T = cur_prompt + take
            mu = np.zeros((512, T), dtype=np.float32)
            mu[:, :cur_prompt] = fea_ref_v[:, :cur_prompt]
            mu[:, cur_prompt:cur_prompt + take] = fea_2d[:, pos:pos + take]
            xv = (np.random.rand(100, T).astype(np.float32) * 2.0 - 1.0)
            prompt_x = np.zeros((100, T), dtype=np.float32)
            prompt_x[:, :cur_prompt] = mel2v[:, :cur_prompt]
            xv[:, :cur_prompt] = 0.0
            tt = np.float32(0.0)
            for _j in range(self.steps):                     # V4 L1：steps 可调（默认 4）
                vp = S["s_cfm"].run(None, {
                    "x": xv[None, :, :], "prompt_x": prompt_x[None, :, :],
                    "x_lens": np.asarray([T], dtype=np.int64),
                    "t": np.asarray(tt, dtype=np.float32),
                    "d": np.asarray(np.float32(d_step), dtype=np.float32),
                    "mu": mu[None, :, :],
                })[0]                                        # (1, T, 100)
                xv = xv + d_step * vp[0].T                   # BTD → BCT
                tt = np.float32(tt + d_step)
                xv[:, :cur_prompt] = 0.0
            gen = xv[:, cur_prompt:cur_prompt + take]        # (100, take)
            parts.append(gen)
            roll = min(t_min, take)
            mel2v[:, :roll] = gen[:, take - roll:take]
            mel2v[:, roll:t_min] = 0.0
            fea_ref_v[:, :roll] = fea_2d[:, pos + take - roll:pos + take]
            fea_ref_v[:, roll:t_min] = 0.0
            pos += take
        mel_all = np.concatenate(parts, axis=1) if parts else np.zeros((100, 0), dtype=np.float32)
        stage["cfm"] = time.perf_counter() - _t
        _t = time.perf_counter()

        # ---- vocoder（先 denorm）----
        mel_d = (mel_all + 1.0) / 2.0 * (SPEC_MAX - SPEC_MIN) + SPEC_MIN
        audio = S["s_vocoder"].run(None, {"mel": mel_d[None, :, :]})[0][0, 0]
        stage["vocoder"] = time.perf_counter() - _t
        self.last_stages = {k: round(v, 2) for k, v in stage.items()}

        # ---- 写 PCM16 wav（头 0.15s + 尾 0.5s 静音）----
        return _pcm16_wav(audio), guard


def _pcm16_wav(audio: np.ndarray) -> bytes:
    """float32 [-1,1] → 48kHz/16bit/mono WAV bytes（+首尾静音，照抄 Kotlin）。"""
    import struct
    pcm = np.concatenate([np.zeros(PAD_HEAD, dtype=np.float32), audio.astype(np.float32),
                          np.zeros(PAD_TAIL, dtype=np.float32)])
    pcm = np.clip(pcm, -1.0, 1.0)
    data = (pcm * 32767.0).astype("<i2").tobytes()
    n = len(data)
    hdr = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", n)
    return hdr + data
