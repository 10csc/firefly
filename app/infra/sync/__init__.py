# -*- coding: utf-8 -*-
"""app/infra/sync — 同步/恢复落盘内核

- restore.py  导入与快照恢复（临时目录 + 整体 rename 的原子换入；失败保持旧数据原样）
"""
