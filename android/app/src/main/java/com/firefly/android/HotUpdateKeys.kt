package com.firefly.android

/**
 * 热更新验签公钥（RSA-2048）。
 *
 * 见 `docs/热更新/02_实现契约.md §八`：
 *  · **公钥**硬编码在这里，随 APK 发布；
 *  · **私钥**只在发布机（`~/.firefly/hotupdate_key.pem`），绝不进仓库、不进服务器；
 *  · 轮换：先用旧钥发一版带新公钥的**整包**，再用新钥签补丁（v1 只支持单钥）。
 *
 * 由 `python tools/hotupdate_keys.py --show --kotlin` 生成，不要手改。
 */
object HotUpdateKeys {
    val PUBLIC_KEY_PEM: String = """
        -----BEGIN PUBLIC KEY-----
        MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA1TlCKE/5OyrO8sTI+hPK
        vLEhv8T9csFWrZH2FZ/AIzohArKMNjvjitdIE94XwCCEBw1t1dXBNNU1hduZlHeF
        CVuVYPVSi8tt3PfZzyBEXXqTRZIxE3xH/FUAT7jT+jHtPWb2t21OxqxlESY7+PLV
        u03fIVbx/DhrGLhBJdT3UfkU8GkVEyxkNlcw1MDHln9PrBtd77/neYdIPHJidRkn
        Jz2Zvt8AStWhYFKqZ8TMA92LzC4PY8lJvDty3sZTDzyNpQt9WmI9EWIfP5lKhdP+
        RmXvEf+TKGlMrwiCcpz8pAdzHvIz7ABsAyfVVa+7+w3q0x19xISD2RnocMH1GkFH
        2QIDAQAB
        -----END PUBLIC KEY-----
    """.trimIndent()
}
