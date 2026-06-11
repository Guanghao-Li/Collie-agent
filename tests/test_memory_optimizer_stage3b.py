from __future__ import annotations

from typing import Any

import pytest

from bootstrap.config import MemoryConfig
from memory.default_engine import DefaultMemoryEngine
from memory.engine import MemoryMutation
from memory.models import MemoryItem
from memory.optimizer import MemoryOptimizer
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


def _item(
    memory_id: str,
    text: str,
    *,
    kind: str = "preference",
    metadata: dict[str, object] | None = None,
) -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        type=kind,  # type: ignore[arg-type]
        text=text,
        tags=[kind],
        importance=0.4,
        confidence=0.5,
        source="test",
        source_ref=f"turn:{memory_id}",
        metadata=metadata or {},
        status="active",
    )


@pytest.mark.asyncio
async def test_optimizer_semantic_dedup_reinforces_existing_vector_match(tmp_path) -> None:
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
    existing = _item("old", "alpha existing preference")
    engine.store.write_index([existing])
    await engine.mutate(MemoryMutation(kind="sync"))
    engine.markdown_store.append_pending_candidate(
        "preference",
        "alpha new wording",
        source_ref="turn:new",
        confidence=0.9,
        importance=0.8,
        metadata={"tags": ["new-tag"]},
    )
    optimizer = MemoryOptimizer(
        engine.store,
        engine.markdown_store,
        config=config,
        memory2_store=engine.memory2_store,
        vector_store=engine.vector_store,
    )

    result = await optimizer.optimize()

    index = engine.store.read_index()
    row = await engine.memory2_store.get_item("old")
    assert result.merged == 1
    assert result.added == 0
    assert len(index) == 1
    assert index[0].metadata["reinforcement"] == 1
    assert index[0].importance == 0.8
    assert index[0].confidence == 0.9
    assert "new-tag" in index[0].tags
    assert index[0].metadata["source_refs"] == ["turn:new", "turn:old"]
    assert row is not None
    assert row["reinforcement"] == 1


@pytest.mark.asyncio
async def test_optimizer_explicit_supersede_marks_old_and_renders_new(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    old = _item("old", "Old project name is Alpha.")
    runtime.store.write_index([old])
    runtime.engine.markdown_store.render_active_memories([old])  # type: ignore[attr-defined]
    await runtime.engine.memory2_store.upsert_item(old)  # type: ignore[attr-defined]
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "New project name is Beta.",
        source_ref="turn:new",
        metadata={"supersedes": ["old"]},
    )

    result = await runtime.optimize_pending()

    index = {item.id: item for item in runtime.store.read_index()}
    memory_md = runtime.engine.markdown_store.memory_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    replacements = await runtime.engine.memory2_store.list_replacements("old")  # type: ignore[attr-defined]
    assert result.added == 1
    assert result.superseded == 1
    assert index["old"].status == "superseded"
    new_items = [item for item in index.values() if item.id != "old"]
    assert new_items[0].status == "active"
    assert new_items[0].supersedes == ["old"]
    assert replacements
    assert "Old project name is Alpha." not in memory_md
    assert "New project name is Beta." in memory_md


@pytest.mark.asyncio
async def test_optimizer_procedure_supersede_by_procedure_key(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    old = _item(
        "old-procedure",
        "Run the old check command.",
        kind="procedure",
        metadata={"procedure_key": "checks"},
    )
    runtime.store.write_index([old])
    runtime.engine.markdown_store.render_active_memories([old])  # type: ignore[attr-defined]
    await runtime.engine.memory2_store.upsert_item(old)  # type: ignore[attr-defined]
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "procedure",
        "Run the new check command.",
        source_ref="turn:new-procedure",
        metadata={"procedure_key": "checks"},
    )

    result = await runtime.optimize_pending()

    index = {item.id: item for item in runtime.store.read_index()}
    new_items = [item for item in index.values() if item.id != "old-procedure"]
    replacements = await runtime.engine.memory2_store.list_replacements("old-procedure")  # type: ignore[attr-defined]
    assert result.added == 1
    assert result.superseded == 1
    assert index["old-procedure"].status == "superseded"
    assert new_items[0].status == "active"
    assert new_items[0].supersedes == ["old-procedure"]
    assert replacements


@pytest.mark.asyncio
async def test_optimizer_correction_without_target_stays_requires_review(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "correction",
        "This correction needs a target.",
        source_ref="turn:correction",
    )

    result = await runtime.optimize_pending()

    pending_md = runtime.engine.markdown_store.pending_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    memory_md = runtime.engine.markdown_store.memory_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert result.requires_review == 1
    assert "## Requires Review" in pending_md
    assert "This correction needs a target." in pending_md
    assert "This correction needs a target." not in memory_md
