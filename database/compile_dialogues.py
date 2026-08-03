# -*- coding: utf-8 -*-
"""剧情对话整理脚本 — 两阶段：场景划分 → 逐块压缩（离线，一次性）

流程（先分再压，原文由脚本机械截取，LLM 永不复制原文）：
  阶段1 划分：全文交给 LLM，只输出场景边界锚点 + 标题 + keep 标记（输出量小，不会截断）
             脚本按锚点在源文件中定位切块 → 原文逐字零损耗
  阶段2 压缩：keep=false 的场景块逐块压缩成摘要（超长块分段递进压缩）
             keep=true 的场景块原样保留
  组装：database/dialogues_compiled/<名>_draft.md，人工审核后去掉 _draft，
        再跑 memory/rag/build_index.py 入索引

用法：
    python database/compile_dialogues.py                # 全部
    python database/compile_dialogues.py 3.8            # 匹配文件名
    python database/compile_dialogues.py --model deepseek-v4-pro
"""

import json, os, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DLG_DIR = ROOT / "database" / "dialogues"
OUT_DIR = ROOT / "database" / "dialogues_compiled"

COMPRESS_CHARS = 4000      # 单次压缩输入上限，超长块分段递进压缩
MIN_COMPRESS_CHARS = 200   # 低于此长度的场景摘要比原文还长，直接保留原文
MERGE_BELOW_CHARS = 500    # 相邻同类（同 keep）场景，前块小于此值时合并——防切碎

# 必保场景白名单（用户指定）：keep 必须为 true，逐字全文保留
MUST_KEEP = {
    "2.0": [
        "初遇：流萤在奥帝购物中心被当作偷渡犯追捕、向开拓者求助的整段",
        "秘密基地/天台的整段（流星、知更鸟歌声、'人们为何选择沉睡'、合照）",
    ],
    "3.0": [
        "结尾与流萤的道别对话整段",
    ],
    "3.8": [
        "流萤独白（虚无视界）整段",
        "流萤结尾部分整段",
    ],
}

_IDENTIFY_PROMPT = """你在整理《崩坏：星穹铁道》的剧情对话原文。任务：只划分场景边界，不改写任何内容。

## 输出（只输出一个 JSON 对象）
{{"scenes": [{{"anchor": "场景第一行开头连续12~20个字，逐字复制原文", "title": "场景短标题", "keep": false}}]}}

## keep 判定（默认 false）
- 只有命中下方"必保场景"清单的场景才标 true，其余一律 false
- 没有清单或没命中 = false
{must_keep}
## 规则
1. anchor 必须逐字复制原文片段（含标点），改一个字程序都会定位失败
2. 按剧情单元划分场景：地点/参与者/话题明显切换处分界
3. 第一个场景从文本开头算起；场景序列必须覆盖全文
4. 只输出 JSON，不要任何其他文字

## 对话原文
{text}"""

_COMPRESS_PROMPT = """把下面的剧情对话压缩成客观摘要：第三人称，2-4 句，保留关键剧情事实与人名，不加解读不加评价。只输出摘要文字。

{text}"""

_MUST_KEEP_MATCH_PROMPT = """下面是剧情对话切分后的场景列表（序号、标题、开头节选），以及若干"必保场景"描述。
判断每条必保描述对应哪些场景（可能一条描述横跨多个连续场景）。

## 必保场景描述
{rules}

## 场景列表
{scene_list}

## 输出（只输出一个 JSON 对象）
{{"keep_ids": [场景序号, ...]}}
命中的序号全部列出；没有命中的描述忽略。只输出 JSON。"""


def _apply_must_keep(scenes: list[dict], rules: list[str], client, model: str):
    """白名单二次精确匹配：划分模型的 keep 只是初筛，
    这里按场景开头节选逐一比对必保描述，命中的强制 keep=True。"""
    if not rules:
        return
    scene_list = "\n".join(
        f"[{i}] {s['title']}：{s['text'][:200]}".replace("\n", " ")
        for i, s in enumerate(scenes))
    raw = _call(client, model, _MUST_KEEP_MATCH_PROMPT.format(
        rules="\n".join(f"- {r}" for r in rules), scene_list=scene_list),
        max_tokens=4000)
    ids = _extract_json(raw).get("keep_ids", [])
    hit = 0
    for i in ids:
        if isinstance(i, int) and 0 <= i < len(scenes):
            if not scenes[i]["keep"]:
                scenes[i]["keep"] = True
                hit += 1
    print(f"  白名单二次匹配: 命中 {len(ids)} 个场景（新增强制保留 {hit}）")


