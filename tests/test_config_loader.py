from __future__ import annotations

import pytest

from agent.llm import EchoProvider
from bootstrap.config import load_config
from bootstrap.config import Settings
from bootstrap.providers import build_fast_provider, build_main_provider


def test_config_extends_and_environment_variables(tmp_path, monkeypatch) -> None:
    base = tmp_path / "base.toml"
    child = tmp_path / "child.toml"
    base.write_text(
        """
[app]
name = "Collie-agent"

[discord]
enabled = true
bot_token = ""

[llm]
provider = "echo"
""".strip(),
        encoding="utf-8",
    )
    child.write_text(
        """
[config]
extends = ["base.toml"]

[discord]
bot_token = "${DISCORD_BOT_TOKEN}"

[llm]
provider = "openai-compatible"

[llm.compatible]
api_key = "${LLM_API_KEY}"
base_url = "https://example.com/v1"
model = "example-model"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")

    config = load_config(child)

    assert config.app.name == "Collie-agent"
    assert config.discord.bot_token == "discord-secret"
    assert config.llm.provider == "openai-compatible"
    assert config.llm.compatible.api_key == "llm-secret"
    assert config.llm.compatible.base_url == "https://example.com/v1"


def test_legacy_openai_section_still_loads(tmp_path, monkeypatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[llm]
provider = "openai"

[llm.openai]
api_key = "${OPENAI_API_KEY}"
base_url = "https://legacy.example.com/v1"
model = "legacy-model"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-secret")

    config = load_config(config_file)

    assert config.llm.provider == "openai"
    assert config.llm.compatible.api_key == "legacy-secret"
    assert config.llm.compatible.base_url == "https://legacy.example.com/v1"
    assert config.llm.openai.model == "legacy-model"


def test_dotenv_values_are_loaded_from_config_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config_file = tmp_path / "config.toml"
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("LLM_API_KEY=dotenv-secret\n", encoding="utf-8")
    config_file.write_text(
        """
[llm]
provider = "openai-compatible"

[llm.compatible]
api_key = "${LLM_API_KEY}"
base_url = "https://example.com/v1"
model = "example-model"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.llm.compatible.api_key == "dotenv-secret"


def test_dotenv_does_not_override_existing_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "real-env-secret")
    config_file = tmp_path / "config.toml"
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("LLM_API_KEY=dotenv-secret\n", encoding="utf-8")
    config_file.write_text(
        """
[llm]
provider = "openai-compatible"

[llm.compatible]
api_key = "${LLM_API_KEY}"
base_url = "https://example.com/v1"
model = "example-model"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.llm.compatible.api_key == "real-env-secret"


def test_missing_fast_llm_config_defaults_to_disabled(tmp_path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[llm]
provider = "echo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.llm.fast.enabled is False
    assert config.llm.fast.fallback_to_main is True


def test_legacy_memory_consolidation_mode_is_ignored(tmp_path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[memory]
consolidation_mode = "legacy"
optimizer_enabled = true
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert not hasattr(config.memory, "consolidation_mode")
    assert config.memory.optimizer_enabled is True


@pytest.mark.asyncio
async def test_fast_provider_disabled_reuses_main_provider() -> None:
    config = Settings()
    main = build_main_provider(config)

    fast = build_fast_provider(config, main)

    assert isinstance(main, EchoProvider)
    assert fast is main


@pytest.mark.asyncio
async def test_fast_provider_enabled_with_complete_config_creates_provider() -> None:
    config = Settings()
    config.llm.fast.enabled = True
    config.llm.fast.model = "fast-model"
    config.llm.fast.api_key = "test-key"
    config.llm.fast.base_url = "https://example.com/v1"
    main = build_main_provider(config)

    fast = build_fast_provider(config, main)

    assert fast is not main
    assert getattr(fast, "model") == "fast-model"
    await fast.close()


def test_fast_provider_incomplete_config_falls_back_to_main() -> None:
    config = Settings()
    config.llm.fast.enabled = True
    config.llm.fast.fallback_to_main = True
    main = build_main_provider(config)

    fast = build_fast_provider(config, main)

    assert fast is main


def test_fast_provider_incomplete_config_can_raise() -> None:
    config = Settings()
    config.llm.fast.enabled = True
    config.llm.fast.fallback_to_main = False
    main = build_main_provider(config)

    with pytest.raises(ValueError):
        build_fast_provider(config, main)
