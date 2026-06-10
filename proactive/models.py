from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class ProactiveCandidate:
    source: str
    title: str
    content: str
    url: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProactiveDecision:
    candidate: ProactiveCandidate
    should_push: bool
    score: float
    reason: str
    message: str


@dataclass(slots=True)
class ProactivePrefilterDecision:
    relevant: bool
    rough_score: float
    reason: str
