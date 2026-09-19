# 工具设计 · tts（语音合成插件）

> 配套 [`docs/开发规范_扩展接入协议.md`](../开发规范_扩展接入协议.md) §7；本文件是该工具的规格文档（协议 §1 要求）。
> 任务归属：[`docs/架构重构计划/05_阶段4_业务深化与语音插件.md`](../架构重构计划/05_阶段4_业务深化与语音插件.md) 任务 4.1。
> ⚠️ **本文件对任务 4.1 原卡有三处修订**（PC/安卓次序、服务器开关、引擎语言），见 §10。
> 状态：设计定稿，待实施。最后更新 2026-09-18。

---

## 1. 目标与边界

本地离线、零云端依赖的角色语音合成。模型为第三方整合包（B 站「星萤青焰灼」）的
GPT-SoVITS V4 微调权重，已导出 ONNX + INT8。

**做**：本地推理、按需点播、结果落盘复用、插件生命周期管理。
**不做**：云端推理、对话流水线改造、把音频写进 `conversation.jsonl`、预生成。

版权：语音原声归米哈游；整合包作者声明禁止售卖。模型**不随安装包分发**，由用户触发下载。

## 2. 触发方式（用户确认 2026-09-18）

**长按消息 → 菜单 → 「转语音」**。菜单已存在（`app/static/js/chat.js` 的 `_msgMenu`，现为 引用/收藏），加第三项。

| 规则 | 行为 |
|---|---|
| 只在**流萤的消息**上可点 | 用户消息无语音 |
| 已有语音 | **直接播放**，不重复合成 |
| 正在合成别的 | **置灰** + 文案「正在生成…」（同时只能一个） |
| 插件未安装 / 未启用 / 不可用 | **置灰** + 一行原因 |

## 3. 目录与存储注册

| 内容 | 位置 | `storage.REGISTRY` 键 | sync |
|---|---|---|---|
| 插件本体（ONNX 7 件 ≈ 872 MB + 预计算参考 ≈ 4 MB） | `user_data/plugins/voice/` | `voice_engine` | **exclude** |
| 生成的语音（`v{seq}.wav`） | `user_data/{mode}/data/voice/` | `voice_cache` | **exclude** + LRU 上限 |

- 两项都 `exclude` → **不进同步、不进快照、不进导出**（"不经过服务器"由配置保证）。
- 插件本体与对话数据**分离** → 不被"清理历史"误删。
- `media=True`（本体是二进制，导出/同步按媒体处理口径）。
- ⚠️ **安卓自动云备份必须排除这两个目录**：清单 P0-1 记录当前 APK `allowBackup=true`，
  app 私有目录会被系统自动云备份 → **872 MB 模型会上传到云**，既拖垮备份，也违背"只在本地"。
  → 构建时用 `android:fullBackupContent` / `dataExtractionRules` 排除
  `user_data/plugins/` 与 `user_data/*/data/voice/`；随 P0-1 一并验收。

## 4. 插件生命周期与状态

`GET /voice/plugin-status` 返回：

| 状态 | 含义 | 菜单可用 | 可执行操作 |
|---|---|---|---|
| `not_installed` | 未下载 | 否 | 下载 |
| `downloading` | 下载中（带 `percent`） | 否 | 取消 |
| `installed_disabled` | 已下载·未启用 | 否 | 启用 / 卸载 |
| `ready` | 已启用·资源就绪 | **是** | 停用 / 卸载 |
| `unavailable` | 已启用但不可用，带 `reason` | 否 | 重试 / 卸载 / 看原因 |

`reason` 取值：`内存不足` / `模型文件损坏` / `缺少组件` / `引擎加载失败`。

**资源就绪判定**（不用环境开关，见 §10）：

```
ready = 引擎可加载（依赖+组件齐+sha256 通过）
        且 当前可用内存 ≥ 阈值
        且 插件处于启用状态
```

### 4.1 UI 落位（用户确认 2026-09-18）

**插件管理入口放在首页**，位置与「角色卡管理」同排、其后：

```html
<!-- app/static/index.html  .home-entry-row（现为 继续聊天 → / 角色卡管理） -->
<button class="home-entry" id="voice-plugin-btn"
        type="button" onclick="openVoicePluginView()">语音插件</button>
```

