# -*- coding: utf-8 -*-
"""热更新/公告验签公钥（RSA-2048）—— **Python 侧副本**。

★ 为什么会有"两份公钥"：
  安卓端验签走 Kotlin（`java.security`，平台自带、零依赖）；PC 端没有 Kotlin 桥，
  于是 `verify.py` 用标准库自实现 RSA PKCS#1 v1.5 验签（见该文件说明）。
  两处都需要这把公钥，就必然有两份副本 —— 所以：

  ⚠️ **两份必须逐字节一致**，由 `tools/check_hotupdate.py`（第 5 门禁）强制比对
     `app/hotupdate/pubkey.py` ↔ `android/.../HotUpdateKeys.kt`。
     改一处忘另一处 → PC 端"补丁全部验签失败"且**零报错**（只是永远不更新）。

本文件由 `python tools/hotupdate_keys.py --show --python` 生成，不要手改。
"""
from __future__ import annotations

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA1TlCKE/5OyrO8sTI+hPK
vLEhv8T9csFWrZH2FZ/AIzohArKMNjvjitdIE94XwCCEBw1t1dXBNNU1hduZlHeF
CVuVYPVSi8tt3PfZzyBEXXqTRZIxE3xH/FUAT7jT+jHtPWb2t21OxqxlESY7+PLV
u03fIVbx/DhrGLhBJdT3UfkU8GkVEyxkNlcw1MDHln9PrBtd77/neYdIPHJidRkn
Jz2Zvt8AStWhYFKqZ8TMA92LzC4PY8lJvDty3sZTDzyNpQt9WmI9EWIfP5lKhdP+
RmXvEf+TKGlMrwiCcpz8pAdzHvIz7ABsAyfVVa+7+w3q0x19xISD2RnocMH1GkFH
2QIDAQAB
-----END PUBLIC KEY-----"""
