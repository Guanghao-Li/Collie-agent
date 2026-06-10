from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.llm import LLMProvider
from memory.runtime import MemoryRuntime
from proactive.runtime import ProactiveRuntime
from session.manager import SessionManager


@dataclass(slots=True)
class DriftContext:
    memory_runtime: MemoryRuntime
    session_manager: SessionManager
    proactive_runtime: ProactiveRuntime
    llm_provider: LLMProvider
    main_llm_provider: LLMProvider
    fast_llm_provider: LLMProvider
    current_time: datetime
    last_user_activity_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DriftResult:
    task_name: str
    success: bool
    summary: str
    created_candidates: int = 0
    updated_memories: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
