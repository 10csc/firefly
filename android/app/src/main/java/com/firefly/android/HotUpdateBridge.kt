package com.firefly.android

import android.util.Base64
import android.util.Log
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
