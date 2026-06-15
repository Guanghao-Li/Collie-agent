from __future__ import annotations

import pytest

from tools.registry import ToolNotFoundError, ToolRegistry
from tools.safe_calculator import CalculatorError, safe_calculate


@pytest.mark.asyncio
async def test_tool_registry_registers_and_calls_async_tool() -> None:
    registry = ToolRegistry()

    async def add_one(value: int) -> int:
        return value + 1

    registry.register("add_one", "Add one.", {"type": "object"}, add_one)

    assert await registry.call_tool("add_one", {"value": 2}) == 3
    tool = registry.list_tools()[0]
    assert tool.risk == "read_only"
    assert tool.source_type == "builtin"
    assert tool.source_name == ""
    assert tool.always_on is True
    assert tool.search_hint == ""
    assert tool.requires_confirmation is False


def test_tool_registry_registers_tool_metadata() -> None:
    registry = ToolRegistry()

    registry.register(
        "write_file",
        "Write a file.",
        {"type": "object"},
        lambda path, content: None,
        risk="write",
        source_type="plugin",
        source_name="files",
        always_on=False,
        search_hint="file writing",
        requires_confirmation=True,
    )

    tool = registry.list_tools()[0]
    assert tool.risk == "write"
    assert tool.source_type == "plugin"
    assert tool.source_name == "files"
    assert tool.always_on is False
    assert tool.search_hint == "file writing"
    assert tool.requires_confirmation is True
    assert registry.list_tools(include_deferred=False) == []


def test_tool_registry_prompt_renders_only_always_on_tools_without_format_change() -> None:
    registry = ToolRegistry()

    registry.register("visible", "Visible tool.", {"type": "object"}, lambda: None)
    registry.register(
        "deferred",
        "Deferred tool.",
        {"type": "object"},
        lambda: None,
        always_on=False,
    )

    assert registry.render_tools_for_prompt() == "- visible: Visible tool.; schema={\"type\": \"object\"}"


@pytest.mark.asyncio
async def test_tool_registry_unknown_tool_error_is_clear() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError, match="未知工具"):
        await registry.call_tool("missing", {})


def test_safe_calculator_allows_basic_math() -> None:
    assert safe_calculate("1 + 2 * 3") == 7


@pytest.mark.parametrize(
    "expression",
    ['__import__("os").system("rm -rf /")', 'open("file")'],
)
def test_safe_calculator_rejects_calls(expression: str) -> None:
    with pytest.raises(CalculatorError):
        safe_calculate(expression)
