package com.firefly.android

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
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
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowCompat
import androidx.core.view.updateLayoutParams
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.firefly.voice.VoiceBridge
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
        private const val HOTUPDATE_POLL_MS = 8000L   // 热更新待生效时的轮询间隔（本地请求，代价可忽略）
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
        // Android 15 (API 35) 起 edge-to-edge 被系统强制：setDecorFitsSystemWindows(true) 被忽略，
        // WebView 会铺到系统导航条底下（真机实测：角色卡管理页底部按钮被手势条盖住半截）。
        // 改为 false + 下方 insets 监听手动给 WebView 加 margin——各版本行为一致。
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val container = FrameLayout(this)
        container.setBackgroundColor(0xFF0f0f23.toInt())
        setContentView(container)

        // 语音插件：把 Context 交给 Kotlin 侧引擎桥（VoiceBridge 读 assets 的映射表要用）。
        // 这里只注入 Context，**不加载任何模型** —— 引擎由 Python 侧（app/voice/）按需 init，
        // 这样"未安装/未启用"时零成本，且保证"同时只有一个合成"由 Python 侧统一调度。
        VoiceBridge.attach(this)

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
        // READ_EXTERNAL_STORAGE 通用于 API 26-32（Manifest maxSdkVersion=32；API 33+ 走 PickVisualMedia 免权限）
        val perm = android.Manifest.permission.READ_EXTERNAL_STORAGE
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

    /** 服务器模式后台主动通知桥（前端 proactive.js 调 window.FireflyJs.notify）：
     * 仅 App 不在前台时发状态栏通知，前台不打扰（复刻本地版 _notify_reply_if_background 语义） */
    inner class FireflyJsBridge {
        @JavascriptInterface
        fun notify(title: String, content: String) {
            if (!KeepAliveService.isAppForeground()) {
                KeepAliveService.notify(title, content)
            }
        }

        /** 把结果回执给网页（必须是主线程碰 WebView；消息做 JS 字符串转义）。 */
        private fun diagResult(msg: String, ok: Boolean) {
            val safe = msg.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
            runOnUiThread {
                try {
                    webView?.evaluateJavascript(
                        "window.__diagResult && window.__diagResult('$safe', $ok)", null)
                } catch (e: Exception) {
                    Log.w(TAG, "[Diag] 回执失败: ${e.message}")
                }
            }
        }

        /**
         * 导出诊断包并**直接调系统分享面板**（QQ 在里面 → 附件已经带好，用户只需选发给谁）。
         *
         * 为什么不是"让网页下载 + 只启动 QQ"：那样用户得自己去下载目录找文件、在 QQ 里找联系人、
         * 再手动加附件——三步都可能失败（2026-09-19 用户指出："打开QQ后肯定要发包的啊，逻辑呢？"）。
         *
         * 实现要点：
         *  · 从**当前页面 origin** 推本地后端地址（本地模式 = http://127.0.0.1:8765），不硬编码端口；
         *    服务器模式下该端点返 403，这里会 Toast 说明。
         *  · 网络必须在后台线程（主线程禁网）。
         *  · 分享必须用 FileProvider 的 content:// URI —— Android 7+ 用 file:// 会
         *    FileUriExposedException 直接崩（白名单见 res/xml/file_paths.xml，只暴露 diagnostics/）。
         */
        @JavascriptInterface
        fun sendDiagnostics(pageOrigin: String?): Boolean {
            // ★ 地址由 **JS 传进来**，Kotlin 侧**绝不读 WebView**：
            //   @JavascriptInterface 方法跑在 'JavaBridge' 线程，而 WebView 的方法只能在主线程调 ——
            //   真机实测 `webView.url` 在这里直接抛：
            //     "A WebView method was called on thread 'JavaBridge' … at WebView.getUrl"
            //   （第一版"拿不到本地地址"就是这个异常被 catch 掉的表现。）
            val candidates = ArrayList<String>()
            val po = (pageOrigin ?: "").trim()
            if (po.startsWith("http")) candidates.add(po.trimEnd('/'))
            candidates.add(SERVER_URL)
            Log.i(TAG, "[Diag] 候选地址: $candidates")
            Thread {
                val err = try {
                    var conn: java.net.HttpURLConnection? = null
                    var code = 0
                    for (base in candidates.distinct()) {
                        conn = (java.net.URL("$base/export-diagnostics")
                            .openConnection() as java.net.HttpURLConnection).apply {
                            connectTimeout = 8000
                            readTimeout = 30000
                        }
                        code = conn.responseCode
                        Log.i(TAG, "[Diag] 试 $base → HTTP $code")
                        if (code == 200) break
                        conn.disconnect()
                        conn = null
                    }
                    if (conn == null) {
                        "所有候选地址都失败（HTTP $code）"
                    } else {
                        val dir = java.io.File(cacheDir, "diagnostics").apply { mkdirs() }
                        val f = java.io.File(dir,
                            "firefly-diagnostics-" + System.currentTimeMillis() + ".zip")
                        conn.inputStream.use { ins -> f.outputStream().use { ins.copyTo(it) } }
                        conn.disconnect()
                        if (f.length() < 200L) {
                            "文件异常（${f.length()} 字节）"
                        } else {
                            val uri = androidx.core.content.FileProvider.getUriForFile(
                                this@MainActivity, "com.firefly.android.fileprovider", f)
                            val send = Intent(Intent.ACTION_SEND).apply {
                                type = "application/zip"
                                putExtra(Intent.EXTRA_STREAM, uri)
                                putExtra(Intent.EXTRA_SUBJECT, "Firefly 诊断包")
                                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                                // ★ 必须同时给 ClipData：只靠 EXTRA_STREAM 时，部分接收方
                                //   （含 QQ）拿不到该 URI 的读权限，附件会打不开。真机日志：
                                //   "Could not read content://… stream types. call Intent#setClipData()"
                                clipData = android.content.ClipData.newUri(
                                    contentResolver, "firefly-diagnostics", uri)
                            }
                            diagResult("已生成诊断包并打开分享面板：选 QQ 发给我即可", true)
                            runOnUiThread {
                                try {
                                    startActivity(Intent.createChooser(send, "发送诊断包"))
                                } catch (e: Exception) {
                                    Log.w(TAG, "[Diag] 分享失败: ${e.message}")
                                    diagResult("分享失败：${e.message}", false)
                                    Toast.makeText(this@MainActivity, "分享失败：${e.message}",
                                        Toast.LENGTH_LONG).show()
                                }
                            }
                            ""
                        }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "[Diag] 生成诊断包失败: ${e.message}")
                    "${e.javaClass.simpleName}: ${e.message}"
                }
                if (err.isNotEmpty()) {
                    diagResult("诊断包导出失败：$err", false)
                    runOnUiThread {
                        Toast.makeText(this@MainActivity, "诊断包导出失败：$err",
                            Toast.LENGTH_LONG).show()
                    }
                }
            }.start()
            return true
        }

        /**
         * 一键清理已导出的诊断包（前端「清理」按钮）。
         *
         * 清两处，缺一不可：
         *  ① `<cacheDir>/diagnostics/ 下的 zip` —— 分享用的那份（每次导出都会新增）；
         *  ② DownloadManager 里本应用发起的 `firefly-diagnostics-*.zip` —— 下载目录那份
         *     （WebView 下载兜底会落盘到 Download/；`remove()` 会连文件一起删）。
         * 返回给前端一句人话摘要。
         */
        @JavascriptInterface
        fun clearDiagnostics(): String {
            var files = 0
            var bytes = 0L
            try {
                val dir = java.io.File(cacheDir, "diagnostics")
                dir.listFiles()?.forEach { f ->
                    if (f.isFile && f.name.endsWith(".zip")) {
                        val len = f.length()
                        if (f.delete()) { files++; bytes += len }
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "[Diag] 清理缓存失败: ${e.message}")
            }
            var records = 0
            try {
                val dm = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
                val c = dm.query(DownloadManager.Query())
                if (c != null) {
                    try {
                        val idCol = c.getColumnIndex(DownloadManager.COLUMN_ID)
                        val titleCol = c.getColumnIndex(DownloadManager.COLUMN_TITLE)
                        val uriCol = c.getColumnIndex(DownloadManager.COLUMN_LOCAL_URI)
                        while (c.moveToNext()) {
                            val title = if (titleCol >= 0) c.getString(titleCol) ?: "" else ""
                            val uri = if (uriCol >= 0) c.getString(uriCol) ?: "" else ""
                            if (title.contains("firefly-diagnostics-") ||
                                uri.contains("firefly-diagnostics-")) {
                                if (idCol >= 0) { dm.remove(c.getLong(idCol)); records++ }
                            }
                        }
                    } finally {
                        c.close()
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "[Diag] 清理下载记录失败: ${e.message}")
            }
            // ★ 补一刀：DownloadManager.remove() 在部分机型只删记录、文件留在 Download/（真机实测），
            //   所以再用 MediaStore 删掉本应用贡献的 firefly-diagnostics-*.zip。
            var media = 0
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    val sel = android.provider.MediaStore.MediaColumns.DISPLAY_NAME + " LIKE ?"
                    media = contentResolver.delete(
                        android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                        sel, arrayOf("firefly-diagnostics-%"))
                }
            } catch (e: Exception) {
                Log.w(TAG, "[Diag] 清理下载目录失败: ${e.message}")
            }
            val msg = if (files == 0 && records == 0 && media == 0) {
                "没有需要清理的诊断包"
            } else {
                "已清理 $files 个缓存文件（${bytes / 1024} KB）" +
                    (if (media > 0) "、下载目录 $media 个文件" else "") +
                    (if (records > 0) "，并移除 $records 条下载记录" else "")
            }
            Log.i(TAG, "[Diag] $msg")
            diagResult(msg, true)
            return msg
        }
    }

    /** 返回键策略：遮罩/全屏页（设置/反馈/菜单/角色卡列表/包详情/纠错）先关闭，
     * 聊天页回首页，首页双击退出。
     * （旧实现只区分 home/chat：角色卡管理等全屏页按返回无反应，真机实测复现。） */
    private fun handleBackPressed() {
        val wv = webView
        if (wv == null) { finish(); return }
        wv.evaluateJavascript(
            "(function(){" +
            " if (document.querySelector('#settings-panel.show')) return 'settings';" +
            " if (document.querySelector('#feedback-panel.show')) return 'feedback';" +
            " if (document.querySelector('#menu-drawer.open')) return 'menu';" +
            " if (document.querySelector('#cards-view.show')) return 'cards';" +
            " if (document.querySelector('#pack-view.show')) return 'pack';" +
            " if (document.querySelector('#fix-view.show')) return 'fix';" +
            " var hv=document.getElementById('home-view');" +
            " return (hv && hv.classList.contains('show')) ? 'home' : 'chat'; })()"
        ) { result ->
            when (result?.trim('"')) {
                "settings" -> wv.evaluateJavascript("closeSettings()", null)
                "feedback" -> wv.evaluateJavascript("closeFeedback()", null)
                "menu" -> wv.evaluateJavascript("closeMenu()", null)
                "cards" -> wv.evaluateJavascript("closeCardsView()", null)
                "pack" -> wv.evaluateJavascript("closePackView()", null)
                "fix" -> wv.evaluateJavascript("closeFixView()", null)
                "home" -> {
                    val now = System.currentTimeMillis()
                    if (now - exitBackPressedAt < 2000) {
                        exitBackPressedAt = 0
                        finish()   // 双击退出
                    } else {
                        exitBackPressedAt = now
                        Toast.makeText(this, "再按一次返回键退出", Toast.LENGTH_SHORT).show()
                    }
                }
                else -> wv.evaluateJavascript("showHome()", null)
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
            addJavascriptInterface(FireflyJsBridge(), "FireflyJs")   // 服务器模式后台主动消息 → 状态栏通知
            loadUrl(baseUrl)
        }
        // Android 15 强制 edge-to-edge 下，系统状态栏/导航条会压住 WebView 内容；
        // CSS env(safe-area-inset-*) 在部分机型 WebView 取不到值（真机复现），壳侧直接让出系统栏高度。
        ViewCompat.setOnApplyWindowInsetsListener(webView!!) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.updateLayoutParams<FrameLayout.LayoutParams> {
                topMargin = bars.top
                bottomMargin = bars.bottom
            }
            insets
        }
        (findViewById<ViewGroup>(android.R.id.content)).addView(webView)
        // 服务器模式后台主动：KeepAliveService 经 evaluateJavascript 触发页面 __serverProactive()
        KeepAliveService.webView = webView
        // 热更新：补丁应用后由壳触发刷新（前端 JS 那份在"补丁本身坏了"时指望不上）
        startHotUpdateWatcher()
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

    // ── 热更新：补丁就绪时由**壳**触发刷新（见 docs/热更新规范.md §5.2）────────
    //
    // 为什么触发权必须在壳这一份、不能只靠前端 JS：
    //   要修的往往就是那段前端 JS —— 一旦坏的是它本身，页面里就没有任何代码会去
    //   轮询状态，补丁永远生效不了（2026-09-19 真机实测踩到：apply 成功、
    //   reload_pending 一直不清）。所以壳必须自己也盯着。
    //
    // 空闲判据由后端复合给出（它合并了前端上报的 typing/playing 与"模型下载中"等）；
    // 壳在这里**再查一次输入框**做兜底：里面有字就不刷，绝不吞掉用户正在打的内容。
    private fun startHotUpdateWatcher() {
        val tick = object : Runnable {
            override fun run() {
                try {
                    Thread {
                        if (hotUpdateReloadPending()) {
                            uiHandler.post { maybeReloadForHotUpdate() }
                        }
                    }.start()
                } catch (_: Exception) {
                }
                uiHandler.postDelayed(this, HOTUPDATE_POLL_MS)
            }
        }
        uiHandler.postDelayed(tick, HOTUPDATE_POLL_MS)
    }

    /** 问后端：补丁是否已应用待生效、且此刻空闲。任何异常都当"不需要刷新"。 */
    private fun hotUpdateReloadPending(): Boolean = try {
        val c = URL("$SERVER_URL/hotupdate/status").openConnection() as HttpURLConnection
        c.connectTimeout = 3000
        c.readTimeout = 3000
        val body = c.inputStream.bufferedReader().use { it.readText() }
        c.disconnect()
        val o = org.json.JSONObject(body)
        o.optBoolean("reload_pending") && o.optBoolean("idle")
    } catch (_: Exception) {
        false
    }

    private fun maybeReloadForHotUpdate() {
        val wv = webView ?: return
        wv.evaluateJavascript(
            "(function(){var i=document.getElementById('msg-input');" +
                "return (i&&i.value&&i.value.trim())?'BUSY':'OK';})()"
        ) { r ->
            if (r != null && r.contains("OK")) {
                Log.i("FireflyHotUpdate", "补丁已就绪且空闲 → 壳触发刷新")
                wv.reload()
            }
        }
    }
}
