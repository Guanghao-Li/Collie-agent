from __future__ import annotations

from typing import Any
import inspect

from agent.frame import TurnFrame
from tools.hooks import ToolHookResult, ToolPreHook
from tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self._pre_hooks: list[ToolPreHook] = []
        self.last_arguments: dict[str, Any] = {}

    def register_pre_hook(self, hook: ToolPreHook) -> None:
        self._pre_hooks.append(hook)
        self._pre_hooks.sort(key=lambda item: item.priority)

    def list_pre_hooks(self) -> list[ToolPreHook]:
        return list(self._pre_hooks)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        frame: TurnFrame | None = None,
    ) -> Any:
        current_arguments = dict(arguments or {})
        self._record_arguments(frame, current_arguments)
        for hook in self._pre_hooks:
            hook_tool_name = getattr(hook, "tool_name", None)
            if hook_tool_name is not None and hook_tool_name != name:
                continue
            result = hook.before_tool_call(name, dict(current_arguments), frame)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
            if not isinstance(result, ToolHookResult):
                continue
            if result.decision == "allow":
                continue
            if result.decision == "modify":
                current_arguments = dict(result.arguments or {})
                self._record_arguments(frame, current_arguments)
                continue
            if result.decision == "deny":
                return {
                    "error": result.reason or "Tool call denied.",
                    "denied": True,
                }
            if result.decision == "confirm":
                return {
                    "error": "Tool call requires confirmation.",
                    "requires_confirmation": True,
                    "reason": result.reason,
                }
        self._record_arguments(frame, current_arguments)
        return await self.registry.call_tool(name, current_arguments)

    def _record_arguments(
        self,
        frame: TurnFrame | None,
        arguments: dict[str, Any],
    ) -> None:
        self.last_arguments = dict(arguments)
        if frame is not None:
            frame.slots["tool:last_arguments"] = dict(arguments)
