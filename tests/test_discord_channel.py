from __future__ import annotations

import pytest

from bootstrap.config import DiscordConfig
from bus.message_bus import MessageBus
from bus.models import OutboundMessage
from channels.discord_channel import DiscordChannel


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, channel_id: str, content: str) -> None:
        self.sent.append((channel_id, content))


@pytest.mark.asyncio
async def test_discord_channel_converts_allowed_message_to_inbound() -> None:
    bus = MessageBus()
    channel = DiscordChannel(
        DiscordConfig(allowed_user_ids=["u1"], allowed_channel_ids=["c1"]),
        bus,
    )

    await channel.handle_discord_message("u1", "c1", "你好")

    inbound = await bus.receive_inbound()
    assert inbound.user_id == "u1"
    assert inbound.content == "你好"


@pytest.mark.asyncio
async def test_discord_channel_sends_outbound_to_fake_client() -> None:
    fake = FakeClient()
    bus = MessageBus()
    channel = DiscordChannel(DiscordConfig(), bus, fake_client=fake)

    await channel.dispatch_outbound(
        OutboundMessage(channel="discord", session_id="c1", content="你好")
    )

    assert fake.sent == [("c1", "你好")]
