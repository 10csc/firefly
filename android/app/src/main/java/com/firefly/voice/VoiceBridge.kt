package com.firefly.voice

import ai.onnxruntime.OrtEnvironment
import android.content.Context
import android.os.SystemClock
import android.util.Log
import org.json.JSONObject
import java.io.File

/**
 * 语音引擎的 **Python(Chaquopy) 侧唯一入口**。
 *
 * 分工（为什么这么切）：
 *   Python（`app/voice/`）：调度、插件状态、文件落盘与清理、HTTP 端点、与主系统耦合
 *   Kotlin（本文件 + TtsEngine + TextFrontend）：**全部推理**。
 *     —— 因为 Chaquopy 装不上 onnxruntime（实测 `No matching distribution found`），
 *        ONNX 只能在 Kotlin 侧跑。
 *
 * Python 侧调用方式：
 * ```python
 * from java import jclass
 * VB = jclass("com.firefly.voice.VoiceBridge")
 * VB.init(models_dir, mood_lib_dir, "happy,sad", "firefly_bert_int8.onnx")
 * wav_bytes = bytes(VB.synthesize("嗯，是我，流萤。", "happy"))
 * ```
 *
 * ★ 线程纪律：所有重活都在 `synchronized(this)` 内串行 —— 满足"同时只能有一个语音在转换"，
 *   也避免 ORT/Kotlin 在并发下的未知行为。调用方（Python）应在工作线程上调用，勿阻塞 UI 线程。
 */
object VoiceBridge {
    private const val TAG = "FireflyVoice"

    /** 由 MainActivity.onCreate 注入（读 assets 用） */
    @Volatile private var appContext: Context? = null

    private var engine: TtsEngine? = null
    private var frontend: TextFrontend? = null
    private var env: OrtEnvironment? = null
    private val refs = HashMap<String, Ref>()

    @Volatile private var ready = false
    @Volatile private var lastError = ""
    @Volatile private var modelsDir = ""
    @Volatile private var moodLibDir = ""

    /** MainActivity 启动时调用一次 */
    @JvmStatic
    fun attach(ctx: Context) {
        appContext = ctx.applicationContext
        Log.i(TAG, "VoiceBridge.attach")
    }

    /** 引擎是否就绪（Python 侧据此决定菜单项是否可点） */
    @JvmStatic
    fun isReady(): Boolean = ready

    /**
     * 装载引擎（幂等；重复调用直接返回现状）。
     * @param modelsDir   模型目录（含 7 个 onnx + weights.bin）
     * @param moodLibDir  语气库根目录（其下每个子目录一本语气）
     * @param moodsCsv    逗号分隔的语气 id，如 "happy,sad"（目录名）
     * @param bertFile    BERT 文件名（int8 / fp16 两档）
     */
    @JvmStatic
    fun init(modelsDir: String, moodLibDir: String, moodsCsv: String,
             bertFile: String = "firefly_bert_int8.onnx"): String {
        synchronized(this) {
            this.modelsDir = modelsDir
            this.moodLibDir = moodLibDir
            if (ready) return statusJson()
            val ctx = appContext ?: return fail("VoiceBridge.attach 未调用（Kotlin 侧未注入 Context）")
            val t0 = SystemClock.elapsedRealtime()
            try {
                val md = File(modelsDir)
                if (!md.isDirectory) return fail("模型目录不存在: $modelsDir")
                for (f in listOf("firefly_t2s_encoder.onnx", "firefly_t2s_fsdec_int8.onnx",
                                 "firefly_t2s_sdec_int8.onnx", "firefly_t2s_weights_int8.bin",
                                 "firefly_vits_int8.onnx", "firefly_cfm_estimator_int8.onnx",
                                 "firefly_vocoder.onnx", bertFile)) {
                    if (!File(md, f).exists()) return fail("缺模型文件: $f")
                }

                // ① 映射表（assets 直读，不落地）
                val a = ctx.assets
                val symToId = JSONObject(a.open("symbols.json").bufferedReader().use { it.readText() })
                    .let { jo -> jo.keys().asSequence().associate { it to jo.getInt(it) } }
                val charToId = JSONObject(a.open("char2id.json").bufferedReader().use { it.readText() })
                    .let { jo -> jo.keys().asSequence().associate { it to jo.getInt(it) } }

                // ② 文本前端（拼音表 + opencpop 映射）
                val fe = TextFrontend(symToId).also {
                    it.loadPinyin(a.open("pinyin.json").bufferedReader().use { r -> r.readText() })
                    it.loadOpencpop(a.open("opencpop-strict.txt").bufferedReader().use { r -> r.readText() })
                }

                // ②.5 语气库：assets 里是 AssetManager 才能读的，而 loadMoodFromDir 要**文件系统路径**
                //      → 首次运行时从 assets 落到私有目录（4 MB，一次性；后续升级不重复）
                val libDir = File(moodLibDir)
                if (!File(libDir, ".copied").exists()) {
                    copyAssetsDir(ctx, "mood_lib", libDir)
                    File(libDir, ".copied").writeText("1")
                    Log.i(TAG, "语气库已从 assets 落地: ${libDir.absolutePath}")
                }

                // ③ 语气参考特征（只读预计算结果，零推理）
                val want = moodsCsv.split(",").map { it.trim() }.filter { it.isNotEmpty() }
                if (want.isEmpty()) return fail("未指定语气")
                val loaded = ArrayList<String>()
                for (m in want) {
                    val d = File(moodLibDir, m)
                    if (!d.isDirectory) return fail("语气目录不存在: ${d.absolutePath}")
                    refs[m] = loadMoodFromDir(d.absolutePath)
                    loaded.add(m)
                }

                // ④ 引擎 session（6 + BERT，会话级常驻；不可每句重建）
                val e = OrtEnvironment.getEnvironment()
                env = e
                engine = TtsEngine(e, md.absolutePath, charToId, bertFile)
                frontend = fe
                ready = true
                lastError = ""
                Log.i(TAG, "语音引擎就绪 ${SystemClock.elapsedRealtime() - t0}ms 语气=$loaded")
                return statusJson()
            } catch (t: Throwable) {
                Log.e(TAG, "语音引擎装载失败", t)
                return fail("装载失败: ${t.javaClass.simpleName}: ${t.message}")
            }
        }
    }

