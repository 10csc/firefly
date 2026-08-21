# 流萤 Android App（0.8.0 双模式）

流萤聊天的 Android 客户端：Kotlin WebView 壳 + Chaquopy 内嵌 Python 3.12 引擎 + 打包前端资产。

## 双模式

- **本地模式（默认）**：壳启动 Chaquopy 引擎 → 引擎 HTTP 绑定 `127.0.0.1:8765` 提供统一前端；数据在手机私有目录（`user_data/`），Key 存后端 config.json；后台主动 = Python 直调 `backdoor_proactive_check`。
- **服务器模式**：壳不启动引擎 → WebView 加载 `file:///android_asset/index.html`；壳拦截 `config.js` 请求动态注入 `FIREFLY_MODE="server"` + `FIREFLY_SERVER_BASE`（`assets/config.js` 单点）；登录 Bearer + Key localStorage + relay 引擎 + 资产本地化；后台主动 = 页面 `__serverProactive()`。
- 模式切换：设置面板「运行模式」→ `FireflyMode.setMode` 桥 → SharedPreferences `firefly_mode` → `finishAffinity()` + 杀进程重启。

## 构建

```powershell
.\build_apk.ps1      # 自动下载 SDK + 构建（产物 app-release.apk）
```

构建时自动执行两件事：
- `syncBackend`（preBuild）：把仓库根 `app/`、`knowledge/`、`database/` 拷进 `src/main/python/backend`（Chaquopy 数据目录）；
- `syncFrontendAssets`：`app/static`（唯一源，排除 config.js）+ `app/assets` 子集 + `server/frontend/{config.js,login.html}` → `src/main/assets`。

依赖：JDK 17+、Android SDK API 35、Gradle 8.9（本机路径见 `docs/构建规范.md`）。
签名：`keystore/firefly.jks` + `keystore/keystore.properties`（缺失时回退 debug 签名）。

## 注意事项

- 前端唯一源是 `app/static/`，改前端后必须跑 `python tools/sync_frontends.py`（app/static → server/frontend + 安卓 assets），验收跑 `--check`。
- AndroidManifest `usesCleartextTraffic=true`（服务器走 http 公网 8787）；Manifest 标签属性列表中间禁止插入 XML 注释（历史事故，见 docs/问题清单与待办.md）。
- 后台回复推送走 KeepAliveService（轮询通道），不依赖 FCM。
