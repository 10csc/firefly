package com.firefly.voice

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.os.SystemClock
import android.util.Log
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.nio.LongBuffer
import org.json.JSONObject

/**
 * 流萤语音引擎（端侧 ONNX，Kotlin 原生）
 *
 * 来源：`FireflyVoiceResearch/android_tts_app/` 的 `MainActivity.TtsEngine`，
 * **按行机械提取**（不手抄）+ 仅做命名适配。该实现已在真机验证过
 * （zcr 0.039、ASR 回读正确、实机 RTF 见论文 §6）。
 *
 * 为什么在主 app 里也要有它：主 app 的 Python 侧走 Chaquopy，
 * 而 **Chaquopy 装不上 onnxruntime**（实测 `No matching distribution found`），
 * 所以 ONNX 推理只能在 Kotlin 侧做；Python 侧通过 `VoiceBridge` 调用。
 *
 * 关键点（都是真机踩出来的，勿改）：
 *  · 7 个 session 会话级常驻，**不可每句重建**（重建会让 AR 从 25s 变 115s）
 *  · 每次 run 的输入 OnnxTensor 与 Result **必须显式 close**（否则 native 内存每句泄漏）
 *  · `setCPUArenaAllocator(false)`（arena 只增不减，连发会涨到被杀后台）
 *  · BERT 用 `BASIC_OPT`（`ALL_OPT` 会把 GELU 融成 fp16 contrib op，CPU EP 没有该内核）
 *  · CFM 循环用**行主序**手写（Kotlin 平铺索引曾出 bug，见论文 Bug 8）
 */

data class Ref(
    val refPhones: LongBuffer, val refBert: FloatBuffer,
    val ssl: FloatBuffer, val promptSem: LongBuffer,
    val referSpec: FloatBuffer, val mel2: FloatBuffer
)

fun loadMoodFromDir(dir: String): Ref {
    val meta = JSONObject(File(dir, "meta.json").readText())
    fun shape(key: String): IntArray =
        meta.getJSONObject(key).getJSONArray("shape").let { arr -> IntArray(arr.length()) { i -> arr.getInt(i) } }

    fun readF(name: String, count: Int): FloatBuffer {
        val bytes = File(dir, name).readBytes()
        val fb = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer()
        check(fb.remaining() == count) { "$name 大小不符 ${fb.remaining()} vs $count" }
        return fb
    }
    fun readL(name: String, count: Int): LongBuffer {
        val bytes = File(dir, name).readBytes()
        val lb = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asLongBuffer()
        check(lb.remaining() == count) { "$name 大小不符" }
        return lb
    }
    val shRefPhones = shape("ref_phones")       // [1, N]
    val shRefBert = shape("ref_bert")           // [N, 1024]（导出时 .T 存储）
    val shSsl = shape("ssl_content")            // [1, 768, T]
    val shPrompt = shape("prompt_semantic")     // [T]
    val shSpec = shape("refer_spec")            // [1, 1025, T]
    val shMel2 = shape("mel2")                  // [1, 100, T]
    val refPhones = readL("ref_phones.raw", shRefPhones[1])
    val refBert = readF("ref_bert.raw", shRefBert[0] * shRefBert[1])
    val ssl = readF("ssl_content.raw", 768 * shSsl[2])
    val promptSem = readL("prompt_semantic.raw", shPrompt[0])
    val referSpec = readF("refer_spec.raw", 1025 * shSpec[2])
    val mel2 = readF("mel2.raw", 100 * shMel2[2])
    return Ref(refPhones, refBert, ssl, promptSem, referSpec, mel2)
}

