# 流萤项目文档

## 阶段总结

[阶段总结_V4.md](阶段总结_V4.md) — 当前阶段完成情况、数据流、已知问题

## 模块规格

| 模块 | 规格 | 代码 |
|------|------|------|
| 规划判断器 | [规格](modules/规划判断器/规格.md) | [planning_judge.py](../app/modules/planning_judge.py) |
| 上下文管理器 | [规格](modules/上下文管理器/规格.md) | [context_manager.py](../app/modules/context_manager.py) |
| 状态更新器 | [规格](modules/状态更新器/规格.md) | [state_updater.py](../app/modules/state_updater.py) |
| 心情更新器 | [规格](modules/心情更新器/规格.md) | [mood_updater.py](../app/modules/mood_updater.py) |
| 倍率变化器 | [规格](modules/倍率变化器/规格.md) · [参考值](modules/倍率变化器/参考值.md) | [rate_modifier.py](../app/modules/rate_modifier.py) |
| 状态解码器 | [规格](modules/状态解码器/规格.md) | [state_decoder.py](../app/modules/state_decoder.py) |
| 规划器 | [规格](modules/规划器/规格.md) | [planner.py](../app/modules/planner.py) |
| 工具调度器 | [规格](modules/工具调度器/规格.md) | [tool_dispatcher.py](../app/modules/tool_dispatcher.py) |
| 回复生成器 | [规格](modules/回复器/规格.md) | [reply_generator.py](../app/modules/reply_generator.py) |
| 消息编排器 | — | [composer.py](../app/modules/composer.py) |
| 记忆管理器 | [规格](modules/记忆管理器/规格.md) | [memory_manager.py](../app/modules/memory_manager.py) |
| 编排器 | — | [orchestrator.py](../app/orchestrator.py) |

## 工具规格

| 工具 | 规格 | 代码 |
|------|------|------|
| 气泡更新器 | — | [bubble_updater.py](../app/tools/bubble_updater.py) |
| 表情包选择器 | [规格](tools/表情包选择器规格.md) | [sticker_picker.py](../app/tools/sticker_picker.py) |

## 数据流 (V4.7)

```
POST /chat → server.py（HTTP+会话）→ orchestrator.handle_chat()
  │
  ├─ PlanningJudge.judge()           → JudgeResult
  ├─ 三路并行
  │   ├─ MoodAdder.add()              → 新增情绪
  │   ├─ MoodDecayer.decay()          → 消退后情绪
  │   └─ StateUpdater.update()        → raw delta
  ├─ merge_moods() + compute_rates() + state_updater.finalize()
  │
  ├─ [direct] → 静态话术
  ├─ [normal]
  │   ├─ state_decoder.decode()       → 数值→自然语言
  │   ├─ Planner.plan()               → {tools, tone, direction}
  │   │   └── 缓存分层: system(stable+menu) → user(历史全量) → user(动态)
  │   ├─ tool_dispatcher.pre_dispatch() → 工具执行
  │   ├─ ReplyGenerator.generate(memory_head)
  │   │   └── 缓存分层: system(稳定) → [system(记忆)] → user(历史全量) → user(动态)
  │   └─ Composer.compose()           → 分句+消息序列
  │
  └─ ContextManager.record()          → 历史写入（纯追加，无截断）
```

## 全局文档

| 文档 | 说明 |
|------|------|
| [错误总结](错误总结.md) | 已知错误模式，每次启动必读 |
| [流萤说话风格对照](流萤说话风格对照.md) | 角色说话风格参考 |
| [状态更新器设计说明](状态更新器设计说明.md) | 贴吧征求意见稿 |
| [../CLAUDE.md](../CLAUDE.md) | 项目设计原则和硬性约束 |
| [../需求设计.md](../需求设计.md) | 项目需求设计 |
