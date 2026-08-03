# 流萤 Android App

流萤聊天的 Android 客户端（WebView 外壳）。

## 功能

- WebView 浏览器加载流萤聊天界面
- 配置服务器 IP:端口
- 保存连接设置
- 支持 WebView 内后退导航

## 前提条件

- [Android Studio](https://developer.android.com/studio)（推荐）或命令行构建环境
- JDK 17+
- Android SDK API 35

## 构建

### 方式 A：Android Studio（推荐）

1. 打开此目录（`android/`）作为 Android Studio 项目
2. 等待 Gradle Sync 完成
3. Build → Build Bundle(s) / APK → Build APK

### 方式 B：命令行

```powershell
# 自动下载 SDK + 构建
.\build_apk.ps1
```

### 方式 C：手动

```powershell
$env:ANDROID_HOME = "C:\Users\...\AppData\Local\Android\Sdk"
$env:JAVA_HOME = "C:\Program Files\Java\jdk-17"
.\gradlew assembleRelease
```

## 使用

1. 在电脑上启动流萤服务端（`dist/firefly/firefly.exe`）
2. 手机连接到同一局域网
3. 打开 APK，输入电脑的 IP 和端口 `192.168.x.x:8765`
4. 点击「连接」开始聊天

## 技术栈

- Kotlin + AndroidX
- WebView + Material Components
- Gradle 8.11 + AGP 8.7
- API 26+（Android 8.0+）
