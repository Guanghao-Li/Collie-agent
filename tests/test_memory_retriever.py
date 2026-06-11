from __future__ import annotations

from typing import Any

import pytest

from bootstrap.config import MemoryConfig
from memory.default_engine import DefaultMemoryEngine
from memory.engine import MemoryMutation, MemoryQuery
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


class DeterministicFakeEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def describe(self) -> dict[str, Any]:
        return {"enabled": True, "backend": "deterministic-fake"}

    def _embed(self, text: str) -> list[float]:
        lowered = text.lower()
        if "alpha" in lowered:
            return [1.0, 0.0]
        if "beta" in lowered:
            return [0.0, 1.0]
        return [0.5, 0.5]


def _active_item(memory_id: str, text: str, *, kind: str = "preference") -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        type=kind,  # type: ignore[arg-type]
        text=text,
        tags=[kind],
        importance=0.7,
        confidence=0.8,
        source="test",
        source_ref=f"turn:{memory_id}",
        status="active",
    )


def _force_active(runtime: MemoryRuntime, *items: MemoryItem) -> None:
    runtime.store.write_index(list(items))
    runtime.engine.markdown_store.render_active_memories(list(items))  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_retriever_keyword_only_does_not_inject_pending_md(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig(enable_vector_memory=False))
    await runtime.initialize()
    _force_active(
        runtime,
        _active_item("pref", "User prefers Rust examples."),
    )
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "Do not inject this pending candidate.",
        source_ref="turn:pending",
    )

    result = await runtime.engine.query(
        MemoryQuery(intent="context", text="Rust examples", limit=5)
    )

    assert result.records
    assert result.metadata["retrieval_mode"] == "keyword_only"
    assert result.metadata["vector_disabled"] is True
    assert "User prefers Rust examples." in result.content
    assert "Do not inject this pending candidate." not in result.content


@pytest.mark.asyncio
async def test_retriever_vector_lane_and_rrf_merge_same_id(tmp_path) -> None:
    config = MemoryConfig(
        enable_vector_memory=True,
        vector_score_threshold=0.0,
        semantic_dedup_threshold=0.8,
    )
    engine = DefaultMemoryEngine(
        tmp_path / "memory",
        config=config,
        embedder=DeterministicFakeEmbedder(),
    )
    await engine.initialize()
    engine.store.write_index([
        _active_item("alpha", "alpha memory about code style."),
    ])
    await engine.mutate(MemoryMutation(kind="sync"))

    result = await engine.query(MemoryQuery(intent="answer", text="alpha code", limit=5))

    assert [record.id for record in result.records] == ["alpha"]
    assert result.metadata["search_backend"] == "vector"
    signals = result.records[0].signals
    assert signals["vector_score"] > 0
    assert signals["rrf_score"] > 0
    assert set(signals["lane_sources"]) == {"vector", "keyword"}


@pytest.mark.asyncio
async def test_retriever_procedure_boost_prioritizes_context_injection(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig(enable_vector_memory=False))
    await runtime.initialize()
    procedure = _active_item(
        "procedure",
        "Always run pytest before final status.",
        kind="procedure",
    )
    preference = _active_item(
        "preference",
        "User likes pytest tips in explanations.",
        kind="preference",
    )
    _force_active(runtime, preference, procedure)

    result = await runtime.engine.query(
        MemoryQuery(intent="context", text="pytest", limit=5)
    )

    assert result.content.index("## Procedures") < result.content.index("## Preferences")
    procedure_record = next(record for record in result.records if record.id == "procedure")
    assert procedure_record.injected is True
    assert "procedure" in procedure_record.signals["lane_sources"]


@pytest.mark.asyncio
async def test_retriever_injection_budget_caps_text_block(tmp_path) -> None:
    config = MemoryConfig(enable_vector_memory=False, memory_injection_budget_chars=90)
    runtime = MemoryRuntime(tmp_path, config)
    await runtime.initialize()
    _force_active(
        runtime,
        _active_item("a", "memory alpha " + "x" * 100),
        _active_item("b", "memory beta " + "y" * 100),
    )

    result = await runtime.engine.retriever.build_injection(  # type: ignore[attr-defined]
        MemoryQuery(intent="context", text="memory", limit=5)
    )

    assert len(result.content) <= 90
    assert result.content == result.text_block
    assert any(record.injected for record in result.records)


@pytest.mark.asyncio
async def test_retriever_timeline_intent_reads_event_memory(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig(enable_vector_memory=False))
    await runtime.initialize()
    _force_active(
        runtime,
        _active_item("event", "Alpha project kickoff happened.", kind="event"),
    )

    result = await runtime.engine.query(
        MemoryQuery(intent="timeline", text="Alpha project", limit=5)
    )

    assert result.records
    assert result.records[0].kind == "event"
    assert "Alpha project kickoff happened." in result.content
