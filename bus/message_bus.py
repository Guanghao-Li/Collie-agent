from __future__ import annotations

import asyncio

from bus.models import InboundMessage, OutboundMessage


class MessageBus:
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, message: InboundMessage) -> None:
        await self.inbound.put(message)

    async def receive_inbound(self) -> InboundMessage:
        return await self.inbound.get()

    async def publish_outbound(self, message: OutboundMessage) -> None:
        await self.outbound.put(message)

    async def receive_outbound(self) -> OutboundMessage:
        return await self.outbound.get()

