from __future__ import annotations

import pytest
import httpx

from memory.embedder import (
    DeterministicFakeEmbedder,
    DisabledEmbedder,
    EmbeddingError,
    OpenAICompatibleEmbedder,
)


@pytest.mark.asyncio
async def test_disabled_embedder_describes_and_raises() -> None:
    embedder = DisabledEmbedder(reason="missing config", requested=True)

    description = await embedder.describe()

    assert description["enabled"] is False
    assert description["requested"] is True
    assert description["reason"] == "missing config"
    with pytest.raises(EmbeddingError, match="missing config"):
        await embedder.embed_texts(["hello"])


@pytest.mark.asyncio
async def test_openai_compatible_embedder_parses_mocked_embeddings() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [1.0, 0.0, 0.5]},
                    {"embedding": [0.0, 1.0, 0.25]},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        embedder = OpenAICompatibleEmbedder(
            model="test-embedding",
            api_key="test-secret",
            base_url="https://example.com/v1/",
            client=client,
        )

        embeddings = await embedder.embed_texts(["one", "two"])
    finally:
        await client.aclose()

    assert seen["url"] == "https://example.com/v1/embeddings"
    assert seen["authorization"] == "Bearer test-secret"
    assert embeddings == [[1.0, 0.0, 0.5], [0.0, 1.0, 0.25]]


@pytest.mark.asyncio
async def test_openai_compatible_embedder_redacts_api_key_from_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key test-secret")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        embedder = OpenAICompatibleEmbedder(
            model="test-embedding",
            api_key="test-secret",
            base_url="https://example.com/v1",
            client=client,
        )

        with pytest.raises(EmbeddingError) as error:
            await embedder.embed_texts(["one"])
    finally:
        await client.aclose()

    assert "test-secret" not in str(error.value)
    assert "[redacted]" in str(error.value)


@pytest.mark.asyncio
async def test_openai_compatible_embedder_rejects_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 2.0]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        embedder = OpenAICompatibleEmbedder(
            model="test-embedding",
            api_key="test-secret",
            base_url="https://example.com/v1",
            dimension=3,
            client=client,
        )

        with pytest.raises(EmbeddingError, match="dimension mismatch"):
            await embedder.embed_texts(["one"])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_deterministic_fake_embedder_is_stable() -> None:
    embedder = DeterministicFakeEmbedder(dimension=6)

    first = await embedder.embed_texts(["same", "different"])
    second = await embedder.embed_texts(["same"])

    assert first[0] == second[0]
    assert first[0] != first[1]
    assert len(first[0]) == 6
