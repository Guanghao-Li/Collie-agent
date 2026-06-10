from __future__ import annotations

from bootstrap.config import Settings
from tools.builtin import register_builtin_tools
from tools.registry import ToolRegistry


def create_tool_registry(config: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    register_builtin_tools(registry, config)
    return registry

