from __future__ import annotations

import json

import pytest

from agent.llm import EchoProvider
from bootstrap.config import MemoryConfig
from memory.extractor import MemoryExtractor
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


class FakeProvider:
    def __init__(self, response: str = "", *, name: str = "fake", raises: bool = False) -> None:
        self.name = name
        self.model = name
        self.response = response
        self.raises = raises
        self.calls: list[str | None] = []

    async def complete(self, messages, *, temperature=None, timeout_seconds=None, purpose=None):
        self.calls.append(purpose)
        if self.raises:
            raise RuntimeError("provider failed")
        return self.response

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_memory_gate_echo_skips_short_ack(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig(), EchoProvider())
    await runtime.initialize()

    decision = await runtime.should_search_memory("谢谢", [])

    assert decision.should_search is False


@pytest.mark.asyncio
async def test_memory_gate_echo_triggers_for_memory_keywords(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig(), EchoProvider())
    await runtime.initialize()

    decision = await runtime.should_search_memory("我之前说过什么偏好吗", [])

    assert decision.should_search is True


@pytest.mark.asyncio
async def test_memory_gate_invalid_json_falls_back_to_search(tmp_path) -> None:
    runtime = MemoryRuntime(
        tmp_path,
        MemoryConfig(),
        EchoProvider(),
        FakeProvider("not json"),
    )
    await runtime.initialize()

    decision = await runtime.should_search_memory("要不要查记忆", [])

    assert decision.should_search is True
    assert decision.reason == "fallback_on_parse_error"


@pytest.mark.asyncio
async def test_query_rewrite_uses_fast_provider_text(tmp_path) -> None:
    fast = FakeProvider("改写后的记忆查询")
    runtime = MemoryRuntime(tmp_path, MemoryConfig(), EchoProvider(), fast)
    await runtime.initialize()

    query = await runtime.rewrite_memory_query("那个项目怎么样了", [])

    assert query == "改写后的记忆查询"
    assert fast.calls == ["memory_query_rewrite"]


@pytest.mark.asyncio
async def test_hyde_disabled_does_not_call_fast_provider(tmp_path) -> None:
    config = MemoryConfig()
    config.enable_hyde = False
    fast = FakeProvider("HyDE 文档")
    runtime = MemoryRuntime(tmp_path, config, EchoProvider(), fast)
    await runtime.initialize()

    result = await runtime.generate_hyde_document("查询", [])

    assert result is None
    assert fast.calls == []


@pytest.mark.asyncio
async def test_hyde_text_is_used_in_search_with_trace(tmp_path) -> None:
    config = MemoryConfig()
    config.enable_hyde = True
    fast = FakeProvider("hyde-only-keyword")
    runtime = MemoryRuntime(tmp_path, config, EchoProvider(), fast)
    await runtime.initialize()
    item = MemoryItem(type="fact", text="hyde-only-keyword", status="active")
    runtime.store.write_index([item])
    runtime.engine.markdown_store.render_active_memories([item])  # type: ignore[attr-defined]

    result = await runtime.search_with_trace("完全不匹配", [])

    assert result.memories
    assert result.trace.hyde_document == "hyde-only-keyword"


@pytest.mark.asyncio
async def test_hyde_failure_does_not_break_search(tmp_path) -> None:
    fast = FakeProvider(raises=True)
    runtime = MemoryRuntime(tmp_path, MemoryConfig(), EchoProvider(), fast)
    await runtime.initialize()

    result = await runtime.search_with_trace("项目", [])

    assert result.trace.hyde_document is None


@pytest.mark.asyncio
async def test_memory_extractor_prefers_fast_provider() -> None:
    payload = json.dumps(
        [{"type": "preference", "text": "用户喜欢简洁回答", "tags": ["style"]}],
        ensure_ascii=False,
    )
    fast = FakeProvider(payload)
    extractor = MemoryExtractor(main_llm_provider=EchoProvider(), fast_llm_provider=fast)

    items = await extractor.extract("s1", "请记住我喜欢简洁回答", "好的")

    assert items[0].text == "用户喜欢简洁回答"
    assert fast.calls == ["memory_extraction_fast"]


@pytest.mark.asyncio
async def test_memory_extractor_falls_back_to_main_provider() -> None:
    payload = json.dumps(
        [{"type": "fact", "text": "用户使用 Python", "tags": ["dev"]}],
        ensure_ascii=False,
    )
    fast = FakeProvider(raises=True)
    main = FakeProvider(payload, name="main")
    extractor = MemoryExtractor(main_llm_provider=main, fast_llm_provider=fast)

    items = await extractor.extract("s1", "请记住我使用 Python", "好的")

    assert items[0].text == "用户使用 Python"
    assert fast.calls == ["memory_extraction_fast"]
    assert main.calls == ["memory_extraction_main"]


@pytest.mark.asyncio
async def test_memory_extractor_returns_empty_when_all_providers_fail() -> None:
    extractor = MemoryExtractor(
        main_llm_provider=FakeProvider(raises=True),
        fast_llm_provider=FakeProvider(raises=True),
    )

    assert await extractor.extract("s1", "请记住 x", "好的") == []


@pytest.mark.asyncio
async def test_rule_extractor_outputs_requested_memory_or_preference() -> None:
    extractor = MemoryExtractor()

    batch = await extractor.extract_batch(
        "s1",
        "记住，我以后希望你解释代码时讲得详细一点。",
        "好的。",
    )

    assert batch.pending_items
    assert batch.pending_items[0].tag in {"preference", "requested_memory"}
    assert "详细" in batch.pending_items[0].content


@pytest.mark.asyncio
async def test_rule_extractor_drops_temporary_tasks() -> None:
    extractor = MemoryExtractor()

    batch = await extractor.extract_batch("s1", "今天帮我查一下天气。", "好的。")

    assert batch.pending_items == []
    assert batch.history_entries == []


@pytest.mark.asyncio
async def test_rule_extractor_marks_corrections_or_procedures() -> None:
    extractor = MemoryExtractor()

    batch = await extractor.extract_batch(
        "s1",
        "刚才那个流程不对，以后不要再那样做。",
        "明白。",
    )

    assert batch.pending_items
    assert batch.pending_items[0].tag in {"correction", "procedure"}
