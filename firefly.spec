# -*- mode: python ; coding: utf-8 -*-
"""流萤 Firefly — 独立角色扮演聊天 Agent

PyInstaller 打包配置。产物：dist/firefly/firefly.exe（one-folder）。
用法：pyinstaller firefly.spec --noconfirm
"""

import sys
from pathlib import Path

block_cipher = None

# SPECPATH 是 PyInstaller 提供的 spec 文件所在目录
ROOT = Path(SPECPATH).resolve()

a = Analysis(
    ['app/server.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 前端静态文件
        (str(ROOT / "app/static"), "static"),
        # 首次启动的默认配置源
        (str(ROOT / "app/config.json"), "."),
        # 角色设定 + 表情包注册表源
        (str(ROOT / "app/assets"), "assets"),
        # 知识层在 2026-09-14 归位进角色包（assets/character/story/knowledge/，
        # 随上面的 assets 条目一并入包，不再单独列 datas）
        # 原始资料库（wiki 抓取物，仅查证）
        (str(ROOT / "database"), "database"),
        # 注意：docs/ 是开发/运维内部文档（含生产 IP、运维流程、协作记忆），
        # 不随发行版分发给用户——2026-09-08 移除（app/server 代码运行时不读 docs/）
    ],
    hiddenimports=[
        "modules.memory_manager",
    ],
    hookspath=[str(ROOT / "_pyinstaller_hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "sentence_transformers", "torch", "torchvision", "torchaudio",
        "PIL", "matplotlib", "scipy", "pandas",
        "cv2", "onnxruntime",
        # 2026-09-08：以下四包代码零 import（urllib3.contrib.pyopenssl 可选路径带入），
        # 实测占 _internal ~12.2MB，排除后不影响任何功能
        "cryptography", "bcrypt", "zstandard", "chardet",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='firefly',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "package" / "firefly.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='firefly',
)
