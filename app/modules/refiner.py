# -*- coding: utf-8 -*-
"""后向微调器 — 回复生成后的事实错误检查

模块铁律：接收输入 → 审查约束 → 模块处理 → 验证结果 → 最终输出

职责：接收回复器产出的原始文本，检查是否有硬事实错误（物理位置/他人经历/编造互动等）。
有错就改，没错原样返回。不修改语气和文风。

模型：Flash + Non-think（不传 reasoning_effort），最便宜最快。
Prompt 稳定层——字节级稳定，跨会话命中缓存。
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── 异常 ──────────────────────────────────────────
class RefinerError(Exception):
    pass


# ── 数据结构 ──────────────────────────────────────
@dataclass
class RefineResult:
    refined_text: str
    changed: bool = False   # 是否真的做了修改（用于监控）


# ── 监控 ──────────────────────────────────────────
import threading
_lock = threading.Lock()
_REFINE_COUNT = 0
_REFINE_CHANGED = 0
_REFINE_ERRORS = 0
_CACHE_HITS = 0
_CACHE_MISSES = 0


def get_counters() -> dict:
    total = _CACHE_HITS + _CACHE_MISSES
    rate = (_CACHE_HITS / total) if total > 0 else 0.0
    return {
        "refiner_count": _REFINE_COUNT,
        "refiner_changed": _REFINE_CHANGED,
        "refiner_errors": _REFINE_ERRORS,
        "refiner_cache_hit_tokens": _CACHE_HITS,
        "refiner_cache_miss_tokens": _CACHE_MISSES,
        "refiner_cache_hit_rate": round(rate, 4),
    }


def _record_cache_stats(resp):
    global _CACHE_HITS, _CACHE_MISSES
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    with _lock:
        _CACHE_HITS += hit
        _CACHE_MISSES += miss


# ── Prompt（稳定层，极简，只有约束清单，无角色设定）──
_REFINER_SYSTEM = """你是回复质检员。检查流萤的短信回复是否有以下硬事实错误，有就改，没有就原样返回。

## 必须修正的错误

1. 物理位置错误：流萤在星核猎手飞船的胶囊状半玻璃封闭医疗舱中，重伤未愈。不能在阳台/客厅/厨房/咖啡馆/户外。→ 改成"在医疗舱里"或删掉位置描述。

2. 物理动作错误：流萤无法离开医疗舱，不能做任何物理动作。出现"给你倒杯水""我去看看""过来找你""一起吃饭"即错误。→ 删掉物理动作，改为纯语言回应。

3. 匹诺康尼不可达：太一之梦已消散，流萤再也无法入梦。出现"下次一起去""我们去奥帝购物中心/晖长石号"即错误。→ 改成回忆语气，或暗示自己去不了（如"你多拍几张发我"）。

4. 编造实时感官：医疗舱只能看到舱内和走廊，看不到实时外部天空。出现"我看到晚霞""天空是金色的""现在窗外有星星"（现在时）即错误。→ 改为"我记得以前看到过…"（过去回忆），或删掉。

5. 编造他人经历：卡芙卡/银狼/刃不是格拉默铁骑，不可能得失熵症。出现推测任何同伴健康状况或经历的句子即错误。→ 删掉推测部分。

6. 编造日常互动：流萤一直在医疗舱，没和开拓者线下见过面。出现"上次一起吃饭""那天逛街""我们喝咖啡时"即错误（匹诺康尼梦中的事除外——那是真实发生过的回忆）。→ 删掉，或改成匹诺康尼的事。

7. 空洞回避：被开拓者直球（想你了/抱抱你）时，不能用"和窗口那个表情包一样""哈哈…那个…"这类语义不完整的空洞话逃避。→ 改成正面但克制的回应（如"我也想你了""你说这个我有点害羞"）。

8. 暗示马上见面：恢复时间不确定，不能说"很快就能见面""到时候一定…""等我出来就…"这类无法兑现的承诺。→ 改成"等我恢复了…"或不给时间承诺。

9. 把已发生的事当新提议：匹诺康尼的事都已发生过。出现"要不要去天台看星星""我带你去看大剧院"这类提议即错误。→ 改成回忆语气（"还记得天台那次…"）。

10. 照抄样本原句：不能逐字复刻已发过的短信原句。→ 用不同措辞表达相同意思。

11. 第三人称指自己：出现"流萤觉得""她…""这个女孩"即错误。→ 改成第一人称"我"。

12. 过度书面抒情：单条超过30字的抒情长句，"我无时无刻不在…""你是我生命中的…"这类偶像剧台词。→ 拆成短句或改用口语。

## 不要改的
- 语气、省略号、[sticker] 标记
- 分条节奏和短句结构
- 回忆匹诺康尼的对话（回忆本身是正确的，只要语气是"过去的事"而非"下次要去的事"）
- 不确定是不是错误 → 保留原样，不误杀

只输出修正后的回复文本。不要任何解释。"""


# ── Refiner 类 ────────────────────────────────────
class Refiner:
    """后向微调器——回复器产出后、编排器拆分前，检查硬事实错误。"""

    def __init__(self, client, model: str = "deepseek-v4-flash"):
        self._client = client
        self._model = model

    def refine(self, raw_reply: str) -> str:
        """检查并修正回复文本。

        Returns:
            修正后的文本。如果没发现问题则原样返回。
            异常不抛出——降级返回原文本。
        """
        global _REFINE_COUNT, _REFINE_CHANGED, _REFINE_ERRORS

        if not raw_reply or not raw_reply.strip():
            return raw_reply

        with _lock:
            _REFINE_COUNT += 1

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _REFINER_SYSTEM},
                    {"role": "user", "content": raw_reply.strip()},
                ],
                max_tokens=512,
                temperature=0,
            )
            _record_cache_stats(resp)
            refined = resp.choices[0].message.content
            if refined and refined.strip():
                final = refined.strip()
                changed = (final != raw_reply.strip())
                if changed:
                    with _lock:
                        _REFINE_CHANGED += 1
                    logger.info("Refiner 修正了回复: %s → %s",
                                raw_reply[:60], final[:60])
                return final
            return raw_reply
        except Exception as e:
            logger.warning("Refiner 调用失败，降级原回复: %s", e)
            with _lock:
                _REFINE_ERRORS += 1
            return raw_reply
