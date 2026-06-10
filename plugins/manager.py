from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any
import importlib.util
import logging
import uuid

from plugins.base import Plugin
from plugins.context import PluginContext


class PluginManager:
    def __init__(
        self,
        plugin_paths: list[str],
        context: PluginContext,
        base_dir: str | Path | None = None,
        strict: bool = False,
    ) -> None:
        self.plugin_paths = plugin_paths
        self.context = context
        self.base_dir = Path(base_dir or Path.cwd())
        self.strict = strict
        self.plugins: list[Plugin] = []
        self.errors: list[str] = []
        self._logger = logging.getLogger(__name__)

    async def load_plugins(self) -> None:
        if not self.context.config.plugins.enabled:
            return
        for configured_path in self.plugin_paths:
            root = Path(configured_path)
            if not root.is_absolute():
                root = self.base_dir / root
            if not root.exists():
                self._record_error(f"插件路径不存在：{root}")
                continue
            for plugin_file in sorted(root.glob("*/plugin.py")):
                await self._load_plugin_file(plugin_file)

    async def _load_plugin_file(self, plugin_file: Path) -> None:
        try:
            module = _import_module(plugin_file)
            plugin = _plugin_from_module(module)
            await plugin.setup(self.context)
            self.plugins.append(plugin)
        except Exception as exc:
            self._record_error(f"{plugin_file}: {exc}")

    def _record_error(self, message: str) -> None:
        self.errors.append(message)
        self._logger.exception(message)
        if self.strict:
            raise RuntimeError(message)


def _import_module(path: Path) -> ModuleType:
    name = f"_mini_akashic_plugin_{path.parent.name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法导入插件文件：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_from_module(module: ModuleType) -> Plugin:
    maybe_plugin: Any = getattr(module, "plugin", None)
    if maybe_plugin is not None:
        return maybe_plugin
    create_plugin = getattr(module, "create_plugin", None)
    if callable(create_plugin):
        return create_plugin()
    raise RuntimeError("插件模块必须暴露 plugin 或 create_plugin()。")
