# -*- coding: utf-8 -*-
"""app/domain/stickers — 表情包领域（阶段 2.7 自 app/tools/ 归位）

历史问题：表情包读写逻辑原来放在 `app/tools/` 包下，与仓库根的 `tools/`（开发脚本）
同名不同物，`import tools.sticker_picker` 的归属取决于 sys.path 顺序。现归到领域层，
旧的 `app/tools/sticker_picker.py` 保留为 re-export 兼容层（消费方零改动）。
"""
