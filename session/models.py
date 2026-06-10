from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SessionRole = Literal["user", "assistant", "tool", "system"]


@dataclass(slots=True)
class SessionMessage:
    role: SessionRole
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMessage":
        created_at = data.get("created_at")
        parsed_at = (
            datetime.fromisoformat(created_at)
            if isinstance(created_at, str)
            else datetime.now(timezone.utc)
        )
        return cls(
            role=data["role"],
            content=str(data.get("content", "")),
            created_at=parsed_at,
            metadata=dict(data.get("metadata", {})),
        )
