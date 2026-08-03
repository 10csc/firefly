# 流萤（Firefly）— 角色扮演 Agent

基于 DeepSeek API（Flash + Think High）的独立角色扮演系统。开源版，本地运行，不依赖服务器。

## 环境要求

- **Python 3.10+**（代码使用了 `X | None` 语法）
- 需要可访问 DeepSeek API 的网络（对话走 API）
- 无本地模型依赖（知识检索由 LLM 子代理完成，无需安装 embedding 模型）

## 快速开始

```powershell
# 1. 安装依赖（仅两个；国内网络慢可加清华镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple）
pip install openai requests

# 2. 启动（Windows 下建议先设 UTF-8）
cd app
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
python server.py

# 3. 启动后自动打开浏览器；未自动打开则手动访问 http://localhost:8765，
#    在设置中填入 DeepSeek API Key 即可开始对话
```

Linux / macOS：`cd app && python server.py` 即可（路径与编码自动处理）。
> 说明：Windows 为验证主平台；Linux/macOS 代码层已做路径/编码兼容，但未实测，遇到问题欢迎反馈。

## 功能

- 角色扮演对话：分析器（意图/事实核查）→ 回复器（短信生成）→ 表情包调度
- 知识检索：LLM 子代理读取完整设定库输出压缩摘要（全局关联，非向量截断）
- 跨会话记忆：对话在"休息"时整理进 memory.md（`user_data/data/`），起床加载
- 手账：重要对话与约定以流萤口吻记录（`user_data/story/手账.md`）
- 前端设定编辑：角色文件/短信样本/表情包管理
- 调试面板：流水线中间态、请求日志、token/费用统计

## 数据与隐私

- 所有用户数据在 `user_data/`（对话历史、记忆、手账、配置、用户表情包），**升级版本不会覆盖**
- 服务只绑定 `127.0.0.1`（仅本机访问）
- API Key 只存本机 `user_data/config.json`

## 发行版

### Windows（PyInstaller）

```powershell
pip install pyinstaller
pyinstaller firefly.spec --noconfirm
# 产物：dist/firefly/firefly.exe
```

双击 `dist/firefly/firefly.exe` 启动，浏览器自动打开 `http://localhost:8765`。
用户数据在 exe 同级的 `user_data/` 目录——覆盖安装新版本前**先备份该目录**。

> **发布者注意**：发布安装包前必须删除 `dist/firefly/user_data/`（内含用户 API Key 与对话数据），只发布 `firefly.exe + _internal/`。

### Android（完全独立运行）

后端（Python + DeepSeek API）通过 Chaquopy 直接打进 APK，**无需电脑端服务**——手机独立运行，数据存 App 私有目录（卸载即清除，注意备份）。

**适配系统（重要，避免安装问题）**：

| 项目 | 要求 |
|------|------|
| Android 版本 | **Android 8.0（API 26）及以上** |
| CPU 架构 | **仅 64 位**：arm64-v8a（现代手机）/ x86_64（模拟器）——**32 位设备（armeabi-v7a）不支持，无法安装** |
| 存储空间 | 建议 ≥200MB（APK 49MB + 运行时数据） |
| 网络 | 需要联网（调用 DeepSeek API，无本地模型） |
| 权限 | 仅 INTERNET（无其他权限） |
| 已验证 | Android 15 国产旗舰真机、Android 16 模拟器（API 36.1） |

**已知行为说明**：
- 首次启动：内嵌服务初始化约 5-8 秒（加载设定资料库），随后进入聊天页
- 锁屏/后台：系统可能冻结 App（服务暂停响应），**回到前台自动恢复**，无需重开
- 数据存于 App 私有目录：**卸载 App 会清空全部数据**（对话/记忆/手账/Key），升级覆盖安装则保留

```powershell
# 构建（首次会自动下载 Android SDK，需 JDK 17+ 与 Gradle）
.\android\build_apk.ps1
# 产物：android\firefly.apk（arm64-v8a + x86_64）
```

**App 图标**：内置萤火虫主题 adaptive icon（深蓝夜空 + 暖黄光点 + 轨迹余韵），可自行替换：`android/app/src/main/res/mipmap-anydpi-v26/`（ic_launcher.xml）与 `drawable/ic_launcher_foreground.xml`、`ic_launcher_background.xml`。

- 安装：`adb install firefly.apk` 或直接传到手机安装
- 首次启动：App 内嵌服务启动（约 5 秒）→ 设置页填入 DeepSeek API Key → 开始对话
- 升级：直接覆盖安装（数据保留在内部存储）
- **签名注意**：`android/keystore/` 是发布签名（已 gitignore），**务必自行备份**；密码在 `keystore/keystore.properties`。更换设备/重装前请先导出签名，否则无法覆盖升级
- 图标：内置萤火虫 adaptive icon，可自行替换 `res/mipmap-anydpi-v26/`

> 旧版 WebView 客户端（连接电脑端服务）已由独立运行版取代；如需局域网远程模式，参考 git 历史。

## 运行测试

```powershell
cd F:\CodeFile\firefly
$env:PYTHONUTF8=1
$tests = @("test_orchestrator","test_organizer","test_polisher","test_analyzer",
           "test_llm_retriever","test_memory_manager","test_conversation_store",
           "test_sticker_picker","test_bubble_updater","test_llm_base")
foreach ($t in $tests) { python "tests/$t.py" }
```

## 项目结构

```
firefly/
├── app/                    核心流程
│   ├── server.py           HTTP 骨架（端口 8765，只绑 127.0.0.1）
│   ├── routes.py           API 路由
│   ├── orchestrator.py     编排器（子代理检索→分析器→回复器→表情包）
│   ├── modules/
│   │   ├── llm_retriever.py  LLM 子代理检索（设定库压缩摘要）
│   │   ├── analyzer.py      分析器：意图 + 事实核查
│   │   ├── polisher.py      回复器：全权生成短信
│   │   ├── organizer.py     组织器：表情包调度
│   │   ├── llm_base.py      共享基础设施：设定加载/JSON解析/统计
│   │   ├── app_config.py    路径引导 + 配置状态
│   │   └── conversation_store.py 对话持久化
│   ├── assets/character/   每轮必用的设定文件
│   └── static/             前端
├── memory/                 记忆层（memory_manager.py + story/ 设定资料）
├── knowledge/              知识层（星神/势力/地区/纪时表）
├── database/               原始资料（wiki 抓取物，仅查证）
├── android/                Android WebView 客户端
└── tests/                  白盒测试
```

## 常见问题

- **前端打不开**：确认服务日志出现 `打开浏览器访问`，且没有其他进程占用 8765 端口
- **对话报"信号不好"**：API Key 无效或网络不通，检查设置页
- **端口被占用**：`netstat -ano | findstr 8765` 找到进程后关闭
- **改设定不生效**：设定文件在 `user_data/character/`，前端编辑保存后自动生效
