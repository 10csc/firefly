# -*- coding: utf-8 -*-
"""监控聚合 — 收集所有模块 get_counters() 为一个 dict"""

import logging

logger = logging.getLogger(__name__)

_COLLECT_COUNT = 0


def collect() -> dict:
    global _COLLECT_COUNT
    _COLLECT_COUNT += 1

    result = {}

    modules = [
        ("analyzer", "modules.analyzer"),
        ("organizer", "modules.organizer"),
        ("polisher", "modules.polisher"),
        ("orchestrator", "orchestrator"),
    ]

    for name, import_path in modules:
        try:
            mod = __import__(import_path, fromlist=["get_counters"])
            if hasattr(mod, "get_counters"):
                result[name] = mod.get_counters()
        except Exception as e:
            logger.warning("metrics: 无法加载 %s: %s", name, e)
            result[name] = {"error": str(e)}

    result["_collect_count"] = _COLLECT_COUNT
    return result
