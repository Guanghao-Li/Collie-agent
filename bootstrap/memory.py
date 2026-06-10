from __future__ import annotations

from pathlib import Path

from agent.llm import LLMProvider
from bootstrap.config import Settings
from memory.runtime import MemoryRuntime


def create_memory_runtime(
    workspace: str | Path,
    config: Settings,
    llm_provider: LLMProvider,
    fast_llm_provider: LLMProvider | None = None,
) -> MemoryRuntime:
    return MemoryRuntime(workspace, config.memory, llm_provider, fast_llm_provider)
