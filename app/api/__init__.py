# -*- coding: utf-8 -*-
"""app/api — HTTP 端点层（阶段 2 拆分产物）

- data_export.py  导出/导入/本地备份（/export-data、/import-data、/backup/*）
- data_sync.py    增量同步（/sync/manifest、/sync/import、/sync/export、/sync/now）

端点层只做参数审查与响应组装，落盘内核在 infra/（如 infra.sync.restore）。
对外的路由模块名仍是 `routes_data`（兼容层 re-export），既有消费方零改动。
"""
