from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bootstrap.config import MemoryConfig
from memory.consolidator import MemoryConsolidator
from memory.engine import MemoryMutation
from memory.markdown_store import MarkdownMemoryStore
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


def test_legacy_mode_is_removed_from_config_and_example() -> None:
    assert not hasattr(MemoryConfig(), "consolidation_mode")
    assert not hasattr(MemoryConsolidator, "_consolidate_legacy")
    assert "consolidation_mode" not in Path("config.example.toml").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_markdown_store_appends_history_pending_and_preserves_recent_context(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    await store.initialize()

    assert store.append_history_entry(
        "用户希望代码解释更详细。",
        source_ref="turn:abc#history:1",
        emotional_weight=3,
    )
    assert not store.append_history_entry(
        "用户希望代码解释更详细。",
        source_ref="turn:abc#history:1",
        emotional_weight=3,
    )
    assert store.append_pending_candidate(
        "preference",
        "用户希望解释代码时讲得详细一点。",
        source_ref="turn:abc#pending:1",
    )
    assert not store.append_pending_candidate(
        "preference",
        "用户希望解释代码时讲得详细一点。",
        source_ref="turn:abc#pending:2",
    )
    store.update_recent_context_sections(recent_turns="[user] hello")

    history_md = store.history_md.read_text(encoding="utf-8")
    pending_md = store.pending_md.read_text(encoding="utf-8")
    recent_context_md = store.recent_context_md.read_text(encoding="utf-8")

    assert history_md.count("source_ref: turn:abc#history:1") == 1
    assert pending_md.count("用户希望解释代码时讲得详细一点。") == 1
    assert "## Compression" in recent_context_md
    assert "## Ongoing Threads" in recent_context_md
    assert "## Recent Turns" in recent_context_md


def test_pending_parser_reads_metadata_without_comment_content(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    store.write_text(
        store.pending_md,
        (
            "# Pending\n\n"
            "- [preference] 用户希望解释代码更详细。 "
            "<!-- source_ref: turn:abc confidence: 0.80 importance: 0.75 -->\n"
        ),
    )

    [candidate] = store.parse_pending_candidates()

    assert candidate["tag"] == "preference"
    assert candidate["content"] == "用户希望解释代码更详细。"
    assert candidate["source_ref"] == "turn:abc"
    assert candidate["confidence"] == 0.8
    assert candidate["importance"] == 0.75
    assert candidate["correction"] is False
    assert candidate["requires_review"] is False
    assert candidate["metadata"] == {}


def test_pending_parser_adds_fallback_source_and_reads_metadata_json(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    store.write_text(
        store.pending_md,
        (
            "# Pending\n\n"
            "- [procedure] Use the project test helper. "
            "<!-- confidence: 0.70 metadata_json: {\"priority\":\"stable\",\"created_at\":\"2026-01-02T03:04:05+00:00\"} -->\n"
        ),
    )

    [candidate] = store.parse_pending_candidates()

    assert candidate["tag"] == "procedure"
    assert candidate["content"] == "Use the project test helper."
    assert str(candidate["source_ref"]).startswith("pending:")
    assert candidate["confidence"] == 0.7
    assert candidate["metadata"] == {
        "priority": "stable",
        "created_at": "2026-01-02T03:04:05+00:00",
    }


@pytest.mark.asyncio
async def test_consolidation_writes_aka_like_buffers_without_refreshing_memory(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.append_pending_memory(
        MemoryItem(
            type="event",
            text="用户要求以后代码解释更详细。",
            tags=["history_entry"],
            source_ref="turn:abc#history:1",
            metadata={"batch_source_ref": "turn:abc"},
            status="pending",
        )
    )
    await runtime.append_pending_memory(
        MemoryItem(
            type="preference",
            text="用户希望解释代码时讲得详细一点。",
            tags=["preference"],
            source_ref="turn:abc#pending:1",
            metadata={"batch_source_ref": "turn:abc", "tag": "preference"},
            status="pending",
        )
    )
    memory_before = runtime.engine.markdown_store.memory_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]

    result = await runtime.consolidate()

    memory_after = runtime.engine.markdown_store.memory_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    history_md = runtime.engine.markdown_store.history_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    pending_md = runtime.engine.markdown_store.pending_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]

    assert result.added == 2
    assert memory_after == memory_before
    assert "用户要求以后代码解释更详细。" in history_md
    assert "source_ref: turn:abc#history:1" in history_md
    assert "- [preference] 用户希望解释代码时讲得详细一点。" in pending_md
    assert runtime.store.read_pending() == []

    await runtime.append_pending_memory(
        MemoryItem(
            type="preference",
            text="用户希望解释代码时讲得详细一点。",
            tags=["preference"],
            source_ref="turn:abc#pending:1",
            metadata={"batch_source_ref": "turn:abc", "tag": "preference"},
            status="pending",
        )
    )

    second = await runtime.consolidate()
    pending_again = runtime.engine.markdown_store.pending_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]

    assert second.discarded == 1
    assert pending_again.count("用户希望解释代码时讲得详细一点。") == 1


@pytest.mark.asyncio
async def test_optimizer_converts_pending_to_active_and_renders_markdown(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    store.append_pending_candidate(
        "preference",
        "用户希望解释代码时讲得详细一点。",
        source_ref="turn:abc",
        confidence=0.8,
        importance=0.75,
    )
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    runtime.optimizer.now_fn = lambda: fixed_now

    result = await runtime.optimize_pending()

    index_data = json.loads(runtime.store.index_json.read_text(encoding="utf-8"))
    memory_md = store.memory_md.read_text(encoding="utf-8")
    self_md = store.self_md.read_text(encoding="utf-8")
    pending_md = store.pending_md.read_text(encoding="utf-8")

    assert result.added == 1
    assert result.ok is True
    assert index_data[0]["status"] == "active"
    assert index_data[0]["type"] == "preference"
    assert index_data[0]["source_ref"] == "turn:abc"
    assert index_data[0]["created_at"] == fixed_now.isoformat()
    assert "用户希望解释代码时讲得详细一点。" in memory_md
    assert "用户希望解释代码时讲得详细一点。" in self_md
    assert "- [preference] 用户希望解释代码时讲得详细一点。" not in pending_md
    assert "- archived [preference] 用户希望解释代码时讲得详细一点。" in pending_md


@pytest.mark.asyncio
async def test_optimizer_dry_run_does_not_consume_pending(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    store.append_pending_candidate("preference", "Dry run candidate.", source_ref="turn:dry")
    before = store.pending_md.read_text(encoding="utf-8")

    result = await runtime.optimize_pending(dry_run=True)

    assert result.ok is True
    assert result.added == 1
    assert runtime.store.read_index() == []
    assert store.pending_md.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_optimizer_auto_run_is_explicitly_configured(tmp_path) -> None:
    config = MemoryConfig(optimizer_auto_run=True)
    runtime = MemoryRuntime(tmp_path, config)
    await runtime.initialize()
    await runtime.append_pending_memory(
        MemoryItem(
            type="preference",
            text="Auto-run candidate.",
            source_ref="turn:auto#pending:1",
            metadata={"batch_source_ref": "turn:auto", "tag": "preference"},
        )
    )

    await runtime.consolidate()

    assert runtime.store.read_index()[0].text == "Auto-run candidate."
    assert "Auto-run candidate." in await runtime.read_core_memory()


@pytest.mark.asyncio
async def test_optimizer_merges_duplicate_normalized_text(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    existing = MemoryItem(
        type="preference",
        text="User likes detailed code explanations",
        tags=["style"],
        importance=0.4,
        confidence=0.4,
        source_ref="turn:old",
    )
    runtime.store.write_index([existing])
    store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    store.append_pending_candidate(
        "preference",
        "  user   likes detailed code explanations  ",
        source_ref="turn:new",
        confidence=0.9,
        importance=0.8,
    )

    result = await runtime.optimize_pending()

    index = runtime.store.read_index()
    assert result.merged == 1
    assert len(index) == 1
    assert index[0].importance == 0.8
    assert index[0].confidence == 0.9
    assert index[0].metadata["source_refs"] == ["turn:new", "turn:old"]


@pytest.mark.asyncio
async def test_correction_requires_review_and_does_not_become_active(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    store.append_pending_candidate(
        "correction",
        "以后不要把这个项目叫成旧名字。",
        source_ref="turn:correction",
        confidence=0.9,
        importance=0.8,
    )

    result = await runtime.optimize_pending()

    memory_md = store.memory_md.read_text(encoding="utf-8")
    pending_md = store.pending_md.read_text(encoding="utf-8")
    assert result.requires_review == 1
    assert "以后不要把这个项目叫成旧名字。" not in memory_md
    assert "## Requires Review" in pending_md
    assert "- [correction] 以后不要把这个项目叫成旧名字。" in pending_md


@pytest.mark.asyncio
async def test_remember_stable_waits_for_optimizer_before_active_write(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()

    result = await runtime.engine.mutate(
        MemoryMutation(
            kind="remember",
            summary="User wants code explanations to include examples",
            memory_kind="preference",
            source_ref="turn:remember",
            stable=True,
        )
    )

    memory_before = runtime.engine.markdown_store.memory_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert result.ok is True
    assert runtime.store.read_index() == []
    assert "User wants code explanations to include examples" not in memory_before
    assert "User wants code explanations to include examples" in runtime.engine.markdown_store.pending_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]

    optimized = await runtime.optimize_pending()

    assert optimized.added == 1
    assert runtime.store.read_index()[0].text == "User wants code explanations to include examples"
    assert "User wants code explanations to include examples" in await runtime.read_core_memory()


@pytest.mark.asyncio
async def test_sync_does_not_overwrite_pending_md(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    pending_text = (
        "# Pending\n\n"
        "- [preference] xxx <!-- source_ref: turn:abc -->\n"
    )
    runtime.engine.markdown_store.write_text(  # type: ignore[attr-defined]
        runtime.engine.markdown_store.pending_md,  # type: ignore[attr-defined]
        pending_text,
    )

    await runtime.engine.mutate(MemoryMutation(kind="sync"))

    assert runtime.engine.markdown_store.pending_md.read_text(encoding="utf-8") == pending_text  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_optimizer_restores_pending_snapshot_on_failure(tmp_path, monkeypatch) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    store.append_pending_candidate("preference", "Keep this pending.", source_ref="turn:abc")
    original = store.pending_md.read_text(encoding="utf-8")

    def fail_rewrite(*args, **kwargs) -> None:
        store.write_text(store.pending_md, "BROKEN\n")
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "rewrite_pending_candidates", fail_rewrite)

    result = await runtime.optimize_pending()

    assert result.ok is False
    assert result.errors == ["boom"]
    assert store.pending_md.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_consolidation_restores_pending_snapshot_on_failure(tmp_path, monkeypatch) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    store.append_pending_candidate("preference", "Existing candidate.", source_ref="turn:old")
    original = store.pending_md.read_text(encoding="utf-8")
    await runtime.append_pending_memory(
        MemoryItem(
            type="preference",
            text="New candidate that fails.",
            source_ref="turn:new#pending:1",
            metadata={"batch_source_ref": "turn:new", "tag": "preference"},
        )
    )

    def fail_append(*args, **kwargs) -> bool:
        store.write_text(store.pending_md, "BROKEN\n")
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "append_pending_candidate", fail_append)

    result = await runtime.consolidate()

    assert result.conflicts == 1
    assert store.pending_md.read_text(encoding="utf-8") == original
    assert runtime.store.read_pending()
