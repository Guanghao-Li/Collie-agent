from __future__ import annotations

import logging

from agent.llm import EchoProvider, LLMProvider, OpenAICompatibleProvider
from bootstrap.config import Settings


def build_main_provider(config: Settings) -> LLMProvider:
    provider = config.llm.provider.lower()
    if provider == "echo":
        return EchoProvider()
    if provider in {"compatible", "openai", "openai-compatible", "openai_compatible"}:
        compatible = config.llm.compatible
        return OpenAICompatibleProvider(
            model=compatible.model,
            api_key=compatible.api_key,
            base_url=compatible.base_url,
            timeout_seconds=compatible.timeout_seconds,
            temperature=compatible.temperature,
            name="openai-compatible",
        )
    raise ValueError(f"Unknown LLM provider: {config.llm.provider}")


def build_fast_provider(config: Settings, main_provider: LLMProvider) -> LLMProvider:
    fast = config.llm.fast
    if not fast.enabled:
        return main_provider
    try:
        missing = [
            name
            for name, value in {
                "model": fast.model,
                "api_key": fast.api_key,
                "base_url": fast.base_url,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"llm.fast 配置不完整，缺少：{', '.join(missing)}")
        return OpenAICompatibleProvider(
            model=fast.model,
            api_key=fast.api_key,
            base_url=fast.base_url,
            timeout_seconds=fast.timeout_seconds,
            temperature=fast.temperature,
            name="openai-fast",
        )
    except Exception:
        if fast.fallback_to_main:
            logging.getLogger(__name__).warning(
                "fast LLM provider 创建失败，已 fallback 到 main provider。",
                exc_info=True,
            )
            return main_provider
        raise


def create_llm_provider(config: Settings) -> LLMProvider:
    return build_main_provider(config)
