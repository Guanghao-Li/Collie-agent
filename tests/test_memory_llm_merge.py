from __future__ import annotations

import json
from typing import Any

import pytest

from bootstrap.config import MemoryConfig
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


class FakeLLMProvider:
    name = "fake-llm"

    def __init__(self, *responses: dict[str, Any] | str) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        purpose: str | None = None,
    ) -> str:
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, str):
            return response
        return json.dumps(response)

    async def close(self) -> None:
        return None


def _active_item(memory_id: str, text: str, *, kind: str = "preference") -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        type=kind,  # type: ignore[arg-type]
        text=text,
        tags=[kind],
        importance=0.5,
        confidence=0.7,
        source="test",
        source_ref=f"turn:{memory_id}",
        status="active",
    )


async def _runtime(
    tmp_path,
    config: MemoryConfig,
    provider: FakeLLMProvider | None = None,
) -> MemoryRuntime:
    runtime = MemoryRuntime(
        tmp_path,
        config,
        llm_provider=provider,
        fast_llm_provider=provider,
    )
    await runtime.initialize()
    return runtime


async def _seed_active(runtime: MemoryRuntime, *items: MemoryItem) -> None:
    runtime.store.write_index(list(items))
    runtime.engine.markdown_store.render_active_memories(list(items))  # type: ignore[attr-defined]
    for item in items:
        await runtime.engine.memory2_store.upsert_item(item)  # type: ignore[attr-defined]


def _pending_text(runtime: MemoryRuntime) -> str:
    return runtime.engine.markdown_store.pending_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]


def _merge_log_text(runtime: MemoryRuntime) -> str:
    path = tmp_path_for_runtime(runtime) / ".collie" / "memory" / "LLM_MERGE_LOG.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def tmp_path_for_runtime(runtime: MemoryRuntime):
    return runtime.workspace


@pytest.mark.asyncio
async def test_llm_merge_disabled_by_default_does_not_call_provider(tmp_path) -> None:
    provider = FakeLLMProvider({"action": "requires_review", "confidence": 1.0})
    runtime = await _runtime(tmp_path, MemoryConfig(), provider)
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User likes concise summaries.",
        source_ref="turn:new",
    )

    result = await runtime.optimize_pending()

    assert result.added == 1
    assert provider.calls == []
    assert _merge_log_text(runtime) == ""


@pytest.mark.asyncio
async def test_llm_merge_missing_provider_falls_back_and_logs(tmp_path) -> None:
    config = MemoryConfig(llm_merge_enabled=True)
    runtime = await _runtime(tmp_path, config, provider=None)
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User likes fallback rules.",
        source_ref="turn:fallback",
    )

    result = await runtime.optimize_pending()
    log_text = _merge_log_text(runtime)

    assert result.added == 1
    assert "turn:fallback" in log_text
    assert "fallback" in log_text


@pytest.mark.asyncio
async def test_llm_invalid_json_goes_requires_review_and_logs_error(tmp_path) -> None:
    provider = FakeLLMProvider("this is not json")
    runtime = await _runtime(tmp_path, MemoryConfig(llm_merge_enabled=True), provider)
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User likes invalid json tests.",
        source_ref="turn:bad-json",
    )

    result = await runtime.optimize_pending()
    pending_md = _pending_text(runtime)
    log_text = _merge_log_text(runtime)

    assert result.requires_review == 1
    assert "## Requires Review" in pending_md
    assert "llm_reason" in pending_md
    assert "invalid JSON" in log_text


@pytest.mark.asyncio
async def test_llm_reinforce_updates_existing_without_new_item(tmp_path) -> None:
    provider = FakeLLMProvider(
        {
            "action": "reinforce",
            "target_ids": ["alpha"],
            "confidence": 0.93,
            "reason": "same preference restated",
        }
    )
    runtime = await _runtime(tmp_path, MemoryConfig(llm_merge_enabled=True), provider)
    await _seed_active(runtime, _active_item("alpha", "User likes alpha examples."))
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User still likes alpha examples with details.",
        source_ref="turn:alpha-new",
    )

    result = await runtime.optimize_pending()
    items = runtime.store.read_index()

    assert result.merged == 1
    assert len(items) == 1
    assert items[0].metadata["reinforcement"] == 1
    assert "applied: true" in _merge_log_text(runtime)


@pytest.mark.asyncio
async def test_llm_merge_updates_summary_body_memory2_and_markdown(tmp_path) -> None:
    provider = FakeLLMProvider(
        {
            "action": "merge",
            "target_ids": ["alpha"],
            "new_summary": "User prefers alpha examples with concise explanations.",
            "new_body": "Keep alpha examples concise and practical.",
            "confidence": 0.91,
            "reason": "updates the same preference",
            "tags": ["examples"],
        }
    )
    runtime = await _runtime(tmp_path, MemoryConfig(llm_merge_enabled=True), provider)
    await _seed_active(runtime, _active_item("alpha", "User likes alpha examples."))
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User prefers alpha examples with concise explanations.",
        source_ref="turn:alpha-merge",
    )

    result = await runtime.optimize_pending()
    item = runtime.store.read_index()[0]
    row = await runtime.engine.memory2_store.get_item("alpha")  # type: ignore[attr-defined]
    memory_md = runtime.engine.markdown_store.memory_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]

    assert result.merged == 1
    assert item.text == "User prefers alpha examples with concise explanations."
    assert item.metadata["body"] == "Keep alpha examples concise and practical."
    assert "examples" in item.tags
    assert row is not None
    assert row["summary"] == item.text
    assert item.text in memory_md


