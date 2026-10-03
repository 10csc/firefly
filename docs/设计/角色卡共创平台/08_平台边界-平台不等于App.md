# 08 平台边界：平台 ≠ App

> 建立：2026-10-01（task-22）。**本文写死一条架构约束**：服务器只做后台，不对外提供 App 前端。
> 上游：06（卡片格式）、07（客户端消费与安装）；部署侧矩阵见 `tools/check_server_surface.py` 的文档串。

## 一、用户原话与由此定下的边界

> 「不能直接在服务器使用 app 功能，服务器仅作为后台……我要的是没有所谓的
> `http://101.200.14.126:8787/index.html`，**只有角色卡平台**（只是这个平台需要登录），
> **不是让你搬一个 app 到服务器里**。」

排查确认的历史违规面（2026-10-01，task-22/23 修复前）：线上 `/index.html` **公开 200/79988B**
（App 壳）、`/js/bundle.js` 200/526KB、`/js/panels/*` 含 data/debug/packs/stickers 等**全部 App 面板**、
`/style.css`、`/pc.css` 均可公开取；`login.html` 登录成功还 `location.href = "index.html"`
⇒ **服务器登录页直接把人送进 App**。机制根源：`tools/sync_frontends.py` 当时把 `app/static`
**整份镜像**进 `server/frontend/`。

## 二、架构约束（硬性）

**服务器只提供三样东西**：

| # | 提供什么 | 落点 |
|---|---|---|
| 1 | **客户端 API**（账号/会话、数据同步、下载/更新/热更/公告——手机与 PC 都从这里取） | `server/*.py` + `app/` 的路由表 |
| 2 | **角色卡平台**（登录后使用：广场 / 制卡 / 我的卡 / 治理台） | `server/frontend/platform.html` / `platform.css` / `platform.bundle.js` |
| 3 | **登录页**（平台的入口与出口） | `server/frontend/login.html`、`/login` 别名 |

**不得提供**：`/index.html`（App 壳）、`/js/bundle.js`（App 运行时）、`/js/panels/*`（App 面板）、
`/style.css`、`/pc.css`（App 样式）。服务端闸门把它们挡成 **302 → 登录页（壳）/ 404（资源）**，
**即使文件还在 frontend/ 里也挡**（纵深防御）。

## 三、平台有什么、**没有**什么

| 有（平台的四个 tab） | 没有（一律属于 App，平台不出现） |
|---|---|
| **广场**：列表/分类/标签/搜索/官方筛选/排序/详情 | 聊天与角色扮演（消息流、回复器、分析器、组织器） |
| **制卡**：自由文本（固定三件 + 1..6 份知识库）+ 图片（头像/thumb/详情图/≤8 表情包）+ 存草稿/提交审核/发布/下架 | 记忆与手账、主动性消息 |
| **我的卡**：我的草稿箱（草稿/待复核/已发布/驳回），继续编辑/删除 | 设置页（模型/Key/供应商/公告/关于） |
| **治理台**（仅 admin）：全部卡（含未公开）、举报、下架/恢复 | 语音插件、诊断面板、热更页、教程引导 |
| | 角色卡管理（本机包编辑/导出/快照）、数据导入导出 |

判据不是"我觉得"：`tests/manual_platform_smoke.py` 逐条断言 DOM 里
`#app/#chat-view/#menu-btn/#messages/#cards-view/#pack-view/#pv-tree/…` **全部查不到**，
页面文本不含「打开服务器版/角色卡管理/聊天/设置/记忆/语音插件」。

## 四、资源边界（平台自包含）

- **只引用相对同目录的资源**：`bg-sunset.css`（晚霞背景）、`platform.css`、`config.js`、`platform.bundle.js`。
  **绝不**引用 `/js/bundle.js`、`/style.css`、`/pc.css`、App 面板 —— 服务端会 404（`check_server_contract.py` 有反向断言）。
- **`platform.bundle.js` 只打包平台真正依赖的 14 个模块**：
  `util / ui_select / imgzip / session_crypto` + 9 个 `panels/plaza*` + `server/frontend/platform.js`。
  App 壳（state/api/panels/packs/data/debug/stickers/chat/settings/voice/diag/guide/notice/hotupdate/relay）**一个都不进**。
  生成：`python tools/build_frontend_bundle.py --platform`（`--check` 进回归）。
- **`platform.css` 从 `app/static/style.css` 按选择器抽**（不整份复制）：
  `python tools/build_platform_css.py [--check]`。抽取白名单是**显式常量**——
  2026-10-01 踩过：漏抽 `.pfc-*`（制卡两列布局，写法是 `.pfc-` 而不是 `.pf-`）⇒ 表单被撑到 4696px、
  审核弹窗落到表单后面**点不到**（假 DOM 测试看不见，真浏览器 e2e 才抓到）。
