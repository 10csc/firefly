# -*- coding: utf-8 -*-
"""文本损坏体检：**判定性**检测"UTF-8 被按 GBK 读写"造成的乱码 + JS 语法错。

为什么需要它（2026-10-01 事故，见 `docs/错误总结.md` #26）：
`server/frontend/login.html` 在 2026-08-14 被一次 `Get-Content -Raw`/`Set-Content` 式改写整体弄坏——
52 行中文变乱码，其中 **6 处字符串的闭合引号被连带吃掉** ⇒ 内联 `<script>` **解析失败、
整段不执行** ⇒ 网站登录页"能打开、点登录没反应"，而且**线上躺了一个半月没人发现**
（curl 200、表单可见，全都骗过去了）。`server/` 不在 git 里，连发布快照都是坏的。

**判据（不看"像不像乱码"，而看能不能反解）**：
真正的 GBK 乱码是"原 UTF-8 字节被按 GBK 解码"的产物 ⇒ 把该行**重新按 gb18030 编码、再按 utf-8 解码**
会成功；而正常中文文本走这一步会失败。这样 `pinyin.json` 这类字典文件里的生僻字**不会误报**
（第一版靠"含某些怪字"去猜，一次性误报 20+ 个文件）。

顺带体检：每个 `.js` 与每个 `.html` 的内联 `<script>` 都过一遍 `node --check`。

用法：
    python tools/scan_text_corruption.py                 # 扫默认目录
    python tools/scan_text_corruption.py app/static server/frontend
退出码：0 = 干净；1 = 有问题（**适合当作发版前的附加检查，或升格为第 6 道门禁**）。
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = ("app/static", "server/frontend", "android/app/src/main/assets")
EXTS = {".html", ".js", ".css", ".json"}
CJK = re.compile(r"[\u4e00-\u9fff]")
PUA = re.compile(r"[\ue000-\uf8ff\ufffd]")


def reversible_mojibake(line: str) -> str | None:
    """该行是否为 GBK 乱码 ⇒ 返回反解后的文本；否则 None。"""
    if not any(ord(c) > 127 for c in line):
        return None
    try:
        cand = line.encode("gb18030").decode("utf-8")
    except Exception:
        return None
    if cand == line:
        return None
    if CJK.search(cand) or len(cand) < len(line) * 0.8:
        return cand
    return None


def main(argv: list) -> int:
    dirs = argv[1:] or list(DEFAULT_DIRS)
    files = []
    for d in dirs:
        p = (ROOT / d) if not Path(d).is_absolute() else Path(d)
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in EXTS]
    files = sorted(set(files))
    print(f"扫描 {len(files)} 个文件（目录：{', '.join(dirs)}）")

    mojibake, syntax_bad, undecodable = {}, {}, []
    tmp = Path(tempfile.mkdtemp(prefix="ff_corrupt_"))
    for fp in files:
        try:
            txt = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            undecodable.append((fp, str(e)[:70]))
            continue
        bad_lines = [(i, fx) for i, ln in enumerate(txt.splitlines(), 1)
                     if (fx := reversible_mojibake(ln)) is not None]
        if bad_lines:
            mojibake[fp] = bad_lines
        if PUA.search(txt):
            mojibake.setdefault(fp, []).append((0, "（含 U+FFFD/私用区字符）"))
        # 语法体检
        if fp.suffix.lower() == ".js":
            srcs, is_mod = [txt], bool(re.search(r"^\s*(import|export)\s", txt, re.M))
        elif fp.suffix.lower() == ".html":
            srcs = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", txt, re.S | re.I)
            is_mod = False
        else:
            srcs, is_mod = [], False
        for k, s in enumerate(srcs):
            if not s.strip():
                continue
            tgt = tmp / (fp.stem + f"_{k}" + (".mjs" if is_mod else ".js"))
            tgt.write_text(s, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(tgt)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                first = (r.stderr or "").strip().splitlines()
                syntax_bad[(fp, k)] = first[0] if first else "?"

    ok = True
    if mojibake:
        ok = False
        print("\nX 文本损坏（GBK 乱码 或 含替换符/私用区）：")
        for fp, bad in mojibake.items():
            print(f"  {fp.relative_to(ROOT)}  共 {len(bad)} 行")
            for i, fx in bad[:5]:
                print(f"     {i}: {fx.strip()[:100]}")
            if len(bad) > 5:
                print(f"     …（还有 {len(bad) - 5} 行）")
    if syntax_bad:
        ok = False
        print("\nX JS 语法不通过（丢了引号/括号的强信号）：")
        for (fp, k), msg in syntax_bad.items():
            print(f"  {fp.relative_to(ROOT)} [块{k}] {msg[:100]}")
    if undecodable:
        ok = False
        print("\nX UTF-8 解码失败：")
        for fp, msg in undecodable:
            print(f"  {fp.relative_to(ROOT)} {msg}")

    print(f"\n结果: {'PASS 未发现文本损坏' if ok else 'FAIL 见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
