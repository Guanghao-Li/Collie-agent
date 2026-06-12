from __future__ import annotations

import json

import pytest

from agent.intent import IntentRouter


class StubLLMProvider:
    name = "stub"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        purpose: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
                "purpose": purpose,
            }
        )
        return self.response

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("请记住我喜欢喝冰美式", "memory_add"),
        ("纠正一下，我不是住在上海，是住在杭州", "memory_correction"),
        ("忘掉我刚才说的项目代号", "memory_delete"),
        ("以后别推晚上的提醒", "proactive_config"),
        ("空闲时在后台整理一下待办", "drift_task_create"),
        ("打开面板看一下记忆", "dashboard_command"),
        ("启用插件 weather", "plugin_management"),
        ("帮我搜索一下 Python dataclass", "tool_execution"),
    ],
)
async def test_intent_router_rule_matches(message: str, expected: str) -> None:
    router = IntentRouter(llm_fallback_enabled=False)

    decision = await router.classify(message)

    assert decision.intent == expected
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.route


@pytest.mark.asyncio
async def test_intent_router_defaults_to_general_chat_without_fallback() -> None:
    router = IntentRouter(llm_fallback_enabled=False)

    decision = await router.classify("今天心情不错，聊聊电影吧")

    assert decision.intent == "general_chat"
    assert decision.route == "chat"


@pytest.mark.asyncio
async def test_intent_router_fallback_invalid_json_degrades_to_general_chat() -> None:
    provider = StubLLMProvider("not-json")
    router = IntentRouter(fast_llm_provider=provider, timeout_seconds=5.0)

    decision = await router.classify("帮我处理一下这个请求")

    assert decision.intent == "general_chat"
    assert decision.confidence == 0.0
    assert provider.calls[0]["temperature"] == 0
    assert provider.calls[0]["timeout_seconds"] == 5.0
    assert provider.calls[0]["purpose"] == "intent_classification"


@pytest.mark.asyncio
async def test_intent_router_fallback_parses_valid_json_and_clamps_confidence() -> None:
    provider = StubLLMProvider(
        json.dumps(
            {
                "intent": "plugin_management",
                "confidence": 1.3,
                "route": "plugins",
                "entities": {"plugin": "calendar"},
                "reason": "plugin request",
            }
        )
    )
    router = IntentRouter(fast_llm_provider=provider)

    decision = await router.classify("Can you route this ambiguous request?")

    assert decision.intent == "plugin_management"
    assert decision.confidence == 1.0
    assert decision.route == "plugins"
    assert decision.entities == {"plugin": "calendar"}
