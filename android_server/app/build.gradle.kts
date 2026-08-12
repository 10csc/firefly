import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.firefly.server"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.firefly.server"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.7.1"
    }

    // 发布签名：复用 android/keystore/firefly.jks（与本地版同签名，可覆盖共存）
    signingConfigs {
        create("release") {
            val ks = rootProject.file("../android/keystore/firefly.jks")
            val props = Properties().apply {
                load(rootProject.file("../android/keystore/keystore.properties").inputStream())
            }
            storeFile = ks
            storePassword = props.getProperty("storePassword")
            keyAlias = props.getProperty("keyAlias")
            keyPassword = props.getProperty("keyPassword")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
