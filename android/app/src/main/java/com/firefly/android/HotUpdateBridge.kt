package com.firefly.android

import android.content.Context
import android.content.Intent
import android.os.Process
import android.util.Base64
import android.util.Log
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import java.security.KeyFactory
import java.security.Signature
import java.security.spec.X509EncodedKeySpec

/**
 * 热更新验签桥（Python 侧经 Chaquopy 调用）。
 *
 * 见 `docs/热更新/02_实现契约.md §八`：**密码学只在 Kotlin 壳实现**。
 * Python 侧不重复实现（引库违反"禁止新增依赖"、手写密码学是事故；而 `java.security`
 * 是平台自带）。两端各实现一套密码学产生的不一致是最难查的一类 bug。
 *
 * 输入是补丁的**规范化字节**（`manifest_b64` 解码后的原始字节）与 base64 签名 ——
 * 客户端**从不重新序列化** manifest，所以不存在跨语言 JSON 规范化差异（契约 §三）。
 *
 * 失败一律返回 false（调用方 Python 侧也把异常当不通过）。
 */
object HotUpdateBridge {

    private const val TAG = "FireflyHotUpdate"
    private const val HANDOFF_TIMEOUT_MS = 5_000L
    private const val HANDOFF_SETTLE_CHECK_MS = 3_000L
    private const val HANDOFF_FILE = "hotupdate_restart_ready"
    private val restartScheduled = AtomicBoolean(false)
    private var appContext: Context? = null

    @JvmStatic
    fun attach(context: Context) {
        appContext = context.applicationContext
    }

    /**
     * 通过独立进程里的可见交接 Activity 真正重启应用。
     *
     * 仅调用 finishAffinity() 不会销毁 JVM；KeepAliveService 还在时尤其如此，Chaquopy
     * 已加载的模块也不会重走 import。
     *
     * Android 14 会拦截“进程已经死亡后，AlarmManager 再拉起 Activity”的 BAL 请求。
     * 因此顺序必须是：旧进程启动 `:hotupdate_restart` 里的可见交接窗口 → 交接窗口
     * 在 onResume 写入就绪令牌 → 交接进程按 Intent 中的旧 PID 执行原生 kill →
     * 仍有可见窗口的交接 Activity 拉起全新的默认进程。旧 PID 的销毁权交给交接
     * 进程，避免默认进程自毁失败后无人兜底；也绝不依赖 ColorOS 可能不可见的 /proc。
     */
    @JvmStatic
    fun requestProcessRestart(): Boolean {
        if (!restartScheduled.compareAndSet(false, true)) return true
        val context = appContext
        if (context == null) {
            restartScheduled.set(false)
            Log.w(TAG, "原生重启失败：MainActivity 尚未 attach Context")
            return false
        }
        return try {
            val token = "${Process.myPid()}-${System.nanoTime()}"
            val readyFile = File(context.filesDir, HANDOFF_FILE)
            readyFile.writeText("")
            val intent = Intent(context, HotUpdateRestartActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
                putExtra(HotUpdateRestartActivity.EXTRA_OLD_PID, Process.myPid())
                putExtra(HotUpdateRestartActivity.EXTRA_READY_TOKEN, token)
            }
            context.startActivity(intent)
            Log.i(TAG, "已启动隔离进程交接窗口：旧 PID=${Process.myPid()}")
            Thread {
                try {
                    val deadline = System.currentTimeMillis() + HANDOFF_TIMEOUT_MS
                    while (readyFile.readText() != token &&
                        System.currentTimeMillis() < deadline
                    ) {
                        Thread.sleep(50)
                    }
                    if (readyFile.readText() != token) {
                        restartScheduled.set(false)
                        readyFile.delete()
                        Log.e(TAG, "交接窗口未就绪，取消销毁旧进程（可安全重试）")
                        return@Thread
                    }
                    // 正常路径由可见交接进程 kill(oldPid)；这里仅在交接进程异常停滞时
                    // 解除一次性锁，让下一轮热更轮询可以重新发起，而不是永久卡死。
                    Thread.sleep(HANDOFF_SETTLE_CHECK_MS)
                    restartScheduled.set(false)
                    readyFile.delete()
                    Log.e(TAG, "交接进程未在期限内销毁旧 PID，允许安全重试")
                } catch (e: Exception) {
                    restartScheduled.set(false)
                    Log.e(TAG, "交接进程确认失败: ${e.javaClass.simpleName}: ${e.message}")
                }
            }.apply {
                name = "firefly-hotupdate-restart"
                start()
            }
            true
        } catch (e: Exception) {
            restartScheduled.set(false)
            Log.e(TAG, "原生重启交接失败: ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }

    @JvmStatic
    fun verifyRsaSha256(data: ByteArray, sigB64: String): Boolean {
        return try {
            if (data.isEmpty() || sigB64.isEmpty()) return false
            val sig = Base64.decode(sigB64, Base64.DEFAULT)
            if (sig.isEmpty()) return false

            val body = HotUpdateKeys.PUBLIC_KEY_PEM
                .replace("-----BEGIN PUBLIC KEY-----", "")
                .replace("-----END PUBLIC KEY-----", "")
                .replace(Regex("\\s"), "")
            val der = Base64.decode(body, Base64.DEFAULT)
            val key = KeyFactory.getInstance("RSA").generatePublic(X509EncodedKeySpec(der))

            val verifier = Signature.getInstance("SHA256withRSA")
            verifier.initVerify(key)
            verifier.update(data)
            verifier.verify(sig)
        } catch (e: Exception) {
            // 不吞错也要记：验签失败是安全事件，不是"没网"
            Log.w(TAG, "验签失败: ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }
}