- **DOM 契约复用**：`#plaza-view` 那一整块是从 `app/static/index.html` **照搬**的
  （id 与嵌套关系是 plaza 九个头文件的行为契约）⇒ **plaza 源码零改动**。
- **平台自己的薄壳**：`platform.html`（头部 + 导航 + 闸门层）、`platform.js`
  （fetch 包装 / App 钩子 shim / 登录探测 / 导航切换）；平台与 App 共享的只有那 14 个模块。

## 五、部署边界（三处机制防线）

| 防线 | 做什么 | 命令 |
|---|---|---|
| 1. 服务端闸门 | App 壳 302、App 资源 404；平台/登录/API 不在闸门内 | `python tools/check_server_surface.py [--strict-platform]` |
| 2. 客户端契约 | `app/static/**` 不得带前导 `/` 引用 App 页路径；客户端调用面 ⊆ 服务器注册表 | `python tools/check_server_contract.py` |
| 3. 同步白名单 | `tools/sync_frontends.py` **不再镜像 `app/static` → `server/frontend`**；server 侧只做平台白名单自检（缺成员=红；残留 App 文件=红） | `python tools/sync_frontends.py --check` |

配套：`app/static → 安卓 assets` 的同步**保持不变**（App 本地模式不受影响）；
`tools/check_apk_frontend.py` 仍是"APK 里前端与源一致"的发版闸（与平台无关）。

## 六、验收命令（本轮实测全绿）

```powershell
python tools/check_server_surface.py --strict-platform   # 服务端对外面矩阵（App 前端不可达 + 平台可达）
python tools/check_server_contract.py                    # 客户端引用面 ⊆ 服务器注册表 + 无 App 页引用
python tools/sync_frontends.py --check                   # server 侧平台白名单 + 安卓同步
python tools/build_frontend_bundle.py --platform --check  # 平台 bundle 与源码一致（14 模块 / 180.6 KB）
python tools/build_platform_css.py --check                # platform.css 与抽取口径一致
python tests/manual_platform_smoke.py                     # 真浏览器最小冒烟（51 项：未登录跳登录、零 App 入口、导航可切）
python tests/manual_platform_e2e.py                       # 真浏览器**完整链路**（29 项，见 07/本文 §七）
```

**完整链路**（`tests/manual_platform_e2e.py`，真 HTTP + 进程内真服务器 + 只桩"调模型那一次"）：
登录 → `/platform.html` → 广场有卡 → 详情 → 制卡（知识库 + 表情包 + thumb）→ 存草稿 →
提交审核 → 发布 → 我的卡 → 回广场看到新卡 → 非管理员被治理台拒绝 → admin 治理台列出全部卡；
`pageerror = 0`。截图存档 `_harden/shots/20..26_platform_e2e_*.png`。

## 七、已知边界 / 待办

1. `login.html` 登录后的落点是**模式感知**的：`http(s)` → `platform.html`；`file://`（安卓服务器模式
   加载的是同一个 `login.html` 拷贝）→ `index.html`（APK 里没有平台页，跳它会 404）。
2. 平台页的登录探测用 **`GET /auth/me`**（401=未登录）。**不要**用 `/auth/state`：服务器版下它固定
   返回 **403** `{"error":"服务器版请用 /auth/me"}`，拿它探测会把"已登录"也判成未登录 ⇒ 与登录页死循环。
3. 敏感头加密（`X-Firefly-Enc`）：平台与 App 同款——客户端在 `crypto.subtle` 可用时**总是**加密并
   删掉明文头；服务器只有 `FIREFLY_ENC_ENABLED=1` 且拿得到私钥（`FIREFLY_ENC_KEY`）时才解密。
   ⇒ **部署侧必须确认这两项已配**，否则任何浏览器客户端都拿不到自己的凭证（表现为登录后立刻 401）。
   本地 e2e 用的是 `~/.firefly/session_priv.pem`（与前端公钥同源，已核对模数一致）。
4. 平台与 App 的表单校验口径存在一处**宽严不一致**：制卡页本地放行 `sk-` 后跟 `[\w-]{8,}`，
   服务端要求 `^sk-[A-Za-z0-9]{16,64}$`（`app/plaza/review.py:30`）⇒ 带连字符的 Key 会被服务端 400。
   已报 Lead（改前端 regex 即可，不影响安全）。
5. `platform.css` 是"抽取快照"：App 的 `style.css` 改了之后平台**不会自动跟**，靠
   `build_platform_css.py --check` 把差异变成红灯（改完重跑一次即可）。
6. `/assets/`（App 静态资产：字体/背景/表情包）在服务器上仍公开——属**已知残留**，非本卡范围
   （`check_server_surface.py` 只作信息行，不判定）。
