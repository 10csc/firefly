import org.gradle.api.tasks.Sync
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 鍚庣鏁版嵁鍚屾锛氭妸浠撳簱鏍逛笅鐨?app/ knowledge/ database/ 鎷疯繘 python 鏁版嵁鐩綍
// 锛圕haquopy 鎶婇潪 __init__.py 鍖呯洰褰曞綋浣滄暟鎹枃浠舵墦杩?APK锛岃繍琛屾椂瑙ｅ帇锛屽彧璇伙級
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

// 鍓嶇鎵撳寘 assets 鍚屾锛?.8.0 鍙屾ā寮忥級锛氭湇鍔″櫒妯″紡 file:// 鍔犺浇缁熶竴鍓嶇銆?
// 婧?= app/static锛堝敮涓€婧愶級+ app/assets 瀛愰泦锛堣儗鏅?瀛椾綋/鍥炬爣锛? server/frontend 鐨?
// config.js锛堟湇鍔″櫒鍦板潃鍗曠偣锛屽３鎸夋ā寮忔嫤鎴敞鍏?FIREFLY_MODE锛? login.html锛堟敞鍐岀櫥褰曢〉锛夈€?
// 鍙敤 tools/sync_frontends.py 鎵嬪姩鍚屾锛?-check 鏍￠獙婕傜Щ锛夛紝鏈换鍔℃瀯寤烘椂鑷姩鍚屾銆?
val syncFrontendAssets = tasks.register<Sync>("syncFrontendAssets") {
    into(layout.projectDirectory.dir("src/main/assets"))
    from("../../app/static") {
        exclude("config.js")   // 瀹夊崜 assets 鐢ㄦ湇鍔″櫒鐗?config.js锛堟湰鍦版ā寮忛〉闈㈢敱寮曟搸 HTTP 鎻愪緵锛?
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
}
tasks.named("preBuild") { dependsOn(syncFrontendAssets) }

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

    buildFeatures {
        // AGP 8+ 榛樿鍏抽棴 BuildConfig 鐢熸垚锛汳ainActivity 鐨?BuildConfig.DEBUG锛圵ebView 璋冭瘯寮€鍏筹級渚濊禆瀹?
        buildConfig = true
    }

    defaultConfig {
        applicationId = "com.firefly.android"
        minSdk = 26
        targetSdk = 35
        versionCode = 804
        versionName = "0.7.2"
        ndk {
            // 鐪熸満 arm64 + 妯℃嫙鍣?x86_64锛圥ython 3.12 浠呮敮鎸?64 浣嶏級
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
                // keystore 缂哄け鏃跺洖閫€ debug 绛惧悕锛堝紑鍙戠幆澧冿級
                storeFile = null
            }
        }
    }

    buildTypes {
        release {
            // minify 鍏抽棴锛歱roguard 浼氳鍓?Chaquopy 鍙嶅皠/JNI 璋冪敤锛岀ǔ瀹氫紭鍏?
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
