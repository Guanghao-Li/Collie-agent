from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Protocol

import httpx


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend cannot produce valid embeddings."""


class Embedder(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    async def describe(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class DisabledEmbedder:
    reason: str = "embedding backend disabled"
    requested: bool = False

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError(f"embedding backend disabled: {self.reason}")

    async def describe(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "requested": self.requested,
            "backend": "disabled",
            "reason": self.reason,
        }


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 20.0,
        dimension: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model.strip()
        self.api_key = api_key.strip()
        self.base_url = base_url.strip().rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.dimension = dimension
        self._client = client

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.model:
            raise EmbeddingError("embedding model is required")
        if not self.api_key:
            raise EmbeddingError("embedding API key is required")
        if not self.base_url:
            raise EmbeddingError("embedding base_url is required")
        if not texts:
            return []

        payload = {"model": self.model, "input": [str(text) for text in texts]}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/embeddings"

        try:
            if self._client is not None:
                response = await self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = _safe_response_snippet(exc.response, redactions=[self.api_key])
            raise EmbeddingError(
                f"embedding request failed with HTTP {status}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise EmbeddingError("embedding response was not valid JSON") from exc

        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise EmbeddingError("embedding response missing data list")
        if len(rows) != len(texts):
            raise EmbeddingError(
                f"embedding response count mismatch: expected {len(texts)}, got {len(rows)}"
            )

        embeddings: list[list[float]] = []
        expected_dimension = self.dimension
        for index, row in enumerate(rows):
            embedding = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(embedding, list) or not embedding:
                raise EmbeddingError(f"embedding response item {index} missing embedding")
            vector = [_coerce_float(value, index=index) for value in embedding]
            if expected_dimension is None:
                expected_dimension = len(vector)
            elif len(vector) != expected_dimension:
                raise EmbeddingError(
                    f"embedding dimension mismatch: expected {expected_dimension}, "
                    f"got {len(vector)} at item {index}"
                )
            embeddings.append(vector)
        return embeddings

    async def describe(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.model and self.api_key and self.base_url),
            "requested": True,
            "backend": "openai-compatible",
            "model": self.model,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "dimension": self.dimension,
        }


class DeterministicFakeEmbedder:
    def __init__(self, *, dimension: int = 8) -> None:
        if dimension <= 0:
            raise ValueError("fake embedding dimension must be positive")
        self.dimension = dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    async def describe(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "requested": True,
            "backend": "deterministic-fake",
            "dimension": self.dimension,
        }

    def _embed_one(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(f"{text}\0{counter}".encode("utf-8")).digest()
            for byte in digest:
                values.append((byte / 127.5) - 1.0)
                if len(values) == self.dimension:
                    break
            counter += 1
        return values


def _coerce_float(value: object, *, index: int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise EmbeddingError(f"embedding item {index} contains a non-numeric value") from exc


def _safe_response_snippet(
    response: httpx.Response,
    *,
    redactions: list[str] | None = None,
) -> str:
    text = response.text.strip()
    for secret in redactions or []:
        if secret:
            text = text.replace(secret, "[redacted]")
    if not text:
        return "(empty response body)"
    return text[:300]
