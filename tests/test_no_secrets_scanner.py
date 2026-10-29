# -*- coding: utf-8 -*-
"""凭据扫描器必须区分字面量和运行时字符串模板。"""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "firefly_check_no_secrets",
    ROOT / "tools" / "check_no_secrets.py",
)
SCANNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCANNER)


def _scan_line(tmp_path: Path, line: str):
    sample = tmp_path / "sample.kt"
    sample.write_text(line + "\n", encoding="utf-8")
    return SCANNER.scan([str(sample)], 10 * 1024 * 1024, False)[0]


def test_no_secrets_still_flags_literal_token_assignment(tmp_path):
    key = "to" + "ken"
    literal_value = "real" + "-value-123456"
    hits = _scan_line(tmp_path, f'{key} = "{literal_value}"')
    assert len(hits) == 1
    assert hits[0][2] == "token 赋值"


def test_no_secrets_ignores_runtime_template_assignment(tmp_path):
    key = "to" + "ken"
    runtime_value = "$" + "{Process.myPid()}-" + "$" + "{System.nanoTime()}"
    hits = _scan_line(tmp_path, f'{key} = "{runtime_value}"')
    assert hits == []
