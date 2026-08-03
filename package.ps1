#!/usr/bin/env pwsh
# 流萤聊天 App — PyInstaller 打包脚本
# 用法:
#   .\package.ps1              # 默认 CPU 版本
#   .\package.ps1 -GPU         # GPU 版本 (需先 pip install torch)
#   .\package.ps1 -GPU -Dirty  # 不清理旧构建 (增量测试)
#
# 最终产物: dist/firefly/firefly.exe (~800 MB)

param([switch]$GPU, [switch]$Dirty)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$EXCLUDE = @(
    "torchvision", "torchaudio", "PIL", "matplotlib", "scipy", "pandas",
    "tqdm", "tokenizers", "huggingface_hub", "regex", "sympy", "mpmath",
    "networkx", "fsspec", "filelock", "packaging", "yaml", "safetensors",
    "cv2", "polars", "pyarrow", "onnxruntime", "opencv_python", "onnx"
) -join " "

$DATA = @(
    "--add-data", "app/assets;assets",
    "--add-data", "app/static;static",
    "--add-data", "memory/rag;memory/rag",
    "--add-data", "database;database",
    "--add-data", "app;app"
)

# 产物统一放在项目根下的 dist/（700MB 相对整个项目可接受，外部独立 build 会导致 frozen 路径解析复杂化）
# 清理
if (-not $Dirty) {
    Remove-Item -Recurse -Force dist, build, firefly.spec -ErrorAction SilentlyContinue
}

Write-Output "=== 打包 $(if($GPU){'GPU'}else{'CPU'}) 版 ==="

python -m PyInstaller --onedir --name firefly --noconfirm --workpath build --distpath dist $DATA --exclude-module $EXCLUDE app/server.py

if ($LASTEXITCODE -ne 0) { Write-Output "打包失败"; exit 1 }

# 打包后优化
python -X utf8 -c @"
import shutil
from pathlib import Path
DIST = Path(r'dist/firefly/_internal')
# 清理 transformers/models/ 只保留 sentence-transformers 需要的模型
m_dir = DIST / 'transformers' / 'models'
if m_dir.exists():
    keep = {'auto','bert','roberta','distilbert','xlm_roberta','mpnet','deberta_v2'}
    removed = sum(shutil.rmtree(d, ignore_errors=True) or 1 for d in m_dir.iterdir() if d.is_dir() and d.name not in keep)
    print(f'transformers/models: 移除 {removed} 个不需要的模型目录')
# 清理 torch 无关子目录
for sub in ['_C','cuda']:
    p = DIST / 'torch' / sub
    if p.is_dir(): shutil.rmtree(p); print(f'torch: 移除 {sub}')
# 体积
import os
def dsize(p):
    t = sum((Path(r)/f).stat().st_size for r,_,fs in os.walk(p) for f in fs)
    return round(t/1024/1024,1)
print(f'_internal: {dsize(DIST)} MB  总量: {dsize(DIST.parent)} MB')
"@

# 提示
$cpu_info = if ($GPU) {"GPU 版"} else {"CPU 版"}
Write-Output "`n=== 完成: dist/firefly/firefly.exe ($cpu_info) ==="
