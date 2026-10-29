package com.firefly.android

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.os.Process
import android.view.Gravity
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.util.Log
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 独立进程中的可见重启交接页。
 *
 * Android 14 不允许旧进程死亡后再由 AlarmManager 拉起 Activity。这里先取得一个真实
 * 可见窗口，再让旧默认进程销毁；本 Activity 位于 `:hotupdate_restart` 进程，不会被
 * 旧 PID 的退出带走，随后从可见 Activity 中合法拉起 MainActivity 的新进程。
 */
class HotUpdateRestartActivity : Activity() {

    companion object {
        const val EXTRA_OLD_PID = "old_pid"
        const val EXTRA_READY_TOKEN = "ready_token"
        private const val TAG = "FireflyHotUpdate"
        private const val KILL_DELAY_MS = 150L
        private const val KILL_SETTLE_MS = 300L
        private const val HANDOFF_FILE = "hotupdate_restart_ready"
    }

    private val relaunchStarted = AtomicBoolean(false)
    private lateinit var messageView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val density = resources.displayMetrics.density
        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.rgb(15, 15, 35))
        }
        val progress = ProgressBar(this).apply {
            indeterminateTintList = android.content.res.ColorStateList.valueOf(
                Color.rgb(255, 209, 102)
            )
        }
        messageView = TextView(this).apply {
            text = "正在应用修复并重启…"
            setTextColor(Color.WHITE)
            textSize = 16f
            gravity = Gravity.CENTER
        }
        root.addView(
            progress,
            FrameLayout.LayoutParams(
                (48 * density).toInt(),
                (48 * density).toInt(),
                Gravity.CENTER
            ).apply {
                bottomMargin = (56 * density).toInt()
            }
        )
        root.addView(
            messageView,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER
            ).apply {
                topMargin = (72 * density).toInt()
            }
        )
        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        val oldPid = intent.getIntExtra(EXTRA_OLD_PID, 0)
        val token = intent.getStringExtra(EXTRA_READY_TOKEN).orEmpty()
        if (oldPid <= 0 || oldPid == Process.myPid() || token.isEmpty()) {
            finish()
            return
        }
        try {
            File(filesDir, HANDOFF_FILE).writeText(token)
        } catch (e: Exception) {
            Log.e(TAG, "写交接就绪令牌失败: ${e.javaClass.simpleName}: ${e.message}")
            message("重启准备失败，请重新打开应用")
            return
        }
        if (!relaunchStarted.compareAndSet(false, true)) return
        Thread {
            try {
                // 交接窗口已经真实 onResume；稍等一帧后由本独立进程销毁旧默认进程。
                Thread.sleep(KILL_DELAY_MS)
                stopService(Intent(this, KeepAliveService::class.java))
                Log.i(TAG, "可见交接进程销毁旧 JVM/Python 进程 PID=$oldPid")
                Process.killProcess(oldPid)
                Thread.sleep(KILL_SETTLE_MS)
                runOnUiThread {
                    File(filesDir, HANDOFF_FILE).delete()
                    try {
                        startActivity(
                            Intent(this, MainActivity::class.java).apply {
                                addFlags(
                                    Intent.FLAG_ACTIVITY_NEW_TASK or
                                        Intent.FLAG_ACTIVITY_CLEAR_TASK
                                )
                            }
                        )
                        finish()
                    } catch (e: Exception) {
                        Log.e(TAG, "拉起新 MainActivity 失败: ${e.javaClass.simpleName}: ${e.message}")
                        message("新进程启动失败，请重新打开应用")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "隔离进程交接失败: ${e.javaClass.simpleName}: ${e.message}")
                runOnUiThread { message("重启未完成，请重新打开应用") }
            }
        }.apply {
            name = "firefly-hotupdate-handoff"
            start()
        }
    }

    private fun message(text: String) {
        messageView.text = text
    }
}
