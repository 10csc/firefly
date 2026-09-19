#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把语音模型（8 个文件，约 872 MB）上传到魔搭社区（ModelScope）。

## 为什么可以直接上传（2026-09-18 实测，非推测）

从本机安装的 `modelscope` SDK 常量读到平台硬限制：

| 项 | 平台限制 | 本模型 |
|----|---------|--------|
| 单文件 | `UPLOAD_MAX_FILE_SIZE` = **100 GB** | 最大 351 MB |
| LFS 触发阈值 | `UPLOAD_SIZE_THRESHOLD_TO_ENFORCE_LFS` = 1 MB | 8 个文件全部走 LFS |
| 非 LFS 小文件**合计** | `UPLOAD_NORMAL_FILE_SIZE_TOTAL_LIMIT` = 500 MB | 不适用 |
| 文件数 | 100000 | 8 |

> 网上流传的"魔搭单仓库上限 500MB"是把**非 LFS 小文件合计上限**误读成了仓库总容量：
> 官方 `Qwen/Qwen2.5-7B-Instruct` 仓库里就是 4 个 ~3.9 GB（共 15.2 GB）走 LFS 的权重。

## 用法

    # 令牌位置（见 docs/工具/tts.md §12.2）：本机**用户级环境变量** MODELSCOPE_API_TOKEN
    #   → 已配置好，日常不用再传令牌；新开终端才读得到。
    python tools/upload_voice_models.py --repo <命名空间>/<仓库名> --dry-run   # 先看清单
    python tools/upload_voice_models.py --repo <命名空间>/<仓库名>              # 真传
    python tools/upload_voice_models.py --repo <命名空间>/<仓库名> --check      # 只对账

令牌来源优先级：`--token` → 环境变量 `MODELSCOPE_TOKEN` / `MODELSCOPE_API_TOKEN` → SDK 登录态。
**一律只在内存里用，不落盘、不打印。**

★ 本机踩坑（2026-09-18）：`git config --global http.proxy = http://127.0.0.1:7897` 而代理没在跑
  → SDK 建仓时 `git clone` 会失败。跑之前先设 `$env:NO_PROXY="www.modelscope.cn,modelscope.cn"`
  （只影响该进程，不动 git 配置）。

## 版权（必须知悉）

模型是第三方 GPT-SoVITS 整合包 + 原声版权方的语音。公开仓库＝公开分发。因此本脚本：
  · 生成的模型卡**不含任何商标词**，只写"中文语音合成 ONNX 模型（GPT-SoVITS V4 导出）"；
  · 模型卡里写明"仅供个人学习、禁止商用、权利归原权利人、收到通知即下架"；
  · **不替你宣称开源许可**（`--license` 默认 other；平台预置的 8 个标准许可没有一个
    适合"转分发他人权重"这件事，填标准许可等于宣称你无权授予的权利）。

模块铁律：审查（令牌/仓库名/文件/平台限制）→ 处理（建仓 + 上传）→ 验证（sha256 逐文件对账）→ 输出
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# ★ 文件清单与"下载器要哪些文件"**同源**：直接用运行时代码里的那一个常量，
#   否则上传清单和下载清单会各自漂移（少传一个 → 用户下完仍缺文件）。
from voice.paths import MODEL_FILES  # noqa: E402

# 模型源：android_tts_app 的 assets（gitignored 的研究目录，已验证过的那一份）
SRC = ROOT / "FireflyVoiceResearch" / "android_tts_app" / "app" / "src" / "main" / "assets" / "models"

STAGE = ROOT / ".tmp_voice_upload"          # 暂存目录（硬链接，不复制 872MB）
API = "https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Revision={rev}"
REPO_URL_TPL = ("https://www.modelscope.cn/api/v1/models/{repo}"
                "/repo?Revision={rev}&FilePath={{file}}")

_REPO_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*/[A-Za-z0-9][\w.\-]*$")

README = """# 中文语音合成 ONNX 模型（GPT-SoVITS V4 导出）

本仓库是一套**中文语音合成（TTS）模型的 ONNX 部署文件**，供移动端/离线推理使用。
内容为第三方语音合成方案的模型权重与推理计算图，按 int8 量化导出。

## 文件

| 文件 | 用途 |
|---|---|
| `firefly_t2s_encoder.onnx` | 文本 → 音素特征编码 |
| `firefly_t2s_fsdec_int8.onnx` | 自回归解码首步 |
| `firefly_t2s_sdec_int8.onnx` | 自回归解码（带 KV cache） |
| `firefly_t2s_weights_int8.bin` | 上述解码器的权重 |
| `firefly_vits_int8.onnx` | 声学特征 → 隐变量 |
| `firefly_cfm_estimator_int8.onnx` | 条件流匹配（CFM）估算器 |
| `firefly_vocoder.onnx` | 隐变量 → 48 kHz 波形 |
| `firefly_bert_int8.onnx` | 文本语义特征（中文 RoBERTa） |

## 下载

```
https://www.modelscope.cn/api/v1/models/<ns>/<name>/repo?Revision=master&FilePath=<文件名>
```

## 声明

- 本仓库**仅为技术整合与推理格式转换**，不主张对模型权重的所有权。
- 模型所含音色数据来源于第三方作品，**权利归原权利人**；仅供**个人学习与研究**使用，**禁止商用**。
- 如权利人提出异议，将立即下架本仓库。
- 请勿将本模型用于伪造他人声音、冒充真人或任何违法用途。
"""

