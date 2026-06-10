from __future__ import annotations

from bootstrap.config import Settings
from bus.message_bus import MessageBus
from channels.discord_channel import DiscordChannel


def create_discord_channel(config: Settings, message_bus: MessageBus) -> DiscordChannel:
    return DiscordChannel(config.discord, message_bus)

