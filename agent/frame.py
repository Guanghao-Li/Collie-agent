from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.intent import IntentDecision
from agent.trace import AgentTrace
from bus.models import InboundMessage, OutboundMessage
from session.models import SessionMessage


@dataclass(slots=True)
class TurnFrame:
    inbound: InboundMessage
    content: str
    session_id: str
    channel: str
    user_id: str
    abort: bool = False
    abort_reply: str | None = None
    abort_reason: str | None = None
    intent: IntentDecision | None = None
    recent: list[SessionMessage] = field(default_factory=list)
    memory_context: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    response: str = ""
    outbound: OutboundMessage | None = None
    trace: AgentTrace | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_inbound(cls, inbound: InboundMessage) -> TurnFrame:
        return cls(
            inbound=inbound,
            content=inbound.content.strip(),
            session_id=inbound.session_id,
            channel=inbound.channel,
            user_id=inbound.user_id,
            metadata=dict(inbound.metadata),
        )
