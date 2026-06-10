from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from bootstrap.config import DiscordConfig
from bus.message_bus import MessageBus
from bus.models import InboundMessage, OutboundMessage

try:
    import discord
except ImportError:  # pragma: no cover - 未安装 discord.py 时才会走到这里。
    discord = None  # type: ignore[assignment]


class DiscordChannel:
    def __init__(
        self,
        config: DiscordConfig,
        message_bus: MessageBus,
        fake_client: Any | None = None,
    ) -> None:
        self.config = config
        self.message_bus = message_bus
        self.fake_client = fake_client
        self.sent_messages: list[tuple[str, str]] = []
        self._outbound_task: asyncio.Task[None] | None = None
        self._discord_task: asyncio.Task[None] | None = None
        self._client: Any | None = None
        self._running = False
        self._logger = logging.getLogger(__name__)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop(), name="discord-outbound")
        if not self.config.enabled:
            return
        if not self.config.allowed_user_ids:
            self._logger.warning("discord.allowed_user_ids 为空；bot 可能会响应较大范围的用户。")
        if not self.config.bot_token:
            self._logger.warning("Discord 已启用但 bot_token 为空；跳过真实 Discord 连接。")
            return
        if discord is None:
            self._logger.warning("未安装 discord.py；跳过真实 Discord 连接。")
            return
        self._client = self._build_discord_client()
        self._discord_task = asyncio.create_task(
            self._client.start(self.config.bot_token),
            name="discord-client",
        )

    async def stop(self) -> None:
        self._running = False
        if self._discord_task:
            self._discord_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._discord_task
            self._discord_task = None
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            self._client = None
        if self._outbound_task:
            self._outbound_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._outbound_task
            self._outbound_task = None

    async def handle_discord_message(
        self,
        author_id: str,
        channel_id: str,
        content: str,
        is_bot: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> InboundMessage | None:
        if is_bot or not self._is_allowed(author_id, channel_id):
            return None
        message = InboundMessage(
            channel="discord",
            session_id=channel_id,
            user_id=author_id,
            content=content,
            metadata=metadata or {"channel_id": channel_id},
        )
        await self.message_bus.publish_inbound(message)
        return message

    async def dispatch_outbound(self, message: OutboundMessage) -> None:
        channel_id = str(
            message.metadata.get("channel_id")
            or message.session_id
            or self.config.default_push_channel_id
        )
        for chunk in split_discord_message(message.content):
            await self._send(channel_id, chunk)

    async def _outbound_loop(self) -> None:
        while self._running:
            message = await self.message_bus.receive_outbound()
            if message.channel != "discord":
                continue
            await self.dispatch_outbound(message)

    def _is_allowed(self, author_id: str, channel_id: str) -> bool:
        if self.config.allowed_user_ids and str(author_id) not in self.config.allowed_user_ids:
            return False
        if self.config.allowed_channel_ids and str(channel_id) not in self.config.allowed_channel_ids:
            return False
        return True

    async def _send(self, channel_id: str, content: str) -> None:
        if self.fake_client is not None:
            result = self.fake_client.send(channel_id, content)
            if asyncio.iscoroutine(result):
                await result
            self.sent_messages.append((channel_id, content))
            return
        if self._client is not None:
            channel = self._client.get_channel(int(channel_id))
            if channel is not None:
                await channel.send(content)
                return
        self.sent_messages.append((channel_id, content))

    def _build_discord_client(self) -> Any:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_message(message: Any) -> None:
            await self.handle_discord_message(
                author_id=str(message.author.id),
                channel_id=str(message.channel.id),
                content=str(message.content),
                is_bot=bool(message.author.bot),
                metadata={"guild_id": str(message.guild.id) if message.guild else ""},
            )

        return client


def split_discord_message(content: str, limit: int = 1900) -> list[str]:
    if not content:
        return [""]
    chunks: list[str] = []
    remaining = content
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    chunks.append(remaining)
    return chunks
