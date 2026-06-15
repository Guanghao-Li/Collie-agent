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
    risk: str = "read_only"
    source_type: str = "builtin"
    source_name: str = ""
    always_on: bool = True
    search_hint: str = ""
    requires_confirmation: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        func: ToolFunc,
        risk: str = "read_only",
        source_type: str = "builtin",
        source_name: str = "",
        always_on: bool = True,
        search_hint: str = "",
        requires_confirmation: bool = False,
    ) -> None:
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            schema=schema,
            func=func,
            risk=risk,
            source_type=source_type,
            source_name=source_name,
            always_on=always_on,
            search_hint=search_hint,
            requires_confirmation=requires_confirmation,
        )

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

    def list_tools(self, include_deferred: bool = True) -> list[RegisteredTool]:
        tools = self._tools.values() if include_deferred else self.get_visible_tools()
        return sorted(tools, key=lambda tool: tool.name)

    def get_visible_tools(self) -> list[RegisteredTool]:
        return [tool for tool in self._tools.values() if tool.always_on]

    def render_tools_for_prompt(self) -> str:
        lines = []
        for tool in self.list_tools(include_deferred=False):
            lines.append(
                f"- {tool.name}: {tool.description}; schema={json.dumps(tool.schema, ensure_ascii=False)}"
            )
        return "\n".join(lines)
