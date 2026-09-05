# 流萤项目文档

## 当前架构（V5，0.8.1）

详细的交互式架构图见根目录 **`项目架构图/index.html`**（离线双击打开：三端拓扑 / 对话流水线 / 模块矩阵 / 路由地图 / 发布链 / 安全体系）。

```
POST /chat → routes → orchestrator.handle_chat()
  │  前置审查（空/超长直接降级）
  ├─ llm_retriever.retrieve()  → 知识压缩摘要（haruno 跳过，失败不阻塞）
  ├─ analyzer.analyze()        → intent / fact_check / summary
  ├─ polisher.polish()         → 多条文本消息（可带识图）
  ├─ organizer.organize()      → 表情包 / 旁白（失败只丢装饰不阻塞文本）
  └─ context_manager + conversation_store 写历史（pipeline.jsonl 观测）
```

## 代码索引

| 模块 | 代码 |
|------|------|
| 路由（9 文件聚合） | [routes.py](../app/routes.py) + routes_common/auth/config/assets/fix/data/update/relay |
| 编排器 | [orchestrator.py](../app/orchestrator.py) |
| 分析器 / 回复器 / 组织器 | [analyzer.py](../app/modules/analyzer.py) / [polisher.py](../app/modules/polisher.py) / [organizer.py](../app/modules/organizer.py) |
| 检索器 / 记忆 | [llm_retriever.py](../app/modules/llm_retriever.py) / [memory_manager.py](../app/modules/memory_manager.py) |
| 主动性 | [proactive.py](../app/modules/proactive.py)（+ proactive_gate / proactive_gen） |
| 上下文 / 持久化 | [context_manager.py](../app/modules/context_manager.py) / [conversation_store.py](../app/modules/conversation_store.py) |
| 配置 / HTTP 客户端 | [app_config.py](../app/modules/app_config.py) / [api_client.py](../app/modules/api_client.py) |
| 存储 / 同步 | [storage.py](../app/modules/storage.py) / [sync_engine.py](../app/modules/sync_engine.py) |
| 表情包 | [sticker_picker.py](../app/tools/sticker_picker.py) |

## 规范与纪律（living，动工前读）

| 文档 | 说明 |
|------|------|
| [../CLAUDE.md](../CLAUDE.md) | 项目设计原则与开工必读清单 |
| [错误总结](错误总结.md) | 已知错误模式，每次启动必读 |
| [协作记忆](协作记忆.md) | 跨会话记忆：用户习惯 + 各次实施记录 |
| [开发规范](开发规范.md) / [开发规范_扩展接入协议](开发规范_扩展接入协议.md) | 开发执行纪律 + 新工具/消息类型接入契约 |
| [构建规范](构建规范.md) | 依赖位置总表 + PC/APK/安装器构建命令 |
| [服务器管理规范](服务器管理规范.md) | 生产服务器运维：拓扑、部署、数据安全、模型锁、下载通道 |
| [部署与发布约定](部署与发布约定.md) / [版本更新规范](版本更新规范.md) | 提交/推送/发布/归档硬规则 + 版本号规则 |
| [需求设计](需求设计.md) | 需求总览 V5 |

## 设计文档（已实施特性的方案存档）

| 文档 | 说明 |
|------|------|
| [设计/主动性架构v3方案](设计/主动性架构v3方案.md) | 信号量 + 三通道主动性 + 前后端协议 |
| [设计/消息引用与收藏](设计/消息引用与收藏.md) | 长按消息 → 引用/收藏（数据格式、存储、接口、测试） |
| [设计/设定纠错助手设计](设计/设定纠错助手设计.md) | 对齐 → 提案 → 批准应用 → 回滚 |
| [设计/设置页体验优化方案](设计/设置页体验优化方案.md) | 设置五分组 + 默认值降频（已实施） |
| [设计/提示词系统/](设计/提示词系统/) | 提示词系统现状梳理：调用链与实测体量 / 禁令清单 / 结构问题 / 四阶段耗时实测与并行方案 |
| [设计/角色预设化方案](设计/角色预设化方案.md) | 全角色扩展：模式解耦为角色×剧本×形态，预设包格式与七阶段实施计划（附[盘点清单](设计/角色预设化方案_盘点清单.md)） |
| [设计/前端/](设计/前端/) | PC 双栏重构 + 安卓 UI 优化（已实施） |

## 运维记录

| 文档 | 说明 |
|------|------|
| [安全日报](安全日报.md) | 每日巡检 + 攻击事件（攻击哨兵自动追加） |
| [安全加固执行记录-2026-08-14](安全加固执行记录-2026-08-14.md) | DSH 链路加固（受限账号/管理令牌） |
| [安全加固执行记录-2026-08-25](安全加固执行记录-2026-08-25.md) | 三端安全审查修复（已于 08-28 部署） |
| [成本与缓存记录](成本与缓存记录.md) | LLM 成本与 prompt 缓存命中率实测 |

## 归档

- [一期归档-架构重构/](一期归档-架构重构/)：0.8.x 架构重构（W0–W7）全套方案与执行日志
- 更早的快照（项目结构.md / 项目结构报告.md / 问题清单与待办.md / diagrams/）已于 2026-08-28 移出仓库，归档在 `封装（没指定就不读）/2026-08-28_归档/docs/`（git 历史仍可查）
