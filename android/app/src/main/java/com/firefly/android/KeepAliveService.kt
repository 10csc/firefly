package com.firefly.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.webkit.WebView
import com.chaquo.python.Python
import java.util.Random

/**
 * 后台保活前台服务：对话流程较长（检索→分析→回复可能 20-60s），
 * 用户切后台/锁屏时若进程被杀会中断回复。前台服务提高进程优先级，
 * 系统不会轻易回收，内嵌 Python 服务线程得以继续完成回复并落盘。
 *
 * v3：新增隐藏式回复（后台低频触发）——App 在后台时（用户不在应用中，
 * 但未清后台），以随机间隔（正式 10-30 分钟）触发一次隐藏式主动消息，
 * 通过状态栏通知送达。前台时暂停（前端 10s 轮询已覆盖）。
 *
 * 定时器用 Handler（主线程）：短后台可靠；深度 Doze 下触发延迟为系统限制
 * （OPPO/ColorOS 会把 AlarmManager 闹钟推迟数天，Handler 反而更快恢复）。
 */
class KeepAliveService : Service() {

    companion object {
        private const val CHANNEL_ID = "firefly_keepalive"
        private const val AI_CHANNEL_ID = "firefly_ai"
        private const val NOTIF_ID = 1
        private const val AI_NOTIF_ID = 2
        // 正式版：10-30 分钟随机间隔（测试时临时改 20-30 秒）
        private const val MIN_INTERVAL_MS = 10 * 60 * 1000L
        private const val MAX_INTERVAL_MS = 30 * 60 * 1000L
        @Volatile var isForeground = true   // App 前后台状态（MainActivity 通知）

        /** MainActivity 注册的 WebView（服务器模式后台主动：evaluateJavascript 触发页面轮询） */
        @Volatile var webView: WebView? = null

        // Python 侧（com.firefly.android.KeepAliveService）直调：后台回复完成通知
        private var appContext: Context? = null

        fun init(context: Context) {
            appContext = context.applicationContext
        }

        /** 是否在前台：Python 回复完成后判断是否需要发通知 */
        @JvmStatic
        fun isAppForeground(): Boolean = isForeground

        /** 后台回复完成通知（复用 AI 主动消息通道）。Python 流水线完成后直调。 */
        @JvmStatic
        fun notify(title: String, content: String) {
            val ctx = appContext ?: return
            val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(
                NotificationChannel(AI_CHANNEL_ID, "流萤的消息", NotificationManager.IMPORTANCE_HIGH)
            )
            val intent = Intent(ctx, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            }
            val pi = PendingIntent.getActivity(
                ctx, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val notification = Notification.Builder(ctx, AI_CHANNEL_ID)
                .setContentTitle(title)
                .setContentText(content)
                .setStyle(Notification.BigTextStyle().bigText(content))
                .setSmallIcon(R.mipmap.ic_launcher)
                .setAutoCancel(true)
                .setContentIntent(pi)
                .build()
            try {
                nm.notify(AI_NOTIF_ID, notification)
            } catch (e: SecurityException) {
                // 通知权限被拒：静默
            }
        }
    }

    private val handler = Handler(Looper.getMainLooper())
    private val random = Random()
    private val backgroundCheck = object : Runnable {
        override fun run() {
            Log.i("FireflyBG", "定时器触发, isForeground=$isForeground")
            if (!isForeground) {
                // App 在后台 → 隐藏式主动检查（Python 直调，不走 HTTP）
                triggerBackgroundProactive()
            }
            // 无论是否触发，安排下一次随机延迟（非固定时间，避免机械感）
            val delay = MIN_INTERVAL_MS + random.nextInt((MAX_INTERVAL_MS - MIN_INTERVAL_MS).toInt())
            handler.postDelayed(this, delay)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        init(applicationContext)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIF_ID, buildNotification())
        createChannels()
        // 定时器只启动一次（服务可能多次 start，防重复）
        handler.removeCallbacks(backgroundCheck)
        handler.postDelayed(backgroundCheck, MIN_INTERVAL_MS)
        return START_STICKY
    }

    override fun onDestroy() {
        handler.removeCallbacks(backgroundCheck)
        super.onDestroy()
    }

    /** 隐藏式主动检查（双模式）：
     * - local：Chaquopy 直调 Python（与前端共享 REPLY 锁）；
     * - server：无内置引擎 → evaluateJavascript 触发页面 __serverProactive()（页面轮询服务器）。 */
    private fun triggerBackgroundProactive() {
        val ctx = appContext ?: return
        if (MainActivity.currentMode(ctx) == "server") {
            val wv = webView ?: return
            try {
                wv.post {
                    try {
                        wv.evaluateJavascript("window.__serverProactive && window.__serverProactive()", null)
                    } catch (e: Exception) {
                        Log.e("FireflyBG", "触发页面主动失败: ${e.message}")
                    }
                }
            } catch (e: Exception) {
                Log.e("FireflyBG", "后台主动调度失败: ${e.message}")
            }
            return
        }
        Thread {
            try {
                val py = Python.getInstance()
                val module = py.getModule("modules.proactive")
                // 不传模式：Python 侧自动判定最后活跃模式（用户最后聊的是哪个模式，
                // 流萤就从那个世界想起开拓者）——双模式/多模式后台不再固定 story
                val result = module.callAttr("backdoor_proactive_check")
                val messages = result.asList().map { it.toString() }
                Log.i("FireflyBG", "backdoor 返回 ${messages.size} 条")
                if (messages.isNotEmpty()) {
                    val text = if (messages.size == 1) messages[0]
                               else messages[0] + "\n" + messages.drop(1).joinToString("\n")
                    sendAiNotification("流萤 · AI", text)
                }
            } catch (e: Exception) {
                Log.e("FireflyBG", "backdoor 失败: ${e.message}")
            }
        }.start()
    }

    /** AI 主动消息通知（状态栏 + 顶部横幅，类似 QQ/微信） */
    private fun sendAiNotification(title: String, content: String) {
        notify(title, content)
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "流萤后台服务", NotificationManager.IMPORTANCE_LOW)
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("流萤 · AI")
            .setContentText("对话服务保持在线，回复不会中断")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setOngoing(true)
            .build()
    }

    private fun createChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(AI_CHANNEL_ID, "流萤的消息", NotificationManager.IMPORTANCE_HIGH)
        )
    }
}

