package com.firefly.voice

import org.json.JSONObject

/**
 * 纯 Kotlin 文本前端：中文 → phones ID + word2ph（与 GPT-SoVITS v4 官方 chinese.py 逻辑一致）
 * 依赖：pinyin.json（pypinyin 数据）+ opencpop-strict.txt（拼音→符号映射）+ symbols.json（符号→ID）
 */
class TextFrontend(private val symToId: Map<String, Int>) {

    // 拼音 → 符号映射（opencpop-strict.txt，与官方一致）
    private val pinyinToSymbol = HashMap<String, String>()

    // 汉字 → (声母, 韵母+声调)
    private val pinyinData = HashMap<String, Pair<String, String>>()

    // 标点（官方 symbols.punctuation 子集，v4 用符号本身）
    private val punctuationSet = setOf("!", "?", "…", ",", ".", "-", "“", "”", "‘", "’", "(", ")", " ", ":", ";", "、", "！", "？", "，", "。", "：", "；")

    private val repMap = mapOf(
        "：" to ",", "；" to ",", "，" to ",", "。" to ".", "！" to "!", "？" to "?",
        "\n" to ".", "·" to ",", "、" to ",", "..." to "…", "$" to ".", "/" to ",",
        "—" to "-", "~" to "…", "～" to "…",
    )

    init {
        // opencpop 映射（从私有目录读取，MainActivity 拷贝时放入 filesDir/python/）
        val mapFile = java.io.File(android.os.Environment.getDataDirectory(), "unused")
        // 实际路径由 MainActivity 注入：这里用静态缓存
    }

    /** 注入 opencpop 映射内容（MainActivity 从 filesDir 读取后调用） */
    fun loadOpencpop(content: String) {
        for (line in content.lineSequence()) {
            val parts = line.trim().split("\t")
            if (parts.size == 2) pinyinToSymbol[parts[0]] = parts[1]
        }
    }

    /** 注入 pinyin.json 内容 */
    fun loadPinyin(content: String) {
        val jo = JSONObject(content)
        val keys = jo.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            val v = jo.getJSONObject(k)
            pinyinData[k] = v.getString("i") to v.getString("f")
        }
    }

    /**
     * 文本 → (音素ID, word2ph, 与 word2ph 一一对应的字符序列)
     *
     * ★ 第三个返回值必须与 word2ph **逐位对应**：BERT 分支要用它对每个字取
     *   token id，ONNX 图内部按 word2ph 做 repeat_interleave 展开。
     *   之前这里返回的是 normalize 后的完整文本，而查不到拼音/映射的字会被
     *   continue 跳过（不写 word2ph），于是出现 "normText 16 字 vs word2ph 14 项"
     *   → runBert 逐字索引越界（length=14; index=14）。
     */
    fun process(text: String): Triple<IntArray, IntArray, String> {
        val norm = normalize(text)
        // 按标点断句（官方 g2p 的 split 逻辑）
        val escPunc = punctuationSet.joinToString("") { Regex.escape(it) }
        val pattern = Regex("(?<=[$escPunc])\\s*")
        val sentences = norm.split(pattern).filter { it.isNotBlank() }

        val phones = ArrayList<String>()
        val word2ph = ArrayList<Int>()
        val aligned = StringBuilder()   // 真正进入 word2ph 的字符，顺序与 word2ph 一致
        for (seg in sentences) {
            val segClean = seg.replace(Regex("[a-zA-Z]+"), "")
            for (c in segClean) {
                if (c == ' ') continue
                if (punctuationSet.contains(c.toString())) {
                    phones.add(c.toString())
                    word2ph.add(1)
                    aligned.append(c)
                    continue
                }
                val py = pinyinData[c.toString()]
                if (py == null) {
                    // 未知字 → 跳过（不计入 word2ph，也不进 aligned）
                    continue
                }
                val (ini, finTone) = py
                if (finTone.isEmpty()) {
                    // pinyin.json 里「嗯」的 f 是空串；直接 finTone.last() 会抛异常
                    continue
                }
                if (ini == finTone) {  // 声母==韵母（标点已被上面处理，此处理论不达）
                    phones.add(c.toString())
                    word2ph.add(1)
                    aligned.append(c)
                    continue
                }
                // ★★ 轻声修复（关键）★★
                // 并非所有字的 f 都带声调数字：pinyin.json 的 978 字里有 15 个高频虚词
                // （的/了/吗/吧/啊/们/呢/么/呀/啦/嘛/嘞/子/着/蓿）的 f 不带数字。
                // 原实现无条件 `finTone.last()` 当声调、`dropLast(1)` 当韵母，
                // 于是这些字的韵母被切残（"ba"→"b"、"en"→"e"）→ 查不到 opencpop 映射
                // → 静默丢弃。后果：「今天天气真好啊，我们去看星星吧。」里的 啊/们/吧
                // 根本没进合成，听感就是"音频对不上文本"。
                // 官方把轻声记为第 5 声；symbols.json 里 a5/e5/en5 等均存在。
                val hasTone = finTone.last().isDigit()
                val tone = if (hasTone) finTone.last().toString() else "5"
                val base = if (hasTone) finTone.dropLast(1) else finTone
                var pinyin = ini + base
                if (ini.isNotEmpty()) {
                    pinyin = when (base) {
                        "uei" -> ini + "ui"
                        "iou" -> ini + "iu"
                        "uen" -> ini + "un"
                        else -> pinyin
                    }
                } else {
                    pinyin = when (pinyin) {
                        "ing" -> "ying"
                        "i" -> "yi"
                        "in" -> "yin"
                        "u" -> "wu"
                        else -> when (pinyin.firstOrNull()) {
                            'v' -> "yu" + pinyin.drop(1)
                            'e' -> "e" + pinyin.drop(1)
                            'i' -> "y" + pinyin.drop(1)
                            'u' -> "w" + pinyin.drop(1)
                            else -> pinyin
                        }
                    }
                }
                val sym = pinyinToSymbol[pinyin]
                if (sym == null) {
                    // 映射缺失：跳过
                    continue
                }
                val parts = sym.split(" ")
                if (parts.size != 2) continue
                phones.add(parts[0])
                phones.add(parts[1] + tone)
                word2ph.add(2)
                aligned.append(c)
            }
        }
        val ids = IntArray(phones.size) { symToId[phones[it]] ?: 0 }
        return Triple(ids, word2ph.toIntArray(), aligned.toString())
    }

    private fun normalize(text: String): String {
        var t = text.replace("嗯", "恩").replace("呣", "母")
        // 数字转中文（简化：cn2an 不可用时的最小实现——直接保留数字会无法映射，跳过数字处理）
        t = t.replace("，", ",").replace("。", ".").replace("！", "!").replace("？", "?")
        t = t.replace("：", ",").replace("；", ",").replace("、", ",").replace("…", "…").replace("～", "…")
        // 去掉非中文字符和标点集合以外的字符
        val allowed = Regex("[^\\u4e00-\\u9fa5" + punctuationSet.joinToString("") + "]+")
        t = allowed.replace(t, "")
        return t
    }
}
