# 前端优化与重构 —— 文档索引

> 2026-08-17 立项；2026-08-18 已按两方案实施完毕（代码未提交 git）。本目录存放方案文档与实施记录。
> 按 CLAUDE.md 文档铁律：树状分文件，单文档超 ~200 行即拆分。

## 实施状态（2026-08-18）

两份方案均已落地，要点与偏差：

**PC 端重构（已实施 P0+P1+P2 主体）**
- P0：`app/static/app.js`（3138 行）已拆分为 `app/static/js/` 11 个 ES Module
  （state/util/api/panels/chat/fix/views/proactive/relay/guide/main + pc_nav），
  由一次性脚本按行段搬运 + 自动接线完成；重组逐行多重集比对一致、`node --check` 全过、
  DOM 打桩 import 测试通过。跨模块可变状态收口为 `js/state.js` 的 `S` 对象
  （waiting/_modeGen 之外的 7 个：_flushTimer/_hintTimer/_hasMore/_hiddenEnabled/_lastRenderTs/_rendering）。
- **偏差 1**：`window.xxx` 挂载保留（index.html 内联 onclick 暂不改 addEventListener；待 petite-vue 接管视图时自然消亡）。
- **偏差 2**：`window.fetch` 鉴权包装保留在 `js/api.js`（apiFetch 显式化迁移需服务器模式测试环境，另行安排）。
- P1：`vendor/petite-vue.iife.js`（0.4.1，已标注依赖头）驱动 PC 侧栏导航；
  **仅在 ≥1100px 视口动态加载框架**，移动端零接触。桌面默认直进聊天页。
- P2：≥1100px 双栏（左 300px 导航 + 右栏主区）；全屏视图经 `left:300px` 收进右栏，JS 视图逻辑零改动；
  720~1100px 过渡档放宽至 640px。引导层桌面定位未适配（已知余项）。

**安卓端 UI 优化（已实施「建议改」项，「保持现状」项未动）**
- 视觉：萤火暖金体系变量化（发送键/模式标签/时间线/纠错/引导统一）；用户气泡改青金；
  小字辅助文字回退系统字体；轮播指示点改星铁式微光短线；粒子色主题变量化（亮/暗各一套）；
  补齐 `:root` 缺失的 --gold-line/--gold-glow/--red/--green（原多处 fallback 悬空）。
- 交互：typing 圆点加呼吸缩放；输入栏 focus-within 浮起阴影；长按菜单缩放淡入；
  全局面板进出场过渡统一（淡入/上滑，纯 CSS）；发送后 5s 补话窗口占位符提示。
- 首页与导航：轮播限高 30vh 居中 + 新增「继续聊天 →」显式主入口（复用原闲置 .home-start 样式）；
  抽屉 tab 按频率重排（收藏/表情包前置）、状态占位 tab 隐藏；外观组头补缩放摘要。
- 性能：粒子页面隐藏/手动关闭不绘制（设置 → 外观新增开关）；低端机（内存≤4G 或核≤4）自动关毛玻璃。
- **未做**：5.2 WebView 基线实测（需真机）、5.4 图片 WebP 压缩（涉打包流程）、4.1 账号卡片折叠（与轮播限高二选一，已选后者）。

**配套更新**
- `tools/sync_frontends.py`：支持 js/ vendor/ 子目录同步、清理三处旧 app.js、`--check` 校验子目录漂移；
  同步前自动生成/校验 `js/bundle.js`（见下）。
- `tools/build_frontend_bundle.py`（2026-08-18 新增）：把 js/*.js 模块源码拼成 `js/bundle.js`（classic script）。
  **三端运行时统一加载 bundle.js，不再用 type=module**——安卓服务器模式以 file:// 加载页面，
  WebView 按 CORS 拦截 ES Module，曾致手机端整站 JS 失效（卡死在无 JS 的静态聊天页）。
  js/ 模块保留为开发源码（组织结构），改完必须跑该脚本重新生成（sync_frontends 已自动调用）。
- `tools/check_version.py`：CURRENT_VERSION 源改指 `app/static/js/panels.js`。
- 旧 `app/static/app.js` 备份于 `_trash/app.js.bak`（git 亦有历史）。
- 验证：`sync_frontends.py --check` 一致、`check_version.py` PASS、本地服务 8765 全部资源 200。
  无头浏览器本机不可用，PC 页面视觉效果未实机核对——启动 `python app/server.py` 后开
  http://127.0.0.1:8765/ 即可查验（≥1100px 窗口见双栏）。

## 文档清单

| 文档 | 内容 | 状态 |
|------|------|------|
| [PC端重构方案](PC端重构方案.md) | IM 双栏桌面布局 + 轻量框架（petite-vue）+ app.js 模块化拆分 | 已实施（见上） |
| [安卓端UI优化方案](安卓端UI优化方案.md) | 视觉风格 / 交互细节 / 首页与导航 / 性能与兼容，逐条标注「建议改 / 保持现状」 | 已实施（见上） |
| [截图/](截图/) | 2026-08-17 经 adb 从真机（1080×2376）截取的现状图，方案中引用 | — |

## 决策记录（2026-08-17 与用户确认）

1. **PC 技术路线：引入轻量框架**，单文件免构建（petite-vue 优先，Alpine.js 备选），不引入 Vite 等构建链。
2. **PC 布局方向：IM 双栏**——左侧导航栏 + 右侧聊天主区，类微信/QQ PC 版；不再沿用"手机竖屏居中 520px"。
3. **安卓端优化范围：全部四项**（视觉风格、交互细节、首页与导航、性能与兼容），
   **但用户特意设计的细节不改**（如消息间间隔）——方案中每条须标注「建议改 / 保持现状」，由用户逐条勾选后才进入实施。

## 实施前必读（现状关键事实）

- 前端唯一源：`app/static/`；副本：`server/frontend/`、安卓打包 assets。
  同步用 `tools/sync_frontends.py`（`--check` 校验漂移），版本号校验用 `tools/check_version.py`。
  **任何前端改动完成后必须跑这两个脚本**，否则三端漂移。
- `app/static/app.js` 为 3138 行无模块全局脚本，是 PC 重构的前置清理对象（拆分清单见 PC 方案 §4）。
- 安卓 minSdk = 26（`android/app/build.gradle.kts:79`），WebView 可随系统更新，但项目有"兼容旧 WebView 语法"的历史约束（app.js:2628 注释禁用 `??`），引入框架前需重新确认基线。
