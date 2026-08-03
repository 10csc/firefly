package com.firefly.android

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : AppCompatActivity() {

    private var webView: WebView? = null
    private val uiHandler = Handler(Looper.getMainLooper())

    companion object {
        private const val SERVER_URL = "http://127.0.0.1:8765"
        private const val READY_TIMEOUT_MS = 60_000L
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Android 15 (API 35) 默认 edge-to-edge：让系统处理状态栏 insets，
        // WebView 内容从状态栏下方开始（避免 header 被挖孔/状态栏遮挡）
        WindowCompat.setDecorFitsSystemWindows(window, true)
        val container = FrameLayout(this)
        container.setBackgroundColor(0xFF0f0f23.toInt())
        setContentView(container)

        startEmbeddedServer()
        waitForServerReady {
            loadWebView(SERVER_URL)
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

    /** 轮询服务就绪（知识库预加载需要几秒），就绪后回调 */
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
                uiHandler.post { showError("服务启动超时，请重启应用") }
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

    @SuppressLint("SetJavaScriptEnabled")
    private fun loadWebView(baseUrl: String) {
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
            }
            webViewClient = object : WebViewClient() {
                override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {}
                override fun onPageFinished(view: WebView?, url: String?) {}
                override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean = false
            }
            webChromeClient = WebChromeClient()
            loadUrl(baseUrl)
        }
        (findViewById<ViewGroup>(android.R.id.content)).addView(webView)
    }

    override fun onBackPressed() {
        if (webView?.canGoBack() == true) {
            webView?.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
