package com.firefly.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder

/**
 * 后台保活前台服务：对话流程较长（检索→分析→回复可能 20-60s），
 * 用户切后台/锁屏时若进程被杀会中断回复。前台服务提高进程优先级，
 * 系统不会轻易回收，内嵌 Python 服务线程得以继续完成回复并落盘。
 */
class KeepAliveService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIF_ID, buildNotification())
        return START_STICKY
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "流萤后台服务", NotificationManager.IMPORTANCE_LOW)
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("流萤")
            .setContentText("对话服务保持在线，回复不会中断")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "firefly_keepalive"
        private const val NOTIF_ID = 1
    }
}
