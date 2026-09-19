# -*- coding: utf-8 -*-
"""把语音模型推到真机的**外部私有目录**（测试期用）

为什么用外部私有目录而不是内部 filesDir：
  · 装的是 **release 包（不可 debuggable）→ `run-as` 用不了**（实测 package not debuggable）
  · 而 `/sdcard/Android/data/<pkg>/files/` **adb 可直接写、app 自己能读**
  · 且它同样是 app 私有：卸载清空 ✓、覆盖更新保留 ✓
App 侧按优先级找模型（`app/voice/paths.py`）：
  1. `{内部}/voice/models/`      ← 正式：下载/导入后
  2. `{外部}/voice/models/`      ← 本脚本推的位置

用法:
  python tools/push_voice_models.py                 # 自动选设备
  python tools/push_voice_models.py -s 704c2441     # 指定设备
  python tools/push_voice_models.py --check         # 只列清单与体积，不推
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADB = Path(r"F:\Android_SDKset\platform-tools\adb.exe")
PKG = "com.firefly.android"

# 模型源：android_tts_app 的 assets（gitignored 的研究目录，已验证过的那一份）
SRC = ROOT / "FireflyVoiceResearch" / "android_tts_app" / "app" / "src" / "main" / "assets" / "models"

# 只要 int8 那一档（fp16 BERT 571MB 是"高精度档"，默认不用）
FILES = [
    "firefly_t2s_encoder.onnx",
    "firefly_t2s_fsdec_int8.onnx",
    "firefly_t2s_sdec_int8.onnx",
    "firefly_t2s_weights_int8.bin",
    "firefly_vits_int8.onnx",
    "firefly_cfm_estimator_int8.onnx",
    "firefly_vocoder.onnx",
    "firefly_bert_int8.onnx",
]

DST_DIR = f"/sdcard/Android/data/{PKG}/files/voice/models"


def sha12(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()[:12]


def pick_device(explicit=None):
    if explicit:
        return explicit
    out = subprocess.run([str(ADB), "devices"], capture_output=True, text=True).stdout
    devs = [l.split()[0] for l in out.splitlines()[1:]
            if l.strip() and l.split()[-1] == "device"]
    if not devs:
        print("✗ 没有已连接的设备（adb devices 为空）")
        sys.exit(1)
    if len(devs) > 1:
        print(f"⚠ 检测到多台设备 {devs}，用 -s 指定")
        sys.exit(1)
    return devs[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--serial", default=None)
    ap.add_argument("--check", action="store_true", help="只列清单，不推送")
    args = ap.parse_args()

    if not SRC.is_dir():
        print(f"✗ 模型源不存在: {SRC}")
        return 1

    print("=" * 84)
    print("语音模型清单（推到真机外部私有目录）")
    print("=" * 84)
    total = 0
    missing = []
    for f in FILES:
        p = SRC / f
        if not p.exists():
            missing.append(f)
            print(f"  ✗ {f:<36} 缺失")
            continue
        mb = p.stat().st_size / 1048576
        total += mb
        print(f"    {f:<36} {mb:8.1f} MB  sha256:{sha12(p)}")
    print(f"  {'合计':<36} {total:8.1f} MB")
    if missing:
        print(f"\n✗ 缺 {len(missing)} 个文件，先从 android_tts_app 备齐再推")
        return 1

    if args.check:
        print("\n（--check：未推送）")
        return 0

    dev = pick_device(args.serial)
    print(f"\n设备: {dev}")
    r = subprocess.run([str(ADB), "-s", dev, "shell", f"mkdir -p {DST_DIR}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"✗ 建目录失败: {r.stderr.strip()}")
        return 1

    for i, f in enumerate(FILES, 1):
        src = SRC / f
        print(f"  [{i}/{len(FILES)}] {f} …", end="", flush=True)
        r = subprocess.run([str(ADB), "-s", dev, "push", str(src), f"{DST_DIR}/{f}"],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        print("  ✓" if ok else f"  ✗ {r.stderr.strip()[:80]}")
        if not ok:
            return 1

    print("\n校验设备端文件大小：")
    # Android toybox `ls -l` 字段：perms(0) links(1) owner(2) group(3) **size(4)** date(5) time(6) name(7)
    # 曾误用 parts[-2]（那是时间）→ 全部显示"大小不符"的假警报。
    out = subprocess.run([str(ADB), "-s", dev, "shell", f"ls -l {DST_DIR}"],
                         capture_output=True, text=True).stdout
    bad = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[-1] in FILES:
            name = parts[-1]
            local = (SRC / name).stat().st_size
            try:
                dev_size = int(parts[4])
            except ValueError:
                dev_size = -1
            ok = local == dev_size
            if not ok:
                bad.append(name)
            print(f"  {name:<36} 设备 {dev_size:>10} / 本地 {local:>10}  "
                  f"{'✓' if ok else '✗ 大小不符'}")
    if bad:
        print(f"\n✗ {len(bad)} 个文件大小不符，重推: {', '.join(bad)}")
        return 1
    print(f"\n完成（{len(FILES)} 个文件大小全部对上）。")
    print(f"App 会从 {DST_DIR} 读取（无需任何权限）。"
          f"\n如需更强校验：adb shell \"cd {DST_DIR} && sha256sum *\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())