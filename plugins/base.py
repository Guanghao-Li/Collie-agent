from __future__ import annotations

from typing import Protocol

from plugins.context import PluginContext


class Plugin(Protocol):
    name: str

    async def setup(self, context: PluginContext) -> None:
        ...

