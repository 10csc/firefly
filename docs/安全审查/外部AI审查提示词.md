# 外部 AI 安全审查提示词（服务器 + 业务逻辑）

> 用途：把下面「提示词正文」整段复制给外部 AI（Claude / GPT / 其他）。
> 写法遵循两条：**给足位置线索**（省 token，别让它自己满仓库找）+ **不设边界**（允许它越出清单）。
> 生成于 2026-09-19，对应版本 0.9.0。

---

## 交给外部 AI 的材料清单（很重要，别只给仓库）

| 材料 | 为什么必须给 |
|---|---|
| 仓库压缩包（GitHub/Gitee 上的 `master`） | 本地版 `app/` 全在这里 |
| **`server/` 目录单独打包** | ⚠️ 它被 `.gitignore` 排除（规范 §4.4：服务器代码走 scp 部署、不进 git）。**只给仓库 = 看不到任何服务器代码**，而服务器正是本次审查重点 |
| `docs/错误总结.md` | 11 条已犯错误，含两条多用户/告警逻辑事故（见下文「已知历史坑」） |
| `docs/未完成事项清单.md` | 哪些是"已知未做"，避免把待办当漏洞重复报 |
| `docs/服务器管理规范.md`、`docs/部署与发布约定.md` | 部署拓扑与硬约束（判断"设计如此"还是"漏洞"用） |

**不要给**：`promo/`、`voice_samples/`、`docs/设计/`（大且与安全无关）—— 白烧 token。

---

## 提示词正文（从这里整段复制）

````text
你是一名资深安全审计员，请对这个项目做**源代码级安全审查**，重点在**服务器端**与**业务逻辑**。
输出要能让我直接去改代码，所以每条发现必须能落到「哪个文件、哪个函数、怎么触发」。

【项目是什么】
- 「Firefly（萤火虫）」：一个角色扮演 AI Agent，DeepSeek API 做后端模型。
- 三个交付形态，共享同一套前端（app/static/），**后端分两套**：
  1) 本地版（app/）：跑在用户自己的 PC / 安卓手机里，内置 HTTP 服务 127.0.0.1:8765，数据在 user_data/{角色包}/；
  2) 服务器版（server/）：多用户云部署，每个用户自带 DeepSeek API Key（宣称"Key 存浏览器
     localStorage、服务器不落盘"），数据按 user_id 隔离。
- 安卓壳：android/app/src/main/java/com/firefly/android/MainActivity.kt（WebView + JS 桥）。
- 关键信任链：热更新与公告通道都用 RSA-2048/SHA-256 签名，验签有三套实现
  （安卓 Kotlin / Python 标准库自实现（PC+服务器）/ 门禁脚本），公钥一致性由脚本强制。
- 本地版还有个"托管 API"模式：用户不自带 Key 时，请求经服务器转发（server/ 里）。

【材料与前提】
- 仓库里**没有** server/ 目录（被 gitignore）；server/ 我会单独给你。**服务器代码是本次重点，请务必审它。**
- 文档（README/docs）只作背景，**不要相信文档里的安全声明**，一律以代码为准；
  如果代码与文档矛盾，那本身就是一条发现。

【审查重点：请优先看这些位置（这是地图，不是边界）】

A. 服务器端（server/）
  1. 多用户隔离 / IDOR：server/app.py、server/db.py、server/auth_endpoints.py。
     每个按 user_id 取数据的端点，是否真的用**会话里的 uid**去拼路径/查库，而不是用请求里传来的
     mode/包名/uid？有没有"只校验登录、不校验归属"的端点？跨用户读/写/删是否可能？
  2. 认证与会话：server/auth.py。
     口令哈希算法与参数、是否加盐/是否可离线爆破；会话 token 的随机源、长度、过期、注销、并发登录；
     比较是否常量时间；注册/登录/找回密码/邮箱验证（server/mail.py）有无枚举、重放、频率限制缺失。
  3. 管理接口：server/admin.py、server/frontend/admin.html。
     admin 的鉴权方式（我记得有个 admin_token 文件），是否可预测/可爆破/是否在前端泄漏；
     管理动作（封禁、拉数据、改公告）是否有授权与审计。
  4. 配额与计费：server/quota.py。能否绕过（并发、换 Key、换 user_id、直接打上游）；
     托管模式下用户的 Key 是否可能被写进日志/数据库/异常堆栈。
  5. 下载网关：server/download_server.py、server/download_lib.py、server/download_page.py。
     /download/resolve 的 file= 参数是否白名单（路径穿越、任意文件读、SSRF 到 foruda）；
     响应头 CORS 为何是 *，会不会让任意网站在用户浏览器里读走东西；回退服务器本地文件的分支是否安全。
  6. 上传/导入（数据同步）：app/api/data_sync.py、app/routes_snapshot.py、app/modules/multipart.py。
     zip 解包是否防 zip-slip / 符号链接 / zip 炸弹；大小上限；导入是否会覆盖他人或系统文件；
     解压后写入的路径是否被用户可控字段拼接。

