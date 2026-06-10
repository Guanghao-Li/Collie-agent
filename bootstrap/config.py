from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
import os
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11 以下版本的兼容分支。
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


@dataclass(slots=True)
class AppConfig:
    name: str = "Collie-agent"
    timezone: str = "America/New_York"


@dataclass(slots=True)
class DiscordConfig:
    enabled: bool = True
    bot_token: str = ""
    guild_id: str = ""
    allowed_channel_ids: list[str] = field(default_factory=list)
    allowed_user_ids: list[str] = field(default_factory=list)
    default_push_channel_id: str = ""


@dataclass(slots=True)
class CompatibleLLMConfig:
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 30.0
    temperature: float = 0.7


OpenAIConfig = CompatibleLLMConfig


@dataclass(slots=True)
class FastLLMConfig:
    enabled: bool = False
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout_seconds: float = 15.0
    temperature: float = 0.0
    fallback_to_main: bool = True


@dataclass(slots=True)
class LLMConfig:
    provider: str = "echo"
    compatible: CompatibleLLMConfig = field(default_factory=CompatibleLLMConfig)
    fast: FastLLMConfig = field(default_factory=FastLLMConfig)

    @property
    def openai(self) -> CompatibleLLMConfig:
        return self.compatible

    @openai.setter
    def openai(self, value: CompatibleLLMConfig) -> None:
        self.compatible = value


@dataclass(slots=True)
class MemoryConfig:
    enabled: bool = True
    auto_extract: bool = True
    auto_consolidate: bool = True
    enable_hyde: bool = True
    max_recent_messages: int = 30
    search_limit: int = 8
    workspace_dir: str = "memory"


@dataclass(slots=True)
class ProactiveConfig:
    enabled: bool = True
    interval_seconds: int = 900
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    min_score_to_push: float = 0.72
    max_pushes_per_day: int = 6
    fast_prefilter_enabled: bool = True
    fast_prefilter_min_score: float = 0.4


@dataclass(slots=True)
class DriftConfig:
    enabled: bool = True
    interval_seconds: int = 1800
    run_only_when_idle: bool = True
    idle_after_seconds: int = 600
    max_tasks_per_cycle: int = 2


@dataclass(slots=True)
class PluginsConfig:
    enabled: bool = True
    paths: list[str] = field(default_factory=lambda: ["plugins_builtin"])
    strict_plugins: bool = False


@dataclass(slots=True)
class Settings:
    app: AppConfig = field(default_factory=AppConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    drift: DriftConfig = field(default_factory=DriftConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)


def _strip_env_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        os.environ.setdefault(key, _strip_env_quotes(value))


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_resolve_env_value(str(item)) for item in value if str(item)]
    return [_resolve_env_value(str(value))]


def _resolve_env_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    def replace_match(match: re.Match[str]) -> str:
        env_name = match.group(1)
        return os.getenv(env_name, "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace_match, value)


def _resolve_env_in_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env_in_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_in_mapping(item) for item in value]
    return _resolve_env_value(value)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key == "config":
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_toml_with_extends(path: Path, seen: set[Path] | None = None) -> dict[str, Any]:
    if tomllib is None:
        raise RuntimeError("在 Python 3.11 以下版本读取 TOML 配置需要安装 tomli。")
    if seen is None:
        seen = set()
    resolved = path.resolve()
    if resolved in seen:
        chain = " -> ".join(str(item) for item in [*seen, resolved])
        raise RuntimeError(f"配置文件 extends 存在循环引用：{chain}")
    seen.add(resolved)

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    config_section = data.get("config", {})
    extends = config_section.get("extends", [])
    if isinstance(extends, str):
        extends = [extends]

    merged: dict[str, Any] = {}
    for item in extends:
        parent_path = Path(str(item))
        if not parent_path.is_absolute():
            parent_path = path.parent / parent_path
        if not parent_path.exists():
            raise FileNotFoundError(f"被继承的配置文件不存在：{parent_path}")
        merged = _deep_merge(merged, _load_toml_with_extends(parent_path, seen))
    return _resolve_env_in_mapping(_deep_merge(merged, data))


