from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
import json

try:
    import httpx
except ImportError:  # pragma: no cover - 只有使用 OpenAI 兼容 provider 时才需要该依赖。
    httpx = None  # type: ignore[assignment]


class LLMError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        purpose: str | None = None,
    ) -> str:
        ...

    async def close(self) -> None:
        ...


class EchoProvider:
    name = "echo"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        purpose: str | None = None,
    ) -> str:
        last = next((m for m in reversed(messages) if m.get("role") in {"user", "tool"}), {})
        content = last.get("content", "")
        if last.get("role") == "tool":
            return f"工具结果：{content}"
        if "TOOL:calculator" in content:
            expression = content.split("TOOL:calculator", 1)[1].strip() or "1 + 1"
            return _tool_call("calculator", {"expression": expression})
        if "TOOL:search_memory" in content:
            query = content.split("TOOL:search_memory", 1)[1].strip()
            return _tool_call("search_memory", {"query": query})
        return f"回声：{content}"

    async def close(self) -> None:
        return None


def _tool_call(name: str, arguments: dict[str, object]) -> str:
    return f"<tool_call>\n{json.dumps({'name': name, 'arguments': arguments})}\n</tool_call>"


@dataclass(slots=True)
class OpenAICompatibleProvider:
    model: str
    api_key: str
    base_url: str
    timeout_seconds: float = 30.0
    temperature: float = 0.7
    name: str = "openai-compatible"
    _client: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if httpx is None:
            self._client = None
            return
        self._client = httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            timeout=self.timeout_seconds,
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        purpose: str | None = None,
    ) -> str:
        missing = [
            name
            for name, value in {
                "model": self.model,
                "api_key": self.api_key,
                "base_url": self.base_url,
            }.items()
            if not value
        ]
        if missing:
            raise LLMError(
                "OpenAI 兼容 provider 配置不完整，缺少："
                f"{', '.join(f'llm.compatible.{name}' for name in missing)}。"
            )
        if httpx is None or self._client is None:
            raise LLMError("OpenAI 兼容 provider 需要安装 httpx。")
        try:
            response = await self._client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature if temperature is None else temperature,
                },
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return str(data["choices"][0]["message"]["content"])
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败：{exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM 响应不符合 chat completions 格式。") from exc

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