B. 业务逻辑（本地版同样适用）
  7. 记忆与归档口径：app/modules/memory_archive.py、app/modules/auto_rest.py、app/modules/conversation_store.py。
     ★ 这个项目**已经出过一次同类事故**：记忆整理游标用了"被 40 轮截断的内存窗口"，导致长对话重启后
     记忆永不更新且**零报错**（见 docs/错误总结.md #10）。请检查是否还有"用内存里的截断窗口当权威口径"
     的地方；后台线程/定时任务有没有取错角色包目录（历史上 auto_rest 后台线程取错过用户目录）。
  8. 提示词注入与消息拼装：app/orchestrator.py、app/api/chat.py、app/api/chat_ops.py。
     用户输入 / 角色设定 / 检索结果 / 公告或热更内容 是否会进入系统提示词？能否越权改变工具行为、
     读出别包数据、或让"组织器"产出破坏性指令？工具调用（如果有）的边界在哪。
  9. 密钥与隐私：app/core/config.py、app/modules/llm_base.py、app/routes_config.py、app/api/debug.py。
     API Key 的存储位置/权限/是否明文/是否进日志与异常；调试端点（debug.py）在生产形态下是否可达；
     本地版 127.0.0.1:8765 是否缺少鉴权（同机其它程序、浏览器页面、DNS rebinding 能否打它并改配置）。
  10. 诊断包脱敏：app/api/diag.py（本版本新加）。
      它宣称"不含 API Key、不含对话/记忆/手账正文"。请**证伪**：有没有任何成员会带出正文或密钥
      （注意 pipeline.shape.json 的设计意图是只留长度）。
  11. 公告通道：app/notice.py、app/routes_notice.py、app/static/js/notice.js。
      签名校验是否可绕过、manifest 能否被替换/降级/重放、kill-switch 与撤回是否可信；
      端侧渲染是否**完全** textContent（有没有漏出一个 innerHTML 面）。
  12. 热更新信任链：app/hotupdate/verify.py、app/hotupdate/pubkey.py、app/routes_hotupdate.py，
      以及 tools/check_hotupdate.py。补丁包解包是否防 zip-slip；能否用旧补丁覆盖新版本；
      三套验签实现是否**语义一致**（不一致 = 一处能过、另一处能过恶意包）；公钥替换路径是否可写。

C. 前端与安卓壳（只找可利用的，不做风格审查）
  13. XSS：app/static/js/ 下所有 innerHTML 赋值点（尤其 chat_render.js、views.js、notice.js、
      js/diag.js），有没有把服务端下发内容或用户内容当 HTML 插入。
  14. 安卓壳：MainActivity.kt。addJavascriptInterface 暴露给网页的方法有哪些、分别能做什么
      （注意有 clearDiagnostics/sendDiagnostics/notify 这类新加的方法）；
      shouldOverrideUrlLoading 对所有非内部 URL 直接 startActivity(ACTION_VIEW) 的风险；
      FileProvider 的路径白名单（res/xml/file_paths.xml）暴露面是否最小。

