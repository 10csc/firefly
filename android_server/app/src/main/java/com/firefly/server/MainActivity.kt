package com.firefly.server

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Bundle
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast

/**
 * 流萤服务器版 · 手机壳
 *
 * 架构：WebView 加载服务器前端（8787 网关），Python 核心全在服务器。
 * - 资产（知识库/设定/Key）存 WebView 数据目录（localStorage，本地化）
 * - 后端代理：LLM 请求体由服务器构建、APP 代发（用户 Key 直连 DeepSeek）
 * - 零第三方依赖（纯 framework API），APK 极小
 */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private val serverBase = "http://101.200.14.126:8787"
    private val localHome = "file:///android_asset/index.html"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        setContentView(webView)
        WebView.setWebContentsDebuggingEnabled(true)   // 真机调试/测试用（需 adb 才能连接）

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true        // localStorage：资产/Key 本地化
            databaseEnabled = true
            loadWithOverviewMode = true
            useWideViewPort = true
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            allowFileAccess = true          // file:// 本地页面（打包的前端）
            // file:// 页面加载跨域 http 资源（验证码图片/服务器资产等）——
            // 默认禁止，必须放开；打包页面固定可信，无注入面
            allowUniversalAccessFromFileURLs = true
            allowFileAccessFromFileURLs = true
        }
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
                // 仅放行本地页面与服务器域，外部链接交系统浏览器
                if (url.startsWith("file:///android_asset/") || url.startsWith(serverBase)) {
                    return false
                }
                return super.shouldOverrideUrlLoading(view, url)
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError,
            ) {
                if (request.isForMainFrame) {
                    Toast.makeText(this@MainActivity, "网络异常，请检查网络后重试", Toast.LENGTH_LONG).show()
                }
            }
        }
        webView.webChromeClient = WebChromeClient()
        webView.loadUrl(localHome)
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }
}
