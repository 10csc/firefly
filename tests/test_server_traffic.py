# -*- coding: utf-8 -*-
"""网关流量调度回归 —— 共创平台 M4（docs/设计/角色卡共创平台/04）

钉死的事：
- 关闭限速（kbps=0）时行为与改造前一致（永远放行、64KB 块）；
- **保底份额**：三类各自按权重拿到带宽，谁都不会被饿死；
- **空闲带宽可借用**：单类单独跑时能吃满全局速率（不浪费）；
- 申请量超过「速率×超时」→ 返回 False（调用方断流，不无限占用）；
- 按 IP 字节日配额：超限拒绝、窗口滚动后恢复、按 IP 隔离、limit=0 不限；
- gzip 只在**值得**时生效（类型/长度/客户端支持/压缩后确实更小）；
- 路径分级正确（图片/静态→asset，业务→api）。

时间与睡眠全部注入，测试不依赖真实等待，因此是确定性的。
"""
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import traffic as T   # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}")


class Clock:
    """可注入的假时钟：sleep 只推进时间，不真的等待。"""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


print("== 1. 关闭限速 = 改造前行为 ==")
g0 = T.Governor(0, now=Clock().now, sleep=lambda s: None)
check("关闭时不限流", g0.enabled is False and g0.take("bulk", 10 ** 9) is True)
check("关闭时用 64KB 块", g0.chunk("bulk") == 65536)
check("快照标明未启用", g0.snapshot()["enabled"] is False)

print("== 2. 开启限速：突发内小请求立即可发 ==")
c = Clock()
g = T.Governor(800, now=c.now, sleep=c.sleep)      # 800kbps = 100000 B/s
check("突发额度内可立即发送", g.take("api", 1024, timeout=1.0) is True)
# ⚠ 旧断言写的是"限速时恒用 16KB 块"——那编码了一个**错误假设**：块可以大于桶容量。
# 2026-10-01 修的真 bug：`take()` 要等桶里攒够 nbytes，而桶上限就是容量 ⇒ 块比容量大时
# **永远攒不够**（bulk 桶默认仅 ~10.9KB，块却是 16KB，平时靠空闲池掩盖）。
# 不变式：**块 ≤ max(1024, 该类桶容量)**（1024 是"别把块切得太碎"的地板；地板大于极小桶时
# 由 `take()` 的**赤字法**兜住，见 §2b）。
def _cap(gg, cls):
    return max(1024, int(gg._rate[cls] * gg.burst_seconds))


check("块 ≤ max(1KB, 该类桶容量)（不变式）",
      all(g.chunk(c2) <= _cap(g, c2) for c2 in T.CLASSES))
g_hi = T.Governor(T.DEFAULT_KBPS, now=Clock().now, sleep=lambda s: None)
check(f"预算充足（{T.DEFAULT_KBPS}kbps）时 api 块用满 16KB", g_hi.chunk("api") == T.MIN_CHUNK)
check("初始时刻未发生等待", c.t == 0.0)

print("== 2b. 低预算下桶容量 < 块大小：不得卡死（赤字法）==")
c = Clock()
g_low = T.Governor(80, now=c.now, sleep=c.sleep)   # 80kbps：bulk 桶容量仅 ~312B，块取 1KB 地板
check("块不小于 1KB 地板（避免切太碎拖 CPU）", g_low.chunk("bulk") >= 1024)
check("地板大于桶容量时仍能拿到（赤字法，不会卡到超时）",
      g_low.take("bulk", g_low.chunk("bulk"), timeout=5.0) is True)
cl = T.ConnLimiter(400, now=c.now, sleep=c.sleep)  # 400kbps：burst 12500B < 16KB 块
check("单连接限速器：块大于 burst 时用赤字法仍能拿到",
      cl.take(T.MIN_CHUNK, timeout=5.0) is True)
check("赤字被记账（余额可为负，下一次自然多等）", cl._tokens <= 0)