D. ★ 较新功能（2026 年才加、最没被审过 —— 请**重点**看，允许深挖）
  15. 角色卡 / 角色包系统：app/routes_pack.py（518 行）、app/api/pack_lifecycle.py、
      app/api/pack_paths.py、app/api/pack_assist.py、app/static/js/views.js（743 行）、
      app/static/js/panels/。
      要点：**包名/角色名是用户可控字符串且会被拼进目录路径** —— 能否用 `../`、绝对路径、
      空名、超长名、保留名（CON/NUL/.、Unicode 同形字、大小写）做穿越、覆盖他人包、或让删除
      操作删到包外？新建/复制/导入/导出/删除各动作有没有做**归属校验**（本地版单用户也要防
      包外写入）；包导入 zip 是否防 zip-slip；包删除是否可被并发/符号链接利用。
  16. 公告通道：app/notice.py（433 行）、app/routes_notice.py、app/static/js/notice.js。
      要点：签名信任根是否与热更共用（共用则一处失守全线失守）；manifest 的版本定向能否被
      降级/重放/替换；kill-switch 与撤回谁能触发；**图片代理端点**（服务器端 /notice-image）
      能否变成任意 URL 读取/SSRF；
      端侧渲染是否**完全** textContent（找一个漏出的 innerHTML 面就是一条 XSS，因为公告是
      服务端下发内容且会展示给所有用户）。
  17. 热更新：app/hotupdate/verify.py、app/hotupdate/pubkey.py、app/routes_hotupdate.py、
      tools/check_hotupdate.py、app/static/js/hotupdate.js。
      要点：补丁包解包防 zip-slip；能否用旧补丁覆盖新版本（降级攻击）；**三套验签实现
      （Kotlin / Python 自实现 / 门禁脚本）语义是否一致** —— 只要有一处更宽松，就等于全线可绕；
      公钥文件是否可被普通写操作替换；验签失败的降级路径是否安全（是否"失败了也照装"）。
  18. 日志 / 诊断导出：app/api/diag.py、app/api/data_export.py、app/routes_snapshot.py、
      app/static/js/diag.js、app/api/debug.py。
      要点：导出会不会**越界带出数据**（别包、别用户、备份目录、`auth.json`、`config.json`）；
      导出端点是否只在本地版可达（服务器版应 403）；`/requests`、`/pipeline`、`/metrics`
      这类"读全站缓冲"的端点在生产形态下的暴露面；导出文件落点与权限（安卓 Download、
      PC 下载目录）会不会被同机其它程序读到。

E. ★ 较老但必须回查：角色"后台主动消息"
  19. 这条单独审，因为它是**唯一在没有用户交互时自己动数据、自己发通知**的链路：
      app/static/js/proactive.js、app/modules/auto_rest.py、app/modules/memory_manager.py（527 行）、
      app/modules/conversation_store.py（481 行）、android/.../KeepAliveService.kt、
      MainActivity.kt 的 `FireflyJsBridge.notify`、以及服务器模式的推送路径。
      要点：
      a) 后台线程/定时器**取的是哪个角色包、哪个用户**？本项目**已经出过一次**「auto_rest
         后台线程取错用户目录」的事故 —— 请确认现在是否真的按 uid/包隔离，以及并发触发
         （用户切换角色包、删包、导入包的同时后台正在写）会怎样。
      b) 通知内容会不会把**对话正文**显示在锁屏/通知栏（隐私泄漏），通知 ID 能否被冒充/覆盖。
      c) 后台轮询/重试是否有上限（能不能被远端服务端拖着无限重试、无限唤醒、耗电/耗流量）。
      d) 主动消息的触发条件能否被**远端服务端**或**被篡改的本地状态**利用，造成越权发消息、
         或把用户数据带出去。
      e) 服务器模式下 `/relay`（app/routes_relay.py）与推送端点的鉴权：能否伪造他人消息。

F. ★ 上帝文件（God File）与可审计性 —— 请把它当**安全议题**审，不只是代码风格
  20. 下面是我实测的规模数据（有效行数，已排除 bundle.js 等生成物），你可以直接引用，不用自己统计：

      | 行数 | 文件 | 为什么算上帝文件 |
      |---|---|---|
      | 1467 | app/static/style.css（镜像 server/frontend/style.css） | 一个文件承载全部视图样式；PC 适配层已另开 pc.css，仍混杂 |
      | 1049 | app/static/index.html（镜像 server/frontend/index.html） | 视图标记 + 内联 `<style>` + SVG 精灵 + 引导层全塞一起 |
      | 762 | android/.../MainActivity.kt | WebView 初始化 + 两个 JS 桥 + 文件选择 + 返回键策略 + 前台服务 + 下载 + 诊断分享 |
      | 743 | app/static/js/views.js | 首页/角色包/设置/公告/引导 多个视图的渲染混在一起 |
      | 634 | app/static/js/chat.js | 发送/渲染/滚动/媒体/导出 全在一个文件 |
      | 538 | server/frontend/admin.html | 管理台页面 + 内联脚本 |
      | 527 | app/modules/memory_manager.py | 记忆读写 + 整理 + 窗口裁剪 |
      | 518 | app/routes_pack.py | 角色包 增/删/改/复制/导入/导出 全在一个 handler 集合 |
      | 481 | app/modules/conversation_store.py | 会话读写 + 轮数统计 + 迁移 |
      | 472 | app/infra/sync/restore.py | 恢复流程 |
      | 470 | app/static/js/settings.js | 各设置分组逻辑 |
      | 433 | app/core/config.py / app/notice.py | 配置 / 公告 |
      （共 281 个源文件；≥800 行 4 个，≥500 行 13 个）

      请回答三件事：
      · **哪些文件是真正的上帝文件**（判据：一个文件里混了几个**不同的变更原因**、有没有
        "同一段逻辑被复制到多处且已经开始不一致"、有没有哪个函数长到看不完）—— 不要只看行数，
        行数只是线索；也请指出我上表里**判断错的**（比如 CSS/测试文件行数大但不算上帝文件）。
      · **哪些上帝文件有安全后果**：典型是"一个大 handler 里某个分支忘了鉴权/忘了校验归属"，
        请具体点名**哪个文件里的哪个分支**有这种风险。
      · **怎么拆**（要可落地，不要泛泛而谈）：给出拆分方案（按什么维度拆、拆成哪几个文件、
        每个文件负责什么、对外接口怎么保持稳定），并标注**拆的顺序**（先拆哪个性价比最高、
        哪个一动就会牵动多处）。

