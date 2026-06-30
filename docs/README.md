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
| 气泡更新器 | — | [bubble_updater.py](../app/tools/bubble_updater.py) |

## 数据流

```
用户输入 → 规划判断器 → 三路并行(MoodAdder+MoodDecayer+StateUpdater)
  → merge_moods + 倍率变化器 → 回复生成 → 上下文管理器
```

## 全局文档

| 文档 | 说明 |
|------|------|
| [错误总结](错误总结.md) | 已知错误模式，每次启动必读 |
| [流萤说话风格对照](流萤说话风格对照.md) | 角色说话风格参考 |
| [状态更新器设计说明](状态更新器设计说明.md) | 贴吧征求意见稿 |
| [../CLAUDE.md](../CLAUDE.md) | 项目设计原则和硬性约束 |
| [../需求设计.md](../需求设计.md) | 项目需求设计 |
