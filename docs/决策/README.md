# 架构决策记录

ADR 只记录影响长期维护、迁移或回滚的决定。状态可为 `accepted`、`superseded`、`deprecated`；被取代时新增一条 ADR，并链接旧记录。

| ADR | 决定 | 状态 |
|---|---|---|
| [0001](0001-本地优先同步.md) | 同步采用本地优先、服务器分配 rev/epoch | accepted |
| [0002](0002-服务器不提供App前端.md) | 服务器只提供后台 API 与角色卡平台 | accepted |
| [0003](0003-签名累积热更新.md) | 热更新使用签名累积包，只修 bug | accepted |
| [0004](0004-文档按内容类型治理.md) | 文档按 Concept/Task/Reference/Decision 分离 | accepted |
