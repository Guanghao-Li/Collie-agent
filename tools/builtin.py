from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bootstrap.config import Settings
from tools.registry import ToolRegistry
from tools.safe_calculator import safe_calculate


def register_builtin_tools(registry: ToolRegistry, config: Settings) -> None:
    def get_time() -> str:
        return datetime.now(ZoneInfo(config.app.timezone)).isoformat(timespec="seconds")

    def calculator(expression: str) -> float | int:
        return safe_calculate(expression)

    registry.register(
        "get_time",
        "返回当前配置时区下的本地时间。",
        {"type": "object", "properties": {}},
        get_time,
    )
    registry.register(
        "calculator",
        "安全计算基础数学表达式。",
        {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        calculator,
    )
