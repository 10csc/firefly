# -*- coding: utf-8 -*-
"""纯 Python 文本前端 —— `android/.../TextFrontend.kt` 的**逐行移植**（无 torch/transformers）。

为什么要有它（任务单 V1 的核心）：
  官方 PyTorch 文本前端（jieba + g2pw + tone_sandhi + AutoTokenizer）实测让语音子进程的
  提交峰值 +1164 MB（torch 503 + transformers 661，见 `docs/设计/PC端语音/03`）。而手机端
  早就用一份**纯 Kotlin 重写**的简化前端（单读音表 + opencpop 映射，`TextFrontend.kt`），
  质量口径是"与手机持平"。这里把它移植成纯 Python ⇒ 砍掉 torch/transformers 两个重物，
  且**逐条同逻辑**（这是"质量不下滑"的机械保底）。

依赖：只读 4 张打包资产（`app/assets/voice/`）：
  pinyin.json（汉字 → (声母 i, 韵母+声调 f)）· opencpop-strict.txt（拼音 → "声母 韵母"）
  · symbols.json（符号 → ID）· char2id.json（字 → BERT token id）

★ 移植纪律：本文件对 `TextFrontend.kt:60-145` 的 `process()` / `normalize()` **逐分支照抄**，
  包括所有 `continue`（未知字 / 韵母空串 / 映射缺失都**静默跳过**，不写 word2ph、不进 aligned）。
  这是有意的：Kotlin 的第三返回值 `aligned` 必须与 `word2ph` **逐位对应**，
  BERT 分支按它逐字取 token id，错位会 `length=14; index=14` 越界（源码 51~59 行的注释）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ── 与 Kotlin `punctuationSet` 逐字一致（顺序无关，用集合；含半角空格）──────
PUNCTUATION = [
    "!", "?", "…", ",", ".", "-", "\u201c", "\u201d", "\u2018", "\u2019",
    "(", ")", " ", ":", ";", "、", "！", "？", "，", "。", "：", "；",
]
_PUNCT_SET = set(PUNCTUATION)
_ESC_PUNC = "".join(re.escape(p) for p in PUNCTUATION)
# 官方 g2p 的断句：在每个标点之后（含其后续空白）切
_SPLIT_RE = re.compile("(?<=[%s])\\s*" % _ESC_PUNC)
_ALPHA_RE = re.compile("[a-zA-Z]+")
# normalize() 的过滤：只保留汉字 + 标点（字母/数字/其它一律删）
_ALLOWED_RE = re.compile("[^\u4e00-\u9fa5%s]+" % _ESC_PUNC)


class TextFrontend:
    """中文 → (音素 ID 序列, word2ph, 与 word2ph 逐位对应的字符序列)。

    用法：
        fe = TextFrontend.from_assets(assets_dir)      # assets_dir 里含 4 张映射表
        ids, word2ph, aligned = fe.process("嗯，是我，流萤。")
    """

    def __init__(self, sym_to_id: dict, pinyin_data: dict, pinyin_to_symbol: dict):
        # sym_to_id:        symbols.json   符号 → ID
        # pinyin_data:      汉字 → (声母, 韵母+声调)
        # pinyin_to_symbol: opencpop      拼音 → "声母 韵母"
        self.sym_to_id = dict(sym_to_id)
        self.pinyin_data = dict(pinyin_data)
        self.pinyin_to_symbol = dict(pinyin_to_symbol)

    # ── 构造：从打包资产读 4 张表 ────────────────────────────────
    @classmethod
    def from_assets(cls, assets_dir: Path) -> "TextFrontend":
        assets_dir = Path(assets_dir)
        sym_to_id = json.loads((assets_dir / "symbols.json").read_text(encoding="utf-8"))
        pinyin_raw = json.loads((assets_dir / "pinyin.json").read_text(encoding="utf-8"))
        pinyin_data = {k: (v["i"], v["f"]) for k, v in pinyin_raw.items()}
        pinyin_to_symbol: dict = {}
        text = (assets_dir / "opencpop-strict.txt").read_text(encoding="utf-8")
        for line in text.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2:
                pinyin_to_symbol[parts[0]] = parts[1]
        return cls(sym_to_id, pinyin_data, pinyin_to_symbol)

    # ── 归一化（逐行移植 `TextFrontend.kt:147-156`）────────────────
    def normalize(self, text: str) -> str:
        t = text.replace("嗯", "恩").replace("呣", "母")
        # 数字转中文：简化实现直接**不做**（保留数字会在后面被过滤删掉）——与 Kotlin 注释一致
        t = (t.replace("，", ",").replace("。", ".").replace("！", "!")
             .replace("？", "?"))
        t = (t.replace("：", ",").replace("；", ",").replace("、", ",")
             .replace("…", "…").replace("～", "…"))     # "…"→"…" 是 Kotlin 原文的无操作替换，照抄
        # 去掉"非中文字符 + 非标点"的一切（字母、数字、换行等）
        t = _ALLOWED_RE.sub("", t)
        return t

    # ── 主流程（逐行移植 `TextFrontend.kt:60-145`）─────────────────
    def process(self, text: str) -> tuple[list[int], list[int], str]:
        """返回 (phones_ids, word2ph, aligned)。三者约束：sum(word2ph)==len(phones)，len(word2ph)==len(aligned)。"""
        norm = self.normalize(text)
        sentences = [s for s in _SPLIT_RE.split(norm) if s.strip()]

        phones: list[str] = []
        word2ph: list[int] = []
        aligned: list[str] = []          # 真正进入 word2ph 的字符，顺序与 word2ph 一致
        for seg in sentences:
            seg_clean = _ALPHA_RE.sub("", seg)       # Kotlin 原文如此（normalize 已删字母，这里是冗余保险）
            for c in seg_clean:
                if c == " ":
                    continue
                if c in _PUNCT_SET:
                    phones.append(c)
                    word2ph.append(1)
                    aligned.append(c)
                    continue
                py = self.pinyin_data.get(c)
                if py is None:
                    continue                          # 未知字 → 跳过（关键：不写 word2ph）
                ini, fin_tone = py
                if fin_tone == "":
                    continue                          # pinyin.json 里「嗯」的 f 是空串
                if ini == fin_tone:                   # 声母==韵母（标点已处理，理论不达）
                    phones.append(c)
                    word2ph.append(1)
                    aligned.append(c)
                    continue
                # ★★ 轻声修复（与 Kotlin 一致）★★
                # 并非所有字的 f 都带声调数字：高频虚词（的/了/吗/吧/啊/们/呢/么/呀/啦/嘛/嘞/子/着/蓿）
                # 的 f 不带数字。官方把轻声记为**第 5 声**；symbols.json 里 a5/e5/en5 等均存在。
                # 原实现无条件 last() 当声调、dropLast(1) 当韵母 → 韵母被切残 → 查不到映射 → 静默丢字。
                has_tone = fin_tone[-1].isdigit()
                tone = fin_tone[-1] if has_tone else "5"
                base = fin_tone[:-1] if has_tone else fin_tone
                if ini != "":
                    if base == "uei":
                        pinyin = ini + "ui"
                    elif base == "iou":
                        pinyin = ini + "iu"
                    elif base == "uen":
                        pinyin = ini + "un"
                    else:
                        pinyin = ini + base
                else:
                    cand = ini + base                    # 零声母：cand == base
                    if cand == "ing":
                        pinyin = "ying"
                    elif cand == "i":
                        pinyin = "yi"
                    elif cand == "in":
                        pinyin = "yin"
                    elif cand == "u":
                        pinyin = "wu"
                    else:
                        first = cand[0] if cand else ""
                        if first == "v":
                            pinyin = "yu" + cand[1:]
                        elif first == "e":
                            pinyin = "e" + cand[1:]      # == cand（保持 Kotlin 形态）
                        elif first == "i":
                            pinyin = "y" + cand[1:]
                        elif first == "u":
                            pinyin = "w" + cand[1:]
                        else:
                            pinyin = cand                # 首字母不在滑音表 → **原样保留**
                sym = self.pinyin_to_symbol.get(pinyin)
                if sym is None:
                    continue                          # 映射缺失 → 跳过
                parts = sym.split(" ")
                if len(parts) != 2:
                    continue
                phones.append(parts[0])
                phones.append(parts[1] + tone)
                word2ph.append(2)
                aligned.append(c)

        ids = [self.sym_to_id.get(p, 0) for p in phones]
        return ids, word2ph, "".join(aligned)

    # ── 供 V3 锚点/诊断：带"被吞字"原因的过程版本（不影响合成主链）─────
    def process_debug(self, text: str) -> tuple[list[str], list[int], str, list[tuple[str, str]]]:
        """与 `process()` 同逻辑，额外返回 dropped=[(字, 原因)]（只用于验证/诊断）。"""
        norm = self.normalize(text)
        sentences = [s for s in _SPLIT_RE.split(norm) if s.strip()]
        phones: list[str] = []
        word2ph: list[int] = []
        aligned: list[str] = []
        dropped: list[tuple[str, str]] = []
        for seg in sentences:
            for c in _ALPHA_RE.sub("", seg):
                if c == " ":
                    continue
                if c in _PUNCT_SET:
                    phones.append(c); word2ph.append(1); aligned.append(c); continue
                py = self.pinyin_data.get(c)
                if py is None:
                    dropped.append((c, "不在 pinyin.json")); continue
                ini, fin_tone = py
                if fin_tone == "":
                    dropped.append((c, "韵母为空串")); continue
                if ini == fin_tone:
                    phones.append(c); word2ph.append(1); aligned.append(c); continue
                has_tone = fin_tone[-1].isdigit()
                tone = fin_tone[-1] if has_tone else "5"
                base = fin_tone[:-1] if has_tone else fin_tone
                if ini != "":
                    pinyin = {"uei": ini + "ui", "iou": ini + "iu", "uen": ini + "un"}.get(base, ini + base)
                else:
                    cand = ini + base
                    if cand in ("ing", "i", "in", "u"):
                        pinyin = {"ing": "ying", "i": "yi", "in": "yin", "u": "wu"}[cand]
                    else:
                        first = cand[0] if cand else ""
                        pinyin = ({"v": "yu", "e": "e", "i": "y", "u": "w"}[first] + cand[1:]) \
                            if first in ("v", "e", "i", "u") else cand
                sym = self.pinyin_to_symbol.get(pinyin)
                if sym is None:
                    dropped.append((c, f"opencpop 无映射（拼音={pinyin}）")); continue
                parts = sym.split(" ")
                if len(parts) != 2:
                    dropped.append((c, f"映射格式异常: {sym!r}")); continue
                phones.append(parts[0]); phones.append(parts[1] + tone)
                word2ph.append(2); aligned.append(c)
        return phones, word2ph, "".join(aligned), dropped