print("== 3. 权重配置与全局上限 ==")
c = Clock()
g = T.Governor(80, now=c.now, sleep=c.sleep)       # 80kbps = 10000 B/s
snap = g.snapshot()
check("各类速率按权重分配", abs(snap["rates_bps"]["api"] / snap["rates_bps"]["bulk"] - 5) < 0.01)
check("速率之和 = 全局速率", abs(sum(snap["rates_bps"].values()) - 10000) < 2)
counts = {k: 0 for k in T.CLASSES}
end = 6.0
while c.t < end:
    for cls in T.CLASSES:
        if c.t >= end:
            break
        if g.take(cls, 1024, timeout=5.0):
            counts[cls] += 1024
total = sum(counts.values())
check(f"三类都能拿到带宽 {counts}", all(counts[k] > 0 for k in T.CLASSES))
check(f"总吞吐不超过全局上限（{total}B / {end}s）", total < 1.15 * 10000 * end)

print("== 3b. 借用门槛：优先级在链路繁忙时生效（确定性，不靠时序）==")


def _prime(free_bytes):
    """造一个"自己的桶已空、空闲池水位固定"的场景，便于精确验证门槛。"""
    cc = Clock()
    gg = T.Governor(80, now=cc.now, sleep=cc.sleep)
    with gg._lock:
        for k in T.CLASSES:
            gg._tokens[k] = 0
        gg._free = float(free_bytes)
    return gg, cc


gg, _ = _prime(700)          # 空闲池 700：≥ asset 门槛(625)，< bulk 门槛(1250)
check("空闲池不足时 bulk 借不到（返回 False）", gg.take("bulk", 512, timeout=0.02) is False)
gg, _ = _prime(700)
check("asset 在同等水位下能借到", gg.take("asset", 512, timeout=0.02) is True)
gg, _ = _prime(700)
check("api 门槛为 0，永远能借", gg.take("api", 512, timeout=0.02) is True)
gg, _ = _prime(1500)         # 高于 bulk 门槛
check("空闲池充足时 bulk 也能借（不浪费带宽）", gg.take("bulk", 512, timeout=0.02) is True)

print("== 4. 空闲带宽可借用（单类能吃满，不浪费）==")
c = Clock()
g = T.Governor(80, now=c.now, sleep=c.sleep)
got = 0
while c.t < 3.0:
    if g.take("bulk", 1024, timeout=5.0):
        got += 1024
check(f"单独跑 bulk 也能吃满全局（{got}B / 3s）", got > 0.7 * 10000 * 3.0)

print("== 5. 超额申请超时返回 False ==")
c = Clock()
g = T.Governor(80, now=c.now, sleep=c.sleep)
ok = g.take("bulk", 10 ** 9, timeout=1.0)
check("申请量远超速率×超时 → False", ok is False)
check("确实等到了超时点", c.t >= 1.0)

print("== 6. 按 IP 字节日配额 ==")
c = Clock()
b = T.IPBudget(1000, window_seconds=100, now=c.now)
check("配额内放行", b.allow("1.1.1.1") is True)
b.charge("1.1.1.1", 600)
check("已用 600 后仍放行", b.allow("1.1.1.1") is True)
b.charge("1.1.1.1", 500)
check("累计 1100 超 1000 → 拒绝", b.allow("1.1.1.1") is False)
check("另一个 IP 不受影响", b.allow("2.2.2.2") is True)
check("用量查询正确", b.used("1.1.1.1") == 1100)
c.t += 101
check("窗口滚动后恢复", b.allow("1.1.1.1") is True)
b0 = T.IPBudget(0)
check("limit=0 表示不限", b0.allow("3.3.3.3") is True and b0.charge("3.3.3.3", 10 ** 9) is None)

