# -*- coding: utf-8 -*-
"""反馈采集端点测试（harness P1）——不依赖真实 LLM / 真实用户数据。

覆盖：
  1. verdict/reason_label 审查（不合法拒绝）
  2. 合法反馈落盘（临时目录，字段完整、上下文快照存在）
  3. reroll 无 Key / need_key 分支（不触碰真实会话与文件）
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import modules.app_config as cfg
import routes

_PASS = _FAIL = 0


def check(name, ok, extra=""):
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  V {name}")
    else:
        _FAIL += 1
        print(f"  X {name} {extra}")


class FakeH:
    def __init__(self):
        self.resp = None
        self.headers = {}
    def _json(self, obj, code=200):
        self.resp = obj


def main():
    tmp = Path(tempfile.mkdtemp(prefix="firefly_feedback_test_"))

    # 隔离：mode_data_dir 指向临时目录，feedback 不写真实 user_data
    orig_mode_data_dir = cfg.mode_data_dir
    orig_get_client = cfg.get_client
    cfg.mode_data_dir = lambda mode="story": tmp / "data"
    cfg.get_client = lambda: None   # 保证 reroll 走 need_key 分支，不触碰真实会话/文件

    try:
        # ── 1. 审查：非法 verdict / 非法标签 ──
        h = FakeH()
        routes.feedback(h)
        body = json.loads('{"verdict": "bad"}')
        # 直接调用底层分支：构造已读 body 的方式——feedback 内部 _read_json(h)，
        # 无 Content-Length 时返回 {}，等价于空 body → verdict 非法。
        check("空 body verdict 拒绝", h.resp and h.resp.get("ok") is False)

        # 2. 合法 dislike 落盘（临时目录）
        import io
        class H2(FakeH):
            def __init__(self, body):
                super().__init__()
                self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.headers = {"Content-Length": str(len(self.body))}
                self.rfile = io.BytesIO(self.body)
        h = H2({"session_id": "t-feedback", "mode": "story",
                "verdict": "dislike", "reason_label": "记错了",
                "reason_text": "她不该说我们约过这件事"})
        routes.feedback(h)
        check("合法 dislike 返回 ok", h.resp and h.resp.get("ok") is True, str(h.resp))
        fp = tmp / "data" / "feedback.jsonl"
        check("feedback.jsonl 已创建", fp.exists())
        if fp.exists():
            line = json.loads(fp.read_text(encoding="utf-8").strip().splitlines()[-1])
            check("字段 verdict 正确", line.get("verdict") == "dislike")
            check("字段 reason_label 正确", line.get("reason_label") == "记错了")
            check("字段 reason_text 正确", line.get("reason_text") == "她不该说我们约过这件事")
            check("字段 context 为列表", isinstance(line.get("context"), list))
            check("字段 time/mode 存在", bool(line.get("time") and line.get("mode") == "story"))

        # 3. 非法标签拒绝且不写盘
        before = fp.read_text(encoding="utf-8") if fp.exists() else ""
        h = H2({"verdict": "dislike", "reason_label": "让她更甜"})
        routes.feedback(h)
        check("非法标签拒绝", h.resp and h.resp.get("ok") is False)
        after = fp.read_text(encoding="utf-8") if fp.exists() else ""
        check("非法标签不写盘", before == after)

        # 4. reroll 无 Key → need_key，不触碰会话
        h = H2({"session_id": "t-reroll", "mode": "story"})
        routes.reroll(h)
        check("reroll 无 Key 返回 need_key", h.resp and h.resp.get("need_key") is True, str(h.resp))

        # 5. 理由长度截断（200 字符上限，写盘字段不超限）
        long_reason = "长" * 300
        h = H2({"verdict": "like", "reason_text": long_reason})
        routes.feedback(h)
        line = json.loads(fp.read_text(encoding="utf-8").strip().splitlines()[-1])
        check("理由截断到 200 字符", len(line.get("reason_text", "")) <= 200)
    finally:
        cfg.mode_data_dir = orig_mode_data_dir
        cfg.get_client = orig_get_client
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 50)
    print(f"  PASS={_PASS} FAIL={_FAIL}")
    sys.exit(0 if _FAIL == 0 else 1)


if __name__ == "__main__":
    main()
