# -*- coding: utf-8 -*-
"""app/core — 阶段 2 拆分出的核心层

- paths.py      路径公式与平台常量（BASE_DIR / ROOT / USER_DIR / CONFIG_FILE / mode_* 目录）
- userctx.py    每请求用户上下文（contextvars）与配额钩子注册
- config.py     config.json 读写、供应商 CRUD、eff_cfg、get_client
- presets.py    预设包注册表（PRESETS / MODES / reload_presets / char_name / user_name）
- migrations.py 老数据迁移、默认文件拷贝、首启初始化

对外契约仍是 `modules.app_config`（兼容层 re-export），既有消费方零改动。
"""