def _get_client():
    from openai import OpenAI
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        cfg_file = ROOT / "user_data" / "config.json"
        if cfg_file.exists():
            key = json.loads(cfg_file.read_text(encoding="utf-8")).get("api_key", "")
    if not key:
        print("[ERROR] 未找到 API Key（环境变量 DEEPSEEK_API_KEY 或 user_data/config.json）")
        sys.exit(1)
    return OpenAI(api_key=key, base_url="https://api.deepseek.com/v1", timeout=600.0)


def _call(client, model, prompt, max_tokens, retries=3):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                rc = getattr(resp.choices[0].message, "reasoning_content", "") or ""
                raise ValueError(
                    f"空输出 (finish={resp.choices[0].finish_reason}, "
                    f"reasoning={len(rc)}字, out_tokens={resp.usage.completion_tokens})")
            if resp.usage.completion_tokens >= max_tokens - 10:
                raise ValueError("输出顶到 max_tokens，疑似被截断")
            return content
        except Exception as e:
            last_err = e
            print(f"    调用第{attempt}次失败: {e}")
            time.sleep(3)
    raise RuntimeError(f"LLM 调用连续失败: {last_err}")


def _extract_json(raw: str) -> dict:
    start = raw.find("{")
    if start < 0:
        raise ValueError(f"输出无 JSON: {raw[:100]}")
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start:i + 1])
    raise ValueError("JSON 大括号不闭合")


# ── 阶段1：场景划分 ─────────────────────────────────
def _fuzzy_find(text: str, anchor: str, start: int) -> int:
    """去空白后的模糊定位：返回原文中的近似位置，找不到返回 -1。"""
    strip = lambda s: re.sub(r"\s+", "", s)
    a = strip(anchor)
    if not a:
        return -1
    compact = []
    mapping = []
    for idx in range(start, len(text)):
        if not text[idx].isspace():
            compact.append(text[idx])
            mapping.append(idx)
    pos = "".join(compact).find(a)
    return mapping[pos] if pos >= 0 else -1


def identify_scenes(text: str, fp: Path, client, model: str) -> list[dict]:
    """全文划分场景。返回 [{"title","keep","text"}]，覆盖全文无缝隙。"""
    must_keep_rules = []
    for ver, rules in MUST_KEEP.items():
        if fp.stem.startswith(ver):
            must_keep_rules = rules
            break
    mk_section = ""
    if must_keep_rules:
        mk_section = ("\n## 必保场景（以下场景 keep 必须为 true，即使被细分，每个子场景也都是 true）\n"
                      + "\n".join(f"- {r}" for r in must_keep_rules) + "\n")

    raw = _call(client, model,
                _IDENTIFY_PROMPT.format(must_keep=mk_section, text=text),
                max_tokens=16000)
    scenes_raw = _extract_json(raw).get("scenes", [])
    if not scenes_raw:
        raise RuntimeError("划分结果为空")

    # 锚点定位 → 边界
    marks = []
    pos = 0
    misses = 0
    for s in scenes_raw:
        anchor = str(s.get("anchor", "")).strip()
        i = text.find(anchor, pos) if anchor else -1
        if i < 0:
            i = _fuzzy_find(text, anchor, pos)
        if i < 0:
            misses += 1
            print(f"    [WARN] 锚点定位失败（并入前一场景）: {anchor[:30]}")
            continue
        marks.append((i, str(s.get("title", "未命名")), bool(s.get("keep"))))
        pos = i + 1

    if not marks:
        raise RuntimeError("所有锚点定位失败")
    # 文首若有未覆盖文本，并入第一个场景
    if marks[0][0] > 0:
        marks[0] = (0, marks[0][1], marks[0][2])

    scenes = []
    for idx, (start, title, keep) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        if body:
            scenes.append({"title": title, "keep": keep, "text": body})
    total = sum(len(s["text"]) for s in scenes)
    print(f"  阶段1: {len(scenes)} 个场景（keep={sum(1 for s in scenes if s['keep'])}），"
          f"覆盖 {total}/{len(text.strip())} 字，锚点失败 {misses}")
    scenes = _merge_tiny(scenes)
    print(f"  合并碎块后: {len(scenes)} 个场景")
    _apply_must_keep(scenes, must_keep_rules, client, model)
    return scenes


