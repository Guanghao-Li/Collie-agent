from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agent.frame import TurnFrame


ToolHookDecision = Literal["allow", "deny", "modify", "confirm"]


@dataclass(slots=True)
class ToolHookResult:
    decision: ToolHookDecision
    arguments: dict[str, Any] | None = None
    reason: str = ""


class ToolPreHook(Protocol):
    name: str
    priority: int
    tool_name: str | None

    def before_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        frame: TurnFrame | None,
    ) -> object:
        ...


@dataclass(slots=True)
class DangerousToolBlocker:
    name: str = "policy.dangerous_tool_blocker"
    priority: int = 10
    tool_name: str | None = None
    dangerous_tools: set[str] = field(
        default_factory=lambda: {"delete_memory", "send_email", "filesystem_write"}
    )

    async def before_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        frame: TurnFrame | None,
    ) -> ToolHookResult | None:
        if tool_name in self.dangerous_tools:
            return ToolHookResult(
                decision="confirm",
                reason=f"{tool_name} is a high-risk tool and requires user confirmation.",
            )
        return None