- 与「角色卡管理」**同级**（都是首页一级入口），不埋进设置页 —— 与「指出问题」同等的可达性。
- 全屏页形态参照 `角色卡管理`（`#cards-view`，`views.js` 的 `openCardsView()`）。
- 未安装时按钮**不打红点**（避免催促感）；仅当"已下载未启用"或"不可用"时显示一个中性角标。
- ⚠️ 前端唯一源是 `app/static/`，改完必须跑 `python tools/sync_frontends.py` 并 `--check`（三副本 + bundle）。

## 5. 接口

```
GET  /voice/plugin-status            → §4 状态对象
POST /voice/plugin-install           → 开始下载（异步，进度走 plugin-status）
POST /voice/plugin-enable|disable    → 启用/停用
POST /voice/plugin-uninstall         → 卸载（删插件本体目录，互动数据不受影响）
POST /tts {seq, mode}                → 合成指定 seq 的消息 → {ok, src, dur, tokens, error}
GET  /voice/{mode}/v{seq}.wav        → 返回音频（前端 <audio>）
```

`POST /tts` 语义：**幂等**。已有 `v{seq}.wav` 直接返回；否则入队（同刻仅一个任务）。
返回 422（业务失败，带原因）/ 503（插件不可用）/ 409（已有任务在跑，带当前 seq）。

## 6. 清理规则（用户确认 2026-09-18）

| 触发 | 动作 | 挂钩点 |
|---|---|---|
| **卸载软件** | 全清 | `user_data` 在 app 私有目录 → 系统自动，**零代码** |
| **清理历史记录** | 清空 `{mode}/data/voice/` | `app/api/chat_ops.py::clear_history`（已有"会话数据随历史清理"块，加一步） |
| **撤回上一轮** | 删 `seq > 当前最大 seq` 的 wav | `app/api/chat_ops.py::undo`，在 `remove_last_turn()` 之后 |
| **更新（覆盖安装）** | **不触发** | app 私有目录保留；不挂任何版本号流程 |

撤回采用"**按当前最大 seq 截断**"而非"删最后一轮"：对 seq 间隙、连续撤回、跨模式均成立，
不依赖 `remove_last_turn` 的返回值口径。

## 7. 并发

- 引擎侧**串行锁**：同一时刻只跑一次合成（已实测有效）。
- 服务侧**任务状态**：`{state: idle|working, seq, started_at}`，供菜单置灰与 409 判定。
- 合成在**独立执行体**内跑，不阻塞主 API（见 §10）。

## 8. 降级

任何一环失败都**静默退回纯文字**，不重试风暴、不阻塞聊天：

| 环节 | 失败表现 |
|---|---|
| 依赖缺失 / 组件不全 | `ready=false`，菜单置灰 |
| 内存不足 | 合成前探测，拒绝并报 `内存不足` |
| 守卫拦截（输出异常） | 返回 `{ok:false, error}`，前端提示可再试；**不自动重试** |
| 合成超时 / 进程崩溃 | 执行体重启；本次失败 |

## 9. 实测数据（2026-09-17/18，本机 `Ryzen 7 6800H` / 16 GB，CFM=4）

| 项 | 值 |
|---|---|
| 单次合成 | **18.7 s**（44 次均值）；开心 18.7 / 悲伤 18.8 |
| 引擎加载 | **23.6 s**（懒加载，首次点播才发生） |
| 守卫通过率 | **44/48 = 92%**（开心 24/24、悲伤 20/24） |
| 崩溃分布 | **文本相关**：T1 16/16、T2 16/16、**T3 12/16** |
| 常驻内存 | **2835 MB**（优化前 5365 MB） |
| 其中 ONNX 模型 | 872 MB（CFM 332.7 + BERT 351.4 为大头，均已 int8） |
| 预计算参考（2 语气） | **3.91 MB** |

### 独立执行体实测（2026-09-18，`_idle_check.py`）

| 阶段 | 主进程 RSS | 子进程 RSS | 系统可用内存 |
|---|---|---|---|
| 基线 | 18 MB | — | 9702 MB |
| 拉起（未加载模型） | 18 MB | 336 MB | 9335 MB |
| 合成 1 次后 | **17 MB** | 2826 MB | 6749 MB |
| **空闲退出后** | 17 MB | **0** | **9682 MB（差基线仅 21 MB）** |
| 再调用（自动重拉） | 18 MB | 2821 MB | 6858 MB |

