from __future__ import annotations

from pathlib import Path

from bootstrap.config import Settings
from plugins.context import PluginContext
from plugins.manager import PluginManager


def create_plugin_manager(
    config: Settings,
    context: PluginContext,
    base_dir: str | Path,
) -> PluginManager:
    return PluginManager(
        plugin_paths=config.plugins.paths,
        context=context,
        base_dir=base_dir,
        strict=config.plugins.strict_plugins,
    )