@pytest.mark.asyncio
async def test_llm_supersede_explicit_target_is_applied(tmp_path) -> None:
    provider = FakeLLMProvider(
        {
            "action": "supersede",
            "target_ids": ["old"],
            "new_summary": "New project name is Beta.",
            "confidence": 0.95,
            "reason": "explicit replacement",
        }
    )
    runtime = await _runtime(tmp_path, MemoryConfig(llm_merge_enabled=True), provider)
    await _seed_active(runtime, _active_item("old", "Old project name is Alpha."))
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "New project name is Beta.",
        source_ref="turn:beta",
        metadata={"supersedes": ["old"]},
    )

    result = await runtime.optimize_pending()
    index = {item.id: item for item in runtime.store.read_index()}
    replacements = await runtime.engine.memory2_store.list_replacements("old")  # type: ignore[attr-defined]

    assert result.superseded == 1
    assert index["old"].status == "superseded"
    assert any(item.text == "New project name is Beta." for item in index.values())
    assert replacements
    assert "applied: true" in _merge_log_text(runtime)


@pytest.mark.asyncio
async def test_llm_suggested_supersede_without_auto_goes_review(tmp_path) -> None:
    provider = FakeLLMProvider(
        {
            "action": "supersede",
            "target_ids": ["old"],
            "new_summary": "User now wants Gamma.",
            "confidence": 0.96,
            "reason": "likely replacement",
        }
    )
    runtime = await _runtime(tmp_path, MemoryConfig(llm_merge_enabled=True), provider)
    await _seed_active(runtime, _active_item("old", "User wants Alpha."))
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User now wants Gamma.",
        source_ref="turn:gamma",
    )

    result = await runtime.optimize_pending()
    old = next(item for item in runtime.store.read_index() if item.id == "old")
    pending_md = _pending_text(runtime)

    assert result.requires_review == 1
    assert old.status == "active"
    assert "suggested_action" in pending_md
    assert "supersede" in pending_md


@pytest.mark.asyncio
async def test_sensitive_and_low_confidence_decisions_require_review(tmp_path) -> None:
    provider = FakeLLMProvider(
        {
            "action": "merge",
            "target_ids": ["health"],
            "confidence": 0.99,
            "reason": "sensitive health fact",
            "sensitive": True,
        },
        {
            "action": "merge",
            "target_ids": ["pref"],
            "confidence": 0.30,
            "reason": "weak match",
        },
    )
    runtime = await _runtime(tmp_path, MemoryConfig(llm_merge_enabled=True), provider)
    await _seed_active(
        runtime,
        _active_item("health", "User has a health preference.", kind="health_long_term"),
        _active_item("pref", "User likes blue themes."),
    )
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "health_long_term",
        "User has a medical preference about medication reminders.",
        source_ref="turn:health",
    )
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User may like blue-green themes.",
        source_ref="turn:low-confidence",
    )

    result = await runtime.optimize_pending()
    pending_md = _pending_text(runtime)

    assert result.requires_review == 2
    assert "sensitive" in pending_md
    assert "llm_confidence" in pending_md


@pytest.mark.asyncio
async def test_unknown_target_correction_and_low_confidence_skip_stay_review(tmp_path) -> None:
    provider = FakeLLMProvider(
        {
            "action": "merge",
            "target_ids": ["missing"],
            "confidence": 0.90,
            "reason": "bad target",
        },
        {
            "action": "requires_review",
            "confidence": 0.90,
            "reason": "correction has no clear target",
        },
        {
            "action": "skip",
            "confidence": 0.20,
            "reason": "maybe temporary",
        },
    )
    runtime = await _runtime(tmp_path, MemoryConfig(llm_merge_enabled=True), provider)
    await _seed_active(runtime, _active_item("pref", "User likes structured notes."))
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User likes structured notes with headings.",
        source_ref="turn:unknown-target",
    )
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "correction",
        "Do not call it the old thing.",
        source_ref="turn:correction",
    )
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "Maybe remind me once tomorrow.",
        source_ref="turn:skip",
    )

    result = await runtime.optimize_pending()
    pending_md = _pending_text(runtime)

    assert result.requires_review == 3
    assert "missing" in pending_md
    assert "turn:correction" in pending_md
    assert "skip" in pending_md


@pytest.mark.asyncio
async def test_llm_merge_audit_log_omits_api_keys(tmp_path) -> None:
    provider = FakeLLMProvider(
        {
            "action": "add",
            "new_summary": "User likes audit logs.",
            "confidence": 0.95,
            "reason": "new preference",
        }
    )
    config = MemoryConfig(
        llm_merge_enabled=True,
        memory_server_api_key="secret-api-key",
    )
    runtime = await _runtime(tmp_path, config, provider)
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User likes audit logs.",
        source_ref="turn:audit",
        metadata={"api_key": "secret-api-key"},
    )

    result = await runtime.optimize_pending()
    log_text = _merge_log_text(runtime)

    assert result.added == 1
    assert "turn:audit" in log_text
    assert "action: add" in log_text
    assert "applied: true" in log_text
    assert "secret-api-key" not in log_text
