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
        # 知识层（LLM 子代理检索读取，无本地模型）
        (str(ROOT / "knowledge"), "knowledge"),
        # 原始资料库（wiki 抓取物，仅查证）
        (str(ROOT / "database"), "database"),
        # 记忆模块（memory_manager 在 memory/ 下，作为 data 打包以支持动态 import）
        (str(ROOT / "memory/memory_manager.py"), "memory"),
        (str(ROOT / "memory/index.md"), "memory"),
        (str(ROOT / "memory/experience.md"), "memory"),
        (str(ROOT / "memory/story"), "memory/story"),
        (str(ROOT / "memory/wiki-compiled"), "memory/wiki-compiled"),
        # 文档（错误总结等）
        (str(ROOT / "docs"), "docs"),
    ],
    hiddenimports=[
        "openai",
        "memory.memory_manager",
    ],
    hookspath=[str(ROOT / "_pyinstaller_hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "sentence_transformers", "torch", "torchvision", "torchaudio",
        "PIL", "matplotlib", "scipy", "pandas",
        "cv2", "onnxruntime",
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
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='F:\\CodeFile\\firefly\\package\\firefly.ico',
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