LICENSE_NOTE = """本仓库不授予模型权重的任何开源许可。

模型所含音色数据来源于第三方作品，权利归原权利人所有；本仓库仅为技术整合与
推理格式转换，仅供个人学习与研究使用，禁止商用。收到权利人异议将立即下架。
"""


def die(msg: str, code: int = 1):
    print(f"X {msg}")
    sys.exit(code)


def sha256_of(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def api_files(repo: str, rev: str = "master", timeout: int = 60) -> dict:
    """读仓库文件清单 → {文件名: {"size": int, "sha256": str}}。失败抛异常。"""
    url = API.format(repo=repo, rev=rev)
    req = urllib.request.Request(url, headers={"User-Agent": "FireflyVoiceUpload/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    files = ((data or {}).get("Data") or {}).get("Files") or []
    out = {}
    for f in files:
        out[f.get("Name") or f.get("Path")] = {
            "size": int(f.get("Size") or 0),
            "sha256": (f.get("Sha256") or "").lower(),
            "is_lfs": bool(f.get("IsLFS")),
        }
    return out


def resolve_token(cli_token: str) -> str:
    """令牌来源（按优先级）：
      1. `--token`
      2. 环境变量 `MODELSCOPE_TOKEN` / `MODELSCOPE_API_TOKEN`
      3. **SDK 已保存的登录态** —— 你先跑一次
         `modelscope login --token <SDK令牌>`（写到 ~/.modelscope/credentials），
         之后本脚本不用再传令牌，**令牌也不必发给任何人**。
    一律只在本进程内存里用，不落盘、不打印。
    """
    t = (cli_token or os.environ.get("MODELSCOPE_TOKEN")
         or os.environ.get("MODELSCOPE_API_TOKEN") or "").strip()
    if t:
        return t
    try:
        from modelscope.hub.api import HubApi
        return (getattr(HubApi(), "token", "") or "").strip()
    except Exception:
        return ""


# ── 1. 审查 ────────────────────────────────────────
def review(repo: str, token: str, private: bool) -> list:
    print("=== ① 审查 ===")
    if not token:
        die("没有可用令牌。三选一：\n"
            "    ① 先跑一次 `modelscope login --token <SDK令牌>`（写到本机 credentials，之后不用再给）；\n"
            "    ② `--token <SDK令牌>`；\n"
            "    ③ 设环境变量 MODELSCOPE_TOKEN。\n"
            "    令牌在 https://modelscope.cn/my/myaccesstoken 取「SDK 令牌」。")
    if not _REPO_RE.match(repo or ""):
        die(f"仓库名不合法：{repo!r}，应为 <命名空间>/<仓库名>（如 cpt0721/firefly-voice-v4-onnx）")
    if not SRC.is_dir():
        die(f"模型源目录不存在：{SRC}")
    files = []
    total = 0
    for name in MODEL_FILES:
        p = SRC / name
        if not p.is_file() or p.stat().st_size == 0:
            die(f"缺文件或为空：{p}")
        total += p.stat().st_size
        files.append(p)
    print(f"  V 令牌已提供（长度 {len(token)}，不打印内容）")
    print(f"  V 仓库名 {repo}（{'私有' if private else '公开'}）")
    print(f"  V 文件齐备：{len(files)} 个，合计 {total / 1048576:.1f} MB")

    # 平台限制预检（用 SDK 自己的检查器，与我上面表格里读到的常量同源）
    try:
        from modelscope.hub.api import UploadingCheck
        chk = UploadingCheck()
        for p in files:
            chk.check_file(str(p))
            if not chk.is_lfs(str(p), repo_type="model"):
                print(f"    ! {p.name} 未走 LFS（>1MB 应走 LFS），请确认")
        print(f"  V 通过 SDK UploadingCheck（单文件上限 "
              f"{chk.max_file_size / 1073741824:.0f} GB，非 LFS 合计上限 "
              f"{chk.normal_file_size_total_limit / 1048576:.0f} MB —— 我们不占用它）")
    except ImportError:
        print("  ! 未安装 modelscope SDK，跳过平台限制预检（上传阶段仍会校验）")
    return files


# ── 2. 暂存（硬链接 + 生成模型卡）────────────────────
def stage(files: list) -> Path:
    print("\n=== ② 暂存（硬链接，不复制 872MB）===")
    if STAGE.exists():
        shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    linked = copied = 0
    for p in files:
        dst = STAGE / p.name
        try:
            os.link(p, dst)                   # 同卷硬链接：瞬时、零额外占用
            linked += 1
        except OSError:
            shutil.copy2(p, dst)              # 跨卷兜底：真复制
            copied += 1
    (STAGE / "README.md").write_text(README, encoding="utf-8")
    (STAGE / "LICENSE").write_text(LICENSE_NOTE, encoding="utf-8")
    only = sorted(x.name for x in SRC.iterdir()
                  if x.is_file() and x.name not in {f.name for f in files})
    print(f"  V 暂存目录 {STAGE}")
    print(f"  V 硬链接 {linked} 个" + (f"，复制 {copied} 个" if copied else ""))
    print(f"  V 已生成 README.md / LICENSE（中性措辞，不含商标词）")
    if only:
        print(f"  ! 源目录里这些文件**不上传**：{only}")
    return STAGE


# ── 3. 上传 ────────────────────────────────────────
def upload(repo: str, token: str, staging: Path, private: bool, license_: str) -> None:
    print("\n=== ③ 建仓 + 上传 ===")
    from modelscope.hub.api import HubApi
    from modelscope.hub.constants import Licenses
    api = HubApi()
    api.login(access_token=token)
    visibility = "private" if private else "public"
    try:
        api.create_repo(repo_id=repo, visibility=visibility, repo_type="model",
                        license=license_, exist_ok=True)
        print(f"  V 仓库就绪：{repo}（{visibility}，license={license_}）")
    except Exception as e:
        msg = str(e)[:300]
        print(f"  X 建仓失败：{msg}")
        print(f"    平台预置许可是这 8 个：{Licenses.to_list()}")
        print(f"    若因 license 值不被接受：改用 --license \"GPL-3.0\" 重试；"
              f"或先在网页手动建仓（自己选许可），再用 --repo 只上传。")
        sys.exit(2)
    info = api.upload_folder(repo_id=repo, folder_path=str(staging), path_in_repo="",
                             commit_message="上传中文语音合成 ONNX 模型（8 文件）",
                             token=token)
    print(f"  V 上传完成：{info}")


# ── 4. 验证（sha256 对账）──────────────────────────
def verify(repo: str, files: list, rev: str = "master") -> bool:
    print("\n=== ④ 验证（远端 sha256 对本地逐文件对账）===")
    try:
        remote = api_files(repo, rev)
    except Exception as e:
        print(f"  X 读远端清单失败：{type(e).__name__}: {e}")
        return False
    ok = True
    for p in files:
        r = remote.get(p.name)
        if not r:
            print(f"  X {p.name}：远端**缺失**")
            ok = False
            continue
        local_sha = sha256_of(p)
        local_size = p.stat().st_size
        if r["size"] != local_size:
            print(f"  X {p.name}：大小不符 远端 {r['size']} vs 本地 {local_size}")
            ok = False
        elif r["sha256"] and r["sha256"] != local_sha:
            print(f"  X {p.name}：sha256 不符 远端 {r['sha256'][:12]}… vs 本地 {local_sha[:12]}…")
            ok = False
        else:
            flag = "LFS" if r["is_lfs"] else "普通"
            print(f"  V {p.name}：{local_size / 1048576:.1f} MB，sha256 {local_sha[:12]}…（{flag}）")
    # 只有 README/LICENSE/.gitattributes 是我们自己一起传的附属文件，不算"多出"
    expected = {p.name for p in files} | {"README.md", "LICENSE", ".gitattributes"}
    extra = sorted(set(remote) - expected)
    if extra:
        print(f"  ! 远端多出未预期文件：{extra}")
    print("\n=== ⑤ 输出 ===")
    tpl = REPO_URL_TPL.format(repo=repo, rev=rev)
    print(f"  仓库页：https://www.modelscope.cn/models/{repo}")
    print(f"  下载模板（填进 app/voice/plugin.py 的 DEFAULT_REPO_URL）：")
    print(f"    {tpl}")
    print(f"  例：{tpl.replace('{file}', MODEL_FILES[0])}")
    print(f"\n{'结果: PASS 可发布' if ok else '结果: FAIL 见上面的 X'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="上传语音模型到魔搭社区")
    ap.add_argument("--repo", required=True, help="<命名空间>/<仓库名>")
    ap.add_argument("--token", default=None, help="访问令牌（默认取环境变量 MODELSCOPE_TOKEN）")
    ap.add_argument("--license", default="other",
                    help="仓库许可字段（默认 other：**不替你宣称开源许可**）")
    ap.add_argument("--rev", default="master")
    ap.add_argument("--private", action="store_true", help="建私有仓库（匿名下载会失效！）")
    ap.add_argument("--dry-run", action="store_true", help="只审查 + 列清单，不建仓不上传")
    ap.add_argument("--check", action="store_true", help="只对账已上传的仓库")
    args = ap.parse_args()

    token = resolve_token(args.token)
    if args.check:
        files = [SRC / n for n in MODEL_FILES]
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            die(f"本地缺文件，无法对账：{missing}")
        return 0 if verify(args.repo, files, args.rev) else 1

    files = review(args.repo, token, args.private)
    staging = stage(files)
    print(f"\n  计划：上传 {len(files) + 2} 个文件"
          f"（{len(files)} 模型 + README.md + LICENSE）到 {args.repo}")
    if args.dry_run:
        print("  （--dry-run：到此为止，未建仓、未上传）")
        return 0
    upload(args.repo, token, staging, args.private, args.license)
    return 0 if verify(args.repo, files, args.rev) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
