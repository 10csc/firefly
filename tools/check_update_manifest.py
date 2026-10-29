# -*- coding: utf-8 -*-
"""更新清单签名门禁：等价于 tools/sign_update_manifest.py --check。"""
from __future__ import annotations
import subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, str(ROOT / "tools" / "sign_update_manifest.py"), "--check"] + sys.argv[1:]))
