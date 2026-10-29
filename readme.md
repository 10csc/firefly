# Firefly（萤火虫）— 角色扮演 Agent

Firefly 是以流萤为主要角色、同时允许制作和安装其他角色包的独立角色扮演应用。
本地版可运行在 Windows 和 Android；服务器只提供账号、同步、下载、更新、公告和登录后的角色卡平台，
不对外提供 App 前端。

## 环境要求

- Python 3.10+
- `requests>=2.28`（见 `requirements.txt`）
- 可访问所配置 AI 服务商的网络
- 不需要本地 embedding 模型，也不依赖 OpenAI Python SDK

## 快速开始

```powershell
Set-Location F:\CodeFile\firefly
python -m pip install -r requirements.txt
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location app
python server.py
```

服务默认监听 `127.0.0.1:8765`。启动后打开 `http://127.0.0.1:8765`，在设置页填写 API Key。

## 功能

- 对话流水线：知识检索 → 分析 → 回复 → 组织器 → 历史与观测。
- 角色包：人格、知识库、提示词覆盖、开场、消息表现、表情和图片。
- 对话能力：文本、图片、表情、旁白、引用、收藏、主动消息和语音附件。
- 数据能力：本地持久化、备份、快照、账号同步、墓碑和冲突备份。
- 角色卡平台：广场、搜索、制卡、草稿、审核、发布、改编、举报和治理。
- 更新能力：签名整包、签名累积热更新和签名公告通道。

## 数据与隐私

- 本地数据写入 `user_data/`；覆盖升级保留，手机卸载会清除 App 私有目录。
- API Key 保存在本机配置中，不写入项目仓库。
- 本地服务默认只绑定回环地址，不向局域网暴露。
- AI 对话、角色卡审核等操作会把完成任务所需内容发送给所配置的服务商；具体边界见
  [使用指南](docs/使用指南/README.md)。
- `server/`、发布密钥、keystore、用户数据库和备份不得提交或打进发行包。

## 发行版

发行资产固定为：

- Windows：`firefly-setup.exe`
- Android：`firefly.apk`

请从官方 Release 或下载页获取。当前版本、产物尺寸和 SHA-256 以
[当前状态](docs/当前状态.md) 与 `release_manifests/` 为准。

## 构建

PC：

```powershell
python -m PyInstaller firefly.spec --noconfirm
Remove-Item -Recurse -Force dist\firefly\user_data -ErrorAction SilentlyContinue
& 'C:\Program Files\Inno Setup 7\ISCC.exe' package\firefly.iss
```

Android：

```powershell
$env:JAVA_HOME = "D:\Java"
$env:PATH = "D:\python_all\Python312;$env:PATH"
$env:ANDROID_HOME = "F:\CodeFile\firefly\android\sdk"
$env:PYTHONUTF8 = "1"
$gradleBat = Get-ChildItem 'D:\Android\.gradle\wrapper\dists' -Recurse -Filter 'gradle.bat' |
  Select-Object -First 1 -ExpandProperty FullName
& $gradleBat -p F:\CodeFile\firefly\android assembleRelease --no-daemon
```

完整依赖位置、包内验收和真机安装步骤见
[本地开发与构建](docs/操作/本地开发与构建.md)。

## 测试

当前活动测试只保留 hot5 的同步与发布真实链路：

```powershell
$env:PYTHONUTF8 = "1"
python -B tests\test_sync_http_e2e.py
python -B tests\test_sync_concurrency.py
python -B tests\test_sync_crash_recovery.py
python -B tests\test_sync_spec_guard.py
python -B tests\test_hot5_release_pipeline.py
```

`tests/sync_real_harness.py` 是共享 harness，不单独执行。旧模块单测与历史报告已归档，
不再作为当前通过证据。测试设计门槛见 [测试基线](docs/参考/测试基线.md)。

## 服务器边界

服务器提供：

- 客户端 API：账号、数据隔离、同步、下载、更新、热更新和公告。
- 登录后的角色卡共创平台：广场、制卡、我的卡和治理台。

服务器不提供 App 的 `index.html`、App bundle、App 面板或 App 样式。平台前端和 App 前端分别构建；
`tools/sync_frontends.py` 不得把 `app/static/` 镜像进 `server/frontend/`。

生产入口由 `server/server_app.py` 启动，内部装配和主服务实现在 `server/app.py`。
部署约束见 [生产服务器](docs/操作/生产服务器.md)，发布边界见 [发布规则](docs/参考/发布规则.md)。

## 目录

```text
firefly/
├── app/                  本地版后端、前端与角色资产
├── android/              Android 壳与内嵌后端
├── server/               服务器 API、平台前端与生产入口（仅同步私有仓库）
├── tests/                当前活动真实链路测试
├── tools/                构建、同步、门禁与发布工具
├── docs/                 当前事实、操作、参考、决策和事故记录
├── release_manifests/    已发布产物不可变台账
├── package/              Windows 安装器
└── database/             原始资料与查证数据
```

文档入口是 [docs/README.md](docs/README.md)。待办只认
[未完成事项清单](docs/未完成事项清单.md)，已验证错误模式见
[错误总结](docs/错误总结/README.md)。

## 常见问题

- 页面打不开：先确认进程日志出现启动信息，再检查 `8765` 是否被占用。
- 对话提示网络或 Key 错误：检查 API Key、余额、接口地址和本机网络。
- 角色设定修改不生效：确认编辑的是 `user_data/{mode}/character/` 下的用户副本。
- 手机后端失效：先保留当前版本与诊断信息，不要反复登录或清数据；按
  [常见问题与排障](docs/使用指南/05_常见问题与排障对照表.md) 收集证据。

## 致谢

- HeartEase1 / firefly-skill
- SyrieYume / starrail_ui
- 崩坏：星穹铁道 Wiki 与 Fandom Wiki
- 个人整理资料与米哈游官方资源