- **主进程全程 17~18 MB** —— 模型内存完全隔离在子进程。
- **空闲退出后内存真的归还**（差基线 0.2%）—— 这是"释放不彻底"（代码 release 1538 MB、RSS 只降 5 MB）的正解。
  → 所以 `voice_cache` 的 LRU 上限不再是内存问题，只是磁盘问题。
- 退出/崩溃后**自动重拉自愈**（`starts 1→2, idle_kills 1`）。
- 传输：stdin/stdout JSON 行（`_stdio_smoke.py` 实测中文与大行均可靠）。

**内存优化做法**：参考侧 6 个量（`prompt_semantic`/`ssl_content`/`refer_spec`/`mel2`/
`ref_phones`/`ref_bert`）只依赖参考音频 → 离线预计算（同安卓 `mood_lib` 形态）；
`TextPreprocessor` 只接收传入对象，故只需 tokenizer，**绕开官方 `TTS` 的 3.4 GB PyTorch 模型**。
**改造前后喂给 AR 的输入逐位一致**（16 项 `max abs diff = 0`，`_equiv_dump.py`）。

## 10. ⚠️ 对任务 4.1 原卡的三处修订

| # | 原卡 | 修订为 | 依据 |
|---|---|---|---|
| 1 | 「是否只做 PC 本地版第一刀（安卓后续）」 | **安卓优先**，PC 后续 | 用户决策；且安卓端已真机验证过 Kotlin ORT 引擎 |
| 2 | 「服务器版强制关闭（`capabilities` 在 `FIREFLY_SERVER` 下视为 false）」 | **取消环境开关**，改用 §4 的资源就绪判定 | 服务器没装模型自然不可用，效果等价；将来换大内存机器装上即可用，不必改代码。另加全局串行锁防多用户并发 |
| 3 | 「`domain/voice/` 里懒加载引擎（隐含 Python 侧跑 ONNX）」 | **引擎在独立执行体**；Python 侧只做调度 | **实测：Chaquopy 装不上 `onnxruntime`**（`No matching distribution found`，索引 `pypi.org/simple` + `chaquo.com/pypi-13.1` 均无 Android wheel） |

**修订 3 的落地形态**：
- **PC**：语音引擎跑**独立子进程**（空闲可退出 → 内存真正归还；崩溃隔离）
- **安卓**：语音引擎在 **Kotlin 侧**（复用已真机验证的 `android_tts_app` 代码），单线程 + 可 `session.close()` 释放
- 两端上层接口一致：`has_voice(mode)` / `synthesize(text, mood)` / 插件状态机

## 11. 实施顺序

| 步 | 内容 | 状态 |
|---|---|---|
| 0 | 验证引擎可行性 + 等价性 | ✅ 完成（`FireflyVoiceResearch/_proto/`） |
| 1 | 独立执行体 + 空闲退出 + 内存归还实测 | ✅ **完成**（2026-09-18；`engine_worker.py` + `engine_host.py` + `_idle_check.py`） |
| 2 | 子系统落地 `app/voice/`（paths/store/engine/__init__/routes）+ Kotlin 引擎 | ✅ **后端完成** |
| 2b | Kotlin：`TtsEngine.kt`(466行机械搬迁) / `TextFrontend.kt` / `VoiceBridge.kt` + ORT 依赖 + MainActivity.attach | ✅ |
| 2c | assets：`mood_lib/{happy,sad}`（4.0MB，用新选的 2 语气重建）+ 4 张映射表 | ✅ |
| 3 | `storage.REGISTRY` 加 `voice_cache`(exclude) + `api/chat_ops` 两处清理挂钩 | ✅ |
| 3b | 路由 3 条 + `tests/test_routes_oracle.py` 同步（PASS=8 FAIL=0） | ✅ |
| 4a | 前端：长按菜单「转语音」 | ✅ |
| 4b | **前端：首页「语音插件」入口 + 插件管理页（下载/启用停用/卸载/状态）** | ⏳ **未做（需求 #1）** |
| 4c | 模型投送脚本 `tools/push_voice_models.py`（adb → 外部私有目录） | ✅ |
| 5 | 构建真机测试 | ⏳ |

### 实现中相对原设计的三处收敛（2026-09-18）

1. **语音不是消息类型** —— 是某条消息的附属产物（`v{seq}.wav`）。因此
   不改 `append_message` 的 type 白名单、不写 `conversation.jsonl`、不写 ctx。
   理由：触发方式是"用户想听这条"，不是"流萤做了个动作"；改白名单是全局风险。