def _merge_tiny(scenes: list[dict], threshold: int = MERGE_BELOW_CHARS) -> list[dict]:
    """相邻且 keep 相同的场景：前块 < threshold 字时并入后块（划分模型把连续
    对话切成一句一场景时的兜底）。标题保留首块标题。"""
    merged: list[dict] = []
    for s in scenes:
        if merged and merged[-1]["keep"] == s["keep"] and len(merged[-1]["text"]) < threshold:
            merged[-1]["text"] += "\n\n" + s["text"]
        else:
            merged.append(dict(s))
    return merged


# ── 阶段2：逐块压缩 ─────────────────────────────────
def compress_block(block: str, client, model: str) -> str:
    """≤COMPRESS_CHARS 一次压缩；超长则分段压缩后合并再压（递进）。
    max_tokens 给足：思考链与摘要共享输出额度（V4 思考模式 max_tokens 含 reasoning）。"""
    if len(block) <= COMPRESS_CHARS:
        return _call(client, model, _COMPRESS_PROMPT.format(text=block), max_tokens=4000)
    parts = [block[i:i + COMPRESS_CHARS] for i in range(0, len(block), COMPRESS_CHARS)]
    partial = [_call(client, model, _COMPRESS_PROMPT.format(text=p), max_tokens=4000)
               for p in parts]
    return _call(client, model, _COMPRESS_PROMPT.format(text="\n".join(partial)), max_tokens=4000)


# ── 主流程 ──────────────────────────────────────────
def compile_file(fp: Path, client, model: str) -> bool:
    text = fp.read_text(encoding="utf-8")
    print(f"\n== {fp.name}: {len(text)} 字")
    try:
        scenes = identify_scenes(text, fp, client, model)
    except Exception as e:
        print(f"[ERROR] 场景划分失败: {e}")
        return False

    out = [f"# {fp.stem} — 场景整理稿",
           f"\n> 来源：{fp.relative_to(ROOT)}（阶段1 LLM 划界+脚本机械切块，阶段2 LLM 压缩；待人工审核）",
           f"> 整理模型：{model}\n"]
    for i, s in enumerate(scenes, 1):
        keep = s["keep"] or len(s["text"]) < MIN_COMPRESS_CHARS
        tag = "原文" if keep else "摘要"
        print(f"  阶段2 [{i}/{len(scenes)}] {tag}: {s['title']}（{len(s['text'])}字）")
        if keep:
            body = s["text"]
        else:
            try:
                body = compress_block(s["text"], client, model)
            except Exception as e:
                print(f"    [WARN] 压缩失败，该场景保留原文: {e}")
                body, tag = s["text"], "原文"
        out.append(f"## 场景：{s['title']}（{tag}）\n\n{body}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / f"{fp.stem}_draft.md"
    out_file.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    print(f"  → {out_file.relative_to(ROOT)}")
    return True


def main():
    args = list(sys.argv[1:])
    model = "deepseek-v4-flash"
    if "--model" in args:
        idx = args.index("--model")
        model = args[idx + 1]
        del args[idx:idx + 2]
    name_filter = args[0] if args else ""

    files = sorted(DLG_DIR.glob("*.txt"))
    if name_filter:
        files = [f for f in files if name_filter in f.stem]
    if not files:
        print(f"没有匹配的文件: {name_filter}")
        sys.exit(1)

    client = _get_client()
    print(f"模型: {model}，待整理 {len(files)} 个文件")
    ok = sum(1 for fp in files if compile_file(fp, client, model))
    print(f"\n完成 {ok}/{len(files)}。审核 dialogues_compiled/*_draft.md 后去掉 _draft 后缀，"
          "再跑 python memory/rag/build_index.py")


if __name__ == "__main__":
    main()