class TtsEngine(
    private val env: OrtEnvironment,
    private val models: String,
    private val charToId: Map<String, Int>,
    bertFile: String = "firefly_bert_int8.onnx"
) {

    private val sEnc: OrtSession
    private val sFs: OrtSession
    private val sSd: OrtSession
    private val sVits: OrtSession
    private val sCfm: OrtSession
    private val sVoc: OrtSession
    private var sBert: OrtSession

    /** 全部 session 共用的选项；换 BERT 时复用，避免重建 */
    private val so = OrtSession.SessionOptions().apply {
        setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
        // ★ E7：关闭 CPU memory arena
        //   官方明确：arena 分配的内存**永不归还系统**（once allocated it always remains allocated）。
        //   某次推理出现较大峰值后它就永久保持 → 进程内存只增不减。
        //   实测：PC 6 次连续合成 5633MB → 7880MB（+2.2GB）；安卓 bench 5 连发 5601 → 8982MB。
        //   手机会因此被杀后台。关掉后按需分配/释放，代价是少量分配开销（可忽略）。
        setCPUArenaAllocator(false)
    }

    /**
     * BERT 专用选项：**用 BASIC_OPT，不用 ALL_OPT**。
     *
     * 原因（实测）：ALL_OPT 下 ORT 图优化器会把 BERT 的 GELU 模式
     * （Add+Erf+Mul…）融合成 contrib 算子 `com.microsoft.BiasGelu`。
     * onnxruntime-android 的 CPU EP **只实现了该算子的 float32 内核**，
     * 于是 fp16 模型在 session 创建时报：
     *     ORT_NOT_IMPLEMENTED: Failed to find kernel for com.microsoft.BiasGelu(1)
     *     op implemented only for (tensor(float)), node has (tensor(float16))
     * （PC 版 CPU EP 有 fp16 内核，所以同样的模型在 PC 上能跑。）
     * 降到 BASIC_OPT 不做这类 contrib 融合，fp16 即可加载。
     * BERT 本身只是几十毫秒，牺牲这点融合不影响整体。
     */
    private val bertSo = OrtSession.SessionOptions().apply {
        setOptimizationLevel(OrtSession.SessionOptions.OptLevel.BASIC_OPT)
        setCPUArenaAllocator(false)   // 同 E7：BERT 的输入长度随文本变化，arena 同样会只增不减
    }
    private var currentBertFile = bertFile

    /**
     * 合成结果守卫：空串 = 通过；非空 = 该音频应丢弃并降级纯文字（值为原因）。
     *
     * 为什么需要：sdec 图内的 top-k(15) 采样偶尔会复读参考句或跑飞（主报告 6.5，
     * 概率 MLE 6.7% / 后验 11.8%）。图内采样无法设种子，故用输出侧守卫。
     * 参考句 prompt_semantic = 180 token，正常句约 音素数 × 4 token，
     * 所以上限取 音素数 × 8 能把复读稳定拦下。
     */
    var lastGuard: String = ""
        private set

    companion object {
        private const val MAX_TOK_PER_PHONE = 8
        private const val MIN_TOKENS = 12
    }

    init {
        val t0 = SystemClock.elapsedRealtime()
        sEnc = env.createSession("$models/firefly_t2s_encoder.onnx", so)
        sFs = env.createSession("$models/firefly_t2s_fsdec_int8.onnx", so)
        sSd = env.createSession("$models/firefly_t2s_sdec_int8.onnx", so)
        sVits = env.createSession("$models/firefly_vits_int8.onnx", so)
        sCfm = env.createSession("$models/firefly_cfm_estimator_int8.onnx", so)
        sVoc = env.createSession("$models/firefly_vocoder.onnx", so)
        sBert = env.createSession("$models/$bertFile", bertSo)
        Log.i("FireflyTTS", "sessions 加载: ${SystemClock.elapsedRealtime() - t0}ms (BERT=$bertFile)")
    }

    /**
     * 只替换 BERT session，其余 6 个常驻不动。用于 int8/fp16 精度切换与 A/B 对比。
     * 先用新 session 再关旧的，避免中途出现无 sBert 的窗口。
     */
    fun switchBert(file: String) {
        if (file == currentBertFile) return
        val old = sBert
        sBert = env.createSession("$models/$file", bertSo)
        currentBertFile = file
        runCatching { old.close() }
        Log.i("FireflyTTS", "BERT session 已切换: $file")
    }

    /** 构建输入 → run → 输出回调（统一 close 输入与 Result） */
    private inline fun runClose(
        sess: OrtSession,
        build: () -> Map<String, OnnxTensor>,
        block: (OrtSession.Result) -> Unit
    ) {
        val inputs = build()
        try {
            val res = sess.run(inputs)
            try {
                block(res)
            } finally {
                res.close()
            }
        } finally {
            inputs.values.forEach { it.close() }
        }
    }

    // ---------- BERT ----------
    /** 文本 → [N_total, 1024]（拷贝回 Java 数组后释放 native tensor） */
    fun runBert(normText: String, word2ph: IntArray): FloatArray {
        val t0 = SystemClock.elapsedRealtime()
        // 简易 tokenizer：逐字 + 标点（官方 BertTokenizer 近似；中文单字即 token）
        val ids = ArrayList<Int>()
        ids.add(101) // [CLS]
        var n = 0
        val w2phOnTokens = ArrayList<Int>()
        for (c in normText) {
            val id = charToId[c.toString()] ?: 100   // [UNK]=100
            ids.add(id)
            // 防御：normText 应与 word2ph 等长（由 TextFrontend.process 保证）。
            // 不等长时按 1 兜底并告警，而不是越界把整句炸掉。
            w2phOnTokens.add(if (n < word2ph.size) word2ph[n] else 1); n++
        }
        if (n != word2ph.size) {
            Log.w("FireflyTTS", "⚠ word2ph 长度不匹配: normText=$n word2ph=${word2ph.size}（已按 1 兜底）")
        }
        ids.add(102) // [SEP]
        val len = ids.size
        val inputIds = LongArray(len) { ids[it].toLong() }
        val attn = LongArray(len) { 1L }
        val typeIds = LongArray(len) { 0L }
        val w2phArr = LongArray(word2ph.size) { word2ph[it].toLong() }

        var out = FloatArray(0)
        runClose(sBert, {
            mapOf(
                "input_ids" to OnnxTensor.createTensor(env, LongBuffer.wrap(inputIds), longArrayOf(1, len.toLong())),
                "attention_mask" to OnnxTensor.createTensor(env, LongBuffer.wrap(attn), longArrayOf(1, len.toLong())),
                "token_type_ids" to OnnxTensor.createTensor(env, LongBuffer.wrap(typeIds), longArrayOf(1, len.toLong())),
                "word2ph" to OnnxTensor.createTensor(env, LongBuffer.wrap(w2phArr), longArrayOf(word2ph.size.toLong())),
            )
        }) { res ->
            val fb = (res.get(0) as OnnxTensor).floatBuffer
            out = FloatArray(fb.remaining())
            fb.get(out)
        }
        Log.i("FireflyTTS", "BERT: ${SystemClock.elapsedRealtime() - t0}ms, out=${out.size / 1024}×1024")
        return out
    }

    // ---------- 主链路（同步调用，busy 门闩保证单线程） ----------
    fun synthesize(text: IntArray, textBert: FloatArray, ref: Ref): ByteArray {
        val t0 = SystemClock.elapsedRealtime()

        // ---- T2S encoder ----
        var xArr = FloatArray(0); var promptsArr = LongArray(0)
        val nX = ref.refPhones.remaining() + text.size
        val textIds = LongArray(text.size) { text[it].toLong() }
        runClose(sEnc, {
            mapOf(
                "ref_seq" to OnnxTensor.createTensor(env, ref.refPhones, longArrayOf(1, ref.refPhones.remaining().toLong())),
                "text_seq" to OnnxTensor.createTensor(env, LongBuffer.wrap(textIds), longArrayOf(1, text.size.toLong())),
                "ref_bert" to OnnxTensor.createTensor(env, ref.refBert, longArrayOf(ref.refBert.remaining() / 1024L, 1024)),
                "text_bert" to OnnxTensor.createTensor(env, FloatBuffer.wrap(textBert), longArrayOf(textBert.size / 1024L, 1024)),
                "ssl_content" to OnnxTensor.createTensor(env, ref.ssl, longArrayOf(1, 768, ref.ssl.remaining() / 768L)),
            )
        }) { res ->
            xArr = copyFloat(res.get(0) as OnnxTensor)
            promptsArr = copyLong(res.get(1) as OnnxTensor)
        }
        val prefixLen = promptsArr.size
        Log.i("FireflyTTS", "encoder: ${SystemClock.elapsedRealtime() - t0}ms prefix=$prefixLen")

        // ---- fsdec ----
        var yArr = LongArray(0); var kArr = FloatArray(0); var vArr = FloatArray(0)
        var yEmbArr = FloatArray(0); var xExArr = FloatArray(0)
        runClose(sFs, {
            mapOf(
                "x" to OnnxTensor.createTensor(env, FloatBuffer.wrap(xArr), longArrayOf(1, nX.toLong(), 512)),
                "prompts" to OnnxTensor.createTensor(env, LongBuffer.wrap(promptsArr), longArrayOf(1, prefixLen.toLong())),
            )
        }) { res ->
            yArr = copyLong(res.get(0) as OnnxTensor)
            kArr = copyFloat(res.get(1) as OnnxTensor)
            vArr = copyFloat(res.get(2) as OnnxTensor)
            yEmbArr = copyFloat(res.get(3) as OnnxTensor)
            xExArr = copyFloat(res.get(4) as OnnxTensor)
        }
        var kFrames = kArr.size / (24 * 512)
        var vFrames = vArr.size / (24 * 512)
        var yEmbFrames = yEmbArr.size / 512
        Log.i("FireflyTTS", "fsdec: ${SystemClock.elapsedRealtime() - t0}ms kFrames=$kFrames")

        // ---- AR loop（每步 runClose；tensor 零泄漏）----
        // 守卫上限：正常约 音素数×4 token；复读参考句约 180 token
        val eos = 1024L
        val cap = maxOf(MIN_TOKENS, text.size * MAX_TOK_PER_PHONE)
        val yList = ArrayList<Long>()
        yList.add(yArr[prefixLen])
        var idx = 0
        var stoppedOnEos = false
        val tAR = SystemClock.elapsedRealtime()
        while (idx < cap) {
            val len = prefixLen + 1 + idx
            var logitsArr = FloatArray(0); var samplesArr = IntArray(0)
            runClose(sSd, {
                mapOf(
                    "iy" to OnnxTensor.createTensor(env, LongBuffer.wrap(yArr), longArrayOf(1, len.toLong())),
                    "ik" to OnnxTensor.createTensor(env, FloatBuffer.wrap(kArr), longArrayOf(24, kFrames.toLong(), 1, 512)),
                    "iv" to OnnxTensor.createTensor(env, FloatBuffer.wrap(vArr), longArrayOf(24, vFrames.toLong(), 1, 512)),
                    "iy_emb" to OnnxTensor.createTensor(env, FloatBuffer.wrap(yEmbArr), longArrayOf(1, yEmbFrames.toLong(), 512)),
                    "ix_example" to OnnxTensor.createTensor(env, FloatBuffer.wrap(xExArr), longArrayOf(1, nX.toLong())),
                )
            }) { res ->
                yArr = copyLong(res.get(0) as OnnxTensor)
                kArr = copyFloat(res.get(1) as OnnxTensor)
                vArr = copyFloat(res.get(2) as OnnxTensor)
                yEmbArr = copyFloat(res.get(3) as OnnxTensor)
                logitsArr = copyFloat(res.get(4) as OnnxTensor)
                samplesArr = copyInt(res.get(5) as OnnxTensor)
            }
            kFrames = kArr.size / (24 * 512)
            vFrames = vArr.size / (24 * 512)
            yEmbFrames = yEmbArr.size / 512
            idx++
            yList.add(yArr[len])
            if (argmaxFloat(logitsArr, 1025) == eos || samplesArr.getOrNull(0)?.toLong() == eos) {
                stoppedOnEos = true
                break
            }
        }
        Log.i("FireflyTTS", "T2S AR: ${SystemClock.elapsedRealtime() - tAR}ms tokens=${yList.size} cap=$cap")
        lastGuard = when {
            !stoppedOnEos -> "未在 $cap token 内收敛（疑似复读参考句/跑飞）"
            idx < MIN_TOKENS -> "过早收敛（${idx} token < 下限 $MIN_TOKENS），疑似截断"
            else -> ""
        }
        if (lastGuard.isNotEmpty()) {
            Log.w("FireflyTTS", "守卫拦截：$lastGuard —— 该音频应丢弃并降级纯文字")
        }
        val tokArr = yList.filter { it < 1024L }.toLongArray()

        // ---- vits（目标 fea + 参考 fea）----
        var feaArr = FloatArray(0); var feaRefArr = FloatArray(0)
        runClose(sVits, {
            mapOf(
                "codes" to OnnxTensor.createTensor(env, LongBuffer.wrap(tokArr), longArrayOf(1, 1, tokArr.size.toLong())),
                "text" to OnnxTensor.createTensor(env, LongBuffer.wrap(textIds), longArrayOf(1, text.size.toLong())),
                "refer" to OnnxTensor.createTensor(env, ref.referSpec, longArrayOf(1, 1025, ref.referSpec.remaining() / 1025L)),
            )
        }) { res ->
            feaArr = copyFloat(res.get(0) as OnnxTensor)
        }
        val feaLen = tokArr.size * 4
        runClose(sVits, {
            mapOf(
                "codes" to OnnxTensor.createTensor(env, ref.promptSem, longArrayOf(1, 1, ref.promptSem.remaining().toLong())),
                "text" to OnnxTensor.createTensor(env, ref.refPhones, longArrayOf(1, ref.refPhones.remaining().toLong())),
                "refer" to OnnxTensor.createTensor(env, ref.referSpec, longArrayOf(1, 1025, ref.referSpec.remaining() / 1025L)),
            )
        }) { res ->
            feaRefArr = copyFloat(res.get(0) as OnnxTensor)
        }
        Log.i("FireflyTTS", "vits: ${SystemClock.elapsedRealtime() - t0}ms feaLen=$feaLen")

        // ---- CFM（官方 chunk 循环，4 步欧拉；行主序 [c*T+t]）----
        val tC = SystemClock.elapsedRealtime()
        var tMin = minOf(ref.mel2.remaining() / 100, ref.referSpec.remaining() / 1025)
        val mel2Flat = FloatArray(ref.mel2.remaining()); ref.mel2.duplicate().get(mel2Flat)
        val feaRefFlat = FloatArray(feaRefArr.size); System.arraycopy(feaRefArr, 0, feaRefFlat, 0, feaRefArr.size)
        val mel2Cols = mel2Flat.size / 100
        val feaRefCols = feaRefFlat.size / 512
        val mel2v = FloatArray(100 * tMin)
        val feaRefV = FloatArray(512 * tMin)
        for (c in 0 until 100) for (t in 0 until tMin) mel2v[c * tMin + t] = mel2Flat[c * mel2Cols + t]
        for (c in 0 until 512) for (t in 0 until tMin) feaRefV[c * tMin + t] = feaRefFlat[c * feaRefCols + t]
        val tRef = 500
        if (tMin > tRef) {
            for (c in 0 until 100) for (t in 0 until tRef) mel2v[c * tRef + t] = mel2v[c * tMin + (tMin - tRef) + t]
            for (c in 0 until 512) for (t in 0 until tRef) feaRefV[c * tRef + t] = feaRefV[c * tMin + (tMin - tRef) + t]
            tMin = tRef
        }
        val chunkLen = 1000 - tMin
        val cfmParts = ArrayList<FloatArray>()
        var pos = 0
        val steps = 4
        val dStep = 1.0f / steps
        var curPrompt = tMin
        while (pos < feaLen) {
            val take = minOf(chunkLen, feaLen - pos)
            val T = curPrompt + take
            val mu = FloatArray(512 * T)
            for (c in 0 until 512) {
                for (t in 0 until curPrompt) mu[c * T + t] = feaRefV[c * tMin + t]
                for (t in 0 until take) mu[c * T + curPrompt + t] = feaArr[c * feaLen + pos + t]
            }
            var xv = FloatArray(100 * T)
            for (i in 0 until 100 * T) xv[i] = (Math.random() * 2.0 - 1.0).toFloat()
            val promptX = FloatArray(100 * T)
            for (c in 0 until 100) for (t in 0 until curPrompt) promptX[c * T + t] = mel2v[c * tMin + t]
            for (c in 0 until 100) for (t in 0 until curPrompt) xv[c * T + t] = 0f
            var t = 0.0f
            for (j in 0 until steps) {
                var vpArr = FloatArray(T * 100)
                runClose(sCfm, {
                    mapOf(
                        "x" to OnnxTensor.createTensor(env, FloatBuffer.wrap(xv), longArrayOf(1, 100, T.toLong())),
                        "prompt_x" to OnnxTensor.createTensor(env, FloatBuffer.wrap(promptX), longArrayOf(1, 100, T.toLong())),
                        "x_lens" to OnnxTensor.createTensor(env, LongBuffer.wrap(longArrayOf(T.toLong())), longArrayOf(1)),
                        "t" to OnnxTensor.createTensor(env, FloatBuffer.wrap(floatArrayOf(t)), longArrayOf()),
                        "d" to OnnxTensor.createTensor(env, FloatBuffer.wrap(floatArrayOf(dStep)), longArrayOf()),
                        "mu" to OnnxTensor.createTensor(env, FloatBuffer.wrap(mu), longArrayOf(1, 512, T.toLong())),
                    )
                }) { res ->
                    vpArr = copyFloat(res.get(0) as OnnxTensor)
                }
                for (tt in 0 until T) for (c in 0 until 100) xv[c * T + tt] += dStep * vpArr[tt * 100 + c]
                t += dStep
                for (c in 0 until 100) for (tt in 0 until curPrompt) xv[c * T + tt] = 0f
                vpArr = FloatArray(0)
            }
            val gen = FloatArray(100 * take)
            for (c in 0 until 100) for (t in 0 until take) gen[c * take + t] = xv[c * T + curPrompt + t]
            cfmParts.add(gen)
            val roll = minOf(tMin, take)
            for (c in 0 until 100) {
                for (t in 0 until roll) mel2v[c * tMin + t] = gen[c * take + (take - roll) + t]
                for (t in roll until tMin) mel2v[c * tMin + t] = 0f
            }
            for (c in 0 until 512) {
                for (t in 0 until roll) feaRefV[c * tMin + t] = feaArr[c * feaLen + pos + (take - roll) + t]
                for (t in roll until tMin) feaRefV[c * tMin + t] = 0f
            }
            pos += take
        }
        val totalMel = cfmParts.sumOf { it.size / 100 }
        val melAll = FloatArray(100 * totalMel)
        var off = 0
        for (p in cfmParts) { System.arraycopy(p, 0, melAll, off, p.size); off += p.size }
        cfmParts.clear()
        Log.i("FireflyTTS", "CFM: ${SystemClock.elapsedRealtime() - tC}ms mel=$totalMel")

        // ---- vocoder ----
        val tV2 = SystemClock.elapsedRealtime()
        val melD = FloatArray(melAll.size)
        for (i in melAll.indices) melD[i] = (melAll[i] + 1f) / 2f * 14f - 12f
        var audioArr = FloatArray(0)
        runClose(sVoc, {
            mapOf("mel" to OnnxTensor.createTensor(env, FloatBuffer.wrap(melD), longArrayOf(1, 100, totalMel.toLong())))
        }) { res ->
            audioArr = copyFloat(res.get(0) as OnnxTensor)
        }
        Log.i("FireflyTTS", "vocoder: ${SystemClock.elapsedRealtime() - tV2}ms")

        // ---- 写 wav（头 0.15s + 尾 0.5s）----
        val sr = 48000
        val padHead = (sr * 0.15).toInt()
        val padTail = sr / 2
        val total = audioArr.size + padHead + padTail
        val pcm = ByteBuffer.allocate(total * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until padHead) pcm.putShort(0)
        for (i in audioArr.indices) {
            pcm.putShort((audioArr[i].coerceIn(-1f, 1f) * 32767).toInt().toShort())
        }
        for (i in 0 until padTail) pcm.putShort(0)
        val wav = ByteBuffer.allocate(44 + total * 2).order(ByteOrder.LITTLE_ENDIAN)
        wav.put("RIFF".toByteArray()).putInt(36 + total * 2).put("WAVE".toByteArray())
        wav.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(sr).putInt(sr * 2).putShort(2).putShort(16)
        wav.put("data".toByteArray()).putInt(total * 2).put(pcm.array())

        Log.i("FireflyTTS", "synthesize 总计: ${SystemClock.elapsedRealtime() - t0}ms")
        return wav.array()
    }

    fun close() {        listOf(sEnc, sFs, sSd, sVits, sCfm, sVoc, sBert).forEach { runCatching { it.close() } }
    }

    // ---------- tensor 拷贝（close 前取回数据） ----------
    private fun copyLong(t: OnnxTensor): LongArray {
        val b = t.longBuffer
        val a = LongArray(b.remaining()); b.get(a); return a
    }
    private fun copyFloat(t: OnnxTensor): FloatArray {
        val b = t.floatBuffer
        val a = FloatArray(b.remaining()); b.get(a); return a
    }
    private fun copyInt(t: OnnxTensor): IntArray {
        val b = t.intBuffer
        val a = IntArray(b.remaining()); b.get(a); return a
    }

    private fun argmaxFloat(buf: FloatArray, size: Int): Long {
        var best = 0; var bv = Float.NEGATIVE_INFINITY
        for (i in 0 until size) { val f = buf[i]; if (f > bv) { bv = f; best = i } }
        return best.toLong()
    }
}