2. **插件本体放 `$FIREFLY_DATA_DIR/voice/`（user_data 之外）** —— 任何"遍历 user_data"的操作
   （备份/快照/导出/同步）**结构上碰不到它**，871 MB 不会跑进备份。wav 在 user_data 内，故注册 `exclude`。
3. **模型投送（测试期）走外部私有目录** —— 装的是 release 包（`run-as` 不可用，实测
   `package not debuggable`），而 `/sdcard/Android/data/<pkg>/files/` adb 可写、app 能读，
   且同样是 app 私有（卸载清空、升级保留）。App 查找顺序：内部 `voice/models/` → 外部 `voice/models/`，
   **测试与正式共用同一条代码路径**。

## 12. 模型分发与凭据位置

### 12.1 模型仓库（2026-09-18 已上线）

| 项 | 值 |
|----|----|
| 仓库 | `cpt0721/firefly-voice-v4-onnx` —— https://www.modelscope.cn/models/cpt0721/firefly-voice-v4-onnx |
| 文件 | 8 个模型（871.9 MB）+ `README.md` + `LICENSE`；**`firefly_bert_fp16.onnx` 571 MB 故意不传** |
| 存储 | 全部走 **LFS**（平台规则：>1 MB 强制 LFS） |
| 下载模板 | `https://www.modelscope.cn/api/v1/models/cpt0721/firefly-voice-v4-onnx/repo?Revision=master&FilePath={file}`（已写进 `app/voice/plugin.py` 的 `DEFAULT_REPO_URL`） |
| 匿名可下 | ✅ 零 token、HTTP 200；**支持 Range**（LFS 文件同样返回 206）→ 断点续传可用 |
| 实测 | 上传 321 s（~1.4 MB/s 上行）；下载 17 MB/s（家宽）。上传后 **8 个文件逐个 sha256 与远端 API 对账全部一致** |

**平台容量**（读自本机 `modelscope` SDK 常量，非推测）：单文件上限 **100 GB**；
`> 1 MB` 强制 LFS；**非 LFS 小文件合计上限 500 MB**（网上流传的"魔搭上限 500MB"是把这一条
误读成了仓库总容量）；文件数上限 10 万。→ 本模型余量巨大。

### 12.2 凭据（访问令牌）位置 ★

**令牌存在本机用户级环境变量 `MODELSCOPE_API_TOKEN`**：

| 项 | 值 |
|----|----|
| 变量名 | `MODELSCOPE_API_TOKEN` |
| 作用域 | **User**（当前用户，非系统级） |
| 实际位置 | Windows 注册表 `HKCU\Environment\MODELSCOPE_API_TOKEN` |
| 谁读它 | ① `modelscope` SDK 的 `HubApi.login()`（`os.environ.get('MODELSCOPE_API_TOKEN')`）；② `tools/upload_voice_models.py` 的 `resolve_token()` |
| 写入方式 | `[Environment]::SetEnvironmentVariable('MODELSCOPE_API_TOKEN','<令牌>','User')`；或 `modelscope login --token <令牌>` |

**注意（实测踩到）**：`~/.modelscope/credentials` 那个文件在本机**读不出来**
（Python `PermissionError` / PowerShell「访问被拒绝」，ACL 异常），而且
`HubApi()` 的 `__init__` 并**不**自动读它（`self.token = token`，默认 None）。
所以**不要依赖那个文件**，用上面的环境变量。

**刷新/撤销**：令牌在 https://modelscope.cn/my/myaccesstoken（取「SDK 令牌」）。
作废后重新 `SetEnvironmentVariable` 覆盖即可；**注意新开终端才生效**（环境变量是进程启动时快照的）。
2026-09-18 首次上传用的令牌曾在会话记录里出现过，**建议在魔搭后台删掉重建一个**。

### 12.3 上传/换仓库

```powershell
# 令牌已在环境变量里 → 不需要再传 --token
python tools/upload_voice_models.py --repo <命名空间>/<仓库名> --dry-run   # 先看清单
python tools/upload_voice_models.py --repo <命名空间>/<仓库名>             # 真传
python tools/upload_voice_models.py --repo <命名空间>/<仓库名> --check     # 只对账
```

