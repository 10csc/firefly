# 垃圾桶 — 弃用文件暂存区

> 规则：**暂时不删除**，弃用文件按原路径结构移入此处，并在下表标注（原位置 / 原应用逻辑 / 弃用原因 / 恢复条件）。
> 确认彻底无用后，由用户决定删除；git 历史始终可恢复。
> 注意：本目录内的文件**不会被任何代码加载或打包**（firefly.spec / syncBackend 均不包含 _trash/）。

## 索引

| 文件（本目录内） | 原位置 | 原应用逻辑 | 弃用原因 | 恢复条件 |
|---|---|---|---|---|
| `character/composer_samples.md` | `app/assets/character/` | V4「编排器」分条节奏样本：供编排器学习拆分短信、表情包插入方式（含【帕姆_害羞】贴吧标签格式） | V5 起回复器（polisher）全权生成短信序列，编排器模块已删除；内容含已禁止的贴吧表情标签格式，过时且无任何代码引用 | 若未来恢复独立"编排器"模块拆分短信 |
| `app/tools/bubble_updater.py` | `app/tools/` | V4 气泡工具：根据关键词/触发条件选择聊天气泡 key（BubbleDef 含 asset 图片引用、关键词、默认气泡），规划器 suggestion 用 | V5 气泡切换纯前端（localStorage + CSS 主题类），后端不再参与气泡决策；图片气泡资源已随 CSS 化删除，asset 引用失效；仅被自身测试引用 | 若未来恢复后端主导气泡调度 |
| `tests/test_bubble_updater.py` | `tests/` | bubble_updater 的白盒测试 | 依赖已弃用的 bubble_updater.py，一并暂存 | 随 bubble_updater 恢复 |
| `memory/index.md` | `memory/` | 个人经历知识库索引表（experience/story 各文件说明） | memory/ 目录整体重组：story/ 已并入 knowledge/story/，索引职责由 docs/README.md 与 knowledge/index.md 承担 | 无需恢复（内容已过时） |
| `memory/memory.md` | `memory/data/` | V5 早期记忆文件位置（记忆管理器 `_migrate_legacy` 的迁移源） | 仅 5 字节空残留（BOM+空行），迁移早已完成，user_data/data/memory.md 为现行位置 | 无需恢复 |
| `err.log` | `app/` | 运行期错误日志（*.log 已被 .gitignore 忽略） | 日志无逻辑价值，仅历史残留 | 无需恢复 |
| `root/屏幕截图 2026-07-09 194050.png` | 仓库根 | 本地截图杂物（已被 .gitignore 忽略） | 无逻辑价值 | 无需恢复 |

## 移入后已同步的引用清理

- `docs/README.md` 模块表：移除 bubble_updater 行、memory_manager 路径改为 `app/modules/memory_manager.py`
- `readme.md` 测试列表：移除 test_bubble_updater
- `firefly.spec`：移除 memory 相关 datas 与 hiddenimports
- `android/app/build.gradle.kts`：移除 `from("../../memory")` 同步块
