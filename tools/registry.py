from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import inspect
import json


ToolFunc = Callable[..., Any | Awaitable[Any]]


class ToolError(RuntimeError):
    pass


class ToolNotFoundError(ToolError):
    pass


@dataclass(slots=True)
class RegisteredTool:
    name: str
    description: str
    schema: dict[str, Any]
    func: ToolFunc


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        func: ToolFunc,
    ) -> None:
        self._tools[name] = RegisteredTool(name, description, schema, func)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in self._tools:
            raise ToolNotFoundError(f"未知工具：{name}")
        tool = self._tools[name]
        arguments = arguments or {}
        try:
            result = tool.func(**arguments)
            if inspect.isawaitable(result):
                return await result
            return result
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"工具 {name} 执行失败：{exc}") from exc

    def list_tools(self) -> list[RegisteredTool]:
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    def render_tools_for_prompt(self) -> str:
        lines = []
        for tool in self.list_tools():
            lines.append(
                f"- {tool.name}: {tool.description}; schema={json.dumps(tool.schema, ensure_ascii=False)}"
            )
        return "\n".join(lines)