    /** 状态 JSON（Python 侧解析；也用于报错定位） */
    @JvmStatic
    fun statusJson(): String {
        val o = JSONObject()
        o.put("ready", ready)
        o.put("error", lastError)
        o.put("models_dir", modelsDir)
        o.put("mood_lib_dir", moodLibDir)
        o.put("moods", refs.keys.sorted())
        o.put("loaded", engine != null)
        return o.toString()
    }

    /**
     * 文本 → wav 字节（PCM16 WAV，48 kHz）。
     * 失败返回 null，原因写入 [statusJson] 的 `error`。
     */
    @JvmStatic
    fun synthesize(text: String, mood: String): ByteArray? {
        synchronized(this) {
            val e = engine ?: run { lastError = "引擎未初始化"; return null }
            val fe = frontend ?: run { lastError = "文本前端未初始化"; return null }
            val ref = refs[mood] ?: run { lastError = "未知语气: $mood"; return null }
            val t = text.trim()
            if (t.isEmpty()) { lastError = "空文本"; return null }
            return try {
                // TextFrontend 第三个返回值是**与 word2ph 逐位对应**的字符序列，
                // 必须用它（不是 normalize 后的全文）去查 BERT 的 char2id —— 否则索引越界
                val (ids, w2ph, aligned) = fe.process(t)
                if (ids.isEmpty() || w2ph.isEmpty()) {
                    lastError = "文本前端未产出音素（可能含未支持字符）"
                    return null
                }
                val bert = e.runBert(aligned, w2ph)
                val wav = e.synthesize(ids, bert, ref)
                lastError = ""
                wav
            } catch (th: Throwable) {
                Log.e(TAG, "合成失败", th)
                lastError = "合成失败: ${th.javaClass.simpleName}: ${th.message}"
                null
            }
        }
    }

    /** 释放全部 session（内存归还；插件"停用"时调用） */
    @JvmStatic
    fun release() {
        synchronized(this) {
            runCatching { engine?.close() }
            engine = null
            frontend = null
            refs.clear()
            ready = false
            Log.i(TAG, "语音引擎已释放")
        }
    }

    private fun fail(msg: String): String {
        lastError = msg
        ready = false
        Log.w(TAG, "语音引擎不可用: $msg")
        return statusJson()
    }

    /** 把 assets 下某个目录递归落到文件系统（语气库要用 File 读，AssetManager 给不了路径） */
    private fun copyAssetsDir(ctx: Context, assetName: String, dest: File) {
        dest.mkdirs()
        val children = ctx.assets.list(assetName) ?: return
        for (name in children) {
            val childAsset = "$assetName/$name"
            val sub = ctx.assets.list(childAsset)
            if (sub != null && sub.isNotEmpty()) {
                copyAssetsDir(ctx, childAsset, File(dest, name))
            } else {
                ctx.assets.open(childAsset).use { input ->
                    File(dest, name).outputStream().use { out -> input.copyTo(out) }
                }
            }
        }
    }
}