print("== 7. gzip 只在值得时生效 ==")
payload = (b'{"items":[' + b'{"name":"\xe6\xb5\x8b\xe8\xaf\x95\xe5\x8d\xa1"}' * 200 + b']}') * 3
packed = T.gzip_if_worth(payload, "application/json", "gzip, deflate")
check("JSON + 客户端支持 → 压缩", packed is not payload and packed[:2] == b"\x1f\x8b")
check("压缩后确实更小", len(packed) < len(payload))
check("客户端不支持 → 原样", T.gzip_if_worth(payload, "application/json", "br") is payload)
check("小响应不压", T.gzip_if_worth(b"{}", "application/json", "gzip") == b"{}")
check("超 4MB 不压", T.gzip_if_worth(b"a" * (T.GZIP_MAX_BYTES + 1), "application/json", "gzip")
      == b"a" * (T.GZIP_MAX_BYTES + 1))
check("非白名单类型不压", T.gzip_if_worth(payload, "image/png", "gzip") is payload)
noise = os.urandom(4096)
check("压不小就返回原文", T.gzip_if_worth(noise, "application/json", "gzip") is noise)

print("== 8. 路径分级 ==")
check("广场图片 → asset", T.classify_path("/plaza/api/asset?id=x&slot=cover") == "asset")
check("静态资源 → asset", T.classify_path("/assets/character/story/cover.png") == "asset")
check("热更 → asset", T.classify_path("/hotupdate/0.9.1/patch-1.zip") == "asset")
check("业务 API → api", T.classify_path("/chat") == "api")
check("广场列表 → api", T.classify_path("/plaza/api/list?page=1") == "api")
check("relay 轮询 → api", T.classify_path("/relay/pending") == "api")

print("== 9. 快照可观测 ==")
c = Clock()
g = T.Governor(800, now=c.now, sleep=c.sleep)
g.take("api", 2048)
snap = g.snapshot()
check("快照含各类速率", set(snap["rates_bps"]) == set(T.CLASSES))
check("快照记录已发字节", snap["sent_bytes"]["api"] == 2048)
check("快照含空闲池", "free_pool" in snap and snap["free_pool"] >= 0)

print("== 10. 单连接上限 ConnLimiter（用户 2026-10-01 要求：一条流不许占满整线）==")
# 背景：只跑一条 bulk 流时它能拿 344KB/s ≈ 满 2800kbps（实测），整线十几秒没余量。
# 单连接上限把"一条流"压到全局的 50%，同时保留 Governor 的类间公平。
check("关闭时(true 0)直接放行", T.ConnLimiter(0).take(10 ** 9) is True)
check("默认单连接上限 = 全局 × 0.5（2800 → 1400）", T.conn_kbps(2800) == 1400)
check("FIREFLY_BW_KBPS=0 时单连接上限也关", T.conn_kbps(0) == 0)
_old = os.environ.get("FIREFLY_CONN_MAX_KBPS")
os.environ["FIREFLY_CONN_MAX_KBPS"] = "900"
check("环境变量可覆盖绝对值（900）", T.conn_kbps(2800) == 900)
os.environ["FIREFLY_CONN_MAX_KBPS"] = "0"
check("环境变量设 0 = 关闭", T.conn_kbps(2800) == 0)
if _old is None:
    os.environ.pop("FIREFLY_CONN_MAX_KBPS", None)
else:
    os.environ["FIREFLY_CONN_MAX_KBPS"] = _old

c = Clock()
lim = T.ConnLimiter(1400, now=c.now, sleep=c.sleep)      # 175000 B/s
check("初始突发 = 速率 × 0.25s", abs(lim.burst - 43750) < 1)
sent = 0
while c.t < 4.0:                                        # 4 秒最多 4×175000 = 700000 B
    if lim.take(16384, timeout=1.0):
        sent += 16384
check(f"4 秒内发送量受限于单连接速率（{sent}B ≤ 4.5×175000）", sent <= 4.5 * 175000)
check("确实推进了（不是卡死）", sent >= 3 * 175000)
chunk_sent = sent
rate = chunk_sent / c.t
check(f"实测速率≈上限（{rate:.0f} B/s vs 175000）", abs(rate - 175000) / 175000 < 0.15)
check("快照可观测", lim.snapshot()["kbps"] == 1400 and lim.snapshot()["sent_bytes"] == sent)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
