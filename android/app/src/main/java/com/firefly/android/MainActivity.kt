package com.firefly.android

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.ByteArrayInputStream
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : AppCompatActivity() {

    private var webView: WebView? = null
    private val uiHandler = Handler(Looper.getMainLooper())
    private var exitBackPressedAt = 0L   // 双击退出计时
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var pendingFileCallback: ValueCallback<Array<Uri>>? = null   // 导入文件选择回调
    private var activityResultLauncher: ActivityResultLauncher<PickVisualMediaRequest>? = null   // 系统照片选择器（Android 13+）

    companion object {
        private const val SERVER_URL = "http://127.0.0.1:8765"
        private const val LOCAL_HOME = "file:///android_asset/index.html"
        private const val READY_TIMEOUT_MS = 12_000L   // A7c：本地引擎启动探测窗口（超时自动回落服务器）
        private const val NOTIF_PERMISSION_REQUEST = 1001
        private const val FILE_CHOOSER_REQUEST = 1002
        private const val MEDIA_PERMISSION_REQUEST = 1003   // 图片选择存储权限（Android 12-）
        private const val TAG = "Firefly"

        /** 服务器地址单点：读 assets/config.js（Kotlin 启动时读取做 URL 白名单/拦截注入） */
        fun loadServerBase(ctx: Context): String {
            return try {
                Regex("""https?://[^\s"']+""")
                    .find(ctx.assets.open("config.js").bufferedReader().use { it.readText() })
                    ?.value ?: "http://101.200.14.126:8787"
            } catch (e: Exception) {
                "http://101.200.14.126:8787"
            }
        }

        // A7c（登录前提化）：本地优先 / 失败自动回落服务器——删除手动模式切换（SharedPreferences + 杀进程重启）。
        // 当前后端类型："local"（内嵌引擎） | "server"（回落：file:// 页面 + 服务器地址注入）；KeepAliveService 同读。
        @JvmStatic @Volatile
        var backend = "local"

        fun isServerBackend(): Boolean = backend == "server"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Android 15 (API 35) 默认 edge-to-edge：让系统处理状态栏 insets，
        // WebView 内容从状态栏下方开始（避免 header 被挖孔/状态栏遮挡）
        WindowCompat.setDecorFitsSystemWindows(window, true)
        val container = FrameLayout(this)
        container.setBackgroundColor(0xFF0f0f23.toInt())
        setContentView(container)

        // 后台保活：前台服务（对话流程较长，防止切后台/锁屏时进程被杀导致内容丢失）
        startForegroundService(Intent(this, KeepAliveService::class.java))

        // 通知权限（Android 13+ 需动态申请）：后台概率触发需要状态栏通知
        requestNotificationPermission()

        // 电池优化豁免（同通知权限模式）：OPPO/ColorOS 会冻结后台进程（即使前台服务），
        // 豁免后回复流程可在后台完成（发送→切后台→通知栏出现回复）
        requestIgnoreBatteryOptimizations()

        // 回复保活：发送后切后台，AI 回复流程期间 CPU/WiFi 不休眠（JS Bridge 按需持锁）
        initWakeLock()
        initWifiLock()

        // 系统照片选择器（Android 13+ PickVisualMedia）：免权限直进相册，回调喂回 WebView
        activityResultLauncher = registerForActivityResult(
            ActivityResultContracts.PickVisualMedia()
        ) { uri ->
            val cb = pendingFileCallback
            pendingFileCallback = null
            cb?.onReceiveValue(if (uri != null) arrayOf(uri) else arrayOf())
        }

        // 返回键：聊天页 → 回首页；首页 → 双击退出
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                handleBackPressed()
            }
        })

        // A7c：本地优先——始终尝试启动内嵌引擎；探测失败/超时自动回落服务器后端（界面显示"云端连接中"）
        try {
            startEmbeddedServer()
            waitForServerReady {
                backend = "local"
                loadWebView(SERVER_URL)
            }
        } catch (e: Exception) {
            Log.w(TAG, "[Backend] 内嵌引擎启动失败，自动回落服务器后端: ${e.message}")
            fallbackToServer()
        }
    }

    /** 本地后端不可用：加载 file:// 页面并注入服务器模式（登录后直连云端；界面由前端展示"云端连接中"） */
    private fun fallbackToServer() {
        backend = "server"
        uiHandler.post {
            Toast.makeText(this, "本地后端不可用，已切换云端连接", Toast.LENGTH_SHORT).show()
            loadWebView(LOCAL_HOME)
        }
    }

    override fun onResume() {
        super.onResume()
        // 回到前台：暂停后台定时器（前端 10s 轮询接管主动性）
        KeepAliveService.isForeground = true
    }

    override fun onPause() {
        super.onPause()
        // 切到后台：启动后台定时器（10-30 分钟随机间隔概率触发）
        KeepAliveService.isForeground = false
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {   // Android 13+
            if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(android.Manifest.permission.POST_NOTIFICATIONS),
                    NOTIF_PERMISSION_REQUEST
                )
            }
        }
    }

    /** Android 12- 图片选择所需的存储权限（一次申请；拒绝则走 GET_CONTENT 无权限也能选图——SAF 兜底） */
    private fun requestMediaPermissionIfNeeded() {
        val perm = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            android.Manifest.permission.READ_EXTERNAL_STORAGE   // API 30-32
        } else {
            android.Manifest.permission.READ_EXTERNAL_STORAGE   // API 26-29
        }
        if (ContextCompat.checkSelfPermission(this, perm) != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(perm), MEDIA_PERMISSION_REQUEST)
        }
    }

    /** 请求忽略电池优化（后台保活）：所有安卓设备统一引导。
     * - OPPO/Realme/一加（ColorOS）：标准电池优化豁免无效——OplusHansManager 冻结
     *   只看"应用启动管理"，需引导用户去该设置开启「允许后台运行」；
     * - 其他厂商：系统授权弹窗（ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS），
     *   无弹窗（部分厂商）则跳转电池优化设置页。
     * 用户拒绝不影响功能（前台回复正常），仅后台回复受系统冻结限制。 */
    private fun requestIgnoreBatteryOptimizations() {
        val prefs = getSharedPreferences("firefly_prefs", MODE_PRIVATE)
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager

        // ── OPPO 系专属引导（标准豁免无效，必须开"应用启动管理"）──
        val mfg = Build.MANUFACTURER.orEmpty().lowercase() + " " + Build.BRAND.orEmpty().lowercase()
        if (mfg.contains("oppo") || mfg.contains("realme") || mfg.contains("oneplus")) {
            if (!prefs.getBoolean("oppo_bg_guide_shown", false)) {
                prefs.edit().putBoolean("oppo_bg_guide_shown", true).apply()
                AlertDialog.Builder(this)
                    .setTitle("允许后台运行")
                    .setMessage("OPPO/ColorOS 会冻结后台应用（即使已忽略电池优化）。为支持「发送消息后切到后台，流萤仍能完成回复并通知你」，请开启：\n\n设置 → 应用管理 → 流萤 → 应用启动管理 → 打开「允许后台运行」")
                    .setPositiveButton("去设置") { _, _ -> openAppDetailsSettings() }
                    .setNegativeButton("暂不", null)
                    .show()
            }
            return
        }

        // ── 其他厂商：标准电池优化豁免 ──
        if (pm.isIgnoringBatteryOptimizations(packageName)) return   // 已豁免
        if (prefs.getBoolean("battery_guide_shown", false)) return   // 已提示过不重复打扰
        prefs.edit().putBoolean("battery_guide_shown", true).apply()
        AlertDialog.Builder(this)
            .setTitle("允许后台运行")
            .setMessage("为支持「发送消息后切到后台，流萤仍能完成回复并通知你」，请允许流萤忽略电池优化。\n\n若系统未弹出授权窗口（部分机型），请到 设置 → 电池 → 后台耗电管理/应用启动管理 中允许流萤后台运行。")
            .setPositiveButton("去授权") { _, _ -> launchBatteryOptimizationRequest() }
            .setNegativeButton("暂不", null)
            .show()
    }

    /** 跳转本应用详情设置页（OPPO 应用启动管理入口所在） */
    private fun openAppDetailsSettings() {
        try {
            startActivity(Intent(
                Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:$packageName")
            ))
        } catch (e: Exception) {
            Log.w(TAG, "[Battery] 跳转应用详情设置失败: ${e.message}")
        }
    }

    /** 发起系统授权弹窗；部分厂商（OPPO 等）无此弹窗 → 跳转电池优化设置页 */
    private fun launchBatteryOptimizationRequest() {
        try {
            startActivity(Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")
            ))
        } catch (e: Exception) {
            Log.w(TAG, "[Battery] 系统授权弹窗不可用，跳转设置页: ${e.message}")
            try {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (_: Exception) {
            }
        }
    }

    /** 初始化 CPU 唤醒锁：后台生成回复期间保持 CPU 运行（防关屏后降频/挂起） */
    private fun initWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "firefly:chat")
        wakeLock?.setReferenceCounted(false)
        Log.d(TAG, "[WakeLock] 初始化完成 PARTIAL_WAKE_LOCK refCounted=false")
    }

    /** 初始化 WiFi 锁：后台生成回复期间保持 WiFi 全速（防关屏后芯片省电断 TCP） */
    private fun initWifiLock() {
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_HIGH_PERF  // API 29+ 高性能模式
            } else {
                WifiManager.WIFI_MODE_FULL             // API 26-28 全速模式
            }
            wifiLock = wm.createWifiLock(mode, "firefly:wifi")
            wifiLock?.setReferenceCounted(false)
            val modeName = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) "HIGH_PERF" else "FULL"
            Log.d(TAG, "[WifiLock] 初始化完成 mode=$modeName refCounted=false")
        } catch (e: Exception) {
            Log.w(TAG, "[WifiLock] 初始化失败: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    /** 供 WebView 前端调用的回复保活接口：fetch /chat 期间持锁，完成后释放 */
    inner class WakeLockBridge {
        @JavascriptInterface
        fun acquire() {
            Log.d(TAG, "[WakeLockBridge] acquire() 被调用 — 回复流程开始")
            try {
                val wlHeld = wakeLock?.isHeld ?: false
                wakeLock?.acquire(300_000L)   // 5 分钟超时兜底，覆盖最长流水线
                Log.d(TAG, "[WakeLockBridge] WakeLock.acquire(300s) OK (之前held=$wlHeld)")
            } catch (e: SecurityException) {
                Log.e(TAG, "[WakeLockBridge] WakeLock.acquire 权限被拒: ${e.message}")
            } catch (e: Exception) {
                Log.e(TAG, "[WakeLockBridge] WakeLock.acquire 异常: ${e.javaClass.simpleName}: ${e.message}")
            }
            try {
                wifiLock?.acquire()
                Log.d(TAG, "[WakeLockBridge] WifiLock.acquire() OK (held=${wifiLock?.isHeld})")
            } catch (e: SecurityException) {
                Log.e(TAG, "[WakeLockBridge] WifiLock.acquire 权限被拒（需 CHANGE_WIFI_STATE）: ${e.message}")
            } catch (e: Exception) {
                Log.e(TAG, "[WakeLockBridge] WifiLock.acquire 异常: ${e.javaClass.simpleName}: ${e.message}")
            }
        }

        @JavascriptInterface
        fun release() {
            Log.d(TAG, "[WakeLockBridge] release() 被调用 — 回复流程结束")
            try {
                val wasHeld = wakeLock?.isHeld ?: false
                wakeLock?.release()
                Log.d(TAG, "[WakeLockBridge] WakeLock.release() OK (wasHeld=$wasHeld)")
            } catch (e: RuntimeException) {
                Log.w(TAG, "[WakeLockBridge] WakeLock.release 异常（可能已超时释放）: ${e.message}")
            }
            try {
                wifiLock?.release()
                Log.d(TAG, "[WakeLockBridge] WifiLock.release() OK")
            } catch (e: Exception) {
                Log.w(TAG, "[WakeLockBridge] WifiLock.release 异常: ${e.message}")
            }
        }
    }

    /** 返回键策略：聊天页返回首页，首页双击退出 */
    private fun handleBackPressed() {
        val wv = webView
        if (wv == null) { finish(); return }
        wv.evaluateJavascript(
            "document.getElementById('home-view') ? document.getElementById('home-view').classList.contains('show') : true"
        ) { result ->
            val onHome = result == "true"
            if (onHome) {
                val now = System.currentTimeMillis()
                if (now - exitBackPressedAt < 2000) {
                    exitBackPressedAt = 0
                    finish()   // 双击退出
                } else {
                    exitBackPressedAt = now
                    Toast.makeText(this, "再按一次返回键退出", Toast.LENGTH_SHORT).show()
                }
            } else {
                // 聊天页：回到首页（不退出）
                wv.evaluateJavascript("showHome()", null)
            }
        }
    }

    /** 启动内嵌 Python HTTP 服务（后台线程，不阻塞 UI） */
    private fun startEmbeddedServer() {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        val py = Python.getInstance()
        py.getModule("start_server").callAttr("start_in_thread")
    }

    /** 轮询服务就绪（知识库预加载需要几秒），就绪后回调；超时 → 自动回落服务器后端（A7c） */
    private fun waitForServerReady(onReady: () -> Unit) {
        Thread {
            val deadline = System.currentTimeMillis() + READY_TIMEOUT_MS
            var ready = false
            while (!ready && System.currentTimeMillis() < deadline) {
                ready = probe()
                if (!ready) Thread.sleep(500)
            }
            if (ready) {
                uiHandler.post { onReady() }
            } else {
                Log.w(TAG, "[Backend] 引擎探测超时（${READY_TIMEOUT_MS / 1000}s），回落服务器后端")
                fallbackToServer()
            }
        }.start()
    }

    private fun probe(): Boolean {
        return try {
            val conn = URL("$SERVER_URL/config").openConnection() as HttpURLConnection
            conn.connectTimeout = 1500
            conn.readTimeout = 1500
            val ok = conn.responseCode == 200
            conn.disconnect()
            ok
        } catch (e: Exception) {
            false
        }
    }

    private fun showError(msg: String) {
        val container = findViewById<ViewGroup>(android.R.id.content)
        container.removeAllViews()
        container.addView(android.widget.TextView(this).apply {
            text = msg
            textSize = 16f
            setTextColor(0xFFc8d0e0.toInt())
            gravity = android.view.Gravity.CENTER
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        })
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        // 通知权限结果：拒绝则后台概率触发的通知不可见（功能降级，不崩溃）
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun loadWebView(baseUrl: String) {
        // 仅 debug 构建开启远程调试：release 开启后 adb 用户可注入 JS 读取 API Key 与会话内容
        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)
        // 清 WebView 缓存：升级后强制加载新前端（index.html 无版本参数，
        // 不清理会命中旧缓存页面 → 旧 app.js → 旧行为）
        try { WebView(this).clearCache(true) } catch (_: Exception) {}
        val serverBase = loadServerBase(this)
        webView = WebView(this).apply {
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                useWideViewPort = true
                loadWithOverviewMode = true
                setSupportZoom(false)
                builtInZoomControls = false
                displayZoomControls = false
                allowFileAccess = false
                allowContentAccess = false
                cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE   // 每次拉取最新前端
            }
            webViewClient = object : WebViewClient() {
                override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {}
                override fun onPageFinished(view: WebView?, url: String?) {}

                // config.js 动态注入（A7c 单态）：页面先加载 config.js 再加载 app.js，
                // 壳按当前后端类型返回字段——local：FIREFLY_MODE=local；
                // 回落 server：FIREFLY_MODE=server + FIREFLY_SERVER_BASE（读 assets/config.js 的地址单点）
                override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest?): WebResourceResponse? {
                    val url = request?.url?.toString() ?: return null
                    if (url.endsWith("/config.js") || url.endsWith("config.js")) {
                        val js = if (backend == "server") {
                            "window.FIREFLY_MODE=\"server\";window.FIREFLY_SERVER_BASE=\"$serverBase\";"
                        } else {
                            "window.FIREFLY_MODE=\"local\";"
                        }
                        return WebResourceResponse(
                            "application/javascript", "utf-8",
                            ByteArrayInputStream(js.toByteArray(Charsets.UTF_8))
                        )
                    }
                    return null
                }

                override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                    val url = request?.url?.toString() ?: return false
                    // file:///android_asset 页面间跳转：allowFileAccess=false 会拦 JS 发起的 file://
                    // 导航 → 由壳接管 loadUrl。post 到主循环避免在回调内同步 loadUrl（导航重入
                    // 会与当前导航竞态，导致渲染帧空白——0.8.0 曾现「点登录后页面内容消失」）。
                    if (url.startsWith("file:///android_asset/")) {
                        view?.post { view?.loadUrl(url) }
                        return true
                    }
                    // 仅放行内置引擎 / 服务器域；外部链接交系统浏览器
                    if (url.startsWith(SERVER_URL) ||
                        url.startsWith(serverBase)) {
                        return false
                    }
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    } catch (e: Exception) {
                        Log.w(TAG, "[Nav] 外部链接打开失败: ${e.message}")
                    }
                    return true
                }
            }
            webChromeClient = object : WebChromeClient() {
                // 文件选择（数据导入 zip + A9 发图 image/*）：
                // - 发图（image/*）：Android 13+ 用系统照片选择器 PickVisualMedia（APP 内加载相册，
                //   免运行时权限——MediaStore 授权的安全分享）；Android 12- 用 GET_CONTENT 兜底。
                // - zip 导入：GET_CONTENT + 扩展白名单（保持原样）。
                override fun onShowFileChooser(
                    webView: WebView?,
                    filePathCallback: ValueCallback<Array<Uri>>?,
                    fileChooserParams: FileChooserParams?
                ): Boolean {
                    pendingFileCallback = filePathCallback
                    try {
                        val mimeTypes = fileChooserParams?.acceptTypes?.filter { it.isNotBlank() } ?: emptyList()
                        val wantsImage = mimeTypes.isNotEmpty() && mimeTypes.any {
                            it == "image/*" || it.startsWith("image/")
                        }
                        if (wantsImage && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                            // Android 13+ 系统照片选择器：直接进相册，免权限申请
                            val picker = PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                            activityResultLauncher?.launch(picker)
                            return true
                        }
                        // Android 12- 图片：先请求媒体读取权限（用户确认授权后走 GET_CONTENT）
                        if (wantsImage && Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                            requestMediaPermissionIfNeeded()
                        }
                        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                            addCategory(Intent.CATEGORY_OPENABLE)
                            type = "*/*"
                            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf(
                                "application/zip", "image/png", "image/jpeg",
                                "image/webp", "image/gif"))
                            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, false)
                        }
                        startActivityForResult(Intent.createChooser(intent, "选择文件（zip 备份 / 图片）"), FILE_CHOOSER_REQUEST)
                    } catch (e: Exception) {
                        pendingFileCallback = null
                        return false
                    }
                    return true
                }
            }
            // 数据导出下载：/export-data 返回 attachment → 下载到系统"下载"目录。
            // 文件名取响应头 Content-Disposition（带模式+时间戳，多模式不互相覆盖）；
            // 解析失败回退 firefly-backup.zip。
            setDownloadListener { url, _, contentDisposition, mimeType, _ ->
                try {
                    var fname = "firefly-backup.zip"
                    try {
                        val m = Regex("filename=\"?([^\"]+)\"?").find(contentDisposition ?: "")
                        if (m != null && m.groupValues[1].isNotBlank()) fname = m.groupValues[1]
                    } catch (e: Exception) { /* 保持回退文件名 */ }
                    val req = DownloadManager.Request(Uri.parse(url)).apply {
                        setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                        setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fname)
                        setMimeType(mimeType ?: "application/zip")
                        addRequestHeader("Authorization", "")   // 本地后端无鉴权，占位
                    }
                    val dm = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
                    dm.enqueue(req)
                    Toast.makeText(this@MainActivity,
                        "正在导出…完成后可在「文件管理 → 下载/$fname」找到",
                        Toast.LENGTH_LONG).show()
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "导出失败，请检查存储权限", Toast.LENGTH_LONG).show()
                }
            }
            addJavascriptInterface(WakeLockBridge(), "androidWakeLock")
            loadUrl(baseUrl)
        }
        (findViewById<ViewGroup>(android.R.id.content)).addView(webView)
        // 服务器模式后台主动：KeepAliveService 经 evaluateJavascript 触发页面 __serverProactive()
        KeepAliveService.webView = webView
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != FILE_CHOOSER_REQUEST) return
        val cb = pendingFileCallback ?: return
        pendingFileCallback = null
        val results = if (resultCode == Activity.RESULT_OK && data?.data != null) {
            arrayOf(data.data!!)
        } else {
            arrayOf()
        }
        cb.onReceiveValue(results)
    }

    override fun onDestroy() {
        // 兜底释放回复保活锁（正常流程 JS 已释放；Activity 被销毁时防泄漏）
        try { wakeLock?.release() } catch (_: Exception) {}
        try { wifiLock?.release() } catch (_: Exception) {}
        wakeLock = null
        wifiLock = null
        super.onDestroy()
    }
}
