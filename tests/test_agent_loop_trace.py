from __future__ import annotations

import json

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
from bus.models import InboundMessage


def _read_trace_records(tmp_path) -> list[dict[str, object]]:
    trace_file = tmp_path / "traces" / "agent_traces.jsonl"
    return [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_agent_loop_writes_trace_for_regular_message(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="你好")
    )

    records = _read_trace_records(tmp_path)
    assert outbound.content == "回声：你好"
    assert len(records) == 1
    assert records[0]["session_id"] == "c1"
    assert records[0]["finish_reason"] == "final_answer"
    assert records[0]["intent"]["intent"] == "general_chat"
    assert records[0]["prompt_message_count"] == 3
    assert records[0]["steps"][0]["type"] == "llm"
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_agent_loop_trace_records_tool_step(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()

    await runtime.agent_loop.process_message(
        InboundMessage(
            channel="discord",
            session_id="c1",
            user_id="u1",
            content="TOOL:calculator 1 + 2 * 3",
        )
    )

    record = _read_trace_records(tmp_path)[0]
    step_types = [step["type"] for step in record["steps"]]
    assert step_types == ["llm", "tool", "llm"]
    assert record["steps"][0]["has_tool_call"] is True
    assert record["steps"][1]["tool_name"] == "calculator"
    assert record["intent"]["intent"] == "tool_execution"
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_agent_loop_trace_records_max_tool_rounds(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    assert runtime.agent_loop.trace_recorder is not None
    trace = runtime.agent_loop.trace_recorder.start_trace("c1", "TOOL:calculator 1 + 1")

    response = await runtime.agent_loop._complete_with_tools(
        "c1",
        [{"role": "user", "content": "TOOL:calculator 1 + 1"}],
        max_tool_rounds=0,
        trace=trace,
    )

    assert response == "工具调用次数已达到上限。"
    assert trace is not None
    assert trace.finish_reason == "max_tool_rounds"
    assert trace.steps[0].type == "llm"
    assert trace.steps[0].has_tool_call is True
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_agent_loop_trace_includes_intent_decision(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()

    await runtime.agent_loop.process_message(
        InboundMessage(
            channel="discord",
            session_id="c1",
            user_id="u1",
            content="记住我喜欢咖啡",
        )
    )

    record = _read_trace_records(tmp_path)[0]
    assert record["intent"]["intent"] == "memory_add"
    assert record["intent"]["route"] == "memory"
    await runtime.llm_provider.close()
