# 流萤项目术语表（GLOSSARY）

> 规则：新名词追加，改名/废弃在备注列记录去向。名词尽量映射到代码实体，可验证。

| 名词 | 定义 | 范围 | 备注 |
|------|------|------|------|
| 本地版 | PC（PyInstaller）+ 安卓（Chaquopy）独立运行形态，用户设备本机跑 Python | app/、android/ | 代码主线的原始形态 |
| 服务器版 | 云服务器多用户形态：后端在服务器，用户自带 Key，前端打包进 APK（file:// 加载 + 跨域 API） | server/（server_app.py、frontend/）、android_server/ | 复用 app/modules + app/routes，入口独立 |
| contextvars 用户上下文 | 每请求线程的用户数据作用域（user_dir/api_key/api_base），Flask 同款模式 | app/modules/app_config.py：set_user_context/reset_user_context/user_scope_key | 本地版不设置时行为零变化 |
| 匿名 UUID | ~~用户浏览器生成的匿名标识（localStorage firefly_uid），请求带 X-User-Id 头~~ | 已删除（2026-08-12 重构） | 废弃：认证改为 Bearer token，数据按 user_id 隔离 |
| 安装隐藏代码 | install_id：客户端首次启动本地生成的 `inst-`+32hex，一个安装一个，注册一次一用（防不重装重复注册） | server/auth.py `_valid_install_id`、login.html `getInstallId()`、db.users.install_id | 2026-08-13 加固：生成改用 crypto.getRandomValues（密码学随机），格式不变 |
| 邮箱验证码 | 注册验证：6 位数字发到 QQ 邮箱，5 分钟有效、一次性、60s 重发、每 IP 10 次/小时 | server/mail.py（SMTP smtp.qq.com:465） | 仅接受 @qq.com / @foxmail.com |
| QQ 群邀请码 | 注册必填 QQ 群号（1097936258），作为邀请制门槛 | server/auth.py QQ_GROUP_ID | APP 内嵌页可见 |
| Bearer token 会话 | 登录态：secrets.token_urlsafe(32)，30 天过期，可撤销 | server/db.py sessions 表、auth.auth_user | 每请求 Authorization 头 |
| user_scope_key | 缓存/信号量按用户隔离的作用域键（本地版恒为空串） | app_config.user_scope_key、llm_base/proactive 的缓存与状态键 | 防多用户缓存串扰 |
| 流水线 | 一轮对话的处理链：LLM 子代理检索 → 分析器 → 回复器 → 组织器 → 写盘 | app/orchestrator.py handle_chat | 本地版与服务器版共用 |
| 分析器 | 理解输入：意图 + 事实核查 + 摘要 | app/modules/analyzer.py | |
| 回复器 | 全权生成流萤的短信（[MSG] 格式） | app/modules/polisher.py | |
| 组织器 | 工具调度：story=表情包；haruno=旁白演出 | app/modules/organizer.py | |
| 检索器 | LLM 子代理：知识库整体注入 → 压缩摘要 | app/modules/llm_retriever.py | 替代旧向量 RAG（已废弃） |
| RAG | 旧向量检索方案（embedding + top-k 截断） | 已废弃 | 安卓无本地模型约束被弃用，勿按旧文档实现 |
| 双模式 | story（剧情模式）/ haruno（春日手信）数据独立 | app_config.MODES、user_data/{mode}/ | 各模式独立 character/data/journal |
| 信号量 | 主动性并发控制：REPLY（回复通道锁）/ ACTIVE（主动互斥）/ HIDDEN（隐藏式冷却） | app/modules/proactive.py | 服务器版按 (mode, 用户) 分键 |
| 主动性 | 流萤主动找开拓者：主动式（轮次+概率）/ 概率式（空闲触发）/ 隐藏式（安卓后台） | app/modules/proactive.py、前端轮询 | |
| 手账 | 流萤口吻的重要对话与约定记录 | user_data/{mode}/journal/手账.md | 休息时 LLM 更新 |
| 记忆 | 跨会话记忆：休息时整理 memory.md（头部概括 + 尾部事实） | user_data/{mode}/data/memory.md、memory_manager.py | |
| version.json | 服务器版检查更新的单文件版本源 | server/version.json（运维放置） | 本地版仍走 GitHub/Gitee 双源 |
| server_url | 遗留死配置：config.json 字段，无消费者 | app/modules/app_config.py | 待清理（服务器版用 Bearer token，不用此字段） |
| X-API-Key 头 | 服务器版用户 Key 传递方式（浏览器 localStorage → 请求头 → 服务器内存用后即弃） | server/frontend/app.js、server/server_app.py | 服务器不落盘 |
| 管理端口 8766 | 管理后台（admin.html），仅监听 127.0.0.1，SSH 隧道访问 | server/server_app.py AdminHandler | 能开隧道=有 SSH 密钥=管理者，无角色体系 |
| 优雅关闭 | 多开检测时旧实例保存文件后退出（os._exit） | app/shared_http.py shutdown_server | /shutdown 仅本机来源可触发 |
| relay 后端代理 | 服务器构建 LLM 请求入队（资产占位符化），APP 轮询取件→本地填充→用户 Key 直连代发→回传 | api_client.py、routes.py、前端 relayTick | 2026-08-13 补前端引擎 |
| 中转降级 | 直连被 CORS 拦截（GO 端点不支持浏览器跨域）时服务器用 X-API-Key 头代发：Key 内存即弃；call_id 须匹配队列中真实 pending 项（非开放代理） | routes.py relay_proxy、api_client.py relay_has | |
| 后台主动（服务器版） | KeepAliveService 前台服务定时触发页面 __serverProactive()，hidden_reply_enabled 门控 + FireflyJs 桥通知（仅后台） | android_server KeepAliveService/MainActivity、app.js | 复刻本地版隐藏式语义 |