【已知历史坑（请顺便验证是否真修好，别重复劳动）】
- docs/错误总结.md #10：截断的窗口当权威口径 → 记忆永不更新（见上 B7）。
- docs/错误总结.md #11：告警去重键选了"每次都变"的时间戳 → 一次 SSH 封禁被当 4-5 个事件反复弹窗。
  请审 server/attack_watch.sh 的去重键与冷却窗口。
- 线上服务器代码是**外科式最小改动**过去的，可能落后于仓库（规范里有记录）。若你发现
  "服务器行为与仓库代码不一致"的迹象，请单独列出。

【输出格式（请严格遵守，我要省 token 直接改代码）】
1. 先给 3-8 行**总体判断**：这套系统最危险的 2-3 个点是什么，为什么。
2. 然后一张表，按严重度从高到低，**最多 15 条**：
   | # | 严重度 | 位置（文件:函数/行） | 攻击者是谁、怎么触发（步骤） | 影响 | 证据（≤5 行代码摘录） | 修复方向 | 置信度 |
   严重度用：严重 / 高 / 中 / 低。
   置信度用：确认（代码里能闭环）/ 疑似（需运行验证）/ 存疑（我读不出，说明缺什么信息）。
3. 然后单独一节 **上帝文件**（最多 8 行）：
   | 文件 | 真上帝？(是/否/部分) | 混了哪几种变更原因 | 有无安全后果（点名分支） | 拆分方案（拆成什么，一句话） | 拆的优先级 |
   末尾用一行给**拆分顺序**（先拆哪个、哪个不要动）。
4. 最后一行单独写：**你没看的地方**（超出你上下文/权限/时间的部分），不要假装覆盖了全部。
5. 不要复述项目文档、不要写通用安全建议（"建议上 HTTPS""建议加日志"这类一律不写），
   只写**有具体触发路径**的问题。没发现问题就直说"该区域未发现问题"。

【边界声明】
上面 A–F 的清单是**地图不是围栏**：它只是帮你省 token 快速定位，**不是**审查范围的边界。
清单之外你发现的任何可利用问题（包括架构性、设计性、并发/竞态、异常路径、依赖与构建链、
以及"文档声称安全但代码不是"的矛盾）请一并报出，并在表里注明"清单外"。
D、E、F 三段是本轮**特别要求重点看**的部分（新功能、后台主动消息、上帝文件），
如果时间不够，请优先保 A（服务器）与 D（新功能），并如实说明砍掉了哪段。
````

---

## 使用建议

1. **分两轮打**（token 少时更划算）：
   - 第一轮只给 `server/` + 材料清单 + 提示词，让它专攻服务器与认证（A 段）；
   - 第二轮给 `app/` + 业务逻辑（B/C 段）。
   一次性给全量反而会让它把预算摊薄，报不出深处的东西。
2. 若它开始"概述项目"，回复一句：`跳过背景，直接出表`。
3. 拿到表后**逐条自己复核**再改代码 —— 外部 AI 的"确认"级结论也可能是误读（本项目已有先例：
   把注释里引用的旧写法当成真规则、把表单字段名 `mail_code` 当成 SMTP 授权码）。
4. 复核完把**真问题**写进 `docs/未完成事项清单.md`（唯一待办真相源），别只留在对话里。
