# -*- coding: utf-8 -*-
"""监控聚合 — 收集所有模块 get_counters() 为一个 dict，供 /metrics 端点消费"""

import logging

logger = logging.getLogger(__name__)

_COLLECT_COUNT = 0


def collect() -> dict:
    """聚合所有模块的 get_counters() 为一个 dict。延迟导入避免循环依赖。"""
    global _COLLECT_COUNT
    _COLLECT_COUNT += 1

    result = {}

    modules = [
        ("state_updater", "modules.state_updater"),
        ("mood_updater", "modules.mood_updater"),
        ("state_decoder", "modules.state_decoder"),
        ("planner", "modules.planner"),
        ("tool_dispatcher", "modules.tool_dispatcher"),
        ("reply_generator", "modules.reply_generator"),
        ("composer", "modules.composer"),
        ("refiner", "modules.refiner"),
        ("sticker_picker", "tools.sticker_picker"),
        ("bubble_updater", "tools.bubble_updater"),
        ("memory_manager", "modules.memory_manager"),
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

    result["metrics_collect_count"] = _COLLECT_COUNT
    return result
