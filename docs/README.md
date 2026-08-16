# 流萤项目文档

## 当前架构（V5）

```
POST /chat → routes → orchestrator.handle_chat()
  │
  ├─ retrieve(knowledge) + retrieve(memory)
  ├─ Analyzer.analyze()      → intent / fact_check / summary
  ├─ Polisher.polish()       → 多条文本消息
  ├─ Organizer.organize()    → 可选表情包
  └─ ContextManager + conversation_store 写历史
```

## 代码索引

| 模块 | 代码 |
|------|------|
| 分析器 | [analyzer.py](../app/modules/analyzer.py) |
| 回复器 | [polisher.py](../app/modules/polisher.py) |
| 组织器 | [organizer.py](../app/modules/organizer.py) |
| 检索器 | [llm_retriever.py](../app/modules/llm_retriever.py) |
| LLM 基建 | [llm_base.py](../app/modules/llm_base.py) |
| 上下文 | [context_manager.py](../app/modules/context_manager.py) |
| 会话持久化 | [conversation_store.py](../app/modules/conversation_store.py) |
| 编排器 | [orchestrator.py](../app/orchestrator.py) |
| 路由 | [routes.py](../app/routes.py) |
| 记忆 | [memory_manager.py](../app/modules/memory_manager.py) |
| 表情包 | [sticker_picker.py](../app/tools/sticker_picker.py) |

## 全局文档

| 文档 | 说明 |
|------|------|
| [错误总结](错误总结.md) | 已知错误模式，每次启动必读 |
| [../CLAUDE.md](../CLAUDE.md) | 项目设计原则 |
| [开发规范](开发规范.md) | 开发执行规范：Flash 工作纪律 + 核心流程冻结 + 扩展统一接入 |
| [开发规范_扩展接入协议](开发规范_扩展接入协议.md) | 新工具/新消息类型的接入契约（注册、调度、历史、上下文） |
| [构建规范](构建规范.md) | 依赖位置总表 + PC/APK/安装器构建命令（开工省时间） |
| [服务器管理规范](服务器管理规范.md) | 生产服务器运维：拓扑、部署、数据安全、模型锁、下载通道 |
| [需求设计](需求设计.md) | 需求总览 V5 |
| [部署与发布约定](部署与发布约定.md) | 提交/推送/发布/部署/归档硬规则 |
| [搜索资料结果/](搜索资料结果/) | 调研笔记 |

旧 V4 模块规格（规划判断器/状态更新器等）已随代码删除，勿再引用。
