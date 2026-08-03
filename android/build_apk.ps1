#!/usr/bin/env pwsh
# 流萤 Android App — 构建 APK
# 需要：JDK 17+、Android SDK（可自动下载）
# 用法：.\build_apk.ps1

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$SDK_DIR = "$ROOT\sdk"

# 环境变量（提前设置：sdkmanager 也需要 Java 17+）
$env:ANDROID_HOME = $SDK_DIR
$env:JAVA_HOME = "D:\Java\jdk-21"

# 1. 检测/安装 Android SDK
if (-not (Test-Path "$SDK_DIR\platforms\android-35")) {
    Write-Output "=== 安装 Android SDK ==="
    $cmdlineZip = "$env:TEMP\cmdline-tools.zip"
    if (-not (Test-Path $cmdlineZip)) {
        Write-Output "下载 command-line tools..."
        Invoke-WebRequest -Uri "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip" -OutFile $cmdlineZip -TimeoutSec 180
    }
    Expand-Archive -Path $cmdlineZip -DestinationPath "$SDK_DIR\_tmp" -Force
    New-Item -ItemType Directory -Force -Path "$SDK_DIR\cmdline-tools\latest" | Out-Null
    Move-Item "$SDK_DIR\_tmp\cmdline-tools\*" "$SDK_DIR\cmdline-tools\latest\" -Force
    Remove-Item "$SDK_DIR\_tmp" -Recurse -Force

    $sdkmanager = "$SDK_DIR\cmdline-tools\latest\bin\sdkmanager.bat"
    if (-not (Test-Path $sdkmanager)) { Write-Error "sdkmanager not found"; exit 1 }

    # 预写许可文件（避免交互式 yes 管道）
    New-Item -ItemType Directory -Force -Path "$SDK_DIR\licenses" | Out-Null
    Set-Content -Path "$SDK_DIR\licenses\android-sdk-license" -Value "24333f8a63b6825ea9c5514f83c2829b004d1fee" -NoNewline -Encoding ASCII
    Set-Content -Path "$SDK_DIR\licenses\android-sdk-preview-license" -Value "84831b9409646a918e30573bab4c9c91346d8abd" -NoNewline -Encoding ASCII

    # 安装必要组件（含模拟器与系统镜像，供本地验证）
    & $sdkmanager --sdk_root=$SDK_DIR "platforms;android-35" "build-tools;35.0.0" "platform-tools" "emulator" "system-images;android-36.1;google_apis_playstore;x86_64"
    Write-Output "SDK 安装完成"
}

# 3. 定位 Gradle（优先用用户缓存中的发行版，避免 wrapper jar 缺失问题）
$GRADLE_CANDIDATES = @(
    "$env:USERPROFILE\.gradle\wrapper\dists",
    "D:\Android\.gradle\wrapper\dists"
)
$gradleBat = $null
foreach ($d in $GRADLE_CANDIDATES) {
    if (Test-Path $d) {
        $found = Get-ChildItem $d -Recurse -Filter "gradle.bat" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { $gradleBat = $found.FullName; break }
    }
}
if (-not $gradleBat) {
    Write-Error "未找到 Gradle。请先安装 Gradle（https://gradle.org/install/）或运行 gradle wrapper 生成 wrapper"
    exit 1
}
Write-Output "使用 Gradle: $gradleBat"

# 4. 构建 APK（assembleDebug：免签名，验证用；发布版需另行配置签名）
Write-Output "=== 构建 APK (debug) ==="
Push-Location $ROOT
try {
    & $gradleBat -p $ROOT assembleDebug --no-daemon
    if ($LASTEXITCODE -eq 0) {
        $apk = Get-ChildItem -Recurse -Filter "*.apk" "$ROOT\app\build\outputs" | Select-Object -First 1
        if ($apk) {
            Copy-Item $apk.FullName "$ROOT\firefly.apk"
            Write-Output "APK 已生成: $ROOT\firefly.apk ($([math]::Round($apk.Length/1KB,1)) KB)"
        }
    } else {
        Write-Error "Gradle 构建失败"
    }
} finally {
    Pop-Location
}
