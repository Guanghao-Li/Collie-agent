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
