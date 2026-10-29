# -*- coding: utf-8 -*-
"""G1：部署清单的本地 v1→v2→v1 回滚与缺备份负例。

只在临时沙箱读写，不调用 ssh/scp/systemctl，也不接触生产或 user_data。
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  V {label}")
    else:
        FAIL += 1
        print(f"  X {label}" + (f"  [{detail}]" if detail else ""))


def run() -> None:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "tools"))
    import deploy_manifest as dm

    with tempfile.TemporaryDirectory(prefix="firefly_deploy_g1_") as td:
        tmp = Path(td)
        candidate = tmp / "candidate"
        sandbox = tmp / "sandbox"
        (candidate / "server").mkdir(parents=True)
        (sandbox / "server").mkdir(parents=True)

        rel = "server/app.py"
        (candidate / rel).write_text("V2\n", encoding="utf-8")
        (sandbox / rel).write_text("V1\n", encoding="utf-8")
        (candidate / "server" / "new.py").write_text("NEW\n", encoding="utf-8")

        print("=== A. 真实本地沙箱 v1→v2→v1 ===")
        out = dm.sandbox_rehearse(
            candidate, "g1-roundtrip", [rel, "server/new.py"], str(sandbox))
        check("A1 演练通过并写证据", out.get("ok"), str(out))
        check("A2 回滚后旧文件恢复为 v1", (sandbox / rel).read_text("utf-8") == "V1\n")
        check("A3 回滚后新增文件被移除", not (sandbox / "server" / "new.py").exists())
        evidence = json.loads(Path(out["evidence"]).read_text(encoding="utf-8"))
        check("A4 证据同时记录 v1→v2 与 v2→v1",
              evidence.get("v1_to_v2") is True and evidence.get("v2_to_v1") is True,
              str(evidence))

        mp = Path(out["detail"]["manifest"])
        backup_record = dm.backup_path(str(mp))
        rec = json.loads(backup_record.read_text(encoding="utf-8"))
        check("A5 部署前已有文件有独立备份与哈希",
              rec["files"][0].get("existed") is True
              and bool(rec["files"][0].get("before_sha256"))
              and Path(rec["files"][0]["backup"]).is_file())
        check("A6 原不存在的部署文件记录为创建语义",
              rec["files"][1].get("existed") is False
              and not rec["files"][1].get("backup"))

        print("=== B. 缺备份、备份漂移、manifest 漂移必须失败 ===")
        missing_copy = rec["files"][0]["backup"] + ".missing"
        rec_missing = json.loads(json.dumps(rec))
        rec_missing["files"][0]["backup"] = missing_copy
        missing_record = tmp / "missing-backup.json"
        missing_record.write_text(json.dumps(rec_missing), encoding="utf-8")
        missing_plan = dm.rollback_plan(
            candidate, str(mp), record_path=str(missing_record))
        check("B1 缺备份时拒绝生成恢复步骤",
              not missing_plan.get("ok") and "备份文件缺失" in str(missing_plan.get("msg")),
              str(missing_plan))

        backup = Path(rec["files"][0]["backup"])
        backup.write_text("DRIFT\n", encoding="utf-8")
        drift_plan = dm.rollback_plan(candidate, str(mp))
        check("B2 备份字节漂移时拒绝恢复",
              not drift_plan.get("ok") and "漂移" in str(drift_plan.get("msg")),
              str(drift_plan))

        # 恢复一份正确备份，再修改候选源制造 manifest 漂移。
        dm.sandbox_rehearse(
            candidate, "g1-drift", [rel], str(sandbox))
        drift_manifest = candidate / "_harden" / "deploy" / "g1-drift" / "manifest.json"
        (candidate / rel).write_text("V2-CHANGED\n", encoding="utf-8")
        applied = dm.sandbox_apply(candidate, str(drift_manifest), str(sandbox))
        check("B3 manifest 后候选字节变化时拒绝部署",
              not applied.get("ok") and "sha256 变化" in str(applied.get("msg")),
              str(applied))

        bad_build = dm.build(candidate, "g1-path", ["../escape.py"])
        check("B4 路径穿越在生成清单前被拒绝", not bad_build.get("ok"), str(bad_build))

        no_record = dm.rollback_plan(
            candidate, str(drift_manifest), record_path=str(tmp / "absent.json"))
        check("B5 无备份证据时不输出盲 cp 恢复命令", not no_record.get("ok"))


def main() -> int:
    try:
        run()
    except Exception:
        global FAIL
        FAIL += 1
        print("  X 未捕获异常")
        traceback.print_exc()
    print(f"\n=== deploy rollback: PASS={PASS} FAIL={FAIL} ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
