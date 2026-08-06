import org.gradle.api.tasks.Sync
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 后端数据同步：把仓库根下的 app/ knowledge/ database/ 拷进 python 数据目录
// （Chaquopy 把非 __init__.py 包目录当作数据文件打进 APK，运行时解压，只读）
val syncBackend = tasks.register<Sync>("syncBackend") {
    from("../../app") {
        into("app")
        exclude("**/__pycache__/**", "**/*.pyc")
    }
    from("../../knowledge") {
        into("knowledge")
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

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            install("requests")
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

    defaultConfig {
        applicationId = "com.firefly.android"
        minSdk = 26
        targetSdk = 35
        versionCode = 4
        versionName = "0.7.1"
        ndk {
            // 真机 arm64 + 模拟器 x86_64（Python 3.12 仅支持 64 位）
            abiFilters += listOf("arm64-v8a", "x86_64")
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
}
