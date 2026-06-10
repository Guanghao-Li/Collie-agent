from __future__ import annotations

import pytest

from bus.message_bus import MessageBus
from bus.models import InboundMessage, OutboundMessage


@pytest.mark.asyncio
async def test_message_bus_inbound_and_outbound() -> None:
    bus = MessageBus()
    inbound = InboundMessage(channel="discord", session_id="c1", user_id="u1", content="hi")
    outbound = OutboundMessage(channel="discord", session_id="c1", content="你好")

    await bus.publish_inbound(inbound)
    await bus.publish_outbound(outbound)

    assert await bus.receive_inbound() == inbound
    assert await bus.receive_outbound() == outbound