def load_config(path: str | Path | None) -> Settings:
    settings = Settings()
    if path is None:
        _load_dotenv(Path(".env"))
        return settings
    config_path = Path(path)
    dotenv_path = config_path.parent / ".env"
    _load_dotenv(dotenv_path)
    if not config_path.exists():
        return settings
    data = _load_toml_with_extends(config_path)

    app = data.get("app", {})
    discord = data.get("discord", {})
    llm = data.get("llm", {})
    compatible_llm = llm.get("compatible")
    if compatible_llm is None:
        compatible_llm = llm.get("openai", {})
    fast_llm = llm.get("fast", {})
    memory = data.get("memory", {})
    proactive = data.get("proactive", {})
    drift = data.get("drift", {})
    plugins = data.get("plugins", {})

    return Settings(
        app=replace(settings.app, **{k: v for k, v in app.items() if hasattr(settings.app, k)}),
        discord=DiscordConfig(
            enabled=bool(discord.get("enabled", settings.discord.enabled)),
            bot_token=str(_resolve_env_value(discord.get("bot_token", settings.discord.bot_token))),
            guild_id=str(_resolve_env_value(discord.get("guild_id", settings.discord.guild_id))),
            allowed_channel_ids=_as_str_list(discord.get("allowed_channel_ids", [])),
            allowed_user_ids=_as_str_list(discord.get("allowed_user_ids", [])),
            default_push_channel_id=str(
                _resolve_env_value(
                    discord.get("default_push_channel_id", settings.discord.default_push_channel_id)
                )
            ),
        ),
        llm=LLMConfig(
            provider=str(llm.get("provider", settings.llm.provider)),
            compatible=CompatibleLLMConfig(
                model=str(compatible_llm.get("model", settings.llm.compatible.model)),
                api_key=str(
                    _resolve_env_value(
                        compatible_llm.get("api_key", settings.llm.compatible.api_key)
                    )
                ),
                base_url=str(
                    _resolve_env_value(
                        compatible_llm.get("base_url", settings.llm.compatible.base_url)
                    )
                ),
                timeout_seconds=float(
                    compatible_llm.get(
                        "timeout_seconds",
                        settings.llm.compatible.timeout_seconds,
                    )
                ),
                temperature=float(
                    compatible_llm.get("temperature", settings.llm.compatible.temperature)
                ),
            ),
            fast=FastLLMConfig(
                enabled=bool(fast_llm.get("enabled", settings.llm.fast.enabled)),
                model=str(_resolve_env_value(fast_llm.get("model", settings.llm.fast.model))),
                api_key=str(_resolve_env_value(fast_llm.get("api_key", settings.llm.fast.api_key))),
                base_url=str(_resolve_env_value(fast_llm.get("base_url", settings.llm.fast.base_url))),
                timeout_seconds=float(
                    fast_llm.get("timeout_seconds", settings.llm.fast.timeout_seconds)
                ),
                temperature=float(fast_llm.get("temperature", settings.llm.fast.temperature)),
                fallback_to_main=bool(
                    fast_llm.get("fallback_to_main", settings.llm.fast.fallback_to_main)
                ),
            ),
        ),
        memory=replace(
            settings.memory,
            **{k: v for k, v in memory.items() if hasattr(settings.memory, k)},
        ),
        proactive=replace(
            settings.proactive,
            **{k: v for k, v in proactive.items() if hasattr(settings.proactive, k)},
        ),
        drift=replace(
            settings.drift,
            **{k: v for k, v in drift.items() if hasattr(settings.drift, k)},
        ),
        plugins=PluginsConfig(
            enabled=bool(plugins.get("enabled", settings.plugins.enabled)),
            paths=_as_str_list(plugins.get("paths", settings.plugins.paths)),
            strict_plugins=bool(plugins.get("strict_plugins", settings.plugins.strict_plugins)),
        ),
    )
