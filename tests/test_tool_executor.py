from __future__ import annotations

import json

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
from bus.event_bus import ToolCallEvent
from bus.models import InboundMessage
from tools.executor import ToolExecutor
from tools.hooks import DangerousToolBlocker, ToolHookResult
from tools.registry import ToolRegistry


class _ModifyHook:
    name = "test.modify"
    priority = 10
    tool_name = "calculator"

    async def before_tool_call(self, tool_name, arguments, frame):
        return ToolHookResult(
            decision="modify",
            arguments={"expression": "2 + 2"},
        )


class _AllowHook:
    name = "test.allow"
    priority = 10
    tool_name = "calculator"

    async def before_tool_call(self, tool_name, arguments, frame):
        return ToolHookResult(decision="allow")


class _DenyHook:
    name = "test.deny"
    priority = 10
    tool_name = "calculator"

    async def before_tool_call(self, tool_name, arguments, frame):
        return ToolHookResult(decision="deny", reason="blocked")


class _FilteredDenyHook:
    name = "test.filtered_deny"
    priority = 10
    tool_name = "other_tool"

    async def before_tool_call(self, tool_name, arguments, frame):
        return ToolHookResult(decision="deny", reason="should be skipped")


class _ConfirmHook:
    name = "test.confirm"
    priority = 10
    tool_name = "calculator"

    async def before_tool_call(self, tool_name, arguments, frame):
        return ToolHookResult(decision="confirm", reason="needs approval")


class _DeleteMemoryProvider:
    name = "delete_memory_test"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return (
                "<tool_call>"
                + json.dumps({"name": "delete_memory", "arguments": {"id": "m1"}})
                + "</tool_call>"
            )
        return "done"

    async def close(self) -> None:
        return None


def _executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(
        "calculator",
        "calc",
        {"type": "object"},
        lambda expression: eval(expression),  # noqa: S307 - test-only expression.
    )
    return ToolExecutor(registry)


@pytest.mark.asyncio
async def test_tool_executor_allows_regular_tool_call() -> None:
    executor = _executor()

    result = await executor.call_tool("calculator", {"expression": "1 + 2"})

    assert result == 3
    assert executor.last_arguments == {"expression": "1 + 2"}


@pytest.mark.asyncio
async def test_tool_executor_allow_hook_continues_tool_call() -> None:
    executor = _executor()
    executor.register_pre_hook(_AllowHook())

    result = await executor.call_tool("calculator", {"expression": "1 + 3"})

    assert result == 4
    assert executor.last_arguments == {"expression": "1 + 3"}


@pytest.mark.asyncio
async def test_tool_executor_hook_can_modify_arguments() -> None:
    executor = _executor()
    executor.register_pre_hook(_ModifyHook())

    result = await executor.call_tool("calculator", {"expression": "100 + 100"})

    assert result == 4
    assert executor.last_arguments == {"expression": "2 + 2"}


@pytest.mark.asyncio
async def test_tool_executor_hook_can_deny_tool_call() -> None:
    executor = _executor()
    executor.register_pre_hook(_DenyHook())

    result = await executor.call_tool("calculator", {"expression": "1 + 1"})

    assert result == {"error": "blocked", "denied": True}


@pytest.mark.asyncio
async def test_tool_executor_filters_hooks_by_tool_name() -> None:
    executor = _executor()
    executor.register_pre_hook(_FilteredDenyHook())

    result = await executor.call_tool("calculator", {"expression": "1 + 1"})

    assert result == 2
    assert executor.last_arguments == {"expression": "1 + 1"}


@pytest.mark.asyncio
async def test_tool_executor_hook_can_require_confirmation() -> None:
    executor = _executor()
    executor.register_pre_hook(_ConfirmHook())

    result = await executor.call_tool("calculator", {"expression": "1 + 1"})

    assert result == {
        "error": "Tool call requires confirmation.",
        "requires_confirmation": True,
        "reason": "needs approval",
    }


@pytest.mark.asyncio
async def test_tool_hook_confirm_is_recorded_in_trace_and_event(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    provider = _DeleteMemoryProvider()
    runtime.agent_loop.llm_provider = provider
    runtime.tool_executor.register_pre_hook(DangerousToolBlocker())
    events: list[ToolCallEvent] = []
    runtime.event_bus.subscribe(ToolCallEvent, lambda event: events.append(event))

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="delete it")
    )

    trace_file = tmp_path / "traces" / "agent_traces.jsonl"
    record = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[0])
    assert outbound.content == "done"
    assert events[0].tool_name == "delete_memory"
    assert events[0].arguments == {"id": "m1"}
    assert events[0].result["requires_confirmation"] is True
    assert record["steps"][1]["tool_name"] == "delete_memory"
    assert record["steps"][1]["arguments"] == {"id": "m1"}
    assert record["steps"][1]["error"] == "Tool call requires confirmation."
    await runtime.llm_provider.close()
