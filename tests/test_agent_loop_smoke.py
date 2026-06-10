from __future__ import annotations

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
from bus.models import InboundMessage


@pytest.mark.asyncio
async def test_agent_loop_completes_echo_turn(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="你好")
    )

    assert outbound.content == "回声：你好"
    assert runtime.session_manager.get_messages("c1")[-1].content == "回声：你好"
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_agent_loop_parses_tool_call_protocol(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(
            channel="discord",
            session_id="c1",
            user_id="u1",
            content="TOOL:calculator 1 + 2 * 3",
        )
    )

    assert outbound.content == "工具结果：7"
    await runtime.llm_provider.close()