**踩坑记录（2026-09-18）**：本机 `git config --global http.proxy = http://127.0.0.1:7897`，
而那个本地代理**没在跑** → SDK 建仓时 `git clone` 失败（`Failed to connect to 127.0.0.1 port 7897`）。
解法：给命令加 `$env:NO_PROXY="www.modelscope.cn,modelscope.cn"`（只影响该进程，
**不动用户的 git 配置**）。Windows 上没法用空环境变量覆盖 git 配置（空值会被删掉），
`GIT_CONFIG_GLOBAL` 指向临时配置也行但更麻烦。

### 12.4 真机端到端验收（2026-09-18，全新安装 + 全手动点击）

**验收方式**：`adb uninstall`（清干净）→ 全新 `install` → **只用手点**，不调 API 触发。

| 步骤 | 结果 |
|------|------|
| 首页 → 点「语音插件」 | 状态「↓ 未安装 / 模型文件不齐」，目录 `/data/user/0/.../firefly_data/voice/models`，按钮「下载模型」，**无仓库地址输入框**（零配置 ✅） |
| 点「下载模型」 | 匿名下载成功：**108 s**、914283074 字节（871.9 MB）、`verify=on`、**`verified=8`**、零错误 |
| 完成后 | ✅ 可用 / 已就位 871.9 MB / **「上次下载已通过 sha256 校验（8 个文件）」** |
| 点「卸载」 | 弹确认框「会删除本机的模型文件（约 872 MB）。已生成的语音不受影响」→ 确定 → `not_installed` ✅ |
| 再点「下载模型」 | 再次成功（见下方 percent 修复验证） |
| 进聊天 → 长按消息 → 「转语音」 | 语音条出现「🔊 4″」；`voice_cache` 记录 `story/seq 53/4.09s/392684 字节`；logcat 可见 AudioTrack 播放 |

**副产品**：本 App 登录后**云端同步会把历史拉回来**，所以"卸载重装"并不丢对话数据
（首页显示「已登录 · 云端同步就绪」）；这也让长按试听有消息可用。

### 12.5 真机抓出的两个 bug（都已修）

1. **`percent` 会算出天文数字**。ModelScope 对文件 URL 的 **HEAD 返回 200 但不给
   `Content-Length`** → 原来"先 HEAD 探总大小"全部拿到 None → `total=0`，
   而调用处写的是 `total or 1` → `percent = 已下字节×100/1`，真机日志里出现过
   **`31435628400%`**（UI 只对进度条 clamp 了，文字会照显）。
   **修法**：`_manifest()` 连每个文件的 `Size` 一起取回来当总大小（清单本来就有，还省掉 8 次 HEAD）；
   `_download_one` 只在 `total_hint > 0` 时按字节算百分比并 `min(100, …)`，否则只更新 `done`、
   百分比退回"按文件数"；UI 再加一道 clamp 兜底。
   **回归测试**：`tests/test_voice_download_verify.py` C/C' 组（用 `dict` 子类记录每次 update 的快照，
   断言 percent 全程落在 0–100、total 一旦写入就是清单真值）。真机复验：percent 1→15→38→54→…→100 全合法。
2. **清单 URL 用了写死的 `modelscope.cn` 做 base** —— 用户若配**自建镜像**，清单会跑去魔搭取，
   sha 与镜像内容对不上 → 下载被误判成"校验失败"。改成从模板自身的 origin 推导；
   镜像若没有同路径清单端点 → 404 → 返回 `{}` → 降级跳过校验（安全）。测试 A5 钉住。

## 13. 未决 / 待确认

- **审核与版权风险**：仓库公开＝公开分发（模型含原声版权方的语音）。已按"不写商标词 +
  模型卡声明仅供个人学习/禁止商用/收到通知即下架"降险，风险由用户承担（2026-09-18 确认）。
- **T3 类文本的 75% 崩溃率**：暂靠"重试"消化（随机性，重试有效）。是否需要 lesson 化（记录易崩文本）待定。
- **首次下载体积 872 MB**：是否允许"仅下载生成链、BERT 复用包内已有"待评估
  （实测包内**没有** onnx 可复用，BERT 351 MB 必须下）。
- 移动端内存 2835 MB 的实测（当前数据全部来自 PC）。
- `LICENSE` 字段填的是 `other` —— 平台预置的 8 个标准许可没有一个适合"转分发他人权重"，
  填标准许可等于宣称无权授予的权利，故不填标准许可。