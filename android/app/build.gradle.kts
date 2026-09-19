import org.gradle.api.tasks.Sync
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 后端数据同步：把仓库根下的 app/ database/ 拷进 python 数据目录
// （Chaquopy 把非 __init__.py 包目录当作数据文件打进 APK，运行时解压，只读）
// 2026-09-14：知识层已归位进角色包（app/assets/character/story/knowledge/），
// 随 app/ 一并拷贝，不再单独 from knowledge/。
val syncBackend = tasks.register<Sync>("syncBackend") {
    from("../../app") {
        into("app")
        exclude("**/__pycache__/**", "**/*.pyc")
    }
    from("../../database") {
        into("database")
        exclude("**/__pycache__/**", "**/*.pyc")
    }
    into(layout.projectDirectory.dir("src/main/python/backend"))
}

tasks.named("preBuild") { dependsOn(syncBackend) }
tasks.matching { it.name.contains("PythonSources") }.configureEach { dependsOn(syncBackend) }

// 前端打包 assets 同步（0.8.0 双模式）：服务器模式 file:// 加载统一前端。
// 源 = app/static（唯一源）+ app/assets 子集（背景/字体/图标）+ server/frontend 的
// config.js（服务器地址单点，壳按模式拦截注入 FIREFLY_MODE）+ login.html（注册登录页）。
// 可用 tools/sync_frontends.py 手动同步（--check 校验漂移），本任务构建时自动同步。
val syncFrontendAssets = tasks.register<Sync>("syncFrontendAssets") {
    into(layout.projectDirectory.dir("src/main/assets"))
    from("../../app/static") {
        exclude("config.js")   // 安卓 assets 用服务器版 config.js（本地模式页面由引擎 HTTP 提供）
    }
    from("../../app/assets") {
        into("assets")
        include(
            "background.jpg", "StarRailFont.ttf",
            "icon_home.png", "icon_rest.png", "icon_trash.png", "icon_undo.png",
            "notice_speaker.png", "theme_moon.png", "theme_sun.png",
        )
    }
    from("../../server/frontend") {
        include("config.js", "login.html")
    }
    // 语音插件资产（文本前端映射表 + 语气库 mood_lib）→ **落到 APK assets 根**，
    // Kotlin 侧用 AssetManager 读（VoiceBridge：symbols/pinyin/char2id/opencpop + mood_lib）。
    // ⚠️ 必须在这里登记，**不能**直接把文件放进 src/main/assets/：
    //    本任务是 Gradle **Sync** 类型，会删除目标目录里不属于它的文件——
    //    2026-09-18 实测：5 项语音资产在构建时被静默清空，APK 装上了却读不到 mood_lib。
    from("../../app/assets/voice")
}
tasks.named("preBuild") { dependsOn(syncFrontendAssets) }

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            install("requests")
            // ❌ 2026-09-15 实测：onnxruntime 装不上，已回退。
            //    Chaquopy 17.0.0 的索引 = https://pypi.org/simple + https://chaquo.com/pypi-13.1，
            //    两边都没有 Android ABI 的 onnxruntime wheel：
            //      ERROR: Could not find a version that satisfies the requirement onnxruntime
            //      (from versions: none)  → No matching distribution found
            //    ⇒ 路线 A1（Python/Chaquopy 跑 ONNX）不可行，改走 Kotlin 原生 ORT。
            //    复现：gradle :app:installDebugPythonRequirements
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
        }
    }
}

android {
    namespace = "com.firefly.android"
    compileSdk = 35

    buildFeatures {
        // AGP 8+ 默认关闭 BuildConfig 生成；MainActivity 的 BuildConfig.DEBUG（WebView 调试开关）依赖它
        buildConfig = true
    }

    defaultConfig {
        applicationId = "com.firefly.android"
        minSdk = 26
        targetSdk = 35
        versionCode = 900
        versionName = "0.9.0"
        ndk {
            // 只发 arm64-v8a（真机全是 64 位 arm64；Python 3.12 也不支持 32 位）。
            // ★ 为什么必须砍掉 x86_64（2026-09-19）：Gitee 单个附件上限 **100MB**
            //   （官方配额说明 https://help.gitee.com/repository/release/create/），
            //   而 libonnxruntime.so 单 ABI 就 31–37MB —— 带两个 ABI 的包 132MB，
            //   **整包下载会被 Gitee 直接拒绝**（而下载优先级正是 Gitee 第一，
            //   见 docs/部署与发布约定.md §2）。x86_64 只有模拟器用得到，
            //   挪到 debug 变体（见 buildTypes.debug）。
            abiFilters += listOf("arm64-v8a")
        }
    }

    signingConfigs {
        create("release") {
            val ks = rootProject.file("keystore/firefly.jks")
            val props = Properties().apply {
                val f = rootProject.file("keystore/keystore.properties")
                if (f.exists()) f.inputStream().use { load(it) }
            }
            if (ks.exists() && props.getProperty("storePassword") != null) {
                storeFile = ks
                storePassword = props.getProperty("storePassword")
                keyAlias = props.getProperty("keyAlias", "firefly")
                keyPassword = props.getProperty("keyPassword")
            } else {
                // keystore 缺失时回退 debug 签名（开发环境）
                storeFile = null
            }
        }
    }

    buildTypes {
        release {
            // minify 关闭：proguard 会裁剪 Chaquopy 反射/JNI 调用，稳定优先
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
        debug {
            signingConfig = signingConfigs.getByName("debug")
            // 模拟器（x86_64）只在**调试**构建里带 —— 发布包不带，见 defaultConfig.ndk 的说明。
            // 显式 clear 再列两个：不依赖 defaultConfig 与 buildType 的合并语义（合并方向容易记反）。
            ndk {
                abiFilters.clear()
                abiFilters += listOf("arm64-v8a", "x86_64")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("com.google.android.material:material:1.12.0")

    // ★ 语音插件：端侧 ONNX 推理。**这是项目「禁止新增第三方依赖」铁律的唯一例外**
    //   （README §一.5 / 扩展接入协议 §7 / docs/工具/tts.md §10，用户已批准）。
    //   为什么必须有它：Python 侧走 Chaquopy，而 Chaquopy 装不上 onnxruntime
    //   （实测 `No matching distribution found`；索引 pypi.org/simple 与 chaquo.com/pypi-13.1 均无 Android wheel），
    //   所以 ONNX 推理只能在 Kotlin 侧做。版本与 android_tts_app 真机验证过的一致。
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.29.0")
}
